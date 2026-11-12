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

    def prune_terminated_pid(self, bundle_id: str | None, pid: int) -> None:
        with self._lock:
            if bundle_id and bundle_id in self._tracked:
                self._tracked[bundle_id] = [
                    t for t in self._tracked[bundle_id] if t.pid != pid
                ]
            else:
                for bid in list(self._tracked.keys()):
                    self._tracked[bid] = [
                        t for t in self._tracked[bid] if t.pid != pid
                    ]

    def discover_windows(
        self, bundle_id: str, timeout_sec: float = 0.9
    ) -> tuple[DiscoveredWindow, ...]:
        if bundle_id == APP_BUNDLE_ID:
            return ()

        ws = Cocoa.NSWorkspace.sharedWorkspace()
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
            return ()

        live_pids = {int(a.processIdentifier()) for a in matching_apps}
        with self._lock:
            if bundle_id in self._tracked:
                self._tracked[bundle_id] = [
                    t for t in self._tracked[bundle_id] if t.pid in live_pids
                ]
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
                    if bool(CoreFoundation.CFEqual(existing_el, el)):
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

                is_main = bool(self._copy_attr(ax_win, ApplicationServices.kAXMainAttribute)) or (
                    main_el is not None and bool(CoreFoundation.CFEqual(ax_win, main_el))
                )
                is_focused = is_main or (
                    focused_el is not None
                    and bool(CoreFoundation.CFEqual(ax_win, focused_el))
                )

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

    def is_window_alive(self, window: DiscoveredWindow) -> bool:
        """Verify both process liveness and AXUIElement validity before activation."""
        if window.native_handle is None:
            return False

        ws = Cocoa.NSWorkspace.sharedWorkspace()
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
                if existing.pid == pid and bool(
                    CoreFoundation.CFEqual(existing.ax_element, ax_element)
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
            if not bucket:
                return
            self._tracked[bundle_id] = [
                item
                for item in bucket
                if not (
                    item.pid == pid
                    and any(
                        bool(CoreFoundation.CFEqual(item.ax_element, d))
                        for d in dead_elements
                    )
                )
            ]

    @staticmethod
    def _copy_attr(element: Any, attr_name: str) -> Any:
        err, value = ApplicationServices.AXUIElementCopyAttributeValue(
            element, attr_name, None
        )
        if err == AX_SUCCESS:
            return value
        return None
