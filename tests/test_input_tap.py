"""Unit tests for MacOSInputAdapter physical Caps Lock edge + CGEventTap deduplication."""

from __future__ import annotations

from unittest.mock import patch

import Quartz

from appjuggler.domain.events import HardwareKeyEvent, InputAction, KeyKind
from appjuggler.platforms.macos.input_tap import MacOSInputAdapter
from appjuggler.platforms.macos.keymap import FLAG_ALPHA_SHIFT, VK_CAPS_LOCK


def test_fast_physical_caps_double_tap_triggers_without_cg_flags_changed() -> None:
    """Fast <75ms taps on Apple keyboards never toggle AlphaShift; IOHIDManager still catches them."""
    events: list[HardwareKeyEvent] = []
    adapter = MacOSInputAdapter(enable_caps_hold=False)
    adapter._on_event = lambda ev: (events.append(ev) or InputAction.PASS_THROUGH)  # type: ignore[assignment]

    with patch("appjuggler.platforms.macos.input_tap.time.monotonic_ns", side_effect=[100_000_000, 140_000_000, 210_000_000, 250_000_000]):
        adapter._on_physical_caps_key_change(True)
        adapter._on_physical_caps_key_change(False)
        adapter._on_physical_caps_key_change(True)
        adapter._on_physical_caps_key_change(False)

    assert len(events) == 2
    assert all(e.kind is KeyKind.CAPS_LOCK and e.caps_lock_toggled for e in events)


def test_hid_and_cgeventtap_do_not_double_fire_for_single_press() -> None:
    """A single >80ms Caps Lock press seen by both IOHIDManager and CGEventTap emits exactly once."""
    events: list[HardwareKeyEvent] = []
    adapter = MacOSInputAdapter(enable_caps_hold=False)
    adapter._on_event = lambda ev: (events.append(ev) or InputAction.PASS_THROUGH)  # type: ignore[assignment]

    fake_cg_event = object()

    def _fake_int_field(_ev, _field):
        return VK_CAPS_LOCK

    # Case A: IOHIDManager fires first at t=100ms, CGEventTap FlagsChanged arrives at t=185ms
    with (
        patch("appjuggler.platforms.macos.input_tap.time.monotonic_ns", side_effect=[100_000_000, 185_000_000, 200_000_000]),
        patch.object(Quartz, "CGEventGetIntegerValueField", side_effect=_fake_int_field),
        patch.object(Quartz, "CGEventGetFlags", return_value=FLAG_ALPHA_SHIFT),
    ):
        adapter._on_physical_caps_key_change(True)
        adapter._tap_callback(None, Quartz.kCGEventFlagsChanged, fake_cg_event, None)
        adapter._on_physical_caps_key_change(False)

    assert len(events) == 1

    # Case B: External keyboard where CGEventTap FlagsChanged fires 1ms BEFORE IOHIDManager
    events.clear()
    adapter.resync_modifier_state()
    adapter._decoder.sync_caps_state(False)
    with (
        patch("appjuggler.platforms.macos.input_tap.time.monotonic_ns", side_effect=[500_000_000, 501_000_000, 560_000_000]),
        patch.object(Quartz, "CGEventGetIntegerValueField", side_effect=_fake_int_field),
        patch.object(Quartz, "CGEventGetFlags", return_value=FLAG_ALPHA_SHIFT),
    ):
        adapter._tap_callback(None, Quartz.kCGEventFlagsChanged, fake_cg_event, None)
        adapter._on_physical_caps_key_change(True)
        adapter._on_physical_caps_key_change(False)

    assert len(events) == 1
