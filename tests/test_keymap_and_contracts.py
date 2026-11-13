"""Unit tests for KeyEventDecoder (Caps Lock & layout decoding) and Linux adapter stubs."""

from __future__ import annotations

from appjuggler.contracts import (
    ApplicationRegistry,
    InputAdapter,
    OverlayPresenter,
    PermissionMonitor,
    WindowActivator,
    WindowRegistry,
)
from appjuggler.domain.events import KeyKind
from appjuggler.platforms.linux.stub import (
    LinuxApplicationRegistry,
    LinuxInputAdapter,
    LinuxOverlayPresenter,
    LinuxPermissionMonitor,
    LinuxWindowActivator,
    LinuxWindowRegistry,
)
from appjuggler.platforms.macos.keymap import (
    FLAG_ALPHA_SHIFT,
    FLAG_SHIFT,
    VK_CAPS_LOCK,
    VK_DELETE,
    VK_FORWARD_DELETE,
    VK_GRAVE,
    VK_ISO_SECTION,
    KeyEventDecoder,
)


def test_decoder_distinguishes_caps_lock_toggles_and_ignores_caps_on_letters() -> None:
    decoder = KeyEventDecoder(initial_caps_on=False)

    # Press 1: AlphaShift transitions False -> True => caps_lock_toggled=True
    e1 = decoder.decode_flags_changed(VK_CAPS_LOCK, FLAG_ALPHA_SHIFT, 100)
    assert e1.kind is KeyKind.CAPS_LOCK
    assert e1.caps_lock_toggled is True
    assert e1.caps_lock_on is True

    # Key release sending same AlphaShift=True => caps_lock_toggled=False!
    e_rel = decoder.decode_flags_changed(VK_CAPS_LOCK, FLAG_ALPHA_SHIFT, 140)
    assert e_rel.caps_lock_toggled is False

    # Press 2: AlphaShift transitions True -> False => caps_lock_toggled=True
    e2 = decoder.decode_flags_changed(VK_CAPS_LOCK, 0, 200)
    assert e2.caps_lock_toggled is True
    assert e2.caps_lock_on is False

    # Decoding keycode 17 ('T') yields symbol='T' regardless of Shift or Caps Lock flags
    k_plain = decoder.decode_key_event(17, 0, True, False, 300)
    k_caps_shift = decoder.decode_key_event(17, FLAG_ALPHA_SHIFT | FLAG_SHIFT, True, False, 310)
    assert k_plain.kind is KeyKind.LETTER and k_plain.symbol == "T" and not k_plain.shift_held
    assert (
        k_caps_shift.kind is KeyKind.LETTER
        and k_caps_shift.symbol == "T"
        and k_caps_shift.shift_held is True
        and k_caps_shift.caps_lock_on is True
    )


def test_custom_qwertz_letter_map_separates_keycode_from_letter() -> None:
    # Simulate a QWERTZ layout where physical keycode 16 produces 'Z' and keycode 6 produces 'Y'
    qwertz_map = {16: "Z", 6: "Y", 17: "T"}
    decoder = KeyEventDecoder(initial_caps_on=False, letter_map=qwertz_map)
    ev_z = decoder.decode_key_event(16, 0, True, False, 100)
    ev_y = decoder.decode_key_event(6, 0, True, False, 110)
    assert ev_z.symbol == "Z"
    assert ev_y.symbol == "Y"


def test_linux_stubs_implement_adapter_protocols() -> None:
    assert isinstance(LinuxPermissionMonitor(), PermissionMonitor)
    assert isinstance(LinuxInputAdapter(), InputAdapter)
    assert isinstance(LinuxApplicationRegistry(), ApplicationRegistry)
    assert isinstance(LinuxWindowRegistry(), WindowRegistry)
    assert isinstance(LinuxWindowActivator(), WindowActivator)
    assert isinstance(LinuxOverlayPresenter(), OverlayPresenter)

    status = LinuxPermissionMonitor().check_status()
    assert not status.is_ready
    assert any("TODO(linux)" in step for step in status.remediation_steps)


def test_tilde_and_delete_key_decoding() -> None:
    decoder = KeyEventDecoder(initial_caps_on=False)
    for kc in (VK_GRAVE, VK_ISO_SECTION):
        ev_plain = decoder.decode_key_event(kc, 0, True, False, 100)
        ev_shift = decoder.decode_key_event(kc, FLAG_SHIFT, True, False, 110)
        assert ev_plain.kind is KeyKind.TILDE
        assert ev_shift.kind is KeyKind.TILDE

    for kc in (VK_DELETE, VK_FORWARD_DELETE):
        ev_del = decoder.decode_key_event(kc, 0, True, False, 120)
        assert ev_del.kind is KeyKind.DELETE
