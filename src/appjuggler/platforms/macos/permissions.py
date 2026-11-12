"""macOS TCC permission checks, lock-screen detection, and repair helpers."""

from __future__ import annotations

import sys

import ApplicationServices
import Cocoa
import Quartz

from appjuggler import APP_BUNDLE_ID
from appjuggler.contracts import PermissionMonitor, PermissionStatus


def is_screen_locked() -> bool:
    """Return True if the macOS user session is locked or off-console."""
    session = Quartz.CGSessionCopyCurrentDictionary()
    if not session:
        return False
    if bool(session.get("CGSSessionScreenIsLocked", False)):
        return True
    on_console = session.get("kCGSSessionOnConsoleKey")
    if on_console is not None and not bool(on_console):
        return True
    return False


def is_running_as_packaged_app() -> bool:
    """Return True when executing as the packaged AppJuggler.app bundle."""
    bundle = Cocoa.NSBundle.mainBundle()
    if bundle is None:
        return False
    return str(bundle.bundleIdentifier() or "") == APP_BUNDLE_ID


def probe_active_event_tap_capability() -> bool:
    """Verify whether WindowServer permits creating an active CGEventTap right now."""

    def _noop_callback(proxy, event_type, event, refcon):
        return event

    mask = Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged)
    tap = Quartz.CGEventTapCreate(
        Quartz.kCGSessionEventTap,
        Quartz.kCGHeadInsertEventTap,
        Quartz.kCGEventTapOptionDefault,
        mask,
        _noop_callback,
        None,
    )
    if tap is None:
        return False
    Quartz.CFMachPortInvalidate(tap)
    return True


class MacOSPermissionMonitor(PermissionMonitor):
    """macOS Accessibility & active CGEventTap permission inspector."""

    def __init__(self, live_tap_checker: callable | None = None) -> None:
        self._live_tap_checker = live_tap_checker

    def check_status(self, prompt: bool = False) -> PermissionStatus:
        if prompt:
            options = {ApplicationServices.kAXTrustedCheckOptionPrompt: True}
            ax_trusted = bool(ApplicationServices.AXIsProcessTrustedWithOptions(options))
        else:
            ax_trusted = bool(ApplicationServices.AXIsProcessTrusted())

        locked = is_screen_locked()
        as_app = is_running_as_packaged_app()

        if self._live_tap_checker is not None and self._live_tap_checker():
            tap_ok = True
        elif locked:
            tap_ok = False
        else:
            tap_ok = probe_active_event_tap_capability()

        bundle_path = str(Cocoa.NSBundle.mainBundle().bundlePath() or sys.executable)
        exe_desc = bundle_path if as_app else f"{sys.executable} (Terminal / Host IDE)"

        steps: list[str] = []
        if not ax_trusted:
            if as_app:
                steps.append(
                    "Enable 'AppJuggler' in System Settings → Privacy & Security → Accessibility."
                )
                steps.append(
                    "If you recently rebuilt AppJuggler.app, toggle its Accessibility switch OFF and back ON."
                )
            else:
                steps.append(
                    "Running from CLI/venv: enable your Terminal app (or IDE) in System Settings → Privacy & Security → Accessibility."
                )
        if ax_trusted and not tap_ok and not locked:
            steps.append(
                "Active keyboard tap could not be created. Also enable AppJuggler (or your Terminal) in System Settings → Privacy & Security → Input Monitoring."
            )

        return PermissionStatus(
            accessibility_trusted=ax_trusted,
            input_tap_available=tap_ok,
            screen_locked=locked,
            running_as_app_bundle=as_app,
            executable_description=exe_desc,
            remediation_steps=tuple(steps),
        )

    def open_accessibility_settings(self) -> None:
        url = Cocoa.NSURL.URLWithString_(
            "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
        )
        if url is not None:
            Cocoa.NSWorkspace.sharedWorkspace().openURL_(url)

    def open_input_monitoring_settings(self) -> None:
        url = Cocoa.NSURL.URLWithString_(
            "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent"
        )
        if url is not None:
            Cocoa.NSWorkspace.sharedWorkspace().openURL_(url)
