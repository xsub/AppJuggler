"""Linux platform adapter stubs conforming to AppJuggler's adapter contracts.

AppJuggler's domain (`NavigationController`, `WindowSlotTable`, models) and
persistence (`BindingStore`) are 100% platform-neutral and already run on Linux.
Implementing native Linux support requires completing the OS adapters below for
X11 and Wayland environments (see `docs/linux-roadmap.md` for architecture).
"""

from __future__ import annotations

import os
import sys
from typing import Callable

from appjuggler.contracts import (
    ApplicationRegistry,
    InputAdapter,
    LaunchResult,
    OverlayPresenter,
    PermissionMonitor,
    PermissionStatus,
    WindowActivator,
    WindowRegistry,
)
from appjuggler.domain.events import HardwareKeyEvent, InputAction
from appjuggler.domain.models import (
    ActivationOutcome,
    AppBinding,
    AppRecord,
    DiscoveredWindow,
    Layer,
    WindowSlot,
)


def detect_linux_session_kind() -> str:
    """Detect Wayland vs X11 session from standard Linux environment variables."""
    session_type = os.environ.get("XDG_SESSION_TYPE", "").lower()
    if session_type in ("wayland", "x11"):
        return session_type
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return "unknown"


class LinuxPermissionMonitor(PermissionMonitor):
    """TODO(linux): Check `/dev/input/event*` (`input` group / uinput) or X11/Wayland portal."""

    def check_status(self, prompt: bool = False) -> PermissionStatus:
        session = detect_linux_session_kind()
        return PermissionStatus(
            accessibility_trusted=False,
            input_tap_available=False,
            screen_locked=False,
            running_as_app_bundle=False,
            executable_description=sys.executable,
            remediation_steps=(
                f"TODO(linux): Native Linux adapter not yet implemented (detected session: {session}).",
                "Planned input backend: evdev/uinput or XInput2/XGrabKeyboard (X11) / compositor shortcut protocol (Wayland).",
                "See docs/linux-roadmap.md for full implementation plan.",
            ),
        )

    def open_accessibility_settings(self) -> None:
        raise NotImplementedError("TODO(linux): Open desktop environment accessibility settings.")

    def open_input_monitoring_settings(self) -> None:
        raise NotImplementedError("TODO(linux): Explain udev / input group setup for evdev.")


class LinuxInputAdapter(InputAdapter):
    """TODO(linux): Intercept double-Caps Lock & modal navigation keys on X11/Wayland."""

    def __init__(self) -> None:
        self._running = False

    def start(
        self,
        on_event: Callable[[HardwareKeyEvent], InputAction],
        on_interrupt: Callable[[str], None],
    ) -> bool:
        raise NotImplementedError(
            "TODO(linux): Implement X11 (XInput2 + XGrabKeyboard) and Wayland/evdev input tap."
        )

    def stop(self) -> None:
        self._running = False

    def resync_modifier_state(self) -> None:
        pass

    @property
    def is_running(self) -> bool:
        return self._running


class LinuxApplicationRegistry(ApplicationRegistry):
    """TODO(linux): Map XDG `.desktop` IDs (e.g. `org.gnome.Terminal.desktop`) as `bundle_id`."""

    def running_apps(self) -> tuple[AppRecord, ...]:
        raise NotImplementedError("TODO(linux): Enumerate running desktop applications via EWMH/DBus.")

    def running_bundle_ids(self) -> frozenset[str]:
        raise NotImplementedError("TODO(linux): Return active XDG desktop entry IDs.")

    def is_bundle_running(self, bundle_id: str) -> bool:
        raise NotImplementedError("TODO(linux): Check running state for XDG desktop entry ID.")

    def installed_apps(self) -> tuple[AppRecord, ...]:
        raise NotImplementedError("TODO(linux): Scan XDG_DATA_DIRS/applications/*.desktop.")

    def resolve_bundle_urls(self, bundle_id: str, hint_path: str | None = None) -> tuple[str, ...]:
        raise NotImplementedError("TODO(linux): Resolve .desktop file path(s).")

    def app_icon_for_bundle(self, bundle_id: str, hint_path: str | None = None) -> object | None:
        return None

    def launch_and_wait_for_ready(
        self,
        bundle_id: str,
        hint_path: str | None,
        window_registry: WindowRegistry,
        is_cancelled: Callable[[], bool],
        timeout_sec: float = 8.0,
    ) -> LaunchResult:
        raise NotImplementedError("TODO(linux): Launch via Gio.DesktopAppInfo / gtk-launch.")


class LinuxWindowRegistry(WindowRegistry):
    """TODO(linux): Discover windows via EWMH `_NET_CLIENT_LIST` (X11) or compositor IPC (Wayland)."""

    def discover_windows(
        self,
        bundle_id: str,
        timeout_sec: float = 0.8,
        include_dismissed: bool = False,
    ) -> tuple[DiscoveredWindow, ...]:
        raise NotImplementedError(
            "TODO(linux): Query X11 EWMH or Wayland wlr-foreign-toplevel / GNOME / KWin."
        )

    def is_window_alive(self, window: DiscoveredWindow) -> bool:
        raise NotImplementedError("TODO(linux): Verify XID / Wayland toplevel handle liveness.")

    def dismiss_window(self, bundle_id: str, window_id: str) -> None:
        raise NotImplementedError("TODO(linux): Hide window from quick-choice list.")

    def note_application_activated(self, bundle_id: str) -> None:
        raise NotImplementedError("TODO(linux): Un-dismiss active window on app activation.")


class LinuxWindowActivator(WindowActivator):
    """TODO(linux): Raise/focus via `_NET_ACTIVE_WINDOW` (X11) or Wayland activation token."""

    def activate_window(
        self,
        window: DiscoveredWindow,
        timeout_sec: float = 0.9,
        make_fullscreen: bool = False,
        maximize_on_space: bool = False,
    ) -> ActivationOutcome:
        raise NotImplementedError("TODO(linux): Send _NET_ACTIVE_WINDOW / Wayland toplevel activate.")

    def activate_application_only(
        self,
        bundle_id: str,
        timeout_sec: float = 0.75,
        bundle_path_hint: str | None = None,
        leaving_fullscreen_space: bool = False,
    ) -> ActivationOutcome:
        raise NotImplementedError("TODO(linux): Activate running Linux application.")


class LinuxOverlayPresenter(OverlayPresenter):
    """TODO(linux): Render HUD overlay using GTK4 + `gtk4-layer-shell` (Wayland) / override-redirect (X11)."""

    def show_app_map(
        self,
        active_layer: Layer,
        bindings: tuple[AppBinding, ...],
        running_bundle_ids: frozenset[str],
        duplicate_conflicts: frozenset[str],
        notice: str | None = None,
        direct_jump: bool = False,
    ) -> None:
        raise NotImplementedError("TODO(linux): Show GTK4 layer-shell keyboard map overlay.")

    def update_app_map_layer(
        self, active_layer: Layer, direct_jump: bool = False
    ) -> None:
        raise NotImplementedError("TODO(linux): Switch Base/Shift layer in GTK4 overlay.")

    def show_launch_pending(self, binding: AppBinding) -> None:
        raise NotImplementedError("TODO(linux): Show launching banner in GTK4 overlay.")

    def show_window_list(
        self,
        binding: AppBinding,
        slots: tuple[WindowSlot, ...],
        selected_slot: int | None,
        notice: str | None = None,
    ) -> None:
        raise NotImplementedError("TODO(linux): Show window list in GTK4 overlay.")

    def update_window_selection(
        self,
        selected_slot: int | None,
        notice: str | None = None,
    ) -> None:
        raise NotImplementedError("TODO(linux): Update window selection highlight in GTK4 overlay.")

    def show_status_banner(
        self,
        level: str,
        message: str,
        auto_hide_ms: int = 2400,
        keep_app_map: bool = False,
    ) -> None:
        raise NotImplementedError("TODO(linux): Show status banner in GTK4 overlay.")

    def show_quick_assign_apps(
        self,
        apps: tuple[AppRecord, ...],
        selected_index: int,
        bindings: tuple[AppBinding, ...],
        notice: str | None = None,
    ) -> None:
        raise NotImplementedError("TODO(linux): Show Quick Assign open apps list.")

    def update_quick_assign_app_selection(
        self,
        selected_index: int,
        notice: str | None = None,
    ) -> None:
        raise NotImplementedError("TODO(linux): Update Quick Assign app highlight.")

    def show_quick_assign_keymap(
        self,
        app: AppRecord,
        active_layer: Layer,
        bindings: tuple[AppBinding, ...],
        running_bundle_ids: frozenset[str],
        duplicate_conflicts: frozenset[str],
        conflict_existing: AppBinding | None = None,
    ) -> None:
        raise NotImplementedError("TODO(linux): Show Quick Assign target key map.")

    def hide(self) -> None:
        pass
