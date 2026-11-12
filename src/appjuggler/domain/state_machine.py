"""Pure, deterministic navigation state machine (NavigationController)."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import threading

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
    KeyKind,
    LaunchAppCommand,
    LaunchCompleted,
    NavCommand,
    OpenAppsForAssignResolved,
    OpenFullAssignmentWindowCommand,
    RequestOpenAppsForAssignCommand,
    ShowAppMapCommand,
    ShowQuickAssignAppListCommand,
    ShowQuickAssignKeyMapCommand,
    ShowStatusBannerCommand,
    ShowWindowListCommand,
    StepResult,
    SystemInterruptEvent,
    UpdateAppMapLayerCommand,
    UpdateQuickAssignAppSelectionCommand,
    UpdateWindowSelectionCommand,
    WindowListRefreshed,
)
from appjuggler.domain.models import AppBinding, AppRecord, Layer, NavState, WindowSlot


@dataclass(frozen=True)
class QueuedSelectionInput:
    """Input event buffered while window enumeration or app launch is in progress."""

    kind: KeyKind
    symbol: str | None = None


class NavigationController:
    """Platform-independent state machine translating events into commands.

    Thread-safe with O(1) non-blocking transitions suitable for direct invocation
    from a real-time input tap callback.
    """

    def __init__(
        self,
        trigger_interval_ms: int = 300,
        max_pending_inputs: int = 8,
        single_window_digit_guard_ms: int = 150,
    ) -> None:
        self._lock = threading.Lock()
        self._trigger_interval_ns: int = trigger_interval_ms * 1_000_000
        self._max_pending_inputs: int = max_pending_inputs
        self._single_window_digit_guard_ns: int = single_window_digit_guard_ms * 1_000_000

        # Snapshots updated from BindingStore / ApplicationRegistry
        self._bindings: dict[tuple[str, Layer], AppBinding] = {}
        self._running_bundle_ids: frozenset[str] = frozenset()

        # Core state
        self._state: NavState = NavState.IDLE
        self._op_token: int = 0
        self._active_layer: Layer = Layer.BASE

        # Caps Lock gesture tracking
        self._idle_caps_candidate_ns: int | None = None
        self._panel_caps_candidate_ns: int | None = None

        # Active selection context
        self._selected_binding: AppBinding | None = None
        self._resolving_windows: bool = False
        self._window_slots: tuple[WindowSlot, ...] = ()
        self._selected_slot: int | None = None
        self._window_generation: int = 0
        self._pending_inputs: deque[QueuedSelectionInput] = deque()

        # Quick Assign context (Hold Caps Lock 2s / Tab in HUD)
        self._resolving_assign_apps: bool = False
        self._assign_apps: tuple[AppRecord, ...] = ()
        self._assign_selected_index: int = 0
        self._assign_target_app: AppRecord | None = None
        self._assign_conflict_slot: tuple[str, Layer] | None = None

        # Timing & keyUp balance guards
        self._letter_keydown_ns: int = 0
        self._single_window_digit_guard_until_ns: int = 0
        self._swallowed_keycodes: set[int] = set()

    # --- Read-only properties for diagnostics & tests ---

    @property
    def state(self) -> NavState:
        with self._lock:
            return self._state

    @property
    def active_layer(self) -> Layer:
        with self._lock:
            return self._active_layer

    @property
    def op_token(self) -> int:
        with self._lock:
            return self._op_token

    @property
    def selected_slot(self) -> int | None:
        with self._lock:
            return self._selected_slot

    @property
    def window_generation(self) -> int:
        with self._lock:
            return self._window_generation

    # --- Configuration & runtime snapshot updates ---

    def set_trigger_interval_ms(self, interval_ms: int) -> None:
        with self._lock:
            self._trigger_interval_ns = max(50, min(1500, interval_ms)) * 1_000_000

    def update_bindings(self, bindings: list[AppBinding] | tuple[AppBinding, ...]) -> None:
        mapping = {b.slot_key: b for b in bindings}
        with self._lock:
            self._bindings = mapping

    def update_running_apps(self, running_bundle_ids: set[str] | frozenset[str]) -> None:
        frozen = frozenset(running_bundle_ids)
        with self._lock:
            self._running_bundle_ids = frozen

    # --- Hardware input event entry point ---

    def handle_key_event(self, event: HardwareKeyEvent) -> StepResult:
        with self._lock:
            return self._handle_key_event_locked(event)

    def _handle_key_event_locked(self, event: HardwareKeyEvent) -> StepResult:
        # 1. Always balance swallowed keyDown -> keyUp pairs, even after returning to IDLE.
        if event.kind not in (
            KeyKind.CAPS_LOCK,
            KeyKind.SHIFT_MODIFIER,
            KeyKind.OTHER_MODIFIER,
        ):
            if not event.is_key_down:
                if event.keycode in self._swallowed_keycodes:
                    self._swallowed_keycodes.discard(event.keycode)
                    return StepResult(InputAction.SWALLOW)
                return StepResult(InputAction.PASS_THROUGH)

            if (
                self._state in (NavState.IDLE, NavState.ASSIGNMENT_EDITING)
                and event.is_autorepeat
                and event.keycode in self._swallowed_keycodes
            ):
                return StepResult(InputAction.SWALLOW)

        # 2. AssignmentEditing never mixes with navigation mode.
        if self._state is NavState.ASSIGNMENT_EDITING:
            self._idle_caps_candidate_ns = None
            return StepResult(InputAction.PASS_THROUGH)

        # 3. IDLE mode: detect double-Caps Lock trigger; pass everything else through
        #    (except an immediate trailing chord digit right after a 1-window auto-jump).
        if self._state is NavState.IDLE:
            return self._handle_idle_event_locked(event)

        # 4. Active modal states (AppSelection, WindowSelection, LaunchPending,
        #    ActivationPending, QuickAssignApp, QuickAssignKey).
        return self._handle_modal_event_locked(event)

    def _handle_idle_event_locked(self, event: HardwareKeyEvent) -> StepResult:
        if event.kind is KeyKind.CAPS_LOCK:
            if not event.caps_lock_toggled:
                return StepResult(InputAction.PASS_THROUGH)

            if (
                self._idle_caps_candidate_ns is not None
                and 0 <= (event.timestamp_ns - self._idle_caps_candidate_ns) <= self._trigger_interval_ns
            ):
                # Second Caps Lock actuation within window -> enter AppSelection!
                self._idle_caps_candidate_ns = None
                self._panel_caps_candidate_ns = None
                self._single_window_digit_guard_until_ns = 0
                self._op_token += 1
                self._state = NavState.APP_SELECTION
                self._active_layer = Layer.SHIFT if event.shift_held else Layer.BASE
                self._reset_selection_context_locked()
                return StepResult(
                    InputAction.PASS_THROUGH,
                    (ShowAppMapCommand(op_token=self._op_token, active_layer=self._active_layer),),
                )

            self._idle_caps_candidate_ns = event.timestamp_ns
            return StepResult(InputAction.PASS_THROUGH)

        # Any intervening key press or modifier press invalidates pending 1st Caps Lock tap.
        if event.kind in (KeyKind.SHIFT_MODIFIER, KeyKind.OTHER_MODIFIER):
            if event.shift_held or event.other_modifiers_held:
                self._idle_caps_candidate_ns = None
            return StepResult(InputAction.PASS_THROUGH)

        if event.is_key_down:
            self._idle_caps_candidate_ns = None

            # Single-window fast-chord guard
            if (
                event.kind is KeyKind.DIGIT
                and not event.other_modifiers_held
                and not event.shift_held
                and event.symbol in ("1", "2", "3", "4", "5", "6", "7", "8", "9")
                and event.timestamp_ns <= self._single_window_digit_guard_until_ns
            ):
                self._single_window_digit_guard_until_ns = 0
                self._swallowed_keycodes.add(event.keycode)
                return StepResult(InputAction.SWALLOW)

        return StepResult(InputAction.PASS_THROUGH)

    def _handle_modal_event_locked(self, event: HardwareKeyEvent) -> StepResult:
        if event.kind is KeyKind.CAPS_LOCK:
            if event.caps_lock_toggled:
                if (
                    self._panel_caps_candidate_ns is not None
                    and 0
                    <= (event.timestamp_ns - self._panel_caps_candidate_ns)
                    <= self._trigger_interval_ns
                ):
                    cmds = self._cancel_to_idle_locked("double_caps_close")
                    return StepResult(InputAction.PASS_THROUGH, cmds)
                self._panel_caps_candidate_ns = event.timestamp_ns
            return StepResult(InputAction.PASS_THROUGH)

        if event.kind is KeyKind.SHIFT_MODIFIER:
            self._panel_caps_candidate_ns = None
            if self._state in (NavState.APP_SELECTION, NavState.QUICK_ASSIGN_KEY):
                new_layer = Layer.SHIFT if event.shift_held else Layer.BASE
                if new_layer is not self._active_layer:
                    self._active_layer = new_layer
                    if (
                        self._state is NavState.QUICK_ASSIGN_KEY
                        and self._assign_target_app is not None
                    ):
                        self._assign_conflict_slot = None
                        return StepResult(
                            InputAction.PASS_THROUGH,
                            (
                                ShowQuickAssignKeyMapCommand(
                                    op_token=self._op_token,
                                    app=self._assign_target_app,
                                    active_layer=self._active_layer,
                                    conflict_existing=None,
                                ),
                            ),
                        )
                    return StepResult(
                        InputAction.PASS_THROUGH,
                        (
                            UpdateAppMapLayerCommand(
                                op_token=self._op_token, active_layer=self._active_layer
                            ),
                        ),
                    )
            return StepResult(InputAction.PASS_THROUGH)

        if event.kind is KeyKind.OTHER_MODIFIER:
            self._panel_caps_candidate_ns = None
            return StepResult(InputAction.PASS_THROUGH)

        # Regular keyDown while modal panel/operation is active -> SWALLOW.
        self._panel_caps_candidate_ns = None
        self._swallowed_keycodes.add(event.keycode)

        if event.kind is KeyKind.ESCAPE:
            if event.is_autorepeat:
                return StepResult(InputAction.SWALLOW)
            cmds = self._cancel_to_idle_locked("escape")
            return StepResult(InputAction.SWALLOW, cmds)

        if self._state is NavState.APP_SELECTION:
            return self._handle_app_selection_keydown_locked(event)

        if self._state is NavState.QUICK_ASSIGN_APP:
            return self._handle_quick_assign_app_keydown_locked(event)

        if self._state is NavState.QUICK_ASSIGN_KEY:
            return self._handle_quick_assign_key_keydown_locked(event)

        if self._state in (NavState.WINDOW_SELECTION, NavState.LAUNCH_PENDING):
            return self._handle_window_or_launch_keydown_locked(event)

        # NavState.ACTIVATION_PENDING: swallow all non-Escape keys until activation completes.
        return StepResult(InputAction.SWALLOW)

    def _handle_app_selection_keydown_locked(self, event: HardwareKeyEvent) -> StepResult:
        if event.is_autorepeat or event.other_modifiers_held:
            return StepResult(InputAction.SWALLOW)

        # Keyboard shortcut inside Navigator HUD: Tab -> Quick Assign Open App
        if event.kind is KeyKind.TAB:
            return StepResult(
                InputAction.SWALLOW,
                self._enter_quick_assign_app_locked(),
            )

        # Keyboard shortcut inside Navigator HUD: ',' -> Open full GUI Edit Assignments window
        if event.kind is KeyKind.COMMA:
            cmds = list(self._cancel_to_idle_locked("open_full_editor"))
            cmds.append(OpenFullAssignmentWindowCommand())
            return StepResult(InputAction.SWALLOW, tuple(cmds))

        if event.kind is not KeyKind.LETTER or not event.symbol:
            return StepResult(InputAction.SWALLOW)

        letter = event.symbol.upper()
        chosen_layer = Layer.SHIFT if event.shift_held else Layer.BASE
        self._active_layer = chosen_layer
        binding = self._bindings.get((letter, chosen_layer))

        if binding is None:
            prefix = "⇧" if chosen_layer is Layer.SHIFT else ""
            return StepResult(
                InputAction.SWALLOW,
                (
                    ShowStatusBannerCommand(
                        level="info",
                        message=f"No app assigned to {prefix}{letter}  ·  Hold Caps Lock 2s (or press Tab) to assign open apps",
                        auto_hide_ms=2200,
                        keep_app_map=True,
                    ),
                ),
            )

        self._op_token += 1
        self._selected_binding = binding
        self._letter_keydown_ns = event.timestamp_ns
        self._pending_inputs.clear()
        self._window_slots = ()
        self._selected_slot = None

        if binding.bundle_id in self._running_bundle_ids:
            self._state = NavState.WINDOW_SELECTION
            self._resolving_windows = True
            return StepResult(
                InputAction.SWALLOW,
                (InspectAppWindowsCommand(op_token=self._op_token, binding=binding),),
            )

        self._state = NavState.LAUNCH_PENDING
        self._resolving_windows = False
        return StepResult(
            InputAction.SWALLOW,
            (LaunchAppCommand(op_token=self._op_token, binding=binding),),
        )

    def _handle_quick_assign_app_keydown_locked(self, event: HardwareKeyEvent) -> StepResult:
        if event.other_modifiers_held:
            return StepResult(InputAction.SWALLOW)

        if event.kind is KeyKind.COMMA and not event.is_autorepeat:
            cmds = list(self._cancel_to_idle_locked("open_full_editor"))
            cmds.append(OpenFullAssignmentWindowCommand())
            return StepResult(InputAction.SWALLOW, tuple(cmds))

        if self._resolving_assign_apps or not self._assign_apps:
            return StepResult(InputAction.SWALLOW)

        if event.kind in (KeyKind.ARROW_DOWN, KeyKind.ARROW_UP):
            delta = 1 if event.kind is KeyKind.ARROW_DOWN else -1
            self._assign_selected_index = (self._assign_selected_index + delta) % len(
                self._assign_apps
            )
            return StepResult(
                InputAction.SWALLOW,
                (
                    UpdateQuickAssignAppSelectionCommand(
                        op_token=self._op_token,
                        selected_index=self._assign_selected_index,
                    ),
                ),
            )

        if event.is_autorepeat:
            return StepResult(InputAction.SWALLOW)

        chosen_app: AppRecord | None = None
        if event.kind is KeyKind.ENTER:
            chosen_app = self._assign_apps[self._assign_selected_index]
        elif event.kind is KeyKind.DIGIT and event.symbol in (
            "1",
            "2",
            "3",
            "4",
            "5",
            "6",
            "7",
            "8",
            "9",
        ):
            idx = int(event.symbol) - 1
            if idx < 0 or idx >= len(self._assign_apps):
                return StepResult(
                    InputAction.SWALLOW,
                    (
                        UpdateQuickAssignAppSelectionCommand(
                            op_token=self._op_token,
                            selected_index=self._assign_selected_index,
                            notice=f"No open app #{idx + 1}",
                        ),
                    ),
                )
            self._assign_selected_index = idx
            chosen_app = self._assign_apps[idx]

        if chosen_app is None:
            return StepResult(InputAction.SWALLOW)

        if chosen_app.has_missing_bundle_id or not chosen_app.bundle_id:
            return StepResult(
                InputAction.SWALLOW,
                (
                    UpdateQuickAssignAppSelectionCommand(
                        op_token=self._op_token,
                        selected_index=self._assign_selected_index,
                        notice=f"'{chosen_app.display_name}' has no macOS bundle_id and cannot be bound",
                    ),
                ),
            )

        self._state = NavState.QUICK_ASSIGN_KEY
        self._assign_target_app = chosen_app
        self._active_layer = Layer.SHIFT if event.shift_held else Layer.BASE
        self._assign_conflict_slot = None
        return StepResult(
            InputAction.SWALLOW,
            (
                ShowQuickAssignKeyMapCommand(
                    op_token=self._op_token,
                    app=chosen_app,
                    active_layer=self._active_layer,
                    conflict_existing=None,
                ),
            ),
        )

    def _handle_quick_assign_key_keydown_locked(self, event: HardwareKeyEvent) -> StepResult:
        if event.is_autorepeat or event.other_modifiers_held or self._assign_target_app is None:
            return StepResult(InputAction.SWALLOW)

        target_app = self._assign_target_app

        # Pressing Enter confirms pending slot replacement conflict
        if event.kind is KeyKind.ENTER and self._assign_conflict_slot is not None:
            conf_key, conf_layer = self._assign_conflict_slot
            tok = self._op_token
            self._state = NavState.IDLE
            self._reset_selection_context_locked()
            return StepResult(
                InputAction.SWALLOW,
                (
                    CommitQuickAssignCommand(
                        op_token=tok,
                        app=target_app,
                        key=conf_key,
                        layer=conf_layer,
                        allow_replace=True,
                    ),
                ),
            )

        if event.kind is not KeyKind.LETTER or not event.symbol:
            return StepResult(InputAction.SWALLOW)

        letter = event.symbol.upper()
        chosen_layer = Layer.SHIFT if event.shift_held else Layer.BASE
        self._active_layer = chosen_layer
        existing = self._bindings.get((letter, chosen_layer))

        if (
            existing is None
            or existing.bundle_id == target_app.bundle_id
            or self._assign_conflict_slot == (letter, chosen_layer)
        ):
            tok = self._op_token
            self._state = NavState.IDLE
            self._reset_selection_context_locked()
            return StepResult(
                InputAction.SWALLOW,
                (
                    CommitQuickAssignCommand(
                        op_token=tok,
                        app=target_app,
                        key=letter,
                        layer=chosen_layer,
                        allow_replace=True,
                    ),
                ),
            )

        # Slot is occupied by a DIFFERENT app -> ask inline confirmation before replacing (§2.2)
        self._assign_conflict_slot = (letter, chosen_layer)
        return StepResult(
            InputAction.SWALLOW,
            (
                ShowQuickAssignKeyMapCommand(
                    op_token=self._op_token,
                    app=target_app,
                    active_layer=chosen_layer,
                    conflict_existing=existing,
                ),
            ),
        )

    def _handle_window_or_launch_keydown_locked(self, event: HardwareKeyEvent) -> StepResult:
        if self._state is NavState.LAUNCH_PENDING or self._resolving_windows:
            if event.other_modifiers_held:
                return StepResult(InputAction.SWALLOW)
            if event.kind in (KeyKind.DIGIT, KeyKind.ENTER) and event.is_autorepeat:
                return StepResult(InputAction.SWALLOW)
            if event.kind in (
                KeyKind.DIGIT,
                KeyKind.ARROW_UP,
                KeyKind.ARROW_DOWN,
                KeyKind.ENTER,
            ):
                if len(self._pending_inputs) < self._max_pending_inputs:
                    self._pending_inputs.append(
                        QueuedSelectionInput(kind=event.kind, symbol=event.symbol)
                    )
            return StepResult(InputAction.SWALLOW)

        if event.other_modifiers_held:
            return StepResult(InputAction.SWALLOW)

        if event.kind in (KeyKind.DIGIT, KeyKind.ENTER) and event.is_autorepeat:
            return StepResult(InputAction.SWALLOW)

        cmds = self._apply_window_selection_input_locked(
            QueuedSelectionInput(kind=event.kind, symbol=event.symbol),
            emit_show_instead_of_update=False,
        )
        return StepResult(InputAction.SWALLOW, tuple(cmds))

    # --- Asynchronous backend event entry point ---

    def handle_backend_event(self, event: BackendEvent) -> tuple[NavCommand, ...]:
        with self._lock:
            return self._handle_backend_event_locked(event)

    def _handle_backend_event_locked(self, event: BackendEvent) -> tuple[NavCommand, ...]:
        if isinstance(event, EnterAssignmentEditing):
            cmds: list[NavCommand] = []
            if self._state is not NavState.IDLE:
                cmds.extend(self._cancel_to_idle_locked("enter_assignment_editing"))
            self._state = NavState.ASSIGNMENT_EDITING
            self._idle_caps_candidate_ns = None
            return tuple(cmds)

        if isinstance(event, ExitAssignmentEditing):
            if self._state is NavState.ASSIGNMENT_EDITING:
                self._state = NavState.IDLE
            self._idle_caps_candidate_ns = None
            return ()

        if isinstance(event, CapsHoldTriggered):
            if self._state is NavState.ASSIGNMENT_EDITING:
                return ()
            return self._enter_quick_assign_app_locked()

        if isinstance(event, OpenAppsForAssignResolved):
            if (
                event.op_token != self._op_token
                or self._state is not NavState.QUICK_ASSIGN_APP
            ):
                return ()
            self._resolving_assign_apps = False
            if not event.apps:
                self._state = NavState.IDLE
                self._reset_selection_context_locked()
                return (
                    ShowStatusBannerCommand(
                        level="info",
                        message="No open applications found to assign",
                        auto_hide_ms=2200,
                    ),
                )
            self._assign_apps = event.apps
            self._assign_selected_index = max(
                0, min(len(event.apps) - 1, event.default_index)
            )
            return (
                ShowQuickAssignAppListCommand(
                    op_token=self._op_token,
                    apps=self._assign_apps,
                    selected_index=self._assign_selected_index,
                ),
            )

        if isinstance(event, SystemInterruptEvent):
            self._swallowed_keycodes.clear()
            self._idle_caps_candidate_ns = None
            self._panel_caps_candidate_ns = None
            self._single_window_digit_guard_until_ns = 0
            if self._state not in (NavState.IDLE, NavState.ASSIGNMENT_EDITING):
                return self._cancel_to_idle_locked(event.reason)
            return ()

        if isinstance(event, AppWindowsResolved):
            if event.op_token != self._op_token or self._selected_binding is None:
                return ()
            if not event.app_is_running:
                self._state = NavState.LAUNCH_PENDING
                self._resolving_windows = False
                return (LaunchAppCommand(op_token=self._op_token, binding=self._selected_binding),)
            return self._complete_window_resolution_locked(
                slots=event.slots,
                default_slot=event.default_slot,
                generation=event.generation,
            )

        if isinstance(event, LaunchCompleted):
            if event.op_token != self._op_token or self._selected_binding is None:
                return ()
            if event.missing_bundle or event.error:
                msg = event.error or f"Could not launch {self._selected_binding.display_name}"
                self._state = NavState.IDLE
                self._reset_selection_context_locked()
                return (
                    ShowStatusBannerCommand(level="error", message=msg, auto_hide_ms=3600),
                )
            return self._complete_window_resolution_locked(
                slots=event.slots,
                default_slot=event.default_slot,
                generation=event.generation,
            )

        if isinstance(event, WindowListRefreshed):
            if (
                self._state is not NavState.WINDOW_SELECTION
                or self._resolving_windows
                or self._selected_binding is None
                or self._selected_binding.bundle_id != event.bundle_id
            ):
                return ()
            return self._apply_window_list_refresh_locked(event)

        if isinstance(event, ActivationCompleted):
            if event.op_token != self._op_token:
                return ()
            if event.stale_window and self._selected_binding is not None:
                if event.refreshed_slots:
                    self._state = NavState.WINDOW_SELECTION
                    self._resolving_windows = False
                    self._window_slots = event.refreshed_slots
                    self._window_generation = event.refreshed_generation
                    self._selected_slot = None
                    return (
                        ShowWindowListCommand(
                            op_token=self._op_token,
                            binding=self._selected_binding,
                            slots=self._window_slots,
                            selected_slot=None,
                            generation=self._window_generation,
                            notice="Selected window closed — choose another window (↑/↓ or 1–9)",
                        ),
                    )
                self._state = NavState.IDLE
                self._reset_selection_context_locked()
                return (
                    ShowStatusBannerCommand(
                        level="warning",
                        message="Target window closed before activation",
                        auto_hide_ms=2500,
                    ),
                )

            self._state = NavState.IDLE
            self._reset_selection_context_locked()
            if event.success:
                return (HideOverlayCommand(reason="activated"),)
            return (
                ShowStatusBannerCommand(
                    level="error",
                    message=event.error or "Failed to activate target window",
                    auto_hide_ms=3200,
                ),
            )

        return ()

    # --- Internal helpers ---

    def _enter_quick_assign_app_locked(self) -> tuple[NavCommand, ...]:
        self._idle_caps_candidate_ns = None
        self._panel_caps_candidate_ns = None
        self._single_window_digit_guard_until_ns = 0
        self._op_token += 1
        self._reset_selection_context_locked()
        self._state = NavState.QUICK_ASSIGN_APP
        self._resolving_assign_apps = True
        return (RequestOpenAppsForAssignCommand(op_token=self._op_token),)

    def _complete_window_resolution_locked(
        self,
        slots: tuple[WindowSlot, ...],
        default_slot: int | None,
        generation: int,
    ) -> tuple[NavCommand, ...]:
        assert self._selected_binding is not None
        binding = self._selected_binding
        self._resolving_windows = False

        if len(slots) == 0:
            self._pending_inputs.clear()
            self._state = NavState.ACTIVATION_PENDING
            self._single_window_digit_guard_until_ns = (
                self._letter_keydown_ns + self._single_window_digit_guard_ns
            )
            return (ActivateAppNoWindowsCommand(op_token=self._op_token, binding=binding),)

        if len(slots) == 1:
            self._pending_inputs.clear()
            self._state = NavState.ACTIVATION_PENDING
            self._window_slots = slots
            self._window_generation = generation
            self._selected_slot = slots[0].slot_number
            self._single_window_digit_guard_until_ns = (
                self._letter_keydown_ns + self._single_window_digit_guard_ns
            )
            return (
                ActivateWindowCommand(
                    op_token=self._op_token,
                    binding=binding,
                    slot=slots[0],
                    generation=generation,
                ),
            )

        self._state = NavState.WINDOW_SELECTION
        self._window_slots = slots
        self._window_generation = generation
        slot_numbers = {s.slot_number for s in slots}
        if default_slot in slot_numbers:
            self._selected_slot = default_slot
        else:
            self._selected_slot = slots[0].slot_number

        notice: str | None = None
        while self._pending_inputs:
            queued = self._pending_inputs.popleft()
            cmds = self._apply_window_selection_input_locked(
                queued, emit_show_instead_of_update=True
            )
            if self._state is NavState.ACTIVATION_PENDING:
                self._pending_inputs.clear()
                return tuple(cmds)
            for cmd in cmds:
                if isinstance(cmd, (ShowWindowListCommand, UpdateWindowSelectionCommand)) and cmd.notice:
                    notice = cmd.notice

        return (
            ShowWindowListCommand(
                op_token=self._op_token,
                binding=binding,
                slots=self._window_slots,
                selected_slot=self._selected_slot,
                generation=self._window_generation,
                notice=notice,
            ),
        )

    def _apply_window_selection_input_locked(
        self,
        item: QueuedSelectionInput,
        emit_show_instead_of_update: bool,
    ) -> list[NavCommand]:
        assert self._selected_binding is not None
        if not self._window_slots:
            return []

        if item.kind in (KeyKind.ARROW_DOWN, KeyKind.ARROW_UP):
            ordered_nums = [s.slot_number for s in self._window_slots]
            if self._selected_slot not in ordered_nums:
                self._selected_slot = (
                    ordered_nums[0] if item.kind is KeyKind.ARROW_DOWN else ordered_nums[-1]
                )
            else:
                idx = ordered_nums.index(self._selected_slot)
                delta = 1 if item.kind is KeyKind.ARROW_DOWN else -1
                self._selected_slot = ordered_nums[(idx + delta) % len(ordered_nums)]

            if emit_show_instead_of_update:
                return []
            return [
                UpdateWindowSelectionCommand(
                    op_token=self._op_token,
                    selected_slot=self._selected_slot,
                    generation=self._window_generation,
                )
            ]

        if item.kind is KeyKind.ENTER:
            target = self._find_slot_locked(self._selected_slot)
            if target is None:
                msg = "Window list changed — select a window (↑/↓ or 1–9) first"
                if emit_show_instead_of_update:
                    return [
                        ShowWindowListCommand(
                            op_token=self._op_token,
                            binding=self._selected_binding,
                            slots=self._window_slots,
                            selected_slot=None,
                            generation=self._window_generation,
                            notice=msg,
                        )
                    ]
                return [
                    UpdateWindowSelectionCommand(
                        op_token=self._op_token,
                        selected_slot=None,
                        generation=self._window_generation,
                        notice=msg,
                    )
                ]

            self._state = NavState.ACTIVATION_PENDING
            return [
                ActivateWindowCommand(
                    op_token=self._op_token,
                    binding=self._selected_binding,
                    slot=target,
                    generation=self._window_generation,
                )
            ]

        if item.kind is KeyKind.DIGIT and item.symbol in (
            "1",
            "2",
            "3",
            "4",
            "5",
            "6",
            "7",
            "8",
            "9",
        ):
            num = int(item.symbol)
            target = self._find_slot_locked(num)
            if target is None:
                msg = f"No window #{num}"
                if emit_show_instead_of_update:
                    return [
                        ShowWindowListCommand(
                            op_token=self._op_token,
                            binding=self._selected_binding,
                            slots=self._window_slots,
                            selected_slot=self._selected_slot,
                            generation=self._window_generation,
                            notice=msg,
                        )
                    ]
                return [
                    UpdateWindowSelectionCommand(
                        op_token=self._op_token,
                        selected_slot=self._selected_slot,
                        generation=self._window_generation,
                        notice=msg,
                    )
                ]

            self._selected_slot = num
            self._state = NavState.ACTIVATION_PENDING
            return [
                ActivateWindowCommand(
                    op_token=self._op_token,
                    binding=self._selected_binding,
                    slot=target,
                    generation=self._window_generation,
                )
            ]

        return []

    def _apply_window_list_refresh_locked(
        self, event: WindowListRefreshed
    ) -> tuple[NavCommand, ...]:
        assert self._selected_binding is not None
        if len(event.slots) == 0:
            self._state = NavState.IDLE
            self._reset_selection_context_locked()
            return (
                ShowStatusBannerCommand(
                    level="info",
                    message="All windows for selected application closed",
                    auto_hide_ms=2000,
                ),
            )

        prev_target = self._find_slot_locked(self._selected_slot)
        prev_window_id = prev_target.window_id if prev_target is not None else None
        prev_slot_num = self._selected_slot

        self._window_slots = event.slots
        self._window_generation = event.generation

        notice: str | None = None
        if prev_window_id is not None:
            matching = next((s for s in event.slots if s.window_id == prev_window_id), None)
            if matching is not None:
                self._selected_slot = matching.slot_number
            else:
                self._selected_slot = None
                notice = (
                    f"Window #{prev_slot_num} closed — choose another window (↑/↓ or 1–9)"
                )

        return (
            ShowWindowListCommand(
                op_token=self._op_token,
                binding=self._selected_binding,
                slots=self._window_slots,
                selected_slot=self._selected_slot,
                generation=self._window_generation,
                notice=notice,
            ),
        )

    def _find_slot_locked(self, slot_number: int | None) -> WindowSlot | None:
        if slot_number is None:
            return None
        for slot in self._window_slots:
            if slot.slot_number == slot_number:
                return slot
        return None

    def _cancel_to_idle_locked(self, reason: str) -> tuple[NavCommand, ...]:
        self._op_token += 1
        self._state = NavState.IDLE
        self._idle_caps_candidate_ns = None
        self._panel_caps_candidate_ns = None
        self._reset_selection_context_locked()
        return (HideOverlayCommand(reason=reason),)

    def _reset_selection_context_locked(self) -> None:
        self._selected_binding = None
        self._resolving_windows = False
        self._window_slots = ()
        self._selected_slot = None
        self._window_generation = 0
        self._pending_inputs.clear()
        self._resolving_assign_apps = False
        self._assign_apps = ()
        self._assign_selected_index = 0
        self._assign_target_app = None
        self._assign_conflict_slot = None
