"""macOS WindowRegistry using Accessibility API (AXUIElement) + CGWindowList metadata."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
import threading
import time
from typing import Any

import ApplicationServices
import Cocoa
import CoreFoundation

from appjuggler import APP_BUNDLE_ID
from appjuggler.contracts import WindowRegistry
from appjuggler.domain.models import DiscoveredWindow

logger = logging.getLogger(__name__)

# Standard macOS AXError codes (HIServices/AXError.h)
AX_SUCCESS = 0
AX_ERROR_INVALID_UI_ELEMENT = -25202
AX_ERROR_CANNOT_COMPLETE = -25204
AX_ERROR_API_DISABLED = -25211

QUALIFYING_ROLES: frozenset[str] = frozenset({"AXWindow", "AXDialog", "AXSheet"})
EXCLUDED_SUBROLES: frozenset[str] = frozenset(
    {
        "AXUnknown",
        "AXDecorative",
        "AXSystemFloatingWindow",
        "AXFloatingWindow",
    }
)
STRICT_MODAL_SUBROLES: frozenset[str] = frozenset({"AXSystemDialog", "AXSheet"})


@dataclass
class _TrackedAXWindow:
    """Session-scoped opaque identity mapping an AXUIElementRef to a stable string ID."""

    window_id: str
    pid: int
    ax_element: Any
    creation_hint: int


@dataclass
class _DismissedAXWindow:
    """Tracks a window/tab hidden from the HUD picker until re-invoked in the app."""

    window_id: str
    pid: int
    ax_element: Any
    was_active_at_dismiss: bool


def _ax_equal(a: Any, b: Any) -> bool:
    if a is b:
        return True
    if a is None or b is None:
        return False
    try:
        return bool(CoreFoundation.CFEqual(a, b))
    except Exception:
        return a == b


class MacOSWindowRegistry(WindowRegistry):
    """Enumerates qualifying application windows via public AXUIElement APIs.

    Key macOS 15 behaviors handled empirically:
    1. `kAXWindowsAttribute` omits windows residing on inactive Spaces / Fullscreen Spaces,
       whereas `kAXMainWindowAttribute`, `kAXFocusedWindowAttribute`, and previously
       cached live `AXUIElement` handles remain valid (`AXRole == 'AXWindow'` and non-null
       `AXParent`) across Spaces! Merging all four sources (deduplicated via `CFEqual`)
       discovers off-Space and Fullscreen windows and preserves their slot numbers when
       switching Spaces without any private APIs.
    2. Minimized standard windows on macOS 15 report `AXSubrole == 'AXDialog'` with
       `AXModal == False`; `is_modal` checks `AXModal` / sheet subroles so minimized
       windows are not mislabeled as modal dialogs.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._next_seq: int = 1
        # bundle_id -> list[_TrackedAXWindow]
        self._tracked: dict[str, list[_TrackedAXWindow]] = {}
        # bundle_id -> list[_DismissedAXWindow]
        self._dismissed: dict[str, list[_DismissedAXWindow]] = {}

    @staticmethod
    def _shared_workspace() -> Any:
        return Cocoa.NSWorkspace.sharedWorkspace()

    def prune_terminated_pid(self, bundle_id: str | None, pid: int) -> None:
        with self._lock:
            if bundle_id:
                if bundle_id in self._tracked:
                    self._tracked[bundle_id] = [
                        t for t in self._tracked[bundle_id] if t.pid != pid
                    ]
                if bundle_id in self._dismissed:
                    self._dismissed[bundle_id] = [
                        d for d in self._dismissed[bundle_id] if d.pid != pid
                    ]
                    if not self._dismissed[bundle_id]:
                        self._dismissed.pop(bundle_id, None)
            else:
                for bid in list(self._tracked.keys()):
                    self._tracked[bid] = [
                        t for t in self._tracked[bid] if t.pid != pid
                    ]
                for bid in list(self._dismissed.keys()):
                    self._dismissed[bid] = [
                        d for d in self._dismissed[bid] if d.pid != pid
                    ]
                    if not self._dismissed[bid]:
                        self._dismissed.pop(bid, None)

    def dismiss_window(self, bundle_id: str, window_id: str) -> None:
        """Hide a window/tab from quick-choice discovery until active again in the app."""
        with self._lock:
            tracked = next(
                (
                    t
                    for t in self._tracked.get(bundle_id, ())
                    if t.window_id == window_id
                ),
                None,
            )
        if tracked is None:
            return

        ax_app = ApplicationServices.AXUIElementCreateApplication(tracked.pid)
        ApplicationServices.AXUIElementSetMessagingTimeout(ax_app, 0.20)
        ApplicationServices.AXUIElementSetMessagingTimeout(tracked.ax_element, 0.20)
        main_el = self._copy_attr(ax_app, ApplicationServices.kAXMainWindowAttribute)
        focused_el = self._copy_attr(
            ax_app, ApplicationServices.kAXFocusedWindowAttribute
        )
        is_main = bool(
            self._copy_attr(tracked.ax_element, ApplicationServices.kAXMainAttribute)
        ) or _ax_equal(tracked.ax_element, main_el)
        was_active = is_main or _ax_equal(tracked.ax_element, focused_el)

        with self._lock:
            bucket = self._dismissed.setdefault(bundle_id, [])
            for existing in bucket:
                if existing.pid == tracked.pid and (
                    existing.window_id == window_id
                    or _ax_equal(existing.ax_element, tracked.ax_element)
                ):
                    existing.window_id = window_id
                    existing.ax_element = tracked.ax_element
                    existing.was_active_at_dismiss = was_active
                    return
            bucket.append(
                _DismissedAXWindow(
                    window_id=window_id,
                    pid=tracked.pid,
                    ax_element=tracked.ax_element,
                    was_active_at_dismiss=was_active,
                )
            )

    def note_application_activated(self, bundle_id: str) -> None:
        """Restore any dismissed window/tab that is active when the user invokes the app."""
        if not bundle_id or bundle_id == APP_BUNDLE_ID:
            return
        with self._lock:
            if not self._dismissed.get(bundle_id):
                return

        ws = self._shared_workspace()
        own_pid = os.getpid()
        matching_apps = [
            app
            for app in ws.runningApplications()
            if not app.isTerminated()
            and int(app.processIdentifier()) != own_pid
            and str(app.bundleIdentifier() or "") == bundle_id
        ]
        if not matching_apps:
            with self._lock:
                self._dismissed.pop(bundle_id, None)
            return

        for app in matching_apps:
            pid = int(app.processIdentifier())
            ax_app = ApplicationServices.AXUIElementCreateApplication(pid)
            ApplicationServices.AXUIElementSetMessagingTimeout(ax_app, 0.20)
            main_el = self._copy_attr(ax_app, ApplicationServices.kAXMainWindowAttribute)
            focused_el = self._copy_attr(
                ax_app, ApplicationServices.kAXFocusedWindowAttribute
            )
            with self._lock:
                bucket = list(self._dismissed.get(bundle_id, ()))
            if not bucket:
                return

            restored_ids: set[str] = set()
            for entry in bucket:
                if entry.pid != pid:
                    continue
                ApplicationServices.AXUIElementSetMessagingTimeout(
                    entry.ax_element, 0.15
                )
                is_active = (
                    _ax_equal(entry.ax_element, main_el)
                    or _ax_equal(entry.ax_element, focused_el)
                    or bool(
                        self._copy_attr(
                            entry.ax_element, ApplicationServices.kAXMainAttribute
                        )
                    )
                )
                if is_active:
                    restored_ids.add(entry.window_id)

            if restored_ids:
                with self._lock:
                    cur = self._dismissed.get(bundle_id)
                    if cur:
                        self._dismissed[bundle_id] = [
                            d for d in cur if d.window_id not in restored_ids
                        ]
                        if not self._dismissed[bundle_id]:
                            self._dismissed.pop(bundle_id, None)

    def discover_windows(
        self,
        bundle_id: str,
        timeout_sec: float = 0.9,
        include_dismissed: bool = False,
    ) -> tuple[DiscoveredWindow, ...]:
        if bundle_id == APP_BUNDLE_ID:
            return ()

        ws = self._shared_workspace()
        own_pid = os.getpid()
        matching_apps = [
            app
            for app in ws.runningApplications()
            if not app.isTerminated()
            and int(app.processIdentifier()) != own_pid
            and str(app.bundleIdentifier() or "") == bundle_id
        ]
        if not matching_apps:
            with self._lock:
                self._tracked.pop(bundle_id, None)
                self._dismissed.pop(bundle_id, None)
            return ()

        live_pids = {int(a.processIdentifier()) for a in matching_apps}
        with self._lock:
            if bundle_id in self._tracked:
                self._tracked[bundle_id] = [
                    t for t in self._tracked[bundle_id] if t.pid in live_pids
                ]
            if bundle_id in self._dismissed:
                self._dismissed[bundle_id] = [
                    d for d in self._dismissed[bundle_id] if d.pid in live_pids
                ]
                if not self._dismissed[bundle_id]:
                    self._dismissed.pop(bundle_id, None)
            cached_for_bundle = list(self._tracked.get(bundle_id, ()))

        deadline = time.monotonic() + max(0.2, timeout_sec)
        results: list[DiscoveredWindow] = []

        for app in matching_apps:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            pid = int(app.processIdentifier())
            app_name = str(app.localizedName() or bundle_id)
            per_call_timeout = max(0.15, min(0.40, remaining))

            ax_app = ApplicationServices.AXUIElementCreateApplication(pid)
            ApplicationServices.AXUIElementSetMessagingTimeout(ax_app, per_call_timeout)

            main_el = self._copy_attr(ax_app, ApplicationServices.kAXMainWindowAttribute)
            focused_el = self._copy_attr(
                ax_app, ApplicationServices.kAXFocusedWindowAttribute
            )
            err, raw_windows = ApplicationServices.AXUIElementCopyAttributeValue(
                ax_app, ApplicationServices.kAXWindowsAttribute, None
            )
            if err == AX_ERROR_CANNOT_COMPLETE:
                logger.warning(
                    "AX window enumeration timed out/cannot complete for bundle=%s pid=%d",
                    bundle_id,
                    pid,
                )

            current_space_list = list(raw_windows) if (err == AX_SUCCESS and raw_windows) else []

            # Assemble candidate AXUIElements in priority order:
            # 1) main_el / focused_el (guarantees fast discovery even if app has many AXUnknown children,
            #    and captures off-Space / Fullscreen main windows!)
            # 2) current_space_list (`kAXWindowsAttribute`)
            # 3) previously tracked AXUIElements for this PID that are still alive on other Spaces!
            candidates: list[tuple[Any, bool]] = []  # (ax_el, in_kax_windows)

            def _add_candidate(el: Any, in_win_list: bool) -> None:
                if el is None:
                    return
                for idx_existing, (existing_el, was_in_list) in enumerate(candidates):
                    if _ax_equal(existing_el, el):
                        if in_win_list and not was_in_list:
                            candidates[idx_existing] = (existing_el, True)
                        return
                candidates.append((el, in_win_list))

            _add_candidate(main_el, False)
            _add_candidate(focused_el, False)
            for w_el in current_space_list:
                _add_candidate(w_el, True)
            for tracked_item in cached_for_bundle:
                if tracked_item.pid == pid:
                    _add_candidate(tracked_item.ax_element, False)

            dead_tracked_elements: list[Any] = []

            for idx, (ax_win, in_kax_windows) in enumerate(candidates):
                if time.monotonic() >= deadline:
                    break
                ApplicationServices.AXUIElementSetMessagingTimeout(ax_win, per_call_timeout)

                err_role, raw_role = ApplicationServices.AXUIElementCopyAttributeValue(
                    ax_win, ApplicationServices.kAXRoleAttribute, None
                )
                if err_role != AX_SUCCESS or raw_role is None:
                    dead_tracked_elements.append(ax_win)
                    continue

                role = str(raw_role)
                if role not in QUALIFYING_ROLES:
                    continue

                # If not in current Space's kAXWindows, verify it still has a live parent
                # (closed windows lose their AXParent or return invalid element).
                if not in_kax_windows:
                    err_par, parent = ApplicationServices.AXUIElementCopyAttributeValue(
                        ax_win, ApplicationServices.kAXParentAttribute, None
                    )
                    if err_par != AX_SUCCESS or parent is None:
                        dead_tracked_elements.append(ax_win)
                        continue

                subrole = str(
                    self._copy_attr(ax_win, ApplicationServices.kAXSubroleAttribute) or ""
                )
                if subrole in EXCLUDED_SUBROLES:
                    continue

                minimized = bool(
                    self._copy_attr(ax_win, ApplicationServices.kAXMinimizedAttribute)
                )
                fullscreen = bool(self._copy_attr(ax_win, "AXFullScreen"))
                modal_attr = bool(self._copy_attr(ax_win, "AXModal"))
                is_modal = (
                    modal_attr
                    or (subrole in STRICT_MODAL_SUBROLES)
                    or (role in ("AXDialog", "AXSheet"))
                )

                is_main = bool(
                    self._copy_attr(ax_win, ApplicationServices.kAXMainAttribute)
                ) or _ax_equal(ax_win, main_el)
                is_focused = is_main or _ax_equal(ax_win, focused_el)

                raw_title = self._copy_attr(ax_win, ApplicationServices.kAXTitleAttribute)
                title = str(raw_title).strip() if raw_title else ""
                if not title:
                    title = f"{app_name} Dialog" if is_modal else f"{app_name} Window"

                space_label: str | None = None
                if fullscreen:
                    space_label = "Fullscreen"
                elif not in_kax_windows and not minimized:
                    # Provably on another Space: live AXWindow with valid parent not in active Space's kAXWindows!
                    space_label = "Other Space"

                tracked = self._resolve_or_register_handle(
                    bundle_id=bundle_id,
                    pid=pid,
                    ax_element=ax_win,
                    order_hint=idx + 1,
                )
                if not include_dismissed and self._should_hide_dismissed(
                    bundle_id=bundle_id,
                    pid=pid,
                    window_id=tracked.window_id,
                    ax_element=ax_win,
                    is_focused=is_focused,
                ):
                    continue

                results.append(
                    DiscoveredWindow(
                        window_id=tracked.window_id,
                        bundle_id=bundle_id,
                        pid=pid,
                        title=title,
                        is_minimized=minimized,
                        is_fullscreen=fullscreen,
                        is_modal=is_modal,
                        is_focused=is_focused,
                        space_label=space_label,
                        creation_hint=tracked.creation_hint,
                        native_handle=ax_win,
                    )
                )

            if dead_tracked_elements:
                self._remove_dead_handles(bundle_id, pid, dead_tracked_elements)

        return tuple(results)

    def _should_hide_dismissed(
        self,
        *,
        bundle_id: str,
        pid: int,
        window_id: str,
        ax_element: Any,
        is_focused: bool,
    ) -> bool:
        with self._lock:
            bucket = self._dismissed.get(bundle_id)
            if not bucket:
                return False
            matched: _DismissedAXWindow | None = None
            for entry in bucket:
                if entry.pid == pid and (
                    entry.window_id == window_id
                    or _ax_equal(entry.ax_element, ax_element)
                ):
                    matched = entry
                    break
            if matched is None:
                return False

            matched.window_id = window_id
            matched.ax_element = ax_element
            if not is_focused:
                matched.was_active_at_dismiss = False
                return True
            if matched.was_active_at_dismiss:
                return True

            # Window/tab was inactive and has now become active again -> un-dismiss!
            self._dismissed[bundle_id] = [d for d in bucket if d is not matched]
            if not self._dismissed[bundle_id]:
                self._dismissed.pop(bundle_id, None)
            return False

    def is_window_alive(self, window: DiscoveredWindow) -> bool:
        """Verify both process liveness and AXUIElement validity before activation."""
        if window.native_handle is None:
            return False

        ws = self._shared_workspace()
        pid_alive = any(
            not a.isTerminated()
            and int(a.processIdentifier()) == window.pid
            and str(a.bundleIdentifier() or "") == window.bundle_id
            for a in ws.runningApplications()
        )
        if not pid_alive:
            return False

        ax_win = window.native_handle
        ApplicationServices.AXUIElementSetMessagingTimeout(ax_win, 0.25)
        err, role = ApplicationServices.AXUIElementCopyAttributeValue(
            ax_win, ApplicationServices.kAXRoleAttribute, None
        )
        if err != AX_SUCCESS or role is None:
            return False
        err_par, parent = ApplicationServices.AXUIElementCopyAttributeValue(
            ax_win, ApplicationServices.kAXParentAttribute, None
        )
        return err_par == AX_SUCCESS and parent is not None

    def _resolve_or_register_handle(
        self,
        bundle_id: str,
        pid: int,
        ax_element: Any,
        order_hint: int,
    ) -> _TrackedAXWindow:
        with self._lock:
            bucket = self._tracked.setdefault(bundle_id, [])
            for existing in bucket:
                if existing.pid == pid and _ax_equal(
                    existing.ax_element, ax_element
                ):
                    existing.ax_element = ax_element
                    return existing

            seq = self._next_seq
            self._next_seq += 1
            created = _TrackedAXWindow(
                window_id=f"{pid}:{seq}",
                pid=pid,
                ax_element=ax_element,
                creation_hint=(seq * 1000) + order_hint,
            )
            bucket.append(created)
            return created

    def _remove_dead_handles(
        self, bundle_id: str, pid: int, dead_elements: list[Any]
    ) -> None:
        with self._lock:
            bucket = self._tracked.get(bundle_id)
            if bucket:
                self._tracked[bundle_id] = [
                    item
                    for item in bucket
                    if not (
                        item.pid == pid
                        and any(_ax_equal(item.ax_element, d) for d in dead_elements)
                    )
                ]
            dismissed_bucket = self._dismissed.get(bundle_id)
            if dismissed_bucket:
                self._dismissed[bundle_id] = [
                    item
                    for item in dismissed_bucket
                    if not (
                        item.pid == pid
                        and any(_ax_equal(item.ax_element, d) for d in dead_elements)
                    )
                ]
                if not self._dismissed[bundle_id]:
                    self._dismissed.pop(bundle_id, None)

    @staticmethod
    def _copy_attr(element: Any, attr_name: str) -> Any:
        err, value = ApplicationServices.AXUIElementCopyAttributeValue(
            element, attr_name, None
        )
        if err == AX_SUCCESS:
            return value
        return None
