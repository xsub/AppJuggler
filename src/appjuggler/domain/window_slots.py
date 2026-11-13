"""Session-stable window slot allocator and MRU tracker."""

from __future__ import annotations

from dataclasses import dataclass
import threading

from appjuggler.domain.models import DiscoveredWindow, WindowSlot


@dataclass(frozen=True)
class SlotSnapshot:
    """Consistent snapshot of an application's numbered windows and generation."""

    bundle_id: str
    slots: tuple[WindowSlot, ...]
    default_slot: int | None
    generation: int


class WindowSlotTable:
    """Maintains session-stable 1..N window numbers per application bundle_id.

    Guarantees:
    - Existing windows retain their slot_number across focus, title, and state changes.
    - Closing window #2 leaves window #3 at slot 3 (no renumbering).
    - Newly opened windows receive the lowest free positive integer (>= 1).
    - Structural membership changes increment `generation` monotonically.
    - Default selection prefers the most recently active existing window, falling back
      to a currently focused window or the lowest-numbered available slot.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # bundle_id -> {window_id: slot_number}
        self._assigned_slots: dict[str, dict[str, int]] = {}
        # bundle_id -> Structural generation counter
        self._generations: dict[str, int] = {}
        # bundle_id -> last structural membership tuple[(slot_number, window_id), ...]
        self._memberships: dict[str, tuple[tuple[int, str], ...]] = {}
        # bundle_id -> most recently active window_id
        self._mru_window_id: dict[str, str] = {}
        # bundle_id -> latest SlotSnapshot
        self._snapshots: dict[str, SlotSnapshot] = {}

    def reconcile(
        self, bundle_id: str, discovered: list[DiscoveredWindow] | tuple[DiscoveredWindow, ...]
    ) -> SlotSnapshot:
        """Reconcile freshly discovered windows against session-stable slot numbers."""
        with self._lock:
            slot_map = self._assigned_slots.setdefault(bundle_id, {})
            live_by_id: dict[str, DiscoveredWindow] = {}
            for win in discovered:
                # Deduplicate defensively if adapter ever reports duplicate IDs.
                if win.window_id not in live_by_id:
                    live_by_id[win.window_id] = win

            # 1. Remove closed windows without touching surviving slot assignments.
            closed_ids = [wid for wid in slot_map if wid not in live_by_id]
            for wid in closed_ids:
                del slot_map[wid]

            used_numbers: set[int] = set(slot_map.values())

            # 2. Deterministically assign lowest free positive slots to brand-new windows.
            new_windows = [w for wid, w in live_by_id.items() if wid not in slot_map]
            new_windows.sort(
                key=lambda w: (
                    w.creation_hint if w.creation_hint > 0 else 10**12,
                    w.pid,
                    w.title.casefold(),
                    w.window_id,
                )
            )
            next_candidate = 1
            for win in new_windows:
                while next_candidate in used_numbers:
                    next_candidate += 1
                slot_map[win.window_id] = next_candidate
                used_numbers.add(next_candidate)
                next_candidate += 1

            # 3. Update MRU if a focused window is observed and no valid MRU is set yet,
            #    or when an explicit focused window is reported by the OS.
            focused_windows = [w for w in live_by_id.values() if w.is_focused]
            current_mru = self._mru_window_id.get(bundle_id)
            if focused_windows:
                # Prefer non-minimized focused window.
                focused_non_min = [w for w in focused_windows if not w.is_minimized]
                chosen_focus = focused_non_min[0] if focused_non_min else focused_windows[0]
                self._mru_window_id[bundle_id] = chosen_focus.window_id
            elif current_mru is not None and current_mru not in live_by_id:
                del self._mru_window_id[bundle_id]

            # 4. Build ordered slots (sorted strictly by slot_number ascending).
            slots = tuple(
                sorted(
                    (
                        WindowSlot(slot_number=slot_map[wid], window=win)
                        for wid, win in live_by_id.items()
                    ),
                    key=lambda s: s.slot_number,
                )
            )

            # 5. Advance generation iff structural (slot_number, window_id) membership changed.
            membership = tuple((s.slot_number, s.window_id) for s in slots)
            prev_membership = self._memberships.get(bundle_id)
            gen = self._generations.get(bundle_id, 0)
            if prev_membership is None or membership != prev_membership:
                gen += 1
                self._generations[bundle_id] = gen
                self._memberships[bundle_id] = membership

            # 6. Compute default slot (MRU existing -> focused -> first slot).
            default_slot: int | None = None
            mru_wid = self._mru_window_id.get(bundle_id)
            if mru_wid and mru_wid in slot_map:
                default_slot = slot_map[mru_wid]
            elif slots:
                default_slot = slots[0].slot_number

            snapshot = SlotSnapshot(
                bundle_id=bundle_id,
                slots=slots,
                default_slot=default_slot,
                generation=gen,
            )
            self._snapshots[bundle_id] = snapshot
            return snapshot

    def record_activated(self, bundle_id: str, window_id: str) -> None:
        """Mark `window_id` as the most recently used window for `bundle_id`."""
        with self._lock:
            self._mru_window_id[bundle_id] = window_id

    def dismiss_window(self, bundle_id: str, window_id: str) -> None:
        """Release slot assignment for a window hidden from the quick-choice list."""
        with self._lock:
            slot_map = self._assigned_slots.get(bundle_id)
            if slot_map is not None:
                slot_map.pop(window_id, None)
            if self._mru_window_id.get(bundle_id) == window_id:
                del self._mru_window_id[bundle_id]

    def clear_app(self, bundle_id: str) -> None:
        """Clear session slots when an application completely terminates."""
        with self._lock:
            self._assigned_slots.pop(bundle_id, None)
            self._mru_window_id.pop(bundle_id, None)
            if bundle_id in self._memberships and self._memberships[bundle_id] != ():
                self._generations[bundle_id] = self._generations.get(bundle_id, 0) + 1
                self._memberships[bundle_id] = ()
            self._snapshots.pop(bundle_id, None)

    def latest_snapshot(self, bundle_id: str) -> SlotSnapshot | None:
        """Return the last reconciled snapshot for `bundle_id`, if any."""
        with self._lock:
            return self._snapshots.get(bundle_id)
