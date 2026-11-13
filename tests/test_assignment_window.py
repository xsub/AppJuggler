"""Unit tests for the macOS 'Edit Assignments' window layout, focus guard, and feedback lifecycle."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest

Cocoa = pytest.importorskip("Cocoa")

from appjuggler.contracts import ApplicationRegistry
from appjuggler.domain.models import AppRecord
from appjuggler.persistence.binding_store import BindingStore
from appjuggler.platforms.macos.ui.assignment_window import (
    AssignmentEditorController,
    _FullWidthCaptionView,
    _HighQualityImageView,
    _RoundedCardView,
)


class _StubAppRegistry(ApplicationRegistry):
    def running_apps(self) -> list[AppRecord]:
        return [
            AppRecord(
                bundle_id="com.microsoft.teams2",
                display_name="Microsoft Teams",
                is_running=True,
                bundle_path="/Applications/Microsoft Teams.app",
                candidate_paths=("/Applications/Microsoft Teams.app",),
            )
        ]

    def installed_apps(self) -> list[AppRecord]:
        return self.running_apps()

    def running_bundle_ids(self) -> frozenset[str]:
        return frozenset({"com.microsoft.teams2", "com.apple.Terminal"})

    def is_bundle_running(self, bundle_id: str) -> bool:
        return bundle_id in self.running_bundle_ids()

    def resolve_bundle_urls(
        self, bundle_id: str, hint_path: str | None = None
    ) -> tuple[str, ...]:
        return (hint_path,) if hint_path else ()

    def app_icon_for_bundle(
        self, bundle_id: str, hint_path: str | None = None
    ) -> object | None:
        return None

    def start_observing(self, on_change: Callable[[], None]) -> None:
        pass

    def stop_observing(self) -> None:
        pass


def _make_controller(tmp_path: Path) -> AssignmentEditorController:
    Cocoa.NSApplication.sharedApplication()
    store = BindingStore(tmp_path / "config.json")
    store.load()
    ctrl = AssignmentEditorController(
        store=store,
        app_registry=_StubAppRegistry(),
        on_enter_editing=lambda: None,
        on_exit_editing=lambda: None,
        on_config_updated=lambda: None,
    )
    ctrl._build_window()
    ctrl.refresh_data()
    return ctrl


def test_interval_field_not_default_focused_and_typing_selects_slot(
    tmp_path: Path,
) -> None:
    ctrl = _make_controller(tmp_path)
    win = ctrl._window
    iv = ctrl._interval_field
    assert win is not None and iv is not None

    assert iv.refusesFirstResponder() is True
    assert not isinstance(win.firstResponder(), Cocoa.NSTextView)
    assert str(iv.stringValue()) == "300"

    # Even if AppKit attempts to advance/focus iv while not clicked, it stays unfocused
    win.makeFirstResponder_(iv)
    assert not isinstance(win.firstResponder(), Cocoa.NSTextView)

    # Typing 'R' in the window selects slot R instead of mutating Double-Caps (ms)
    key_ev = Cocoa.NSEvent.keyEventWithType_location_modifierFlags_timestamp_windowNumber_context_characters_charactersIgnoringModifiers_isARepeat_keyCode_(
        Cocoa.NSEventTypeKeyDown,
        Cocoa.NSMakePoint(0.0, 0.0),
        0,
        0.0,
        win.windowNumber(),
        None,
        "r",
        "r",
        False,
        15,
    )
    win.keyDown_(key_ev)
    assert ctrl._selected_key == "R"
    assert str(iv.stringValue()) == "300"


def test_interval_field_focuses_on_explicit_click_and_blurs_outside(
    tmp_path: Path,
) -> None:
    ctrl = _make_controller(tmp_path)
    win = ctrl._window
    iv = ctrl._interval_field
    assert win is not None and iv is not None

    # Simulate user explicitly clicking inside Double-Caps (ms)
    iv.setRefusesFirstResponder_(False)
    win.makeFirstResponder_(iv)
    assert isinstance(win.firstResponder(), Cocoa.NSTextView)

    # Clicking anywhere outside iv_field immediately ends editing and re-locks refusesFirstResponder
    outside_click = Cocoa.NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
        Cocoa.NSEventTypeLeftMouseDown,
        Cocoa.NSMakePoint(400.0, 400.0),
        0,
        0.0,
        win.windowNumber(),
        None,
        0,
        1,
        1.0,
    )
    win.sendEvent_(outside_click)
    assert not isinstance(win.firstResponder(), Cocoa.NSTextView)
    assert iv.refusesFirstResponder() is True


def test_transient_saved_feedback_clears_on_reentry_close_and_app_switch(
    tmp_path: Path,
) -> None:
    ctrl = _make_controller(tmp_path)
    assert ctrl._status_label is not None

    ctrl._set_feedback("Saved R → Microsoft Teams", is_error=False)
    assert str(ctrl._status_label.stringValue()) == "Saved R → Microsoft Teams"

    # Switching away from AppJuggler clears stale feedback before next Edit entry
    ctrl._handle_app_resign_active()
    assert str(ctrl._status_label.stringValue()) == ""

    # Closing Edit Assignments clears stale feedback
    ctrl._set_feedback("Saved R → Microsoft Teams", is_error=False)
    ctrl._handle_close()
    assert str(ctrl._status_label.stringValue()) == ""

    # Re-entering via show_window clears stale feedback
    ctrl._set_feedback("Saved R → Microsoft Teams", is_error=False)
    ctrl._clear_transient_feedback()
    assert str(ctrl._status_label.stringValue()) == ""


def test_header_contains_logo_heading_subtitle_and_right_aligned_branding(
    tmp_path: Path,
) -> None:
    ctrl = _make_controller(tmp_path)
    win = ctrl._window
    assert win is not None
    subviews = list(win.contentView().subviews())

    quit_buttons = [
        v
        for v in subviews
        if isinstance(v, Cocoa.NSButton) and str(v.title()) == "Quit AppJuggler"
    ]
    assert len(quit_buttons) == 1
    assert float(quit_buttons[0].frame().origin.y) > 600.0

    labels = {
        str(v.stringValue()): v
        for v in subviews
        if isinstance(v, Cocoa.NSTextField) and not v.isEditable()
    }
    assert "AppJuggler" in labels
    assert "Jump straight to any app or window from your keyboard." in labels
    assert float(labels["AppJuggler"].frame().origin.y) > 600.0

    app_icons = [v for v in subviews if isinstance(v, _HighQualityImageView)]
    assert len(app_icons) == 1
    assert app_icons[0]._image is not None

    captions = [v for v in subviews if isinstance(v, _FullWidthCaptionView)]
    assert len(captions) == 1
    assert captions[0]._text == "AI - ENGINEERED BY"
    assert float(captions[0].frame().origin.y) > 600.0

    puffy_cards = [
        v
        for v in subviews
        if isinstance(v, _RoundedCardView) and v._logo_image is not None
    ]
    assert len(puffy_cards) == 1
    assert float(puffy_cards[0].frame().origin.y) > 600.0
    # Verify order on right: Quit (top) > AI - ENGINEERED BY (middle) > Puffy Clouds (bottom)
    assert (
        float(quit_buttons[0].frame().origin.y)
        > float(captions[0].frame().origin.y)
        > float(puffy_cards[0].frame().origin.y)
    )


def test_per_slot_show_window_picker_checkbox_persists_and_updates_summary(
    tmp_path: Path,
) -> None:
    from appjuggler.domain.models import AppBinding, Layer

    ctrl = _make_controller(tmp_path)
    chk = ctrl._slot_window_picker_checkbox
    assert chk is not None

    ctrl._store.assign(
        AppBinding(
            key="T",
            layer=Layer.BASE,
            bundle_id="com.apple.Terminal",
            display_name="Terminal",
        ),
        allow_replace=True,
    )
    ctrl.refresh_data()
    ctrl._handle_key_typed("T")
    assert chk.state() == Cocoa.NSControlStateValueOn
    assert "[~ Direct Jump]" not in str(ctrl._slot_summary_label.stringValue())

    # Uncheck 'Show Window / Tab List' -> switches Terminal slot to Direct Jump
    chk.setState_(Cocoa.NSControlStateValueOff)
    ctrl._handle_toggle_slot_window_picker()
    slot_t = ctrl._store.get_slot("T", Layer.BASE)
    assert slot_t is not None and slot_t.show_window_picker is False
    assert "[~ Direct Jump]" in str(ctrl._slot_summary_label.stringValue())

    # Re-check 'Show Window / Tab List' -> restores normal window/tab picker
    chk.setState_(Cocoa.NSControlStateValueOn)
    ctrl._handle_toggle_slot_window_picker()
    slot_t2 = ctrl._store.get_slot("T", Layer.BASE)
    assert slot_t2 is not None and slot_t2.show_window_picker is True
    assert "[~ Direct Jump]" not in str(ctrl._slot_summary_label.stringValue())
