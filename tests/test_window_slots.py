"""Unit tests for WindowSlotTable session slot stability, gap reuse, MRU, and >9 windows."""

from __future__ import annotations

from appjuggler.domain.models import DiscoveredWindow
from appjuggler.domain.window_slots import WindowSlotTable


def _win(
    wid: str,
    title: str = "Terminal",
    hint: int = 1,
    focused: bool = False,
    minimized: bool = False,
) -> DiscoveredWindow:
    return DiscoveredWindow(
        window_id=wid,
        bundle_id="com.apple.Terminal",
        pid=1033,
        title=title,
        creation_hint=hint,
        is_focused=focused,
        is_minimized=minimized,
    )


def test_slot_numbers_persist_across_title_focus_and_close_without_renumbering() -> None:
    table = WindowSlotTable()

    # Initial enumeration assigns 1, 2, 3 deterministically by creation_hint
    s1 = table.reconcile(
        "com.apple.Terminal",
        [_win("wC", "zsh - 3", hint=30), _win("wA", "zsh - 1", hint=10), _win("wB", "zsh - 2", hint=20, focused=True)],
    )
    assert [(s.slot_number, s.window_id) for s in s1.slots] == [
        (1, "wA"),
        (2, "wB"),
        (3, "wC"),
    ]
    assert s1.default_slot == 2  # Focused window selected by default
    assert s1.generation == 1

    # Changing focus and titles MUST NOT change slot numbers OR bump structural generation!
    s2 = table.reconcile(
        "com.apple.Terminal",
        [_win("wA", "vim Cargo.toml", hint=10, focused=True), _win("wB", "htop", hint=20), _win("wC", "logs", hint=30)],
    )
    assert [(s.slot_number, s.window_id, s.title) for s in s2.slots] == [
        (1, "wA", "vim Cargo.toml"),
        (2, "wB", "htop"),
        (3, "wC", "logs"),
    ]
    assert s2.generation == 1

    # Closing window #2 ("wB"): window #3 ("wC") MUST remain #3!
    s3 = table.reconcile(
        "com.apple.Terminal",
        [_win("wA", "vim Cargo.toml", hint=10), _win("wC", "logs", hint=30)],
    )
    assert [(s.slot_number, s.window_id) for s in s3.slots] == [
        (1, "wA"),
        (3, "wC"),
    ]
    assert s3.generation == 2

    # Opening a new window ("wD") fills the lowest free slot (#2)!
    s4 = table.reconcile(
        "com.apple.Terminal",
        [_win("wA", "vim", hint=10), _win("wC", "logs", hint=30), _win("wD", "new tab", hint=40)],
    )
    assert [(s.slot_number, s.window_id) for s in s4.slots] == [
        (1, "wA"),
        (2, "wD"),
        (3, "wC"),
    ]
    assert s4.generation == 3


def test_mru_recording_and_over_nine_windows_ordering() -> None:
    table = WindowSlotTable()
    twelve_wins = [_win(f"w{i}", f"Win {i}", hint=i) for i in range(1, 13)]
    snap = table.reconcile("com.apple.Terminal", twelve_wins)
    assert len(snap.slots) == 12
    assert [s.slot_number for s in snap.slots] == list(range(1, 13))
    assert snap.default_slot == 1

    # Activate window #11 -> MRU becomes slot 11
    table.record_activated("com.apple.Terminal", "w11")
    snap2 = table.reconcile("com.apple.Terminal", twelve_wins)
    assert snap2.default_slot == 11
