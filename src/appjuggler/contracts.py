"""Platform-neutral adapter contracts (Section 8 of the specification).

Both the macOS implementation (`appjuggler.platforms.macos`) and future Linux
adapters (`appjuggler.platforms.linux`) implement these protocols.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol, runtime_checkable

from appjuggler.domain.events import HardwareKeyEvent, InputAction
from appjuggler.domain.models import (
    ActivationOutcome,
    AppBinding,
    AppRecord,
    DiscoveredWindow,
    Layer,
    WindowSlot,
)


@dataclass(frozen=True)
class PermissionStatus:
    """System permission readiness snapshot."""

    accessibility_trusted: bool
    input_tap_available: bool
    screen_locked: bool
    running_as_app_bundle: bool
    executable_description: str
    remediation_steps: tuple[str, ...] = ()

    @property
    def is_ready(self) -> bool:
        return (
            self.accessibility_trusted
            and self.input_tap_available
            and not self.screen_locked
        )


@dataclass(frozen=True)
class LaunchResult:
    """Outcome of launching a closed application and observing initial readiness."""

    success: bool
    missing_bundle: bool = False
    error: str | None = None
    bundle_path_used: str | None = None


@runtime_checkable
class PermissionMonitor(Protocol):
    """Checks and prompts for OS-specific Accessibility / Input permissions."""

    def check_status(self, prompt: bool = False) -> PermissionStatus: ...

    def open_accessibility_settings(self) -> None: ...

    def open_input_monitoring_settings(self) -> None: ...


@runtime_checkable
class InputAdapter(Protocol):
    """Low-level keyboard/modifier listener & modal input interceptor."""

    def start(
        self,
        on_event: Callable[[HardwareKeyEvent], InputAction],
        on_interrupt: Callable[[str], None],
    ) -> bool: ...

    def stop(self) -> None: ...

    def resync_modifier_state(self) -> None: ...

    @property
    def is_running(self) -> bool: ...


@runtime_checkable
class ApplicationRegistry(Protocol):
    """Discovers running/installed applications, observes lifecycle, and launches apps."""

    def running_apps(self) -> tuple[AppRecord, ...]: ...

    def running_bundle_ids(self) -> frozenset[str]: ...

    def is_bundle_running(self, bundle_id: str) -> bool: ...

    def installed_apps(self) -> tuple[AppRecord, ...]: ...

    def resolve_bundle_urls(self, bundle_id: str, hint_path: str | None = None) -> tuple[str, ...]: ...

    def app_icon_for_bundle(self, bundle_id: str, hint_path: str | None = None) -> object | None: ...

    def launch_and_wait_for_ready(
        self,
        bundle_id: str,
        hint_path: str | None,
        window_registry: WindowRegistry,
        is_cancelled: Callable[[], bool],
        timeout_sec: float = 8.0,
    ) -> LaunchResult: ...


@runtime_checkable
class WindowRegistry(Protocol):
    """Enumerates qualifying user windows for a running application."""

    def discover_windows(
        self, bundle_id: str, timeout_sec: float = 0.8
    ) -> tuple[DiscoveredWindow, ...]: ...

    def is_window_alive(self, window: DiscoveredWindow) -> bool: ...


@runtime_checkable
class WindowActivator(Protocol):
    """Performs bounded-time activation of a specific window or windowless running app."""

    def activate_window(
        self,
        window: DiscoveredWindow,
        timeout_sec: float = 0.9,
    ) -> ActivationOutcome: ...

    def activate_application_only(
        self,
        bundle_id: str,
        timeout_sec: float = 0.6,
    ) -> ActivationOutcome: ...


@runtime_checkable
class OverlayPresenter(Protocol):
    """Presents the non-activating keyboard map, window list, and status banners."""

    def show_app_map(
        self,
        active_layer: Layer,
        bindings: tuple[AppBinding, ...],
        running_bundle_ids: frozenset[str],
        duplicate_conflicts: frozenset[str],
        notice: str | None = None,
    ) -> None: ...

    def update_app_map_layer(self, active_layer: Layer) -> None: ...

    def show_launch_pending(self, binding: AppBinding) -> None: ...

    def show_window_list(
        self,
        binding: AppBinding,
        slots: tuple[WindowSlot, ...],
        selected_slot: int | None,
        notice: str | None = None,
    ) -> None: ...

    def update_window_selection(
        self,
        selected_slot: int | None,
        notice: str | None = None,
    ) -> None: ...

    def show_status_banner(
        self,
        level: str,
        message: str,
        auto_hide_ms: int = 2400,
        keep_app_map: bool = False,
    ) -> None: ...

    def show_quick_assign_apps(
        self,
        apps: tuple[AppRecord, ...],
        selected_index: int,
        bindings: tuple[AppBinding, ...],
        notice: str | None = None,
    ) -> None: ...

    def update_quick_assign_app_selection(
        self,
        selected_index: int,
        notice: str | None = None,
    ) -> None: ...

    def show_quick_assign_keymap(
        self,
        app: AppRecord,
        active_layer: Layer,
        bindings: tuple[AppBinding, ...],
        running_bundle_ids: frozenset[str],
        duplicate_conflicts: frozenset[str],
        conflict_existing: AppBinding | None = None,
    ) -> None: ...

    def hide(self) -> None: ...
