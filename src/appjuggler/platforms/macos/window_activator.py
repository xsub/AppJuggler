"""macOS WindowActivator with bounded timeouts, unhide/unminimize, and verification."""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

import ApplicationServices
import Cocoa
import CoreFoundation
import Quartz

from appjuggler.contracts import WindowActivator, WindowRegistry
from appjuggler.domain.models import ActivationOutcome, DiscoveredWindow
from appjuggler.platforms.macos.window_registry import (
    AX_ERROR_API_DISABLED,
    AX_ERROR_CANNOT_COMPLETE,
    AX_ERROR_INVALID_UI_ELEMENT,
    AX_SUCCESS,
)

logger = logging.getLogger(__name__)


class MacOSWindowActivator(WindowActivator):
    """Raises and focuses specific macOS windows with bounded retries and verification."""

    def __init__(self, window_registry: WindowRegistry) -> None:
        self._window_registry = window_registry
        self._state_lock = threading.Lock()
        self._last_space_change_ts: float = 0.0
        self._fullscreen_refused_windows: set[tuple[int, str]] = set()

    def note_space_change(self) -> None:
        """Record timestamp of macOS active Space transition notification."""
        with self._state_lock:
            self._last_space_change_ts = time.monotonic()

    def did_space_change_since(self, timestamp_monotonic: float) -> bool:
        """Return True if macOS reported an active Space switch at or after timestamp."""
        with self._state_lock:
            return self._last_space_change_ts >= timestamp_monotonic

    def _get_last_space_change_ts(self) -> float:
        with self._state_lock:
            return self._last_space_change_ts

    def prune_terminated_pid(self, pid: int) -> None:
        """Clear session fullscreen suppression state when a process exits."""
        with self._state_lock:
            self._fullscreen_refused_windows = {
                item for item in self._fullscreen_refused_windows if item[0] != pid
            }

    def _is_fullscreen_refused(self, pid: int, window_id: str) -> bool:
        with self._state_lock:
            return (pid, window_id) in self._fullscreen_refused_windows

    def _mark_fullscreen_refused(self, pid: int, window_id: str) -> None:
        with self._state_lock:
            self._fullscreen_refused_windows.add((pid, window_id))

    def _clear_fullscreen_refused(self, pid: int, window_id: str) -> None:
        with self._state_lock:
            self._fullscreen_refused_windows.discard((pid, window_id))

    @staticmethod
    def is_frontmost_window_fullscreen(exclude_pid: int | None = None) -> bool:
        """Check whether the currently active macOS app is occupying a Full Screen Space."""
        ws = Cocoa.NSWorkspace.sharedWorkspace()
        front = ws.frontmostApplication()
        if front is None or front.isTerminated():
            return False
        pid = int(front.processIdentifier())
        if exclude_pid is not None and pid == exclude_pid:
            return False
        ax_app = ApplicationServices.AXUIElementCreateApplication(pid)
        ApplicationServices.AXUIElementSetMessagingTimeout(ax_app, 0.15)
        for attr in (
            ApplicationServices.kAXFocusedWindowAttribute,
            ApplicationServices.kAXMainWindowAttribute,
        ):
            err, ax_win = ApplicationServices.AXUIElementCopyAttributeValue(
                ax_app, attr, None
            )
            if err == AX_SUCCESS and ax_win is not None:
                ApplicationServices.AXUIElementSetMessagingTimeout(ax_win, 0.15)
                err_fs, fs_val = ApplicationServices.AXUIElementCopyAttributeValue(
                    ax_win, "AXFullScreen", None
                )
                if err_fs == AX_SUCCESS:
                    return bool(fs_val)
        return False

    @staticmethod
    def is_window_currently_fullscreen(
        window: DiscoveredWindow, default_if_busy: bool = False
    ) -> bool:
        """Read live AXFullScreen without triggering any window action or beep."""
        ax_win = window.native_handle
        if ax_win is None:
            return bool(window.is_fullscreen) if default_if_busy else False
        ApplicationServices.AXUIElementSetMessagingTimeout(ax_win, 0.25)
        err_fs, cur_fs = ApplicationServices.AXUIElementCopyAttributeValue(
            ax_win, "AXFullScreen", None
        )
        if err_fs == AX_SUCCESS:
            return bool(cur_fs)
        return bool(window.is_fullscreen) if default_if_busy else False

    def activate_window(
        self,
        window: DiscoveredWindow,
        timeout_sec: float = 0.9,
        make_fullscreen: bool = False,
    ) -> ActivationOutcome:
        started = time.monotonic()
        deadline = started + max(0.25, timeout_sec)

        running_app = self._find_running_app(window.bundle_id, window.pid)
        if running_app is None or not self._window_registry.is_window_alive(window):
            return ActivationOutcome(
                success=False,
                stale_window=True,
                error="Selected window closed before activation",
                elapsed_ms=(time.monotonic() - started) * 1000.0,
            )

        ax_win = window.native_handle
        ax_app = ApplicationServices.AXUIElementCreateApplication(window.pid)
        msg_timeout = max(0.2, min(0.35, timeout_sec * 0.5))
        ApplicationServices.AXUIElementSetMessagingTimeout(ax_win, msg_timeout)
        ApplicationServices.AXUIElementSetMessagingTimeout(ax_app, msg_timeout)

        # 1. Unhide application if hidden (Cmd+H).
        if running_app.isHidden():
            running_app.unhide()

        # 2. Unminimize window if minimized in the Dock.
        err_min, min_val = ApplicationServices.AXUIElementCopyAttributeValue(
            ax_win, ApplicationServices.kAXMinimizedAttribute, None
        )
        if err_min == AX_ERROR_API_DISABLED:
            return ActivationOutcome(
                success=False,
                error="Accessibility permission is disabled. Grant permission in System Settings.",
                elapsed_ms=(time.monotonic() - started) * 1000.0,
            )
        if err_min == AX_ERROR_INVALID_UI_ELEMENT:
            return ActivationOutcome(
                success=False,
                stale_window=True,
                error="Selected window closed before activation",
                elapsed_ms=(time.monotonic() - started) * 1000.0,
            )

        if err_min == AX_SUCCESS and bool(min_val):
            ApplicationServices.AXUIElementSetAttributeValue(
                ax_win, ApplicationServices.kAXMinimizedAttribute, False
            )
            unmin_deadline = min(deadline, time.monotonic() + 0.25)
            while time.monotonic() < unmin_deadline:
                _, cur_min = ApplicationServices.AXUIElementCopyAttributeValue(
                    ax_win, ApplicationServices.kAXMinimizedAttribute, None
                )
                if not bool(cur_min):
                    break
                time.sleep(0.025)

        # 3. Prime target window inside the app BEFORE process activation so Mission Control /
        #    Spaces swooshes directly to THIS window's Space rather than a different Space.
        raise_err = self._raise_and_focus_ax(ax_app, ax_win)
        if raise_err == AX_ERROR_INVALID_UI_ELEMENT:
            return ActivationOutcome(
                success=False,
                stale_window=True,
                error="Selected window closed during activation",
                elapsed_ms=(time.monotonic() - started) * 1000.0,
            )
        if raise_err == AX_ERROR_API_DISABLED:
            return ActivationOutcome(
                success=False,
                error="Accessibility permission is disabled.",
                elapsed_ms=(time.monotonic() - started) * 1000.0,
            )

        # 4. Activate target process (ignoring other apps, NOT raising all windows).
        self._activate_ns_running_app(running_app)

        # 5. Re-assert target window raise/main after process activation and verify outcome.
        self._raise_and_focus_ax(ax_app, ax_win)

        next_retry_at = time.monotonic() + 0.12
        last_ax_err = raise_err

        while time.monotonic() < deadline:
            if self._is_verified_focused(running_app, ax_app, ax_win):
                # Re-assert target window once process is active so multi-window apps
                # (e.g. Finder with several windows across Spaces) lock focus onto THIS window.
                self._raise_and_focus_ax(ax_app, ax_win)
                if make_fullscreen:
                    crossed = (
                        window.space_label == "Other Space"
                        or self.did_space_change_since(started - 0.10)
                    )
                    self.ensure_window_fullscreen(
                        window,
                        timeout_sec=2.4 if crossed else 1.6,
                        crossed_space=crossed,
                        activation_started_at=started,
                    )
                elapsed = (time.monotonic() - started) * 1000.0
                logger.info(
                    "Activated window for bundle=%s in %.1f ms",
                    window.bundle_id,
                    elapsed,
                )
                return ActivationOutcome(success=True, elapsed_ms=elapsed)

            now = time.monotonic()
            if now >= next_retry_at:
                self._activate_ns_running_app(running_app)
                last_ax_err = self._raise_and_focus_ax(ax_app, ax_win)
                next_retry_at = now + 0.14

            time.sleep(0.025)

        # On cross-Space / Fullscreen transitions, macOS Space swoosh animation (~350-600ms)
        # may still be finishing when `isActive()` becomes True. Accept if process is active
        # and no fatal AX error occurred, otherwise report controlled timeout.
        elapsed = (time.monotonic() - started) * 1000.0
        if running_app.isActive() and last_ax_err == AX_SUCCESS:
            self._raise_and_focus_ax(ax_app, ax_win)
            if make_fullscreen:
                self.ensure_window_fullscreen(
                    window,
                    timeout_sec=2.4,
                    crossed_space=True,
                    activation_started_at=started,
                )
            logger.info(
                "Activated app/window (Space transition in progress) bundle=%s in %.1f ms",
                window.bundle_id,
                elapsed,
            )
            return ActivationOutcome(success=True, elapsed_ms=elapsed)

        if last_ax_err == AX_ERROR_CANNOT_COMPLETE:
            err_msg = "Accessibility activation timed out (target application may be unresponsive)."
        else:
            err_msg = f"Could not confirm focus on target window within {int(timeout_sec * 1000)} ms."
        logger.warning("Window activation incomplete bundle=%s: %s", window.bundle_id, err_msg)
        return ActivationOutcome(success=False, error=err_msg, elapsed_ms=elapsed)

    def activate_application_only(
        self,
        bundle_id: str,
        timeout_sec: float = 0.6,
    ) -> ActivationOutcome:
        """Activate a running windowless application without creating new documents (§2.5)."""
        started = time.monotonic()
        deadline = started + max(0.2, timeout_sec)

        ws = Cocoa.NSWorkspace.sharedWorkspace()
        apps = [
            a
            for a in ws.runningApplications()
            if not a.isTerminated() and str(a.bundleIdentifier() or "") == bundle_id
        ]
        if not apps:
            return ActivationOutcome(
                success=False,
                error=f"Application '{bundle_id}' is no longer running.",
                elapsed_ms=(time.monotonic() - started) * 1000.0,
            )

        target = apps[0]
        if target.isHidden():
            target.unhide()
        self._activate_ns_running_app(target)

        while time.monotonic() < deadline:
            if target.isActive():
                return ActivationOutcome(
                    success=True,
                    elapsed_ms=(time.monotonic() - started) * 1000.0,
                )
            time.sleep(0.025)

        return ActivationOutcome(
            success=bool(target.isActive()),
            error=None if target.isActive() else f"Timed out activating '{bundle_id}'.",
            elapsed_ms=(time.monotonic() - started) * 1000.0,
        )

    @staticmethod
    def _find_running_app(bundle_id: str, pid: int):
        ws = Cocoa.NSWorkspace.sharedWorkspace()
        for app in ws.runningApplications():
            if (
                not app.isTerminated()
                and int(app.processIdentifier()) == pid
                and str(app.bundleIdentifier() or "") == bundle_id
            ):
                return app
        return None

    @staticmethod
    def _activate_ns_running_app(running_app) -> None:
        running_app.activateWithOptions_(Cocoa.NSApplicationActivateIgnoringOtherApps)

    @staticmethod
    def _raise_and_focus_ax(ax_app, ax_win) -> int:
        ApplicationServices.AXUIElementSetAttributeValue(
            ax_win, ApplicationServices.kAXMainAttribute, True
        )
        ApplicationServices.AXUIElementSetAttributeValue(
            ax_win, ApplicationServices.kAXFocusedAttribute, True
        )
        ApplicationServices.AXUIElementSetAttributeValue(
            ax_app, ApplicationServices.kAXFocusedWindowAttribute, ax_win
        )
        err = int(
            ApplicationServices.AXUIElementPerformAction(
                ax_win, ApplicationServices.kAXRaiseAction
            )
        )
        return err

    @staticmethod
    def _is_verified_focused(running_app, ax_app, ax_win) -> bool:
        if not running_app.isActive():
            return False
        err, focused = ApplicationServices.AXUIElementCopyAttributeValue(
            ax_app, ApplicationServices.kAXFocusedWindowAttribute, None
        )
        if err == AX_SUCCESS and focused is not None:
            if bool(CoreFoundation.CFEqual(focused, ax_win)):
                return True
        err_main, is_main = ApplicationServices.AXUIElementCopyAttributeValue(
            ax_win, ApplicationServices.kAXMainAttribute, None
        )
        return err_main == AX_SUCCESS and bool(is_main)

    def ensure_window_fullscreen(
        self,
        window: DiscoveredWindow,
        is_cancelled: Callable[[], bool] | None = None,
        timeout_sec: float = 1.6,
        allow_window_reacquire: bool = False,
        crossed_space: bool = False,
        activation_started_at: float | None = None,
    ) -> bool:
        """Put a non-modal activated window into Full Screen without triggering AppKit NSBeep().

        Key macOS AppKit / Electron invariants enforced here:
        1. Immediate already-fullscreen exit: if `AXFullScreen` is already True (or was known True
           and AX is transiently busy mid-swoosh), return immediately without poking `AXFullScreen`
           or `AXFullScreenButton`.
        2. Cross-Space swoosh settle: calling `toggleFullScreen:` while WindowServer is still
           animating an incoming Space transition (~600-800 ms) causes AppKit's
           `-[NSWindow toggleFullScreen:]` (`anyTransitionInProgress` guard) to play `NSBeep()`
           and abort/corrupt the fullscreen transition (notably in Electron apps like Signal).
        3. Single clean trigger + full animation wait: never fire `AXFullScreen = True` and
           `AXPress(AXFullScreenButton)` back-to-back or hammer `toggleFullScreen:` in a fast loop
           while the ~600 ms Full Screen animation is in flight.
        """
        if window.is_modal and not allow_window_reacquire:
            return False

        cancelled = is_cancelled or (lambda: False)
        if cancelled():
            return False

        cur_win = window
        ax_win = cur_win.native_handle
        if ax_win is not None:
            ApplicationServices.AXUIElementSetMessagingTimeout(ax_win, 0.30)
            err_fs0, cur_fs0 = ApplicationServices.AXUIElementCopyAttributeValue(
                ax_win, "AXFullScreen", None
            )
            if err_fs0 == AX_SUCCESS and bool(cur_fs0):
                self._clear_fullscreen_refused(cur_win.pid, cur_win.window_id)
                return True
            if err_fs0 != AX_SUCCESS and cur_win.is_fullscreen and not allow_window_reacquire:
                return True
            if not allow_window_reacquire and self._is_fullscreen_refused(
                cur_win.pid, cur_win.window_id
            ):
                win_rect0 = self._read_ax_rect(ax_win)
                scr0, primary_h0 = self._screen_for_ax_rect(win_rect0)
                if scr0 is not None and primary_h0 > 0.0:
                    self._fill_screen_visible_frame(
                        ax_win, scr0, primary_h0, current_rect=win_rect0
                    )
                return True

        ax_app = ApplicationServices.AXUIElementCreateApplication(cur_win.pid)
        ApplicationServices.AXUIElementSetMessagingTimeout(ax_app, 0.30)

        started_fs = time.monotonic()
        base_start = activation_started_at if activation_started_at is not None else started_fs
        settled_for_space = False

        if crossed_space or self.did_space_change_since(base_start - 0.10):
            settle_state = self._wait_for_space_settle(
                ax_win=ax_win,
                base_start=base_start,
                started_fs=started_fs,
                cancelled=cancelled,
            )
            if settle_state == "cancelled":
                return False
            if settle_state == "already_fullscreen":
                self._clear_fullscreen_refused(cur_win.pid, cur_win.window_id)
                return True
            settled_for_space = True

        deadline = max(time.monotonic() + 0.65, started_fs + max(0.45, timeout_sec))
        native_grace_sec = 1.1
        attempted_window_ids: set[str] = set()
        menu_attempted_window_ids: set[str] = set()

        while time.monotonic() < deadline:
            if cancelled():
                return False

            running_app = self._find_running_app(cur_win.bundle_id, cur_win.pid)
            win_alive = (
                ax_win is not None
                and running_app is not None
                and self._window_registry.is_window_alive(cur_win)
            )

            err_role, raw_role = (
                ApplicationServices.AXUIElementCopyAttributeValue(
                    ax_win, ApplicationServices.kAXRoleAttribute, None
                )
                if win_alive
                else (-1, None)
            )
            _, raw_subrole = (
                ApplicationServices.AXUIElementCopyAttributeValue(
                    ax_win, ApplicationServices.kAXSubroleAttribute, None
                )
                if win_alive
                else (-1, None)
            )
            subrole = str(raw_subrole or "")
            non_standard = (
                not win_alive
                or err_role != AX_SUCCESS
                or str(raw_role or "") != "AXWindow"
                or cur_win.is_modal
                or subrole in ("AXSystemDialog", "AXSheet", "AXFloatingWindow", "AXUnknown")
            )

            if non_standard:
                if not allow_window_reacquire:
                    return False
                replacement = self._reacquire_standard_window(cur_win.bundle_id)
                if replacement is None:
                    time.sleep(0.14)
                    continue
                cur_win = replacement
                ax_win = cur_win.native_handle
                ax_app = ApplicationServices.AXUIElementCreateApplication(cur_win.pid)
                ApplicationServices.AXUIElementSetMessagingTimeout(ax_win, 0.30)
                ApplicationServices.AXUIElementSetMessagingTimeout(ax_app, 0.30)
                running_app = self._find_running_app(cur_win.bundle_id, cur_win.pid)
                if running_app is None:
                    return False

            err_fs, cur_fs = ApplicationServices.AXUIElementCopyAttributeValue(
                ax_win, "AXFullScreen", None
            )
            if err_fs == AX_SUCCESS and bool(cur_fs):
                self._clear_fullscreen_refused(cur_win.pid, cur_win.window_id)
                return True
            if err_fs != AX_SUCCESS:
                if cur_win.is_fullscreen and not allow_window_reacquire:
                    return True
                time.sleep(0.08)
                continue

            if not settled_for_space and self.did_space_change_since(base_start - 0.10):
                settle_state = self._wait_for_space_settle(
                    ax_win=ax_win,
                    base_start=base_start,
                    started_fs=started_fs,
                    cancelled=cancelled,
                )
                if settle_state == "cancelled":
                    return False
                if settle_state == "already_fullscreen":
                    self._clear_fullscreen_refused(cur_win.pid, cur_win.window_id)
                    return True
                settled_for_space = True
                continue

            # Keep the selected window raised and main in multi-window apps (like Finder)
            # once any incoming Space swoosh or cold launch has settled.
            if not running_app.isActive():
                self._activate_ns_running_app(running_app)
            self._raise_and_focus_ax(ax_app, ax_win)

            err_set, is_settable = ApplicationServices.AXUIElementIsAttributeSettable(
                ax_win, "AXFullScreen", None
            )
            can_set_ax_fs = err_set == AX_SUCCESS and bool(is_settable)

            err_btn, fs_btn = ApplicationServices.AXUIElementCopyAttributeValue(
                ax_win, ApplicationServices.kAXFullScreenButtonAttribute, None
            )
            has_fs_btn = err_btn == AX_SUCCESS and fs_btn is not None

            win_rect = self._read_ax_rect(ax_win)
            scr, primary_h = self._screen_for_ax_rect(win_rect)
            scr_frame = scr.frame() if scr is not None else None
            if (
                not can_set_ax_fs
                and not has_fs_btn
                and win_rect is not None
                and scr_frame is not None
                and win_rect[2] >= float(scr_frame.size.width) - 8.0
                and win_rect[3] >= float(scr_frame.size.height) - 8.0
            ):
                return True

            # Branch A: Native Full Screen supported via AXFullScreen or AXFullScreenButton.
            if can_set_ax_fs or has_fs_btn:
                if cur_win.window_id in attempted_window_ids:
                    if allow_window_reacquire:
                        newer = self._reacquire_standard_window(cur_win.bundle_id)
                        if newer is not None and newer.window_id != cur_win.window_id:
                            cur_win = newer
                            ax_win = cur_win.native_handle
                            ax_app = ApplicationServices.AXUIElementCreateApplication(cur_win.pid)
                            ApplicationServices.AXUIElementSetMessagingTimeout(ax_win, 0.30)
                            ApplicationServices.AXUIElementSetMessagingTimeout(ax_app, 0.30)
                            continue
                    break

                attempted_window_ids.add(cur_win.window_id)
                triggered = False

                # 1. Prefer setting AXFullScreen = True once (never double-fire fs_btn on top).
                if can_set_ax_fs:
                    set_err = int(
                        ApplicationServices.AXUIElementSetAttributeValue(
                            ax_win, "AXFullScreen", True
                        )
                    )
                    if set_err == AX_SUCCESS:
                        triggered = True

                # 2. Fall back to pressing AXFullScreenButton ONLY when AXFullScreen could not
                #    be set via attribute API.
                if not triggered and has_fs_btn:
                    _, btn_sub = ApplicationServices.AXUIElementCopyAttributeValue(
                        fs_btn, ApplicationServices.kAXSubroleAttribute, None
                    )
                    _, btn_en = ApplicationServices.AXUIElementCopyAttributeValue(
                        fs_btn, ApplicationServices.kAXEnabledAttribute, None
                    )
                    _, cur_fs_again = ApplicationServices.AXUIElementCopyAttributeValue(
                        ax_win, "AXFullScreen", None
                    )
                    if (
                        not bool(cur_fs_again)
                        and str(btn_sub or "") == "AXFullScreenButton"
                        and bool(btn_en)
                    ):
                        act_err = int(
                            ApplicationServices.AXUIElementPerformAction(
                                fs_btn, ApplicationServices.kAXPressAction
                            )
                        )
                        if act_err == AX_SUCCESS:
                            triggered = True

                if triggered:
                    if self._wait_for_fullscreen_transition(
                        ax_win,
                        initial_rect=win_rect,
                        cancelled=cancelled,
                        wait_sec=1.05,
                    ):
                        self._clear_fullscreen_refused(cur_win.pid, cur_win.window_id)
                        return True
                    if cancelled():
                        return False
                    if allow_window_reacquire and (time.monotonic() + 0.20) < deadline:
                        newer = self._reacquire_standard_window(cur_win.bundle_id)
                        if newer is not None and newer.window_id != cur_win.window_id:
                            cur_win = newer
                            ax_win = cur_win.native_handle
                            ax_app = ApplicationServices.AXUIElementCreateApplication(cur_win.pid)
                            ApplicationServices.AXUIElementSetMessagingTimeout(ax_win, 0.30)
                            ApplicationServices.AXUIElementSetMessagingTimeout(ax_app, 0.30)
                            continue
                    if not allow_window_reacquire and self._window_registry.is_window_alive(cur_win):
                        self._mark_fullscreen_refused(cur_win.pid, cur_win.window_id)
                    if scr is not None and primary_h > 0.0:
                        self._fill_screen_visible_frame(
                            ax_win, scr, primary_h, current_rect=self._read_ax_rect(ax_win)
                        )
                    return False

                # Neither setter nor button was enabled yet (e.g. cold-launch initialization).
                if allow_window_reacquire and (time.monotonic() + 0.16) < deadline:
                    attempted_window_ids.discard(cur_win.window_id)
                    time.sleep(0.16)
                    continue
                if scr is not None and primary_h > 0.0:
                    self._fill_screen_visible_frame(
                        ax_win, scr, primary_h, current_rect=win_rect
                    )
                return False

            # Branch B: Custom-titlebar / non-standard apps without AXFullScreen settable or button.
            if cur_win.window_id not in menu_attempted_window_ids:
                menu_attempted_window_ids.add(cur_win.window_id)
                if self._trigger_menubar_enter_fullscreen(ax_app):
                    if self._wait_for_fullscreen_transition(
                        ax_win,
                        initial_rect=win_rect,
                        cancelled=cancelled,
                        wait_sec=1.05,
                    ):
                        self._clear_fullscreen_refused(cur_win.pid, cur_win.window_id)
                        return True
                    if cancelled():
                        return False

            if (
                allow_window_reacquire
                and (time.monotonic() - started_fs) < native_grace_sec
                and (time.monotonic() + 0.16) < deadline
            ):
                newer = self._reacquire_standard_window(cur_win.bundle_id)
                if newer is not None and newer.window_id != cur_win.window_id:
                    cur_win = newer
                    ax_win = cur_win.native_handle
                    ax_app = ApplicationServices.AXUIElementCreateApplication(cur_win.pid)
                    ApplicationServices.AXUIElementSetMessagingTimeout(ax_win, 0.30)
                    ApplicationServices.AXUIElementSetMessagingTimeout(ax_app, 0.30)
                else:
                    menu_attempted_window_ids.discard(cur_win.window_id)
                time.sleep(0.14)
                continue

            # Graceful maximize fallback when native Full Screen is unsupported (e.g. System Settings).
            if scr is not None and primary_h > 0.0:
                self._fill_screen_visible_frame(
                    ax_win, scr, primary_h, current_rect=win_rect
                )
            return True

        _, final_fs = ApplicationServices.AXUIElementCopyAttributeValue(
            ax_win, "AXFullScreen", None
        )
        return bool(final_fs)

    def _wait_for_space_settle(
        self,
        ax_win,
        base_start: float,
        started_fs: float,
        cancelled: Callable[[], bool],
    ) -> str:
        """Wait for macOS cross-Space swoosh animation to finish so toggleFullScreen: never NSBeeps."""
        hard_cap = started_fs + 0.95
        while True:
            if cancelled():
                return "cancelled"
            if ax_win is not None:
                err_fs, fs_val = ApplicationServices.AXUIElementCopyAttributeValue(
                    ax_win, "AXFullScreen", None
                )
                if err_fs == AX_SUCCESS and bool(fs_val):
                    return "already_fullscreen"
            target_ts = min(
                hard_cap,
                max(base_start + 0.82, self._get_last_space_change_ts() + 0.58),
            )
            now = time.monotonic()
            if now >= target_ts:
                return "settled"
            time.sleep(min(0.04, target_ts - now))

    def _reacquire_standard_window(self, bundle_id: str) -> DiscoveredWindow | None:
        discovered = self._window_registry.discover_windows(bundle_id, timeout_sec=0.35)
        for win in discovered:
            if not win.is_modal and not win.is_minimized:
                return win
        return discovered[0] if discovered else None

    @classmethod
    def _wait_for_fullscreen_transition(
        cls,
        ax_win,
        initial_rect: tuple[float, float, float, float] | None,
        cancelled: Callable[[], bool],
        wait_sec: float,
    ) -> bool:
        """Observe a single Full Screen transition without sending duplicate toggle commands."""
        started = time.monotonic()
        base_end = started + wait_sec
        max_end = base_end + 0.50
        rect_animating = False

        while True:
            if cancelled():
                return False
            err, fs_val = ApplicationServices.AXUIElementCopyAttributeValue(
                ax_win, "AXFullScreen", None
            )
            if err == AX_SUCCESS and bool(fs_val):
                return True

            now = time.monotonic()
            if now >= base_end and not rect_animating and initial_rect is not None:
                cur_rect = cls._read_ax_rect(ax_win)
                if cur_rect is not None and (
                    abs(cur_rect[1] - initial_rect[1]) > 4.0
                    or abs(cur_rect[2] - initial_rect[2]) > 4.0
                    or abs(cur_rect[3] - initial_rect[3]) > 4.0
                ):
                    rect_animating = True

            limit = max_end if rect_animating else base_end
            if now >= limit:
                return False
            time.sleep(0.04)

    @staticmethod
    def _trigger_menubar_enter_fullscreen(ax_app) -> bool:
        err, menubar = ApplicationServices.AXUIElementCopyAttributeValue(
            ax_app, ApplicationServices.kAXMenuBarAttribute, None
        )
        if err != AX_SUCCESS or menubar is None:
            return False
        err_c, top_items = ApplicationServices.AXUIElementCopyAttributeValue(
            menubar, ApplicationServices.kAXChildrenAttribute, None
        )
        if err_c != AX_SUCCESS or not top_items:
            return False

        for top_item in top_items:
            _, top_title = ApplicationServices.AXUIElementCopyAttributeValue(
                top_item, ApplicationServices.kAXTitleAttribute, None
            )
            if str(top_title or "") not in ("View", "Window", "Widok", "Okno"):
                continue
            _, submenus = ApplicationServices.AXUIElementCopyAttributeValue(
                top_item, ApplicationServices.kAXChildrenAttribute, None
            )
            if not submenus:
                continue
            _, menu_items = ApplicationServices.AXUIElementCopyAttributeValue(
                submenus[0], ApplicationServices.kAXChildrenAttribute, None
            )
            for mi in menu_items or []:
                _, enabled = ApplicationServices.AXUIElementCopyAttributeValue(
                    mi, ApplicationServices.kAXEnabledAttribute, None
                )
                if not bool(enabled):
                    continue
                _, ident = ApplicationServices.AXUIElementCopyAttributeValue(
                    mi, ApplicationServices.kAXIdentifierAttribute, None
                )
                _, title = ApplicationServices.AXUIElementCopyAttributeValue(
                    mi, ApplicationServices.kAXTitleAttribute, None
                )
                _, mark = ApplicationServices.AXUIElementCopyAttributeValue(
                    mi, "AXMenuItemMarkChar", None
                )
                ident_str = str(ident or "")
                title_str = str(title or "")
                mark_str = str(mark or "").strip()
                if title_str.startswith(
                    (
                        "Exit Full Screen",
                        "Leave Full Screen",
                        "Opuść tryb pełnoekranowy",
                        "Wyjdź z trybu pełnoekranowego",
                    )
                ):
                    return False
                if mark_str and (
                    ident_str
                    in (
                        "enterFullScreenMode:",
                        "toggleFullScreen:",
                        "toggleFullScreenMode:",
                    )
                    or "Full Screen" in title_str
                    or "pełnoekranow" in title_str.lower()
                    or "pełny ekran" in title_str.lower()
                ):
                    return False
                if ident_str in (
                    "enterFullScreenMode:",
                    "toggleFullScreen:",
                    "toggleFullScreenMode:",
                ) or title_str.startswith(
                    (
                        "Enter Full Screen",
                        "Toggle Full Screen",
                        "Wejdź do trybu pełnoekranowego",
                        "Przełącz tryb pełnoekranowy",
                        "Pełny ekran",
                    )
                ):
                    act_err = ApplicationServices.AXUIElementPerformAction(
                        mi, ApplicationServices.kAXPressAction
                    )
                    if act_err == AX_SUCCESS:
                        return True
        return False

    @staticmethod
    def _read_ax_rect(ax_win) -> tuple[float, float, float, float] | None:
        err_p, pos_val = ApplicationServices.AXUIElementCopyAttributeValue(
            ax_win, ApplicationServices.kAXPositionAttribute, None
        )
        err_s, sz_val = ApplicationServices.AXUIElementCopyAttributeValue(
            ax_win, ApplicationServices.kAXSizeAttribute, None
        )
        if err_p != AX_SUCCESS or err_s != AX_SUCCESS or pos_val is None or sz_val is None:
            return None
        ok_p, pt = ApplicationServices.AXValueGetValue(
            pos_val, ApplicationServices.kAXValueCGPointType, None
        )
        ok_s, sz = ApplicationServices.AXValueGetValue(
            sz_val, ApplicationServices.kAXValueCGSizeType, None
        )
        if not (ok_p and ok_s):
            return None
        return (float(pt.x), float(pt.y), float(sz.width), float(sz.height))

    @staticmethod
    def _screen_for_ax_rect(
        win_rect: tuple[float, float, float, float] | None,
    ) -> tuple[object | None, float]:
        screens = list(Cocoa.NSScreen.screens() or [])
        if not screens:
            return None, 0.0
        primary_h = float(screens[0].frame().size.height)
        if win_rect is None:
            return (Cocoa.NSScreen.mainScreen() or screens[0]), primary_h

        cx = win_rect[0] + win_rect[2] * 0.5
        cy_ax = win_rect[1] + win_rect[3] * 0.5
        # Convert AX top-left Y to AppKit bottom-left Y
        cy_cocoa = primary_h - cy_ax
        pt = Cocoa.NSMakePoint(cx, cy_cocoa)
        for scr in screens:
            if Cocoa.NSPointInRect(pt, scr.frame()):
                return scr, primary_h
        return (Cocoa.NSScreen.mainScreen() or screens[0]), primary_h

    @staticmethod
    def _fill_screen_visible_frame(
        ax_win,
        scr,
        primary_h: float,
        current_rect: tuple[float, float, float, float] | None = None,
    ) -> None:
        vis = scr.visibleFrame()
        ax_x = float(vis.origin.x)
        ax_y = primary_h - (float(vis.origin.y) + float(vis.size.height))
        ax_w = float(vis.size.width)
        ax_h = float(vis.size.height)
        if current_rect is not None and (
            abs(current_rect[0] - ax_x) <= 4.0
            and abs(current_rect[1] - ax_y) <= 4.0
            and abs(current_rect[2] - ax_w) <= 4.0
            and abs(current_rect[3] - ax_h) <= 4.0
        ):
            return

        _, set_p = ApplicationServices.AXUIElementIsAttributeSettable(
            ax_win, ApplicationServices.kAXPositionAttribute, None
        )
        _, set_s = ApplicationServices.AXUIElementIsAttributeSettable(
            ax_win, ApplicationServices.kAXSizeAttribute, None
        )
        if not (bool(set_p) and bool(set_s)):
            return
        v_pos = ApplicationServices.AXValueCreate(
            ApplicationServices.kAXValueCGPointType, Quartz.CGPoint(ax_x, ax_y)
        )
        v_sz = ApplicationServices.AXValueCreate(
            ApplicationServices.kAXValueCGSizeType, Quartz.CGSize(ax_w, ax_h)
        )
        ApplicationServices.AXUIElementSetAttributeValue(
            ax_win, ApplicationServices.kAXPositionAttribute, v_pos
        )
        ApplicationServices.AXUIElementSetAttributeValue(
            ax_win, ApplicationServices.kAXSizeAttribute, v_sz
        )
