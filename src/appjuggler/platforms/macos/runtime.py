"""macOS Runtime Coordinator wiring NavigationController, system adapters, and AppKit UI."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import logging
import os
import queue
import signal
import threading
import time
from typing import Callable

import Cocoa
import objc

from appjuggler.contracts import PermissionStatus
from appjuggler.domain.events import (
    ActivateAppNoWindowsCommand,
    ActivateWindowCommand,
    ActivationCompleted,
    AppWindowsResolved,
    BackendEvent,
    CapsHoldTriggered,
    CommitQuickAssignCommand,
    EnterAssignmentEditing,
    ExitAssignmentEditing,
    HardwareKeyEvent,
    HideOverlayCommand,
    InputAction,
    InspectAppWindowsCommand,
    LaunchAppCommand,
    LaunchCompleted,
    MouseClickAppSlot,
    MouseClickWindowSlot,
    MouseHoverWindowSlot,
    MouseScrollWindowSelection,
    MouseToggleLayer,
    NavCommand,
    OpenAppsForAssignResolved,
    OpenFullAssignmentWindowCommand,
    RequestOpenAppsForAssignCommand,
    ShowAppMapCommand,
    ShowQuickAssignAppListCommand,
    ShowQuickAssignKeyMapCommand,
    ShowStatusBannerCommand,
    ShowWindowListCommand,
    SystemInterruptEvent,
    UpdateAppMapLayerCommand,
    UpdateQuickAssignAppSelectionCommand,
    UpdateWindowSelectionCommand,
    WindowListRefreshed,
)
from appjuggler.domain.models import AppBinding, AppRecord, Layer, NavState
from appjuggler.domain.state_machine import NavigationController
from appjuggler.domain.window_slots import WindowSlotTable
from appjuggler.persistence.binding_store import BindingStore, CorruptedConfigLockError
from appjuggler.platforms.macos.app_registry import MacOSApplicationRegistry
from appjuggler.platforms.macos.input_tap import MacOSInputAdapter
from appjuggler.platforms.macos.permissions import MacOSPermissionMonitor
from appjuggler.platforms.macos.ui.assignment_window import AssignmentEditorController
from appjuggler.platforms.macos.ui.overlay_panel import MacOSOverlayPresenter
from appjuggler.platforms.macos.ui.status_bar import StatusBarController
from appjuggler.platforms.macos.window_activator import MacOSWindowActivator
from appjuggler.platforms.macos.window_registry import MacOSWindowRegistry

logger = logging.getLogger(__name__)


class _MainThreadDispatcher(Cocoa.NSObject):
    """Executes Python callables on the main AppKit thread without blocking callers."""

    def init(self):
        self = objc.super(_MainThreadDispatcher, self).init()
        if self is None:
            return None
        self._queue: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        return self

    @objc.python_method
    def dispatch(self, fn: Callable[[], None]) -> None:
        if Cocoa.NSThread.isMainThread():
            fn()
            return
        self._queue.put(fn)
        self.performSelectorOnMainThread_withObject_waitUntilDone_modes_(
            "drainQueue:",
            None,
            False,
            [Cocoa.NSRunLoopCommonModes],
        )

    def drainQueue_(self, _arg) -> None:
        while True:
            try:
                fn = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:
                logger.exception("Unhandled error in main-thread UI callback")


class _PowerAndLockObserver(Cocoa.NSObject):
    """Observes sleep, wake, session switch, screen lock, and periodic health pulse."""

    def initWithCallbacks_(self, callbacks: dict[str, Callable[[], None]]):
        self = objc.super(_PowerAndLockObserver, self).init()
        if self is None:
            return None
        self._cb = callbacks
        return self

    def willSleep_(self, _note) -> None:
        self._cb["on_sleep"]()

    def didWake_(self, _note) -> None:
        self._cb["on_wake"]()

    def sessionResigned_(self, _note) -> None:
        self._cb["on_lock"]()

    def screenLocked_(self, _note) -> None:
        self._cb["on_lock"]()

    def screenUnlocked_(self, _note) -> None:
        self._cb["on_wake"]()

    def activeSpaceChanged_(self, _note) -> None:
        self._cb["on_space_change"]()

    def signalPipeWake_(self, _note) -> None:
        self._cb["on_signal_wake"]()

    def healthPulse_(self, _timer) -> None:
        self._cb["on_pulse"]()


class MacOSAppRuntime:
    """Top-level lifecycle manager for AppJuggler on macOS."""

    def __init__(self, store: BindingStore) -> None:
        self._store = store
        self._slot_table = WindowSlotTable()
        self._controller = NavigationController(
            trigger_interval_ms=store.trigger.interval_ms,
            enable_caps_hold_assign=store.trigger.enable_caps_hold_assign,
            enable_mouse_selection=store.enable_mouse_selection,
        )
        self._controller.update_bindings(store.bindings)

        self._input_adapter = MacOSInputAdapter(
            hold_assign_ms=store.trigger.hold_assign_ms,
            enable_caps_hold=store.trigger.enable_caps_hold_assign,
        )
        self._permission_monitor = MacOSPermissionMonitor(
            live_tap_checker=lambda: self._input_adapter.is_running
        )
        self._app_registry = MacOSApplicationRegistry()
        self._window_registry = MacOSWindowRegistry()
        self._window_activator = MacOSWindowActivator(self._window_registry)

        self._main = _MainThreadDispatcher.alloc().init()
        self._overlay = MacOSOverlayPresenter(self._app_registry)
        self._overlay.set_mouse_enabled(store.enable_mouse_selection)
        self._overlay.set_mouse_callbacks(
            on_app_key_clicked=lambda key, layer: self._post_backend_event(
                MouseClickAppSlot(key=key, layer=layer)
            ),
            on_layer_pill_clicked=lambda: self._post_backend_event(MouseToggleLayer()),
            on_window_slot_hovered=lambda slot_num: self._post_backend_event(
                MouseHoverWindowSlot(slot_number=slot_num)
            ),
            on_window_slot_clicked=lambda slot_num: self._post_backend_event(
                MouseClickWindowSlot(slot_number=slot_num)
            ),
            on_window_scroll=lambda delta: self._post_backend_event(
                MouseScrollWindowSelection(delta=delta)
            ),
        )
        self._assignment_editor = AssignmentEditorController(
            store=self._store,
            app_registry=self._app_registry,
            on_enter_editing=lambda: self._post_backend_event(EnterAssignmentEditing()),
            on_exit_editing=lambda: self._post_backend_event(ExitAssignmentEditing()),
            on_config_updated=self._sync_config_to_controller,
            on_quit_app=self.shutdown_and_terminate,
        )
        self._status_bar = StatusBarController(
            permission_monitor=self._permission_monitor,
            on_open_navigator=self._manual_open_navigator,
            on_edit_assignments=self._assignment_editor.show_window,
            on_reload_config=self._reload_config_from_disk,
            on_quit=self.shutdown_and_terminate,
            on_toggle_auto_fullscreen=self._toggle_auto_fullscreen_from_menu,
            on_toggle_mouse_selection=self._toggle_mouse_selection_from_menu,
        )

        # Separate latency-critical navigation pool from background Full Screen / scan tasks
        # so multi-second Space/fullscreen animations can NEVER starve keyboard navigation.
        self._nav_executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="AppJuggler-Nav"
        )
        self._bg_executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="AppJuggler-BG"
        )
        self._active_window_list_bundle: str | None = None
        self._duplicate_conflicts: frozenset[str] = frozenset()
        self._power_observer: _PowerAndLockObserver | None = None
        self._pulse_timer = None
        self._shutting_down: bool = False
        self._signal_shutdown_requested: bool = False
        self._sig_pipe_r: int | None = None
        self._sig_pipe_w: int | None = None
        self._sig_file_handle = None
        self._watchdog_lock = threading.Lock()
        self._backend_wait_token: int = 0
        self._backend_wait_deadline: float = 0.0

    def run(self) -> int:
        app = Cocoa.NSApplication.sharedApplication()
        app.setActivationPolicy_(Cocoa.NSApplicationActivationPolicyAccessory)

        self._status_bar.install()
        self._app_registry.start_observing(self._on_app_lifecycle)
        self._refresh_running_apps_snapshot()
        self._refresh_duplicate_conflicts_async()
        self._register_power_and_lock_observers()
        self._install_unix_signal_handlers()

        status = self._ensure_input_tap_state()
        if self._store.load_error:
            self._overlay.show_status_banner(
                level="error",
                message="Config file damaged (not overwritten). Press Caps×2 then ',' to repair.",
                auto_hide_ms=5000,
            )
        elif not status.is_ready and not status.screen_locked:
            self._status_bar.show_permissions_dialog(prompt_system=True)
        else:
            ready_msg = (
                "AppJuggler Ready  ·  Caps×2: Jump  ·  Hold Caps 2s: Quick Assign"
                if self._store.trigger.enable_caps_hold_assign
                else "AppJuggler Ready  ·  Caps×2: Jump  ·  Caps×2 then ',': Edit Assignments"
            )
            self._overlay.show_status_banner(
                level="success",
                message=ready_msg,
                auto_hide_ms=2200,
            )

        self._pulse_timer = (
            Cocoa.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                1.5,
                self._power_observer,
                "healthPulse:",
                None,
                True,
            )
        )

        logger.info("AppJuggler running (config=%s)", self._store.path)
        app.run()
        return 0

    def shutdown_and_terminate(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        logger.info("Shutting down AppJuggler")
        if self._pulse_timer is not None:
            self._pulse_timer.invalidate()
            self._pulse_timer = None
        self._teardown_unix_signal_handlers()
        self._unregister_power_and_lock_observers()
        self._input_adapter.stop()
        self._app_registry.stop_observing()
        self._overlay.hide()
        self._nav_executor.shutdown(wait=False, cancel_futures=True)
        self._bg_executor.shutdown(wait=False, cancel_futures=True)
        Cocoa.NSApp.terminate_(None)
        # Post a harmless wakeup event so -[NSApplication run] exits immediately even when idle.
        wake_ev = Cocoa.NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
            Cocoa.NSEventTypeApplicationDefined,
            Cocoa.NSMakePoint(0.0, 0.0),
            0,
            0.0,
            0,
            None,
            0,
            0,
            0,
        )
        if wake_ev is not None:
            Cocoa.NSApp.postEvent_atStart_(wake_ev, True)

    def _install_unix_signal_handlers(self) -> None:
        """Hook SIGINT/SIGTERM via wakeup fd so Ctrl+C exits cleanly without OC_PythonException."""
        try:
            r_fd, w_fd = os.pipe()
            os.set_blocking(r_fd, False)
            os.set_blocking(w_fd, False)
            self._sig_pipe_r = r_fd
            self._sig_pipe_w = w_fd
            signal.set_wakeup_fd(w_fd, warn_on_full_buffer=False)

            def _on_unix_signal(_signum: int, _frame) -> None:
                self._signal_shutdown_requested = True
                if self._sig_pipe_w is not None:
                    try:
                        os.write(self._sig_pipe_w, b"\x01")
                    except OSError:
                        pass

            signal.signal(signal.SIGINT, _on_unix_signal)
            signal.signal(signal.SIGTERM, _on_unix_signal)

            fh = Cocoa.NSFileHandle.alloc().initWithFileDescriptor_closeOnDealloc_(
                r_fd, False
            )
            self._sig_file_handle = fh
            Cocoa.NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
                self._power_observer,
                "signalPipeWake:",
                Cocoa.NSFileHandleDataAvailableNotification,
                fh,
            )
            fh.waitForDataInBackgroundAndNotifyForModes_([Cocoa.NSRunLoopCommonModes])
        except Exception:
            logger.debug("Could not install wakeup-fd signal handler", exc_info=True)

    def _teardown_unix_signal_handlers(self) -> None:
        try:
            signal.set_wakeup_fd(-1)
        except Exception:
            pass
        if self._sig_file_handle is not None and self._power_observer is not None:
            try:
                Cocoa.NSNotificationCenter.defaultCenter().removeObserver_name_object_(
                    self._power_observer,
                    Cocoa.NSFileHandleDataAvailableNotification,
                    self._sig_file_handle,
                )
            except Exception:
                pass
            self._sig_file_handle = None
        for fd_attr in ("_sig_pipe_r", "_sig_pipe_w"):
            fd = getattr(self, fd_attr)
            setattr(self, fd_attr, None)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def _on_signal_pipe_wake(self) -> None:
        if self._sig_pipe_r is not None:
            try:
                while os.read(self._sig_pipe_r, 64):
                    pass
            except OSError:
                pass
        if self._signal_shutdown_requested:
            self.shutdown_and_terminate()
            return
        if self._sig_file_handle is not None:
            self._sig_file_handle.waitForDataInBackgroundAndNotifyForModes_(
                [Cocoa.NSRunLoopCommonModes]
            )

    # --- Event tap & State Machine plumbing ---

    def _arm_backend_watchdog(self, op_token: int, timeout_sec: float) -> None:
        with self._watchdog_lock:
            self._backend_wait_token = op_token
            self._backend_wait_deadline = time.monotonic() + timeout_sec

    def _recover_if_backend_stalled(self) -> None:
        if not self._controller.is_waiting_on_backend:
            return
        with self._watchdog_lock:
            tok = self._backend_wait_token
            deadline = self._backend_wait_deadline
        if tok > 0 and self._controller.op_token == tok and time.monotonic() > deadline:
            logger.warning(
                "Backend operation op_token=%d exceeded watchdog deadline; releasing keyboard capture",
                tok,
            )
            cmds = self._controller.handle_backend_event(
                SystemInterruptEvent(reason="backend_watchdog_timeout")
            )
            if cmds:
                self._dispatch_commands(cmds)

    def _on_hardware_key(self, event: HardwareKeyEvent) -> InputAction:
        self._recover_if_backend_stalled()
        step = self._controller.handle_key_event(event)
        if step.commands:
            self._dispatch_commands(step.commands)
        return step.action

    def _on_tap_interrupt(self, reason: str) -> None:
        cmds = self._controller.handle_backend_event(SystemInterruptEvent(reason=reason))
        if cmds:
            self._dispatch_commands(cmds)
        if reason in ("screen_locked", "tap_suspended_temporary"):
            self._main.dispatch(self._handle_tap_suspended_on_main)
        elif reason == "permissions_revoked":
            self._main.dispatch(self._handle_permissions_revoked_on_main)

    def _on_caps_hold_triggered(self) -> None:
        logger.info("Caps Lock 2-second hold detected -> entering Quick Assign mode")
        self._post_backend_event(CapsHoldTriggered())

    def _post_backend_event(self, event: BackendEvent) -> None:
        cmds = self._controller.handle_backend_event(event)
        if cmds:
            self._dispatch_commands(cmds)

    def _dispatch_commands(self, commands: tuple[NavCommand, ...]) -> None:
        for cmd in commands:
            if isinstance(cmd, InspectAppWindowsCommand):
                self._arm_backend_watchdog(cmd.op_token, timeout_sec=2.5)
                self._nav_executor.submit(
                    self._task_inspect_windows, cmd.op_token, cmd.binding
                )
            elif isinstance(cmd, LaunchAppCommand):
                self._arm_backend_watchdog(cmd.op_token, timeout_sec=10.0)
                self._main.dispatch(lambda b=cmd.binding: self._overlay.show_launch_pending(b))
                self._nav_executor.submit(
                    self._task_launch_app, cmd.op_token, cmd.binding
                )
            elif isinstance(cmd, ActivateWindowCommand):
                self._arm_backend_watchdog(cmd.op_token, timeout_sec=2.5)
                self._nav_executor.submit(
                    self._task_activate_window,
                    cmd.op_token,
                    cmd.binding,
                    cmd.slot,
                    cmd.from_launch,
                )
            elif isinstance(cmd, ActivateAppNoWindowsCommand):
                self._arm_backend_watchdog(cmd.op_token, timeout_sec=2.0)
                self._nav_executor.submit(
                    self._task_activate_app_only,
                    cmd.op_token,
                    cmd.binding,
                    cmd.from_launch,
                )
            elif isinstance(cmd, RequestOpenAppsForAssignCommand):
                self._arm_backend_watchdog(cmd.op_token, timeout_sec=3.0)
                self._nav_executor.submit(
                    self._task_resolve_open_apps_for_assign, cmd.op_token
                )
            elif isinstance(cmd, CommitQuickAssignCommand):
                self._main.dispatch(lambda c=cmd: self._execute_commit_quick_assign_on_main(c))
            elif isinstance(cmd, OpenFullAssignmentWindowCommand):
                self._main.dispatch(self._assignment_editor.show_window)
            else:
                self._main.dispatch(lambda c=cmd: self._execute_ui_command_on_main(c))

    def _execute_ui_command_on_main(self, cmd: NavCommand) -> None:
        if isinstance(cmd, ShowAppMapCommand):
            self._active_window_list_bundle = None
            if self._store.reload_if_modified():
                self._sync_config_to_controller()
            self._overlay.set_mouse_enabled(self._store.enable_mouse_selection)
            running = self._app_registry.running_bundle_ids()
            self._controller.update_running_apps(running)
            self._overlay.show_app_map(
                active_layer=cmd.active_layer,
                bindings=self._store.bindings,
                running_bundle_ids=running,
                duplicate_conflicts=self._duplicate_conflicts,
                quick_assign_enabled=self._store.trigger.enable_caps_hold_assign,
            )
        elif isinstance(cmd, UpdateAppMapLayerCommand):
            self._overlay.update_app_map_layer(cmd.active_layer)
        elif isinstance(cmd, ShowQuickAssignAppListCommand):
            self._active_window_list_bundle = None
            self._overlay.show_quick_assign_apps(
                apps=cmd.apps,
                selected_index=cmd.selected_index,
                bindings=self._store.bindings,
                notice=cmd.notice,
            )
        elif isinstance(cmd, UpdateQuickAssignAppSelectionCommand):
            self._overlay.update_quick_assign_app_selection(
                selected_index=cmd.selected_index,
                notice=cmd.notice,
            )
        elif isinstance(cmd, ShowQuickAssignKeyMapCommand):
            self._active_window_list_bundle = None
            running = self._app_registry.running_bundle_ids()
            self._overlay.show_quick_assign_keymap(
                app=cmd.app,
                active_layer=cmd.active_layer,
                bindings=self._store.bindings,
                running_bundle_ids=running,
                duplicate_conflicts=self._duplicate_conflicts,
                conflict_existing=cmd.conflict_existing,
            )
        elif isinstance(cmd, ShowWindowListCommand):
            self._active_window_list_bundle = cmd.binding.bundle_id
            self._overlay.set_mouse_enabled(self._store.enable_mouse_selection)
            self._overlay.show_window_list(
                binding=cmd.binding,
                slots=cmd.slots,
                selected_slot=cmd.selected_slot,
                notice=cmd.notice,
            )
        elif isinstance(cmd, UpdateWindowSelectionCommand):
            self._overlay.update_window_selection(
                selected_slot=cmd.selected_slot,
                notice=cmd.notice,
            )
        elif isinstance(cmd, ShowStatusBannerCommand):
            if not cmd.keep_app_map:
                self._active_window_list_bundle = None
            self._overlay.show_status_banner(
                level=cmd.level,
                message=cmd.message,
                auto_hide_ms=cmd.auto_hide_ms,
                keep_app_map=cmd.keep_app_map,
            )
        elif isinstance(cmd, HideOverlayCommand):
            self._active_window_list_bundle = None
            self._overlay.hide()

    def _execute_commit_quick_assign_on_main(
        self, cmd: CommitQuickAssignCommand
    ) -> None:
        if not cmd.app.bundle_id:
            self._overlay.show_status_banner(
                level="error",
                message=f"Cannot bind '{cmd.app.display_name}': missing macOS bundle_id",
                auto_hide_ms=2600,
            )
            return

        new_binding = AppBinding(
            key=cmd.key,
            layer=cmd.layer,
            bundle_id=cmd.app.bundle_id,
            display_name=cmd.app.display_name,
            bundle_path=cmd.app.bundle_path,
        )
        try:
            self._store.assign(
                new_binding,
                allow_replace=cmd.allow_replace,
                remove_previous_for_bundle=True,
            )
        except CorruptedConfigLockError as exc:
            self._overlay.show_status_banner(
                level="error",
                message=str(exc),
                auto_hide_ms=3600,
            )
            return

        self._sync_config_to_controller()
        prefix = "⇧" if cmd.layer is Layer.SHIFT else ""
        self._overlay.show_status_banner(
            level="success",
            message=f"Assigned {prefix}{cmd.key} → {cmd.app.display_name}   (Jump anytime: Caps ×2 → {prefix}{cmd.key})",
            auto_hide_ms=1900,
        )

    # --- Background Worker Tasks ---

    def _task_resolve_open_apps_for_assign(self, op_token: int) -> None:
        if self._controller.op_token != op_token:
            return
        ws = Cocoa.NSWorkspace.sharedWorkspace()
        front_app = ws.frontmostApplication()
        front_bid = (
            str(front_app.bundleIdentifier())
            if (front_app is not None and front_app.bundleIdentifier())
            else None
        )

        running = list(self._app_registry.running_apps())
        bound_bids = {b.bundle_id for b in self._store.bindings}

        # Sort order:
        # 1. Currently frontmost open app (so user can press Enter immediately!)
        # 2. Unassigned open apps (alphabetical)
        # 3. Already-bound open apps (alphabetical)
        def _sort_key(rec: AppRecord) -> tuple[int, str]:
            if front_bid and rec.bundle_id == front_bid:
                return (0, rec.display_name.casefold())
            if rec.bundle_id is None or rec.bundle_id not in bound_bids:
                return (1, rec.display_name.casefold())
            return (2, rec.display_name.casefold())

        running.sort(key=_sort_key)
        self._post_backend_event(
            OpenAppsForAssignResolved(
                op_token=op_token,
                apps=tuple(running),
                default_index=0,
            )
        )

    def _task_inspect_windows(self, op_token: int, binding) -> None:
        if self._controller.op_token != op_token:
            return
        is_running = self._app_registry.is_bundle_running(binding.bundle_id)
        if not is_running:
            self._post_backend_event(
                AppWindowsResolved(
                    op_token=op_token,
                    bundle_id=binding.bundle_id,
                    slots=(),
                    default_slot=None,
                    generation=0,
                    app_is_running=False,
                )
            )
            return

        discovered = self._window_registry.discover_windows(
            binding.bundle_id, timeout_sec=0.75
        )
        snap = self._slot_table.reconcile(binding.bundle_id, discovered)
        self._post_backend_event(
            AppWindowsResolved(
                op_token=op_token,
                bundle_id=binding.bundle_id,
                slots=snap.slots,
                default_slot=snap.default_slot,
                generation=snap.generation,
                app_is_running=True,
            )
        )

    def _task_launch_app(self, op_token: int, binding) -> None:
        res = self._app_registry.launch_and_wait_for_ready(
            bundle_id=binding.bundle_id,
            hint_path=binding.bundle_path,
            window_registry=self._window_registry,
            is_cancelled=lambda: self._controller.op_token != op_token,
            timeout_sec=8.0,
        )
        if self._controller.op_token != op_token:
            return
        if not res.success:
            self._post_backend_event(
                LaunchCompleted(
                    op_token=op_token,
                    bundle_id=binding.bundle_id,
                    error=res.error,
                    missing_bundle=res.missing_bundle,
                )
            )
            return

        self._refresh_running_apps_snapshot()
        discovered = self._window_registry.discover_windows(
            binding.bundle_id, timeout_sec=0.75
        )
        snap = self._slot_table.reconcile(binding.bundle_id, discovered)
        self._post_backend_event(
            LaunchCompleted(
                op_token=op_token,
                bundle_id=binding.bundle_id,
                slots=snap.slots,
                default_slot=snap.default_slot,
                generation=snap.generation,
            )
        )

    def _hide_overlay_sync(self, timeout_sec: float = 0.06) -> None:
        done = threading.Event()

        def _do_hide() -> None:
            self._active_window_list_bundle = None
            self._overlay.hide()
            done.set()

        self._main.dispatch(_do_hide)
        done.wait(timeout=timeout_sec)

    def _task_activate_window(
        self,
        op_token: int,
        binding,
        slot,
        from_launch: bool = False,
    ) -> None:
        if self._controller.op_token != op_token:
            return
        # Hide the picker NSPanel BEFORE raising/fullscreening so no floating
        # FullScreenAuxiliary panel interferes with WindowServer transitions.
        self._hide_overlay_sync()

        want_fullscreen = self._store.auto_fullscreen_on_switch
        started_switch = time.monotonic()
        was_already_fullscreen = bool(slot.window.is_fullscreen) and not from_launch
        leaving_fs_space = (
            want_fullscreen
            and not was_already_fullscreen
            and self._window_activator.is_frontmost_window_fullscreen(
                exclude_pid=slot.window.pid
            )
        )
        outcome = self._window_activator.activate_window(
            slot.window,
            timeout_sec=1.1 if from_launch else 0.9,
            make_fullscreen=False,
        )
        if outcome.stale_window:
            discovered = self._window_registry.discover_windows(
                binding.bundle_id, timeout_sec=0.5
            )
            snap = self._slot_table.reconcile(binding.bundle_id, discovered)
            # If a freshly launched app replaced its splash window with its real main
            # window during activation, activate & fullscreen that main window directly!
            if from_launch and len(snap.slots) == 1:
                slot = snap.slots[0]
                outcome = self._window_activator.activate_window(
                    slot.window,
                    timeout_sec=0.9,
                    make_fullscreen=False,
                )
            else:
                self._post_backend_event(
                    ActivationCompleted(
                        op_token=op_token,
                        success=False,
                        stale_window=True,
                        refreshed_slots=snap.slots,
                        refreshed_generation=snap.generation,
                        error=outcome.error,
                    )
                )
                return

        if outcome.success:
            self._slot_table.record_activated(binding.bundle_id, slot.window_id)
        # Release modal keyboard capture immediately once target window has focus.
        self._post_backend_event(
            ActivationCompleted(
                op_token=op_token,
                success=outcome.success,
                error=outcome.error,
            )
        )
        # Enforce Full Screen on the background pool so navigation worker returns immediately
        # and can never block subsequent Caps×2 jumps.
        if outcome.success and want_fullscreen and self._controller.op_token == op_token:
            if was_already_fullscreen and self._window_activator.is_window_currently_fullscreen(
                slot.window, default_if_busy=True
            ):
                return
            crossed_space = (
                leaving_fs_space
                or slot.window.space_label == "Other Space"
                or self._window_activator.did_space_change_since(started_switch - 0.10)
            )
            target_win = slot.window
            self._bg_executor.submit(
                self._window_activator.ensure_window_fullscreen,
                target_win,
                lambda: self._controller.op_token != op_token,
                3.2 if from_launch else (2.4 if crossed_space else 1.6),
                from_launch,
                crossed_space,
                started_switch,
            )

    def _task_activate_app_only(
        self,
        op_token: int,
        binding,
        from_launch: bool = False,
    ) -> None:
        if self._controller.op_token != op_token:
            return
        self._hide_overlay_sync()
        want_fullscreen = self._store.auto_fullscreen_on_switch
        started_switch = time.monotonic()
        leaving_fs_space = (
            want_fullscreen and self._window_activator.is_frontmost_window_fullscreen()
        )
        outcome = self._window_activator.activate_application_only(
            binding.bundle_id, timeout_sec=0.6
        )
        self._post_backend_event(
            ActivationCompleted(
                op_token=op_token,
                success=outcome.success,
                error=outcome.error,
            )
        )
        if outcome.success and want_fullscreen and self._controller.op_token == op_token:
            # Cold-launched apps (e.g. heavy Electron apps) or apps auto-opening a default
            # window upon activation may create their main window slightly after activation;
            # watch on the background pool without holding a navigation worker.
            wait_budget = 6.0 if from_launch else 0.9
            self._bg_executor.submit(
                self._await_launched_window_and_fullscreen,
                op_token,
                binding,
                wait_budget,
                leaving_fs_space,
                started_switch,
            )

    def _await_launched_window_and_fullscreen(
        self,
        op_token: int,
        binding,
        timeout_sec: float,
        leaving_fs_space: bool = False,
        activation_started_at: float | None = None,
    ) -> None:
        started_watch = activation_started_at if activation_started_at is not None else time.monotonic()
        deadline = time.monotonic() + max(0.3, timeout_sec)
        ws = Cocoa.NSWorkspace.sharedWorkspace()
        while time.monotonic() < deadline:
            if self._controller.op_token != op_token:
                return
            if not self._store.auto_fullscreen_on_switch:
                return
            front = ws.frontmostApplication()
            if front is not None and front.bundleIdentifier():
                front_bid = str(front.bundleIdentifier())
                if front_bid != binding.bundle_id:
                    return

            discovered = self._window_registry.discover_windows(
                binding.bundle_id, timeout_sec=0.35
            )
            if discovered:
                snap = self._slot_table.reconcile(binding.bundle_id, discovered)
                target = next(
                    (w for w in discovered if not w.is_modal and not w.is_minimized),
                    discovered[0],
                )
                for s in snap.slots:
                    if s.window_id == target.window_id:
                        self._slot_table.record_activated(binding.bundle_id, s.window_id)
                        break
                self._window_activator.activate_window(
                    target,
                    timeout_sec=0.8,
                    make_fullscreen=False,
                )
                crossed = (
                    leaving_fs_space
                    or target.space_label == "Other Space"
                    or self._window_activator.did_space_change_since(started_watch - 0.10)
                )
                self._window_activator.ensure_window_fullscreen(
                    target,
                    is_cancelled=lambda: self._controller.op_token != op_token,
                    timeout_sec=2.8,
                    allow_window_reacquire=True,
                    crossed_space=crossed,
                    activation_started_at=started_watch,
                )
                return
            time.sleep(0.14)

    def _task_poll_active_window_list(self, bundle_id: str) -> None:
        if self._controller.state is not NavState.WINDOW_SELECTION:
            return
        prev = self._slot_table.latest_snapshot(bundle_id)
        discovered = self._window_registry.discover_windows(bundle_id, timeout_sec=0.35)
        snap = self._slot_table.reconcile(bundle_id, discovered)
        if prev is None or snap.generation != prev.generation:
            self._post_backend_event(
                WindowListRefreshed(
                    bundle_id=bundle_id,
                    slots=snap.slots,
                    generation=snap.generation,
                )
            )

    # --- Lifecycle, Sleep/Wake, Lock Screen, and Menu Handlers ---

    def _on_app_lifecycle(self, kind: str, bundle_id: str | None, pid: int) -> None:
        if kind == "terminate":
            self._window_registry.prune_terminated_pid(bundle_id, pid)
            self._window_activator.prune_terminated_pid(pid)
            if bundle_id and not self._app_registry.is_bundle_running(bundle_id):
                self._slot_table.clear_app(bundle_id)

        self._refresh_running_apps_snapshot()
        self._main.dispatch(self._refresh_ui_after_app_lifecycle_on_main)

    def _refresh_ui_after_app_lifecycle_on_main(self) -> None:
        self._update_status_bar_summary()
        if self._assignment_editor.is_visible():
            self._assignment_editor.refresh_data()

    def _refresh_running_apps_snapshot(self) -> None:
        running = self._app_registry.running_bundle_ids()
        self._controller.update_running_apps(running)

    def _refresh_duplicate_conflicts_async(self) -> None:
        def _scan() -> None:
            installed = self._app_registry.installed_apps()
            conflicts = frozenset(
                r.bundle_id
                for r in installed
                if r.bundle_id and r.has_duplicate_install_conflict
            )
            self._duplicate_conflicts = conflicts

        self._bg_executor.submit(_scan)

    def _sync_config_to_controller(self) -> None:
        trig = self._store.trigger
        mouse_enabled = self._store.enable_mouse_selection
        self._controller.set_trigger_interval_ms(trig.interval_ms)
        self._controller.set_caps_hold_assign_enabled(trig.enable_caps_hold_assign)
        self._controller.set_mouse_selection_enabled(mouse_enabled)
        self._main.dispatch(lambda enabled=mouse_enabled: self._overlay.set_mouse_enabled(enabled))
        self._input_adapter.set_hold_assign_ms(trig.hold_assign_ms)
        if trig.enable_caps_hold_assign != self._input_adapter.caps_hold_enabled:
            was_running = self._input_adapter.is_running
            if was_running:
                self._input_adapter.stop()
            self._input_adapter.set_caps_hold_enabled(trig.enable_caps_hold_assign)
            if was_running:
                self._ensure_input_tap_state()
        self._controller.update_bindings(self._store.bindings)
        self._refresh_duplicate_conflicts_async()
        self._update_status_bar_summary()

    def _reload_config_from_disk(self) -> None:
        self._store.load()
        self._sync_config_to_controller()
        if self._store.load_error:
            self._overlay.show_status_banner(
                level="error",
                message="Reload failed: config.json is damaged and was NOT overwritten.",
                auto_hide_ms=4000,
            )
        else:
            self._overlay.show_status_banner(
                level="info",
                message="Reloaded AppJuggler configuration.",
                auto_hide_ms=1600,
            )

    def _manual_open_navigator(self) -> None:
        status = self._ensure_input_tap_state()
        if not status.is_ready:
            self._status_bar.show_permissions_dialog(prompt_system=True)
            return
        if self._controller.state is NavState.IDLE:
            t = 1_000_000_000
            ev1 = HardwareKeyEvent(
                kind=self._caps_kind(),
                keycode=57,
                timestamp_ns=t,
                caps_lock_on=True,
                caps_lock_toggled=True,
            )
            ev2 = HardwareKeyEvent(
                kind=self._caps_kind(),
                keycode=57,
                timestamp_ns=t + 50_000_000,
                caps_lock_on=False,
                caps_lock_toggled=True,
            )
            self._on_hardware_key(ev1)
            self._on_hardware_key(ev2)

    @staticmethod
    def _caps_kind():
        from appjuggler.domain.events import KeyKind

        return KeyKind.CAPS_LOCK

    def _ensure_input_tap_state(self) -> PermissionStatus:
        status = self._permission_monitor.check_status(prompt=False)
        if status.screen_locked:
            if self._input_adapter.is_running:
                self._input_adapter.stop()
            self._update_status_bar_summary(status)
            return status

        if status.accessibility_trusted and not self._input_adapter.is_running:
            hold_cb = (
                self._on_caps_hold_triggered
                if self._store.trigger.enable_caps_hold_assign
                else None
            )
            started = self._input_adapter.start(
                on_event=self._on_hardware_key,
                on_interrupt=self._on_tap_interrupt,
                on_caps_hold=hold_cb,
            )
            if started:
                status = self._permission_monitor.check_status(prompt=False)

        self._update_status_bar_summary(status)
        return status

    def _update_status_bar_summary(
        self, status: PermissionStatus | None = None
    ) -> None:
        st = status or self._permission_monitor.check_status(prompt=False)
        bound = {b.bundle_id for b in self._store.bindings}
        # Use lightweight in-memory running_bundle_ids() rather than full LaunchServices /
        # filesystem bundle URL resolution so main-thread status updates take < 0.1 ms.
        running_bids = self._app_registry.running_bundle_ids()
        unassigned = len(running_bids - bound)
        self._status_bar.update_status(
            st,
            unassigned_count=unassigned,
            auto_fullscreen=self._store.auto_fullscreen_on_switch,
            mouse_selection=self._store.enable_mouse_selection,
        )

    def _toggle_auto_fullscreen_from_menu(self) -> None:
        new_val = not self._store.auto_fullscreen_on_switch
        try:
            self._store.set_auto_fullscreen_on_switch(new_val)
        except Exception:
            logger.exception("Failed toggling auto_fullscreen_on_switch")
            return
        self._sync_config_to_controller()
        if self._assignment_editor.is_visible():
            self._assignment_editor.refresh_data()

    def _toggle_mouse_selection_from_menu(self) -> None:
        new_val = not self._store.enable_mouse_selection
        try:
            self._store.set_enable_mouse_selection(new_val)
        except Exception:
            logger.exception("Failed toggling enable_mouse_selection")
            return
        self._sync_config_to_controller()
        if self._assignment_editor.is_visible():
            self._assignment_editor.refresh_data()

    def _handle_tap_suspended_on_main(self) -> None:
        self._input_adapter.stop()
        self._ensure_input_tap_state()

    def _handle_permissions_revoked_on_main(self) -> None:
        self._input_adapter.stop()
        status = self._ensure_input_tap_state()
        if not status.accessibility_trusted and not status.screen_locked:
            self._overlay.show_status_banner(
                level="error",
                message="Keyboard permissions lost — grant Accessibility to AppJuggler / Terminal.",
                auto_hide_ms=4500,
            )

    def _on_sleep_or_lock(self) -> None:
        self._post_backend_event(SystemInterruptEvent(reason="sleep_or_lock"))
        self._input_adapter.stop()
        self._update_status_bar_summary()

    def _on_wake_or_unlock(self) -> None:
        self._post_backend_event(SystemInterruptEvent(reason="wake"))
        self._input_adapter.resync_modifier_state()
        self._refresh_running_apps_snapshot()
        self._ensure_input_tap_state()

    def _on_health_pulse(self) -> None:
        self._recover_if_backend_stalled()
        if (
            self._controller.state is not NavState.ASSIGNMENT_EDITING
            and self._store.reload_if_modified()
        ):
            self._sync_config_to_controller()
        self._ensure_input_tap_state()
        if (
            self._controller.state is NavState.WINDOW_SELECTION
            and self._active_window_list_bundle is not None
        ):
            bid = self._active_window_list_bundle
            self._bg_executor.submit(self._task_poll_active_window_list, bid)

    def _register_power_and_lock_observers(self) -> None:
        obs = _PowerAndLockObserver.alloc().initWithCallbacks_(
            {
                "on_sleep": self._on_sleep_or_lock,
                "on_wake": self._on_wake_or_unlock,
                "on_lock": self._on_sleep_or_lock,
                "on_space_change": self._window_activator.note_space_change,
                "on_signal_wake": self._on_signal_pipe_wake,
                "on_pulse": self._on_health_pulse,
            }
        )
        ws_nc = Cocoa.NSWorkspace.sharedWorkspace().notificationCenter()
        ws_nc.addObserver_selector_name_object_(
            obs, "willSleep:", Cocoa.NSWorkspaceWillSleepNotification, None
        )
        ws_nc.addObserver_selector_name_object_(
            obs, "didWake:", Cocoa.NSWorkspaceDidWakeNotification, None
        )
        ws_nc.addObserver_selector_name_object_(
            obs,
            "sessionResigned:",
            Cocoa.NSWorkspaceSessionDidResignActiveNotification,
            None,
        )
        ws_nc.addObserver_selector_name_object_(
            obs,
            "activeSpaceChanged:",
            Cocoa.NSWorkspaceActiveSpaceDidChangeNotification,
            None,
        )

        dist_nc = Cocoa.NSDistributedNotificationCenter.defaultCenter()
        dist_nc.addObserver_selector_name_object_(
            obs, "screenLocked:", "com.apple.screenIsLocked", None
        )
        dist_nc.addObserver_selector_name_object_(
            obs, "screenUnlocked:", "com.apple.screenIsUnlocked", None
        )
        self._power_observer = obs

    def _unregister_power_and_lock_observers(self) -> None:
        if self._power_observer is None:
            return
        try:
            Cocoa.NSWorkspace.sharedWorkspace().notificationCenter().removeObserver_(
                self._power_observer
            )
        except Exception:
            pass
        try:
            Cocoa.NSDistributedNotificationCenter.defaultCenter().removeObserver_(
                self._power_observer
            )
        except Exception:
            pass
        self._power_observer = None
