"""Unit tests for the macOS 'Edit Assignments' window layout, focus guard, and feedback lifecycle."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest

Cocoa = pytest.importorskip("Cocoa")

from appjuggler import CONTACT_EMAIL
from appjuggler.contracts import ApplicationRegistry
from appjuggler.domain.models import AppRecord
from appjuggler.persistence.binding_store import BindingStore
from appjuggler import build_info
from appjuggler.platforms.macos.ui.assignment_window import (
    AssignmentEditorController,
    _CopyableAddressButton,
    _FullWidthCaptionView,
    _HighQualityImageView,
    _LinkButton,
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


def _make_controller(
    tmp_path: Path, on_quit_app: Callable[[], None] | None = None
) -> AssignmentEditorController:
    Cocoa.NSApplication.sharedApplication()
    store = BindingStore(tmp_path / "config.json")
    store.load()
    ctrl = AssignmentEditorController(
        store=store,
        app_registry=_StubAppRegistry(),
        on_enter_editing=lambda: None,
        on_exit_editing=lambda: None,
        on_config_updated=lambda: None,
        # Never fall back to the default, which terminates the process running the tests.
        on_quit_app=on_quit_app or (lambda: None),
    )
    ctrl._build_window()
    ctrl.refresh_data()
    return ctrl


def _content_subviews(ctrl: AssignmentEditorController) -> list:
    assert ctrl._window is not None
    return list(ctrl._window.contentView().subviews())


def _quit_button(ctrl: AssignmentEditorController):
    buttons = [
        v
        for v in _content_subviews(ctrl)
        if isinstance(v, Cocoa.NSButton) and str(v.title()) == "Quit AppJuggler"
    ]
    assert len(buttons) == 1
    return buttons[0]


def _address_button(ctrl: AssignmentEditorController) -> _CopyableAddressButton:
    buttons = [
        v for v in _content_subviews(ctrl) if isinstance(v, _CopyableAddressButton)
    ]
    assert len(buttons) == 1
    return buttons[0]


def _shown_text(button) -> str:
    return str(button.attributedTitle().string())


def _is_underlined(button) -> bool:
    attrs, _ = button.attributedTitle().attributesAtIndex_effectiveRange_(0, None)
    return bool(attrs.get(Cocoa.NSUnderlineStyleAttributeName))


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
    subviews = _content_subviews(ctrl)
    # The header is the 116pt band above the first separator.
    header_floor = float(win.contentView().frame().size.height) - 116.0

    labels = {
        str(v.stringValue()): v
        for v in subviews
        if isinstance(v, Cocoa.NSTextField) and not v.isEditable()
    }
    assert "AppJuggler 1.9" in labels
    assert "Jump straight to any app or window from your keyboard." in labels
    assert float(labels["AppJuggler 1.9"].frame().origin.y) > header_floor
    # The product number follows the name in the quieter secondary label color.
    title = labels["AppJuggler 1.9"].attributedStringValue()
    name_attrs, _ = title.attributesAtIndex_effectiveRange_(0, None)
    number_attrs, _ = title.attributesAtIndex_effectiveRange_(title.length() - 1, None)
    assert name_attrs[Cocoa.NSForegroundColorAttributeName] == Cocoa.NSColor.labelColor()
    assert (
        number_attrs[Cocoa.NSForegroundColorAttributeName]
        == Cocoa.NSColor.secondaryLabelColor()
    )

    app_icons = [v for v in subviews if isinstance(v, _HighQualityImageView)]
    assert len(app_icons) == 1
    assert app_icons[0]._image is not None

    captions = [v for v in subviews if isinstance(v, _FullWidthCaptionView)]
    assert len(captions) == 1
    assert captions[0]._text == "AI - ENGINEERED BY"

    puffy_cards = [
        v
        for v in subviews
        if isinstance(v, _RoundedCardView) and v._logo_image is not None
    ]
    assert len(puffy_cards) == 1

    address = _address_button(ctrl)
    assert _shown_text(address) == CONTACT_EMAIL == "hello@puffyclouds.works"

    # Right column, top to bottom: AI - ENGINEERED BY > Puffy Clouds > contact address,
    # stacked without overlap inside the header and sharing one left edge and width.
    caption_frame = captions[0].frame()
    card_frame = puffy_cards[0].frame()
    address_frame = address.frame()
    assert (
        float(caption_frame.origin.y)
        >= Cocoa.NSMaxY(card_frame)
        > float(card_frame.origin.y)
        >= Cocoa.NSMaxY(address_frame)
        > float(address_frame.origin.y)
        > header_floor
    )
    assert Cocoa.NSMaxY(caption_frame) < float(win.contentView().frame().size.height)
    for frame in (caption_frame, address_frame):
        assert float(frame.origin.x) == float(card_frame.origin.x)
        assert float(frame.size.width) == float(card_frame.size.width)

    # Nothing else is visible in the header: Quit moved to the bottom row, and the
    # version links stay hidden until a logo is clicked.
    header_buttons = [
        v
        for v in subviews
        if isinstance(v, Cocoa.NSButton)
        and v is not address
        and not v.isHidden()
        and Cocoa.NSMaxY(v.frame()) > header_floor
    ]
    assert header_buttons == []


def test_quit_button_sits_at_right_end_of_bottom_row_and_quits(
    tmp_path: Path,
) -> None:
    quit_calls: list[bool] = []
    ctrl = _make_controller(tmp_path, on_quit_app=lambda: quit_calls.append(True))
    assert ctrl._window is not None and ctrl._status_label is not None
    content_w = float(ctrl._window.contentView().frame().size.width)

    quit_btn = _quit_button(ctrl)
    quit_frame = quit_btn.frame()
    # Same baseline as the other bottom-row buttons, flush with the 20pt right margin
    assert float(quit_frame.origin.y) == 12.0
    assert Cocoa.NSMaxX(quit_frame) == content_w - 20.0

    # Feedback text ends before Quit; the repair button stays hidden for a healthy config
    assert Cocoa.NSMaxX(ctrl._status_label.frame()) <= float(quit_frame.origin.x)
    assert ctrl._repair_btn.isHidden()

    quit_btn.performClick_(None)
    assert quit_calls == [True]


def test_damaged_config_repair_button_fits_between_feedback_and_quit(
    tmp_path: Path,
) -> None:
    (tmp_path / "config.json").write_text("{ broken json: [1, 2, ", encoding="utf-8")
    ctrl = _make_controller(tmp_path)
    status = ctrl._status_label
    repair = ctrl._repair_btn
    assert status is not None and repair is not None
    quit_x = float(_quit_button(ctrl).frame().origin.x)

    assert not repair.isHidden()
    assert str(status.stringValue()) == "Config damaged — not overwritten"
    assert Cocoa.NSMaxX(status.frame()) <= float(repair.frame().origin.x)
    assert Cocoa.NSMaxX(repair.frame()) <= quit_x
    # Neither the message nor the repair button's title is cut short
    assert float(status.cell().cellSize().width) <= float(status.frame().size.width)
    assert float(repair.cell().cellSize().width) <= float(repair.frame().size.width)

    # Backup & reset hides the button again and gives the feedback its full width back
    ctrl._handle_repair_config()
    assert repair.isHidden()
    assert str(status.stringValue()).startswith("Backed up damaged file to ")
    assert float(repair.frame().origin.x) < Cocoa.NSMaxX(status.frame()) <= quit_x


def test_feedback_tooltip_carries_full_message_and_clears_with_it(
    tmp_path: Path,
) -> None:
    ctrl = _make_controller(tmp_path)
    status = ctrl._status_label
    assert status is not None

    ctrl._set_feedback("Saved R → Microsoft Teams", is_error=False)
    assert str(status.toolTip()) == "Saved R → Microsoft Teams"

    ctrl._clear_transient_feedback()
    assert str(status.stringValue()) == ""
    assert status.toolTip() is None


def test_contact_address_click_copies_to_clipboard_and_confirms(
    tmp_path: Path,
) -> None:
    ctrl = _make_controller(tmp_path)
    address = _address_button(ctrl)

    # A private pasteboard keeps the test away from the user's real clipboard.
    pasteboard = Cocoa.NSPasteboard.pasteboardWithUniqueName()
    try:
        address.configure(CONTACT_EMAIL, pasteboard=pasteboard)
        assert pasteboard.stringForType_(Cocoa.NSPasteboardTypeString) is None

        address.performClick_(None)
        assert (
            str(pasteboard.stringForType_(Cocoa.NSPasteboardTypeString))
            == CONTACT_EMAIL
        )
        assert _shown_text(address) == "Copied to clipboard"

        # The confirmation is temporary: the address comes back when the timer fires
        address.endConfirmation_(None)
        assert _shown_text(address) == CONTACT_EMAIL
    finally:
        Cocoa.NSObject.cancelPreviousPerformRequestsWithTarget_(address)
        pasteboard.releaseGlobally()


def test_contact_address_underlines_on_hover_only(tmp_path: Path) -> None:
    ctrl = _make_controller(tmp_path)
    address = _address_button(ctrl)
    assert not _is_underlined(address)

    address.mouseEntered_(None)
    assert _is_underlined(address)
    assert _shown_text(address) == CONTACT_EMAIL

    address.mouseExited_(None)
    assert not _is_underlined(address)


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


def _logos(ctrl: AssignmentEditorController):
    subviews = _content_subviews(ctrl)
    app_icon = next(v for v in subviews if isinstance(v, _HighQualityImageView))
    puffy_card = next(
        v for v in subviews if isinstance(v, _RoundedCardView) and v._logo_image is not None
    )
    keyboard_map_card = next(
        v for v in subviews if isinstance(v, _RoundedCardView) and v._logo_image is None
    )
    return app_icon, puffy_card, keyboard_map_card


def test_clicking_either_logo_toggles_version_details_in_the_header(tmp_path: Path) -> None:
    ctrl = _make_controller(tmp_path)
    app_icon, puffy_card, keyboard_map_card = _logos(ctrl)
    links = [v for v in _content_subviews(ctrl) if isinstance(v, _LinkButton)]
    title = ctrl._title_lbl
    details = build_info.version_and_build(build_info.current())

    assert str(title.stringValue()) == "AppJuggler 1.9"
    assert len(links) == 2 and all(link.isHidden() for link in links)

    app_icon.mouseDown_(None)
    assert str(title.stringValue()) == f"AppJuggler 1.9 {details}"
    assert not any(link.isHidden() for link in links)
    # The details are smaller and greyed, on the title's line.
    shown = title.attributedStringValue()
    attrs, _ = shown.attributesAtIndex_effectiveRange_(shown.length() - 1, None)
    assert float(attrs[Cocoa.NSFontAttributeName].pointSize()) == 13.0
    assert attrs[Cocoa.NSForegroundColorAttributeName] == Cocoa.NSColor.secondaryLabelColor()

    # The Puffy Clouds logo (here pressed through VoiceOver) hides them again.
    assert puffy_card.accessibilityPerformPress()
    assert str(title.stringValue()) == "AppJuggler 1.9"
    assert all(link.isHidden() for link in links)

    # Closing the window hides them as well.
    app_icon.mouseDown_(None)
    ctrl._handle_close()
    assert str(title.stringValue()) == "AppJuggler 1.9"
    assert all(link.isHidden() for link in links)

    for logo in (app_icon, puffy_card):
        assert logo.isAccessibilityElement()
        assert str(logo.accessibilityRole()) == str(Cocoa.NSAccessibilityButtonRole)
    assert keyboard_map_card._on_click is None
    assert not keyboard_map_card.accessibilityPerformPress()


def test_version_links_sit_under_the_subtitle_and_open_their_pages(tmp_path: Path) -> None:
    ctrl = _make_controller(tmp_path)
    _, puffy_card, _ = _logos(ctrl)
    subviews = _content_subviews(ctrl)
    subtitle = next(
        v
        for v in subviews
        if isinstance(v, Cocoa.NSTextField)
        and str(v.stringValue()) == "Jump straight to any app or window from your keyboard."
    )
    links = [v for v in subviews if isinstance(v, _LinkButton)]
    assert [str(v.attributedTitle().string()) for v in links] == [
        "Source code: https://github.com/xsub/AppJuggler",
        "Native version: https://PuffyClouds.Works/AppJuggler2",
    ]

    # One row under the subtitle, inside the header, left of the branding column.
    header_floor = float(ctrl._window.contentView().frame().size.height) - 116.0
    first, second = (v.frame() for v in links)
    assert float(first.origin.y) == float(second.origin.y) > header_floor
    assert Cocoa.NSMaxY(first) <= float(subtitle.frame().origin.y) + 4.0
    assert Cocoa.NSMaxX(first) < float(second.origin.x)
    assert Cocoa.NSMaxX(second) < float(puffy_card.frame().origin.x)

    ctrl._set_version_details(True)
    opened: list[str] = []
    for link in links:
        link.configure(link._prefix, link._url, opened.append)
        link.performClick_(None)
    assert opened == [
        "https://github.com/xsub/AppJuggler",
        "https://PuffyClouds.Works/AppJuggler2",
    ]
