"""Unit tests for MacOSWindowRegistry dismiss/re-invocation and HUD window-list '×' hit-testing."""

from __future__ import annotations

import sys
import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS AX & AppKit tests")


class _FakeAXObj:
    def __init__(self, label: str) -> None:
        self.label = label


class _FakeRunningApp:
    def __init__(self, pid: int, bundle_id: str, name: str = "Terminal") -> None:
        self._pid = pid
        self._bundle_id = bundle_id
        self._name = name

    def isTerminated(self) -> bool:
        return False

    def processIdentifier(self) -> int:
        return self._pid

    def bundleIdentifier(self) -> str:
        return self._bundle_id

    def localizedName(self) -> str:
        return self._name


class _FakeWorkspace:
    def __init__(self, apps: list[_FakeRunningApp]) -> None:
        self._apps = apps

    def runningApplications(self) -> list[_FakeRunningApp]:
        return self._apps


def test_registry_dismiss_hides_tab_until_reinvoked_on_that_tab(monkeypatch) -> None:
    import ApplicationServices
    from appjuggler.platforms.macos.window_registry import AX_SUCCESS, MacOSWindowRegistry

    app_el = _FakeAXObj("app:5000")
    parent_el = _FakeAXObj("parent:app")
    tab1 = _FakeAXObj("tab1")
    tab2 = _FakeAXObj("tab2")
    tab3 = _FakeAXObj("tab3")

    active_state = {"current": tab1}
    titles = {tab1: "Tab 1", tab2: "Tab 2", tab3: "Tab 3"}

    monkeypatch.setattr(
        MacOSWindowRegistry,
        "_shared_workspace",
        staticmethod(lambda: _FakeWorkspace([_FakeRunningApp(5000, "com.apple.Terminal")])),
    )
    monkeypatch.setattr(
        ApplicationServices, "AXUIElementCreateApplication", lambda _pid: app_el
    )
    monkeypatch.setattr(
        ApplicationServices, "AXUIElementSetMessagingTimeout", lambda *_: 0
    )

    def _fake_copy_attr(el, attr, _out):
        if el is app_el:
            if attr in (
                ApplicationServices.kAXMainWindowAttribute,
                ApplicationServices.kAXFocusedWindowAttribute,
            ):
                return (AX_SUCCESS, active_state["current"])
            if attr == ApplicationServices.kAXWindowsAttribute:
                return (AX_SUCCESS, [active_state["current"]])
            return (AX_SUCCESS, None)
        if el in titles:
            if attr == ApplicationServices.kAXRoleAttribute:
                return (AX_SUCCESS, "AXWindow")
            if attr == ApplicationServices.kAXParentAttribute:
                return (AX_SUCCESS, parent_el)
            if attr == ApplicationServices.kAXSubroleAttribute:
                return (AX_SUCCESS, "AXStandardWindow")
            if attr == ApplicationServices.kAXTitleAttribute:
                return (AX_SUCCESS, titles[el])
            if attr == ApplicationServices.kAXMainAttribute:
                return (AX_SUCCESS, el is active_state["current"])
            return (AX_SUCCESS, False)
        return (AX_SUCCESS, None)

    monkeypatch.setattr(
        ApplicationServices, "AXUIElementCopyAttributeValue", _fake_copy_attr
    )

    reg = MacOSWindowRegistry()

    # Visit all 3 Terminal tabs so their AXWindow handles are tracked in session cache
    active_state["current"] = tab1
    reg.discover_windows("com.apple.Terminal")
    active_state["current"] = tab2
    reg.discover_windows("com.apple.Terminal")
    active_state["current"] = tab3
    discovered = reg.discover_windows("com.apple.Terminal")
    assert {w.title for w in discovered} == {"Tab 1", "Tab 2", "Tab 3"}

    by_title = {w.title: w for w in discovered}

    # 1. Dismiss inactive Tab 2 and currently-active Tab 3 via '×'
    reg.dismiss_window("com.apple.Terminal", by_title["Tab 2"].window_id)
    reg.dismiss_window("com.apple.Terminal", by_title["Tab 3"].window_id)

    after_dismiss = reg.discover_windows("com.apple.Terminal")
    assert [w.title for w in after_dismiss] == ["Tab 1"]

    # Direct Jump (include_dismissed=True) still sees all 3 tabs
    all_for_direct = reg.discover_windows("com.apple.Terminal", include_dismissed=True)
    assert {w.title for w in all_for_direct} == {"Tab 1", "Tab 2", "Tab 3"}

    # 2. User invokes Terminal while still on Tab 3 -> Tab 3 is restored, Tab 2 stays hidden!
    reg.note_application_activated("com.apple.Terminal")
    after_reinvoke_tab3 = reg.discover_windows("com.apple.Terminal")
    assert {w.title for w in after_reinvoke_tab3} == {"Tab 1", "Tab 3"}

    # 3. User switches Terminal to Tab 2 -> Tab 2 automatically reappears on next discovery!
    active_state["current"] = tab2
    after_switch_tab2 = reg.discover_windows("com.apple.Terminal")
    assert {w.title for w in after_switch_tab2} == {"Tab 1", "Tab 2", "Tab 3"}


def test_hud_window_list_draws_letter_slots_and_clicks_x_dismiss_button() -> None:
    import Cocoa
    from appjuggler.domain.models import AppBinding, DiscoveredWindow, Layer, WindowSlot
    from appjuggler.platforms.macos.ui.overlay_panel import _HUDContentView

    Cocoa.NSApplication.sharedApplication()
    view = _HUDContentView.alloc().initWithFrame_(Cocoa.NSMakeRect(0.0, 0.0, 720.0, 680.0))
    view.mode = "windows"
    view.mouse_enabled = False  # Even with HUD mouse selection OFF, '×' click must work!
    view.selected_binding = AppBinding("T", Layer.BASE, "com.apple.Terminal", "Terminal")
    view.slots = tuple(
        WindowSlot(
            slot_number=i,
            window=DiscoveredWindow(
                window_id=f"w{i}",
                bundle_id="com.apple.Terminal",
                pid=100,
                title=f"Tab {i}",
            ),
        )
        for i in range(1, 13)
    )
    view.selected_slot = 10  # Slot 'A'

    dismissed: list[int] = []
    view.on_window_slot_dismissed = lambda slot_num: dismissed.append(slot_num)

    # Offscreen bitmap render populates _window_row_rects and _window_dismiss_rects for 1..12
    rep = view.bitmapImageRepForCachingDisplayInRect_(view.bounds())
    view.cacheDisplayInRect_toBitmapImageRep_(view.bounds(), rep)

    assert 10 in view._window_dismiss_rects
    assert view.slots[9].badge_label == "A"
    assert view.slots[11].badge_label == "C"

    # Simulate clicking the right-side '×' button on row 'A' (slot 10)
    d_rect = view._window_dismiss_rects[10]
    win = Cocoa.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        Cocoa.NSMakeRect(0.0, 0.0, 720.0, 680.0),
        Cocoa.NSWindowStyleMaskBorderless,
        Cocoa.NSBackingStoreBuffered,
        False,
    )
    win.setContentView_(view)
    win_pt = view.convertPoint_toView_(
        Cocoa.NSMakePoint(d_rect.origin.x + 10.0, d_rect.origin.y + 10.0), None
    )
    click_ev = Cocoa.NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
        Cocoa.NSEventTypeLeftMouseDown,
        win_pt,
        0,
        0.0,
        win.windowNumber(),
        None,
        0,
        1,
        1.0,
    )
    view.mouseDown_(click_ev)
    assert dismissed == [10]
