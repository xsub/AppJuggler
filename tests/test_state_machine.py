"""Unit tests for NavigationController state machine, timing, queuing, and safety guards."""

from __future__ import annotations

from appjuggler.domain.events import (
    ActivateAppNoWindowsCommand,
    ActivateWindowCommand,
    ActivationCompleted,
    AppWindowsResolved,
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
    MouseClickAppSlot,
    MouseClickWindowSlot,
    MouseHoverWindowSlot,
    MouseScrollWindowSelection,
    MouseToggleLayer,
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
    UpdateWindowSelectionCommand,
    WindowListRefreshed,
)
from appjuggler.domain.models import AppBinding, AppRecord, DiscoveredWindow, Layer, NavState, WindowSlot
from appjuggler.domain.state_machine import NavigationController

MS = 1_000_000  # 1 millisecond in nanoseconds


def _caps_event(ts_ms: int, caps_on: bool, toggled: bool = True, shift: bool = False) -> HardwareKeyEvent:
    return HardwareKeyEvent(
        kind=KeyKind.CAPS_LOCK,
        keycode=57,
        timestamp_ns=ts_ms * MS,
        caps_lock_on=caps_on,
        caps_lock_toggled=toggled,
        shift_held=shift,
    )


def _letter_event(
    letter: str,
    ts_ms: int,
    *,
    down: bool = True,
    keycode: int = 17,
    shift: bool = False,
    caps: bool = False,
    repeat: bool = False,
) -> HardwareKeyEvent:
    return HardwareKeyEvent(
        kind=KeyKind.LETTER,
        keycode=keycode,
        timestamp_ns=ts_ms * MS,
        is_key_down=down,
        is_autorepeat=repeat,
        symbol=letter.upper(),
        shift_held=shift,
        caps_lock_on=caps,
    )


def _digit_event(
    digit: str,
    ts_ms: int,
    *,
    down: bool = True,
    keycode: int = 20,
) -> HardwareKeyEvent:
    return HardwareKeyEvent(
        kind=KeyKind.DIGIT,
        keycode=keycode,
        timestamp_ns=ts_ms * MS,
        is_key_down=down,
        symbol=digit,
    )


def _slot(num: int, wid: str, title: str = "Win") -> WindowSlot:
    return WindowSlot(
        slot_number=num,
        window=DiscoveredWindow(
            window_id=wid,
            bundle_id="com.apple.Terminal",
            pid=100,
            title=f"{title} {num}",
        ),
    )


def test_single_caps_passes_through_and_double_caps_triggers_from_off_and_on() -> None:
    ctrl = NavigationController(trigger_interval_ms=300)

    # Single Caps Lock press and slow second press (301 ms > 300 ms): never triggers
    r1 = ctrl.handle_key_event(_caps_event(100, caps_on=True))
    assert r1.action is InputAction.PASS_THROUGH
    assert r1.commands == ()
    assert ctrl.state is NavState.IDLE

    r2 = ctrl.handle_key_event(_caps_event(401, caps_on=False))
    assert r2.action is InputAction.PASS_THROUGH
    assert r2.commands == ()
    assert ctrl.state is NavState.IDLE

    # Exact boundary 300 ms when Caps was initially OFF (401 -> OFF, 1000 -> ON, 1300 -> OFF)
    ctrl.handle_key_event(_caps_event(1000, caps_on=True))
    r_trig = ctrl.handle_key_event(_caps_event(1300, caps_on=False))
    assert r_trig.action is InputAction.PASS_THROUGH
    assert ctrl.state is NavState.APP_SELECTION
    assert isinstance(r_trig.commands[0], ShowAppMapCommand)

    # Escape closes back to IDLE
    esc = ctrl.handle_key_event(
        HardwareKeyEvent(kind=KeyKind.ESCAPE, keycode=53, timestamp_ns=1350 * MS)
    )
    assert esc.action is InputAction.SWALLOW
    assert ctrl.state is NavState.IDLE
    ctrl.handle_key_event(
        HardwareKeyEvent(kind=KeyKind.ESCAPE, keycode=53, timestamp_ns=1360 * MS, is_key_down=False)
    )

    # Double Caps when Caps Lock was INITIALLY ON (ON -> OFF at 2000ms -> ON at 2180ms)
    ctrl.handle_key_event(_caps_event(2000, caps_on=False))
    r_on = ctrl.handle_key_event(_caps_event(2180, caps_on=True))
    assert r_on.action is InputAction.PASS_THROUGH
    assert ctrl.state is NavState.APP_SELECTION


def test_non_toggling_caps_flags_changed_does_not_false_trigger() -> None:
    ctrl = NavigationController(trigger_interval_ms=300)
    # Press 1 toggles Caps ON
    ctrl.handle_key_event(_caps_event(100, caps_on=True, toggled=True))
    # External keyboard sends redundant flagsChanged on key release with toggled=False
    res = ctrl.handle_key_event(_caps_event(160, caps_on=True, toggled=False))
    assert res.commands == ()
    assert ctrl.state is NavState.IDLE


def test_intervening_key_invalidates_caps_candidate() -> None:
    """Typing 'Caps -> A -> Caps' fast to produce capital 'A' must NOT trigger navigator."""
    ctrl = NavigationController(trigger_interval_ms=300)
    ctrl.handle_key_event(_caps_event(100, caps_on=True))
    r_a = ctrl.handle_key_event(_letter_event("A", 180, keycode=0, caps=True))
    assert r_a.action is InputAction.PASS_THROUGH
    r_caps2 = ctrl.handle_key_event(_caps_event(250, caps_on=False))
    assert r_caps2.commands == ()
    assert ctrl.state is NavState.IDLE


def test_double_caps_while_panel_open_closes_panel() -> None:
    ctrl = NavigationController(trigger_interval_ms=300)
    ctrl.handle_key_event(_caps_event(100, caps_on=True))
    ctrl.handle_key_event(_caps_event(220, caps_on=False))
    assert ctrl.state is NavState.APP_SELECTION

    # Single Caps inside panel does NOT close panel
    r1 = ctrl.handle_key_event(_caps_event(400, caps_on=True))
    assert r1.action is InputAction.PASS_THROUGH
    assert ctrl.state is NavState.APP_SELECTION

    # Second Caps within 300ms closes panel cleanly
    r2 = ctrl.handle_key_event(_caps_event(550, caps_on=False))
    assert r2.action is InputAction.PASS_THROUGH
    assert ctrl.state is NavState.IDLE
    assert isinstance(r2.commands[0], HideOverlayCommand)


def test_fast_caps_t_3_queues_digit_before_window_enumeration_finishes() -> None:
    ctrl = NavigationController(trigger_interval_ms=300)
    term = AppBinding(key="T", layer=Layer.BASE, bundle_id="com.apple.Terminal", display_name="Terminal")
    ctrl.update_bindings((term,))
    ctrl.update_running_apps(frozenset({"com.apple.Terminal"}))

    # Caps x2 -> T -> 3 in 70ms total!
    ctrl.handle_key_event(_caps_event(100, caps_on=True))
    ctrl.handle_key_event(_caps_event(200, caps_on=False))
    r_t = ctrl.handle_key_event(_letter_event("T", 230, keycode=17))
    assert r_t.action is InputAction.SWALLOW
    assert isinstance(r_t.commands[0], InspectAppWindowsCommand)
    token = r_t.commands[0].op_token

    # User presses '3' BEFORE AppWindowsResolved arrives!
    r_3 = ctrl.handle_key_event(_digit_event("3", 260, keycode=20))
    assert r_3.action is InputAction.SWALLOW
    assert r_3.commands == ()

    # Backend finishes enumerating 3 windows -> replays '3' directly into ActivateWindowCommand!
    slots = (_slot(1, "w1"), _slot(2, "w2"), _slot(3, "w3"))
    cmds = ctrl.handle_backend_event(
        AppWindowsResolved(
            op_token=token,
            bundle_id="com.apple.Terminal",
            slots=slots,
            default_slot=1,
            generation=1,
            app_is_running=True,
        )
    )
    assert ctrl.state is NavState.ACTIVATION_PENDING
    assert len(cmds) == 1
    assert isinstance(cmds[0], ActivateWindowCommand)
    assert cmds[0].slot.slot_number == 3

    # Activation finishes -> returns to IDLE
    done_cmds = ctrl.handle_backend_event(ActivationCompleted(op_token=token, success=True))
    assert ctrl.state is NavState.IDLE
    assert isinstance(done_cmds[0], HideOverlayCommand)

    # Subsequent keyUps for 'T' (keycode 17) and '3' (keycode 20) in IDLE MUST be swallowed!
    up_t = ctrl.handle_key_event(_letter_event("T", 320, down=False, keycode=17))
    up_3 = ctrl.handle_key_event(_digit_event("3", 330, down=False, keycode=20))
    assert up_t.action is InputAction.SWALLOW
    assert up_3.action is InputAction.SWALLOW

    # Once released, the next normal 'T' press/release in IDLE passes through normally!
    normal_down = ctrl.handle_key_event(_letter_event("T", 500, down=True, keycode=17))
    normal_up = ctrl.handle_key_event(_letter_event("T", 550, down=False, keycode=17))
    assert normal_down.action is InputAction.PASS_THROUGH
    assert normal_up.action is InputAction.PASS_THROUGH


def test_single_window_trailing_digit_never_leaks_to_target_app() -> None:
    ctrl = NavigationController(trigger_interval_ms=300, single_window_digit_guard_ms=150)
    term = AppBinding(key="T", layer=Layer.BASE, bundle_id="com.apple.Terminal", display_name="Terminal")
    ctrl.update_bindings((term,))
    ctrl.update_running_apps(frozenset({"com.apple.Terminal"}))

    ctrl.handle_key_event(_caps_event(100, caps_on=True))
    ctrl.handle_key_event(_caps_event(200, caps_on=False))
    r_t = ctrl.handle_key_event(_letter_event("T", 300, keycode=17))
    tok = r_t.commands[0].op_token

    # Single window resolves ultra-fast at 305ms and activates by 320ms
    ctrl.handle_backend_event(
        AppWindowsResolved(
            op_token=tok,
            bundle_id="com.apple.Terminal",
            slots=(_slot(1, "only"),),
            default_slot=1,
            generation=1,
            app_is_running=True,
        )
    )
    ctrl.handle_backend_event(ActivationCompleted(op_token=tok, success=True))
    assert ctrl.state is NavState.IDLE

    # Trailing chord digit '1' pressed at 380ms (80ms after 'T') MUST be swallowed!
    r_digit = ctrl.handle_key_event(_digit_event("1", 380, keycode=18))
    assert r_digit.action is InputAction.SWALLOW
    r_digit_up = ctrl.handle_key_event(_digit_event("1", 420, down=False, keycode=18))
    assert r_digit_up.action is InputAction.SWALLOW

    # Normal digit typed later at 600ms passes through cleanly!
    r_later = ctrl.handle_key_event(_digit_event("1", 600, keycode=18))
    assert r_later.action is InputAction.PASS_THROUGH


def test_shift_layer_selection_ignores_caps_lock_and_shift_release_after_keydown() -> None:
    ctrl = NavigationController(trigger_interval_ms=300)
    base_t = AppBinding(key="T", layer=Layer.BASE, bundle_id="com.apple.Terminal", display_name="Terminal")
    shift_t = AppBinding(key="T", layer=Layer.SHIFT, bundle_id="com.apple.TextEdit", display_name="TextEdit")
    ctrl.update_bindings((base_t, shift_t))
    ctrl.update_running_apps(frozenset({"com.apple.Terminal", "com.apple.TextEdit"}))

    # Open navigator with Caps Lock initially ON
    ctrl.handle_key_event(_caps_event(100, caps_on=False))
    ctrl.handle_key_event(_caps_event(200, caps_on=True))

    # Hold Shift -> updates overlay layer to SHIFT; release -> returns to BASE; hold again -> SHIFT
    s_down = ctrl.handle_key_event(
        HardwareKeyEvent(kind=KeyKind.SHIFT_MODIFIER, keycode=56, timestamp_ns=240 * MS, shift_held=True)
    )
    assert isinstance(s_down.commands[0], UpdateAppMapLayerCommand)
    assert s_down.commands[0].active_layer is Layer.SHIFT

    s_up = ctrl.handle_key_event(
        HardwareKeyEvent(kind=KeyKind.SHIFT_MODIFIER, keycode=56, timestamp_ns=260 * MS, shift_held=False)
    )
    assert s_up.commands[0].active_layer is Layer.BASE

    # Press 'T' with Caps=ON and Shift=False -> MUST pick BASE layer (Terminal), ignoring Caps=ON!
    r_base = ctrl.handle_key_event(_letter_event("T", 280, shift=False, caps=True))
    assert isinstance(r_base.commands[0], InspectAppWindowsCommand)
    assert r_base.commands[0].binding.bundle_id == "com.apple.Terminal"

    # Cancel and test Shift+T followed by immediate Shift release before windows resolve
    ctrl.handle_key_event(HardwareKeyEvent(kind=KeyKind.ESCAPE, keycode=53, timestamp_ns=300 * MS))
    ctrl.handle_key_event(_caps_event(400, caps_on=False))
    ctrl.handle_key_event(_caps_event(500, caps_on=True))

    r_shift = ctrl.handle_key_event(_letter_event("T", 530, shift=True, caps=False))
    assert isinstance(r_shift.commands[0], InspectAppWindowsCommand)
    assert r_shift.commands[0].binding.bundle_id == "com.apple.TextEdit"
    tok = r_shift.commands[0].op_token

    # Releasing Shift at 540ms MUST NOT alter the already-selected TextEdit binding!
    ctrl.handle_key_event(
        HardwareKeyEvent(kind=KeyKind.SHIFT_MODIFIER, keycode=56, timestamp_ns=540 * MS, shift_held=False)
    )
    cmds = ctrl.handle_backend_event(
        AppWindowsResolved(
            op_token=tok,
            bundle_id="com.apple.TextEdit",
            slots=(),
            default_slot=None,
            generation=1,
            app_is_running=True,
        )
    )
    assert isinstance(cmds[0], ActivateAppNoWindowsCommand)
    assert cmds[0].binding.bundle_id == "com.apple.TextEdit"


def test_window_list_arrows_enter_escape_and_stale_generation_guard() -> None:
    """Verifies Criteria 4 & 9: arrows only highlight, Enter activates, closed selected window prevents blind Enter."""
    ctrl = NavigationController(trigger_interval_ms=300)
    term = AppBinding(key="T", layer=Layer.BASE, bundle_id="com.apple.Terminal", display_name="Terminal")
    ctrl.update_bindings((term,))
    ctrl.update_running_apps(frozenset({"com.apple.Terminal"}))

    ctrl.handle_key_event(_caps_event(100, caps_on=True))
    ctrl.handle_key_event(_caps_event(200, caps_on=False))
    r_t = ctrl.handle_key_event(_letter_event("T", 240))
    tok = r_t.commands[0].op_token

    slots = (_slot(1, "w1"), _slot(2, "w2"), _slot(3, "w3"))
    show_cmds = ctrl.handle_backend_event(
        AppWindowsResolved(
            op_token=tok,
            bundle_id="com.apple.Terminal",
            slots=slots,
            default_slot=1,
            generation=1,
            app_is_running=True,
        )
    )
    assert isinstance(show_cmds[0], ShowWindowListCommand)
    assert ctrl.selected_slot == 1

    # Arrow Down highlights slot 2 WITHOUT activating
    down_res = ctrl.handle_key_event(
        HardwareKeyEvent(kind=KeyKind.ARROW_DOWN, keycode=125, timestamp_ns=300 * MS)
    )
    assert down_res.action is InputAction.SWALLOW
    assert isinstance(down_res.commands[0], UpdateWindowSelectionCommand)
    assert ctrl.selected_slot == 2
    assert ctrl.state is NavState.WINDOW_SELECTION

    # Criterion 9: Window #2 closes while list is displayed!
    refreshed = (_slot(1, "w1"), _slot(3, "w3"))
    ref_cmds = ctrl.handle_backend_event(
        WindowListRefreshed(
            bundle_id="com.apple.Terminal",
            slots=refreshed,
            generation=2,
        )
    )
    assert isinstance(ref_cmds[0], ShowWindowListCommand)
    assert ctrl.selected_slot is None  # Cleared so stale Enter cannot activate #3!

    # Pressing Enter right after #2 closed does NOT activate #1 or #3!
    enter_stale = ctrl.handle_key_event(
        HardwareKeyEvent(kind=KeyKind.ENTER, keycode=36, timestamp_ns=350 * MS)
    )
    assert ctrl.state is NavState.WINDOW_SELECTION
    assert isinstance(enter_stale.commands[0], UpdateWindowSelectionCommand)

    # User explicitly presses Arrow Down -> selects #1 -> Enter activates #1!
    ctrl.handle_key_event(
        HardwareKeyEvent(kind=KeyKind.ARROW_DOWN, keycode=125, timestamp_ns=380 * MS)
    )
    assert ctrl.selected_slot == 1
    enter_ok = ctrl.handle_key_event(
        HardwareKeyEvent(kind=KeyKind.ENTER, keycode=36, timestamp_ns=400 * MS)
    )
    assert ctrl.state is NavState.ACTIVATION_PENDING
    assert isinstance(enter_ok.commands[0], ActivateWindowCommand)
    assert enter_ok.commands[0].slot.slot_number == 1


def test_closed_app_launch_pending_cancellation_and_missing_bundle() -> None:
    ctrl = NavigationController(trigger_interval_ms=300)
    app = AppBinding(key="S", layer=Layer.BASE, bundle_id="com.spotify.client", display_name="Spotify")
    ctrl.update_bindings((app,))
    ctrl.update_running_apps(frozenset())

    ctrl.handle_key_event(_caps_event(100, caps_on=True))
    ctrl.handle_key_event(_caps_event(200, caps_on=False))
    r_s = ctrl.handle_key_event(_letter_event("S", 230, keycode=1))
    assert ctrl.state is NavState.LAUNCH_PENDING
    assert isinstance(r_s.commands[0], LaunchAppCommand)
    old_tok = r_s.commands[0].op_token

    # User cancels with Escape -> late LaunchCompleted for old_tok is ignored!
    ctrl.handle_key_event(HardwareKeyEvent(kind=KeyKind.ESCAPE, keycode=53, timestamp_ns=300 * MS))
    assert ctrl.state is NavState.IDLE
    ignored = ctrl.handle_backend_event(
        LaunchCompleted(op_token=old_tok, bundle_id="com.spotify.client", slots=(_slot(1, "s1"),))
    )
    assert ignored == ()

    # Missing bundle reports error banner and returns to IDLE cleanly
    ctrl.handle_key_event(_caps_event(500, caps_on=True))
    ctrl.handle_key_event(_caps_event(600, caps_on=False))
    r_s2 = ctrl.handle_key_event(_letter_event("S", 630, keycode=1))
    tok2 = r_s2.commands[0].op_token
    err_cmds = ctrl.handle_backend_event(
        LaunchCompleted(
            op_token=tok2,
            bundle_id="com.spotify.client",
            missing_bundle=True,
            error="Bundle not found",
        )
    )
    assert ctrl.state is NavState.IDLE
    assert isinstance(err_cmds[0], ShowStatusBannerCommand)
    assert err_cmds[0].level == "error"


def test_assignment_editing_and_system_interrupt_isolation() -> None:
    ctrl = NavigationController(trigger_interval_ms=300, enable_caps_hold_assign=True)
    ctrl.handle_backend_event(EnterAssignmentEditing())
    assert ctrl.state is NavState.ASSIGNMENT_EDITING

    # Normal typing and 2s Caps-hold are ignored/passed through while Edit Assignments is focused
    assert ctrl.handle_key_event(_digit_event("3", 50)).action is InputAction.PASS_THROUGH
    assert ctrl.handle_backend_event(CapsHoldTriggered()) == ()
    assert ctrl.state is NavState.ASSIGNMENT_EDITING

    # Double-Caps ALWAYS summons the HUD navigator even if Edit Assignments was left open,
    # and asynchronous ExitAssignmentEditing from closing the editor does not clobber APP_SELECTION!
    ctrl.handle_key_event(_caps_event(100, caps_on=True))
    res = ctrl.handle_key_event(_caps_event(200, caps_on=False))
    assert ctrl.state is NavState.APP_SELECTION
    assert isinstance(res.commands[0], ShowAppMapCommand)

    assert ctrl.handle_backend_event(ExitAssignmentEditing()) == ()
    assert ctrl.state is NavState.APP_SELECTION

    # System interrupt (e.g. outside click or screen lock) cancels active modal panel immediately
    int_cmds = ctrl.handle_backend_event(SystemInterruptEvent(reason="outside_click"))
    assert ctrl.state is NavState.IDLE
    assert isinstance(int_cmds[0], HideOverlayCommand)


def test_caps_2s_hold_disabled_by_default_and_works_when_opted_in() -> None:
    # Default normal path: 2s Caps-hold Quick Assign is OFF
    default_ctrl = NavigationController(trigger_interval_ms=300)
    assert default_ctrl.caps_hold_assign_enabled is False
    assert default_ctrl.handle_backend_event(CapsHoldTriggered()) == ()
    assert default_ctrl.state is NavState.IDLE

    # Opt-in path: enable_caps_hold_assign=True
    ctrl = NavigationController(trigger_interval_ms=300, enable_caps_hold_assign=True)
    term = AppBinding(key="T", layer=Layer.BASE, bundle_id="com.apple.Terminal", display_name="Terminal")
    ctrl.update_bindings((term,))

    # 1. Hold Caps Lock for 2 seconds -> enters QUICK_ASSIGN_APP and requests open apps
    req_cmds = ctrl.handle_backend_event(CapsHoldTriggered())
    assert ctrl.state is NavState.QUICK_ASSIGN_APP
    assert isinstance(req_cmds[0], RequestOpenAppsForAssignCommand)
    tok = req_cmds[0].op_token

    firefox = AppRecord(bundle_id="org.mozilla.firefox", display_name="Firefox", is_running=True)
    safari = AppRecord(bundle_id="com.apple.Safari", display_name="Safari", is_running=True)
    show_cmds = ctrl.handle_backend_event(
        OpenAppsForAssignResolved(op_token=tok, apps=(firefox, safari), default_index=0)
    )
    assert isinstance(show_cmds[0], ShowQuickAssignAppListCommand)

    # 2. Arrow Down selects Safari (index 1), Enter advances to QUICK_ASSIGN_KEY
    ctrl.handle_key_event(HardwareKeyEvent(kind=KeyKind.ARROW_DOWN, keycode=125, timestamp_ns=100 * MS))
    enter_res = ctrl.handle_key_event(
        HardwareKeyEvent(kind=KeyKind.ENTER, keycode=36, timestamp_ns=120 * MS)
    )
    assert ctrl.state is NavState.QUICK_ASSIGN_KEY
    assert isinstance(enter_res.commands[0], ShowQuickAssignKeyMapCommand)
    assert enter_res.commands[0].app.bundle_id == "com.apple.Safari"

    # 3. Pressing occupied key 'T' once shows conflict confirmation instead of silently overwriting!
    r_conflict = ctrl.handle_key_event(_letter_event("T", 160, keycode=17, caps=True))
    assert ctrl.state is NavState.QUICK_ASSIGN_KEY
    assert isinstance(r_conflict.commands[0], ShowQuickAssignKeyMapCommand)
    assert r_conflict.commands[0].conflict_existing == term

    # 4. Choosing free Shift+S instead binds immediately without replacing 'T'!
    r_free = ctrl.handle_key_event(_letter_event("S", 200, keycode=1, shift=True, caps=True))
    assert ctrl.state is NavState.IDLE
    assert isinstance(r_free.commands[0], CommitQuickAssignCommand)
    assert r_free.commands[0].app.bundle_id == "com.apple.Safari"
    assert r_free.commands[0].key == "S"
    assert r_free.commands[0].layer is Layer.SHIFT

    # 5. Also verify confirming an occupied slot by pressing the same key twice
    req2 = ctrl.handle_backend_event(CapsHoldTriggered())
    ctrl.handle_backend_event(
        OpenAppsForAssignResolved(op_token=req2[0].op_token, apps=(firefox,), default_index=0)
    )
    # Direct digit '1' picks open app #1 (Firefox)
    ctrl.handle_key_event(_digit_event("1", 300, keycode=18))
    assert ctrl.state is NavState.QUICK_ASSIGN_KEY
    ctrl.handle_key_event(_letter_event("T", 320, keycode=17))  # 1st press warns
    assert ctrl.state is NavState.QUICK_ASSIGN_KEY
    r_confirm = ctrl.handle_key_event(_letter_event("T", 360, keycode=17))  # 2nd press confirms!
    assert ctrl.state is NavState.IDLE
    assert isinstance(r_confirm.commands[0], CommitQuickAssignCommand)
    assert r_confirm.commands[0].key == "T"
    assert r_confirm.commands[0].allow_replace is True


def test_in_hud_comma_opens_editor_by_default_and_tab_requires_opt_in() -> None:
    # Default path: ',' opens Edit Assignments; Tab does NOT enter Quick Assign
    ctrl = NavigationController(trigger_interval_ms=300)
    ctrl.handle_key_event(_caps_event(100, caps_on=True))
    ctrl.handle_key_event(_caps_event(200, caps_on=False))
    assert ctrl.state is NavState.APP_SELECTION

    r_tab_disabled = ctrl.handle_key_event(
        HardwareKeyEvent(kind=KeyKind.TAB, keycode=48, timestamp_ns=220 * MS)
    )
    assert ctrl.state is NavState.APP_SELECTION
    assert r_tab_disabled.commands == ()

    r_comma = ctrl.handle_key_event(
        HardwareKeyEvent(kind=KeyKind.COMMA, keycode=43, timestamp_ns=240 * MS)
    )
    assert ctrl.state is NavState.IDLE
    assert any(isinstance(c, OpenFullAssignmentWindowCommand) for c in r_comma.commands)

    # Opt-in path: Tab enters Quick Assign Open App
    ctrl.set_caps_hold_assign_enabled(True)
    ctrl.handle_key_event(_caps_event(400, caps_on=True))
    ctrl.handle_key_event(_caps_event(500, caps_on=False))
    r_tab = ctrl.handle_key_event(
        HardwareKeyEvent(kind=KeyKind.TAB, keycode=48, timestamp_ns=540 * MS)
    )
    assert ctrl.state is NavState.QUICK_ASSIGN_APP
    assert isinstance(r_tab.commands[0], RequestOpenAppsForAssignCommand)


def test_mouse_selection_disabled_by_default_and_hover_click_works_when_opted_in() -> None:
    term = AppBinding(
        key="F", layer=Layer.BASE, bundle_id="com.apple.finder", display_name="Finder"
    )
    shift_s = AppBinding(
        key="S", layer=Layer.SHIFT, bundle_id="com.apple.Safari", display_name="Safari"
    )

    # 1. Default normal path: enable_mouse_selection is False -> all mouse events are no-ops
    ctrl = NavigationController(trigger_interval_ms=300)
    assert ctrl.mouse_selection_enabled is False
    ctrl.update_bindings((term, shift_s))
    ctrl.update_running_apps(frozenset({"com.apple.finder"}))

    ctrl.handle_key_event(_caps_event(100, caps_on=True))
    ctrl.handle_key_event(_caps_event(200, caps_on=False))
    assert ctrl.state is NavState.APP_SELECTION

    assert ctrl.handle_backend_event(MouseToggleLayer()) == ()
    assert ctrl.active_layer is Layer.BASE
    assert ctrl.handle_backend_event(MouseClickAppSlot(key="F", layer=Layer.BASE)) == ()
    assert ctrl.state is NavState.APP_SELECTION

    # Advance via keyboard to multi-window picker and verify mouse hover/click still ignored when False
    r_f = ctrl.handle_key_event(_letter_event("F", 240, keycode=3))
    tok = r_f.commands[0].op_token
    slots = (_slot(1, "w1", "Downloads"), _slot(2, "w2", "Documents"), _slot(3, "w3", "Desktop"))
    ctrl.handle_backend_event(
        AppWindowsResolved(
            op_token=tok,
            bundle_id="com.apple.finder",
            slots=slots,
            default_slot=1,
            generation=1,
            app_is_running=True,
        )
    )
    assert ctrl.state is NavState.WINDOW_SELECTION
    assert ctrl.selected_slot == 1
    assert ctrl.handle_backend_event(MouseHoverWindowSlot(slot_number=3)) == ()
    assert ctrl.selected_slot == 1
    assert ctrl.handle_backend_event(MouseClickWindowSlot(slot_number=2)) == ()
    assert ctrl.state is NavState.WINDOW_SELECTION

    # 2. Opt-in path: enable_mouse_selection=True -> hover highlights, click activates!
    ctrl.set_mouse_selection_enabled(True)
    assert ctrl.mouse_selection_enabled is True

    # Hovering window #3 highlights slot 3 without activating
    hover_cmds = ctrl.handle_backend_event(MouseHoverWindowSlot(slot_number=3))
    assert ctrl.state is NavState.WINDOW_SELECTION
    assert ctrl.selected_slot == 3
    assert isinstance(hover_cmds[0], UpdateWindowSelectionCommand)
    assert hover_cmds[0].selected_slot == 3

    # Scrolling wheel up (-1) moves highlight from #3 -> #2
    scroll_cmds = ctrl.handle_backend_event(MouseScrollWindowSelection(delta=-1))
    assert ctrl.selected_slot == 2
    assert isinstance(scroll_cmds[0], UpdateWindowSelectionCommand)

    # Clicking window #3 immediately transitions to ACTIVATION_PENDING and activates #3
    click_cmds = ctrl.handle_backend_event(MouseClickWindowSlot(slot_number=3))
    assert ctrl.state is NavState.ACTIVATION_PENDING
    assert isinstance(click_cmds[0], ActivateWindowCommand)
    assert click_cmds[0].slot.slot_number == 3
    ctrl.handle_backend_event(ActivationCompleted(op_token=tok, success=True))
    assert ctrl.state is NavState.IDLE

    # Open HUD again -> toggle Shift layer via mouse pill -> click Shift+S (closed Safari) -> launches!
    ctrl.handle_key_event(_caps_event(600, caps_on=True))
    ctrl.handle_key_event(_caps_event(700, caps_on=False))
    assert ctrl.state is NavState.APP_SELECTION

    layer_cmds = ctrl.handle_backend_event(MouseToggleLayer())
    assert ctrl.active_layer is Layer.SHIFT
    assert isinstance(layer_cmds[0], UpdateAppMapLayerCommand)

    launch_cmds = ctrl.handle_backend_event(MouseClickAppSlot(key="S", layer=Layer.SHIFT))
    assert ctrl.state is NavState.LAUNCH_PENDING
    assert isinstance(launch_cmds[0], LaunchAppCommand)
    assert launch_cmds[0].binding.bundle_id == "com.apple.Safari"
    launch_tok = launch_cmds[0].op_token

    # Cold launch finishing with 1 window marks ActivateWindowCommand(from_launch=True)
    launched_cmds = ctrl.handle_backend_event(
        LaunchCompleted(
            op_token=launch_tok,
            bundle_id="com.apple.Safari",
            slots=(_slot(1, "s1", "Start Page"),),
            default_slot=1,
            generation=1,
        )
    )
    assert isinstance(launched_cmds[0], ActivateWindowCommand)
    assert launched_cmds[0].from_launch is True


def test_backend_wait_watchdog_releases_modal_capture() -> None:
    ctrl = NavigationController(trigger_interval_ms=300)
    ctrl.update_bindings([AppBinding("F", Layer.BASE, "com.apple.finder", "Finder")])
    ctrl.update_running_apps({"com.apple.finder"})

    assert ctrl.is_waiting_on_backend is False
    ctrl.handle_key_event(_caps_event(100, caps_on=True))
    ctrl.handle_key_event(_caps_event(180, caps_on=False))
    assert ctrl.state is NavState.APP_SELECTION
    assert ctrl.is_waiting_on_backend is False

    # Selecting a running app enters WINDOW_SELECTION with _resolving_windows=True
    ctrl.handle_key_event(_letter_event("F", 220))
    assert ctrl.state is NavState.WINDOW_SELECTION
    assert ctrl.is_waiting_on_backend is True

    # Watchdog interrupt cleanly restores IDLE and passes normal typing through again
    cmds = ctrl.handle_backend_event(
        SystemInterruptEvent(reason="backend_watchdog_timeout")
    )
    assert ctrl.state is NavState.IDLE
    assert ctrl.is_waiting_on_backend is False
    assert any(isinstance(c, HideOverlayCommand) for c in cmds)
    assert ctrl.handle_key_event(_letter_event("A", 300)).action is InputAction.PASS_THROUGH


def test_over_nine_windows_letter_selection_a_through_z() -> None:
    ctrl = NavigationController(trigger_interval_ms=300)
    term = AppBinding("T", Layer.BASE, "com.apple.Terminal", "Terminal")
    ctrl.update_bindings((term,))
    ctrl.update_running_apps(frozenset({"com.apple.Terminal"}))

    # 1. Open picker with 12 Terminal tabs (slots 1..9, A=10, B=11, C=12)
    ctrl.handle_key_event(_caps_event(100, caps_on=True))
    ctrl.handle_key_event(_caps_event(180, caps_on=False))
    r_t = ctrl.handle_key_event(_letter_event("T", 210, keycode=17))
    tok = r_t.commands[0].op_token

    twelve_slots = tuple(_slot(i, f"w{i}", "Tab") for i in range(1, 13))
    ctrl.handle_backend_event(
        AppWindowsResolved(
            op_token=tok,
            bundle_id="com.apple.Terminal",
            slots=twelve_slots,
            default_slot=1,
            generation=1,
            app_is_running=True,
        )
    )
    assert ctrl.state is NavState.WINDOW_SELECTION

    # Pressing out-of-range letter 'D' (slot 13) warns "No window D" without closing
    r_d = ctrl.handle_key_event(_letter_event("D", 250, keycode=2))
    assert ctrl.state is NavState.WINDOW_SELECTION
    assert isinstance(r_d.commands[0], UpdateWindowSelectionCommand)
    assert r_d.commands[0].notice == "No window D"

    # Pressing 'B' (slot 11) immediately activates window #11!
    r_b = ctrl.handle_key_event(_letter_event("B", 280, keycode=11))
    assert ctrl.state is NavState.ACTIVATION_PENDING
    assert isinstance(r_b.commands[0], ActivateWindowCommand)
    assert r_b.commands[0].slot.slot_number == 11
    assert r_b.commands[0].slot.badge_label == "B"
    ctrl.handle_backend_event(ActivationCompleted(op_token=tok, success=True))
    assert ctrl.state is NavState.IDLE

    # 2. Fast chord Caps×2 -> T -> A BEFORE AppWindowsResolved arrives queues slot 10!
    ctrl.handle_key_event(_caps_event(500, caps_on=True))
    ctrl.handle_key_event(_caps_event(580, caps_on=False))
    r_t2 = ctrl.handle_key_event(_letter_event("T", 610, keycode=17))
    tok2 = r_t2.commands[0].op_token
    r_a = ctrl.handle_key_event(_letter_event("A", 630, keycode=0))
    assert r_a.action is InputAction.SWALLOW
    assert r_a.commands == ()

    cmds2 = ctrl.handle_backend_event(
        AppWindowsResolved(
            op_token=tok2,
            bundle_id="com.apple.Terminal",
            slots=twelve_slots,
            default_slot=1,
            generation=1,
            app_is_running=True,
        )
    )
    assert ctrl.state is NavState.ACTIVATION_PENDING
    assert isinstance(cmds2[0], ActivateWindowCommand)
    assert cmds2[0].slot.slot_number == 10
    assert cmds2[0].slot.badge_label == "A"


def test_tilde_direct_jump_modifier_and_per_app_show_window_picker() -> None:
    ctrl = NavigationController(trigger_interval_ms=300)
    term_picker = AppBinding(
        "T", Layer.BASE, "com.apple.Terminal", "Terminal", show_window_picker=True
    )
    firefox_direct = AppBinding(
        "F", Layer.BASE, "org.mozilla.firefox", "Firefox", show_window_picker=False
    )
    ctrl.update_bindings((term_picker, firefox_direct))
    ctrl.update_running_apps(frozenset({"com.apple.Terminal", "org.mozilla.firefox"}))
    four_slots = tuple(_slot(i, f"w{i}", "Tab") for i in range(1, 5))

    # 1. Caps×2 -> ~ (with autorepeat while held) -> T skips window list and jumps directly to default_slot!
    ctrl.handle_key_event(_caps_event(100, caps_on=True))
    ctrl.handle_key_event(_caps_event(180, caps_on=False))
    assert ctrl.direct_jump_override is False

    r_tilde = ctrl.handle_key_event(
        HardwareKeyEvent(kind=KeyKind.TILDE, keycode=50, timestamp_ns=210 * MS)
    )
    assert r_tilde.action is InputAction.SWALLOW
    assert ctrl.direct_jump_override is True
    assert isinstance(r_tilde.commands[0], UpdateAppMapLayerCommand)
    assert r_tilde.commands[0].direct_jump is True

    # Holding '~' generating autorepeat MUST NOT untoggle Direct Jump!
    ctrl.handle_key_event(
        HardwareKeyEvent(
            kind=KeyKind.TILDE, keycode=50, timestamp_ns=240 * MS, is_autorepeat=True
        )
    )
    assert ctrl.direct_jump_override is True

    r_t = ctrl.handle_key_event(_letter_event("T", 260, keycode=17))
    assert isinstance(r_t.commands[0], InspectAppWindowsCommand)
    assert r_t.commands[0].direct_jump is True
    tok = r_t.commands[0].op_token

    cmds = ctrl.handle_backend_event(
        AppWindowsResolved(
            op_token=tok,
            bundle_id="com.apple.Terminal",
            slots=four_slots,
            default_slot=3,
            generation=1,
            app_is_running=True,
        )
    )
    assert ctrl.state is NavState.ACTIVATION_PENDING
    assert isinstance(cmds[0], ActivateWindowCommand)
    assert cmds[0].slot.slot_number == 3
    ctrl.handle_backend_event(ActivationCompleted(op_token=tok, success=True))
    assert ctrl.state is NavState.IDLE

    # 2. Per-app show_window_picker=False (Firefox) jumps directly WITHOUT needing '~'!
    ctrl.handle_key_event(_caps_event(500, caps_on=True))
    ctrl.handle_key_event(_caps_event(580, caps_on=False))
    assert ctrl.direct_jump_override is False

    r_f = ctrl.handle_key_event(_letter_event("F", 610, keycode=3))
    assert isinstance(r_f.commands[0], InspectAppWindowsCommand)
    assert r_f.commands[0].direct_jump is True
    tok_f = r_f.commands[0].op_token

    cmds_f = ctrl.handle_backend_event(
        AppWindowsResolved(
            op_token=tok_f,
            bundle_id="org.mozilla.firefox",
            slots=four_slots,
            default_slot=2,
            generation=1,
            app_is_running=True,
        )
    )
    assert ctrl.state is NavState.ACTIVATION_PENDING
    assert isinstance(cmds_f[0], ActivateWindowCommand)
    assert cmds_f[0].slot.slot_number == 2


def test_dismiss_window_slot_via_mouse_x_and_delete_key() -> None:
    from appjuggler.domain.events import (
        DismissWindowSlotCommand,
        MouseDismissWindowSlot,
        WindowSlotDismissed,
    )

    # Deliberately keep enable_mouse_selection=False to prove clicking '×' ALWAYS works!
    ctrl = NavigationController(trigger_interval_ms=300, enable_mouse_selection=False)
    term = AppBinding("T", Layer.BASE, "com.apple.Terminal", "Terminal")
    ctrl.update_bindings((term,))
    ctrl.update_running_apps(frozenset({"com.apple.Terminal"}))

    ctrl.handle_key_event(_caps_event(100, caps_on=True))
    ctrl.handle_key_event(_caps_event(180, caps_on=False))
    r_t = ctrl.handle_key_event(_letter_event("T", 210, keycode=17))
    tok = r_t.commands[0].op_token

    s1, s2, s3 = _slot(1, "w1"), _slot(2, "w2"), _slot(3, "w3")
    ctrl.handle_backend_event(
        AppWindowsResolved(
            op_token=tok,
            bundle_id="com.apple.Terminal",
            slots=(s1, s2, s3),
            default_slot=2,
            generation=1,
            app_is_running=True,
        )
    )
    assert ctrl.state is NavState.WINDOW_SELECTION
    assert ctrl.selected_slot == 2

    # 1. Click '×' on slot 2 -> emits DismissWindowSlotCommand even with mouse selection disabled
    d_cmds = ctrl.handle_backend_event(MouseDismissWindowSlot(slot_number=2))
    assert len(d_cmds) == 1
    assert isinstance(d_cmds[0], DismissWindowSlotCommand)
    assert d_cmds[0].window_id == "w2"
    assert d_cmds[0].slot_number == 2

    # Backend confirms slot 2 hidden -> list stays open with (s1, s3) and selects s3!
    upd = ctrl.handle_backend_event(
        WindowSlotDismissed(
            op_token=tok,
            bundle_id="com.apple.Terminal",
            dismissed_slot=2,
            slots=(s1, s3),
            default_slot=1,
            generation=2,
        )
    )
    assert ctrl.state is NavState.WINDOW_SELECTION
    assert ctrl.selected_slot == 3
    assert isinstance(upd[0], ShowWindowListCommand)
    assert [s.slot_number for s in upd[0].slots] == [1, 3]

    # 2. Pressing Delete/Backspace dismisses highlighted slot 3
    del_step = ctrl.handle_key_event(
        HardwareKeyEvent(kind=KeyKind.DELETE, keycode=51, timestamp_ns=300 * MS)
    )
    assert del_step.action is InputAction.SWALLOW
    assert isinstance(del_step.commands[0], DismissWindowSlotCommand)
    assert del_step.commands[0].window_id == "w3"

    # Dismissing all remaining entries closes HUD cleanly back to IDLE with info banner
    empty_upd = ctrl.handle_backend_event(
        WindowSlotDismissed(
            op_token=tok,
            bundle_id="com.apple.Terminal",
            dismissed_slot=1,
            slots=(),
            default_slot=None,
            generation=3,
        )
    )
    assert ctrl.state is NavState.IDLE
    assert isinstance(empty_upd[0], ShowStatusBannerCommand)
