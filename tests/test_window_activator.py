"""Unit tests for MacOSWindowActivator Full Screen transition safety (no NSBeep double-triggers)."""

from __future__ import annotations

import sys
import time
import pytest

from appjuggler.domain.models import DiscoveredWindow

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS AX activator tests")


class _StubRegistry:
    def __init__(self, alive: bool = True) -> None:
        self.alive = alive

    def discover_windows(self, bundle_id: str, timeout_sec: float = 0.8):
        return ()

    def is_window_alive(self, window: DiscoveredWindow) -> bool:
        return self.alive


class _StubRunningApp:
    def isActive(self) -> bool:
        return True

    def activateWithOptions_(self, _opts: int) -> None:
        pass


def test_already_fullscreen_window_exits_immediately_without_writes(monkeypatch) -> None:
    import ApplicationServices
    from appjuggler.platforms.macos import window_activator as wa
    from appjuggler.platforms.macos.window_registry import AX_SUCCESS

    writes: list[tuple[object, str, object]] = []
    actions: list[tuple[object, str]] = []

    monkeypatch.setattr(ApplicationServices, "AXUIElementSetMessagingTimeout", lambda *_: 0)
    monkeypatch.setattr(
        ApplicationServices,
        "AXUIElementCopyAttributeValue",
        lambda _el, attr, _out: (AX_SUCCESS, True) if attr == "AXFullScreen" else (AX_SUCCESS, None),
    )
    monkeypatch.setattr(
        ApplicationServices,
        "AXUIElementSetAttributeValue",
        lambda el, attr, val: writes.append((el, attr, val)) or AX_SUCCESS,
    )
    monkeypatch.setattr(
        ApplicationServices,
        "AXUIElementPerformAction",
        lambda el, act: actions.append((el, act)) or AX_SUCCESS,
    )

    activator = wa.MacOSWindowActivator(_StubRegistry())
    win = DiscoveredWindow(
        window_id="50714:1",
        bundle_id="org.whispersystems.signal-desktop",
        pid=50714,
        title="Signal",
        is_fullscreen=True,
        native_handle=object(),
    )

    assert activator.ensure_window_fullscreen(win) is True
    assert writes == []
    assert actions == []


def test_known_fullscreen_window_with_busy_ax_never_pokes_fullscreen(monkeypatch) -> None:
    import ApplicationServices
    from appjuggler.platforms.macos import window_activator as wa
    from appjuggler.platforms.macos.window_registry import AX_ERROR_CANNOT_COMPLETE, AX_SUCCESS

    writes: list[tuple[object, str, object]] = []
    actions: list[tuple[object, str]] = []

    monkeypatch.setattr(ApplicationServices, "AXUIElementSetMessagingTimeout", lambda *_: 0)
    monkeypatch.setattr(
        ApplicationServices,
        "AXUIElementCopyAttributeValue",
        lambda _el, _attr, _out: (AX_ERROR_CANNOT_COMPLETE, None),
    )
    monkeypatch.setattr(
        ApplicationServices,
        "AXUIElementSetAttributeValue",
        lambda el, attr, val: writes.append((el, attr, val)) or AX_SUCCESS,
    )
    monkeypatch.setattr(
        ApplicationServices,
        "AXUIElementPerformAction",
        lambda el, act: actions.append((el, act)) or AX_SUCCESS,
    )

    activator = wa.MacOSWindowActivator(_StubRegistry())
    win = DiscoveredWindow(
        window_id="50714:1",
        bundle_id="org.whispersystems.signal-desktop",
        pid=50714,
        title="Signal",
        is_fullscreen=True,
        native_handle=object(),
    )

    assert activator.is_window_currently_fullscreen(win, default_if_busy=True) is True
    assert activator.ensure_window_fullscreen(win) is True
    assert writes == []
    assert actions == []


def test_electron_fullscreen_animation_triggers_once_without_pressing_button(monkeypatch) -> None:
    import ApplicationServices
    from appjuggler.platforms.macos import window_activator as wa
    from appjuggler.platforms.macos.window_registry import AX_SUCCESS

    win_handle = object()
    btn_handle = object()
    fs_set_at: list[float] = []
    btn_presses: list[object] = []

    def fake_copy_attr(el, attr, _out):
        if el is win_handle:
            if attr == "AXFullScreen":
                # Simulate ~260ms Electron animation before AXFullScreen flips True
                is_fs = bool(fs_set_at) and (time.monotonic() - fs_set_at[0]) >= 0.24
                return (AX_SUCCESS, is_fs)
            if attr == ApplicationServices.kAXRoleAttribute:
                return (AX_SUCCESS, "AXWindow")
            if attr == ApplicationServices.kAXSubroleAttribute:
                return (AX_SUCCESS, "AXStandardWindow")
            if attr == ApplicationServices.kAXFullScreenButtonAttribute:
                return (AX_SUCCESS, btn_handle)
        if el is btn_handle:
            if attr == ApplicationServices.kAXSubroleAttribute:
                return (AX_SUCCESS, "AXFullScreenButton")
            if attr == ApplicationServices.kAXEnabledAttribute:
                return (AX_SUCCESS, True)
        return (-25212, None)

    def fake_set_attr(el, attr, val):
        if el is win_handle and attr == "AXFullScreen" and val is True:
            fs_set_at.append(time.monotonic())
        return AX_SUCCESS

    def fake_action(el, act):
        if el is btn_handle and act == ApplicationServices.kAXPressAction:
            btn_presses.append(act)
        return AX_SUCCESS

    monkeypatch.setattr(ApplicationServices, "AXUIElementCreateApplication", lambda _pid: object())
    monkeypatch.setattr(ApplicationServices, "AXUIElementSetMessagingTimeout", lambda *_: 0)
    monkeypatch.setattr(ApplicationServices, "AXUIElementCopyAttributeValue", fake_copy_attr)
    monkeypatch.setattr(
        ApplicationServices,
        "AXUIElementIsAttributeSettable",
        lambda _el, attr, _out: (AX_SUCCESS, attr == "AXFullScreen"),
    )
    monkeypatch.setattr(ApplicationServices, "AXUIElementSetAttributeValue", fake_set_attr)
    monkeypatch.setattr(ApplicationServices, "AXUIElementPerformAction", fake_action)

    activator = wa.MacOSWindowActivator(_StubRegistry())
    monkeypatch.setattr(activator, "_find_running_app", lambda *_: _StubRunningApp())
    monkeypatch.setattr(activator, "_read_ax_rect", lambda *_: (0.0, 38.0, 1512.0, 890.0))
    monkeypatch.setattr(activator, "_screen_for_ax_rect", lambda *_: (None, 0.0))

    win = DiscoveredWindow(
        window_id="50714:2",
        bundle_id="org.whispersystems.signal-desktop",
        pid=50714,
        title="Signal",
        is_fullscreen=False,
        native_handle=win_handle,
    )

    assert activator.ensure_window_fullscreen(win, timeout_sec=1.6) is True
    assert len(fs_set_at) == 1
    assert btn_presses == []


def test_refused_window_cached_to_prevent_repeated_beeps_and_pruned_on_exit(monkeypatch) -> None:
    import ApplicationServices
    from appjuggler.platforms.macos import window_activator as wa
    from appjuggler.platforms.macos.window_registry import AX_SUCCESS

    win_handle = object()
    fs_attempts: list[float] = []

    def fake_copy_attr(el, attr, _out):
        if el is win_handle:
            if attr == "AXFullScreen":
                return (AX_SUCCESS, False)
            if attr == ApplicationServices.kAXRoleAttribute:
                return (AX_SUCCESS, "AXWindow")
            if attr == ApplicationServices.kAXSubroleAttribute:
                return (AX_SUCCESS, "AXStandardWindow")
        return (-25212, None)

    def fake_set_attr(el, attr, val):
        if el is win_handle and attr == "AXFullScreen" and val is True:
            fs_attempts.append(time.monotonic())
        return AX_SUCCESS

    monkeypatch.setattr(ApplicationServices, "AXUIElementCreateApplication", lambda _pid: object())
    monkeypatch.setattr(ApplicationServices, "AXUIElementSetMessagingTimeout", lambda *_: 0)
    monkeypatch.setattr(ApplicationServices, "AXUIElementCopyAttributeValue", fake_copy_attr)
    monkeypatch.setattr(
        ApplicationServices,
        "AXUIElementIsAttributeSettable",
        lambda _el, attr, _out: (AX_SUCCESS, attr == "AXFullScreen"),
    )
    monkeypatch.setattr(ApplicationServices, "AXUIElementSetAttributeValue", fake_set_attr)
    monkeypatch.setattr(ApplicationServices, "AXUIElementPerformAction", lambda *_: AX_SUCCESS)

    activator = wa.MacOSWindowActivator(_StubRegistry())
    monkeypatch.setattr(activator, "_find_running_app", lambda *_: _StubRunningApp())
    monkeypatch.setattr(activator, "_read_ax_rect", lambda *_: (0.0, 38.0, 1512.0, 890.0))
    monkeypatch.setattr(activator, "_screen_for_ax_rect", lambda *_: (None, 0.0))
    monkeypatch.setattr(activator, "_wait_for_fullscreen_transition", lambda *_, **__: False)

    win = DiscoveredWindow(
        window_id="50714:9",
        bundle_id="org.whispersystems.signal-desktop",
        pid=50714,
        title="Signal",
        is_fullscreen=False,
        native_handle=win_handle,
    )

    # First switch tries once and records refusal
    assert activator.ensure_window_fullscreen(win, timeout_sec=0.6) is False
    assert len(fs_attempts) == 1

    # Subsequent switches skip toggleFullScreen completely (zero NSBeep!)
    assert activator.ensure_window_fullscreen(win, timeout_sec=0.6) is True
    assert len(fs_attempts) == 1

    # Process exit prunes the cache cleanly
    activator.prune_terminated_pid(50714)
    assert activator.ensure_window_fullscreen(win, timeout_sec=0.6) is False
    assert len(fs_attempts) == 2
