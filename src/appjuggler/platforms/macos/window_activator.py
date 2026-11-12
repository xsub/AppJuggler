"""macOS WindowActivator with bounded timeouts, unhide/unminimize, and verification."""

from __future__ import annotations

import logging
import time

import ApplicationServices
import Cocoa
import CoreFoundation

from appjuggler.contracts import WindowActivator, WindowRegistry
from appjuggler.domain.models import ActivationOutcome, DiscoveredWindow
from appjuggler.platforms.macos.window_registry import (
    AX_ERROR_API_DISABLED,
    AX_ERROR_CANNOT_COMPLETE,
    AX_ERROR_INVALID_UI_ELEMENT,
    AX_SUCCESS,
)

logger = logging.getLogger(__name__)


class MacOSWindowActivator(WindowActivator):
    """Raises and focuses specific macOS windows with bounded retries and verification."""

    def __init__(self, window_registry: WindowRegistry) -> None:
        self._window_registry = window_registry

    def activate_window(
        self,
        window: DiscoveredWindow,
        timeout_sec: float = 0.9,
    ) -> ActivationOutcome:
        started = time.monotonic()
        deadline = started + max(0.25, timeout_sec)

        running_app = self._find_running_app(window.bundle_id, window.pid)
        if running_app is None or not self._window_registry.is_window_alive(window):
            return ActivationOutcome(
                success=False,
                stale_window=True,
                error="Selected window closed before activation",
                elapsed_ms=(time.monotonic() - started) * 1000.0,
            )

        ax_win = window.native_handle
        ax_app = ApplicationServices.AXUIElementCreateApplication(window.pid)
        msg_timeout = max(0.2, min(0.35, timeout_sec * 0.5))
        ApplicationServices.AXUIElementSetMessagingTimeout(ax_win, msg_timeout)
        ApplicationServices.AXUIElementSetMessagingTimeout(ax_app, msg_timeout)

        # 1. Unhide application if hidden (Cmd+H).
        if running_app.isHidden():
            running_app.unhide()

        # 2. Unminimize window if minimized in the Dock.
        err_min, min_val = ApplicationServices.AXUIElementCopyAttributeValue(
            ax_win, ApplicationServices.kAXMinimizedAttribute, None
        )
        if err_min == AX_ERROR_API_DISABLED:
            return ActivationOutcome(
                success=False,
                error="Accessibility permission is disabled. Grant permission in System Settings.",
                elapsed_ms=(time.monotonic() - started) * 1000.0,
            )
        if err_min == AX_ERROR_INVALID_UI_ELEMENT:
            return ActivationOutcome(
                success=False,
                stale_window=True,
                error="Selected window closed before activation",
                elapsed_ms=(time.monotonic() - started) * 1000.0,
            )

        if err_min == AX_SUCCESS and bool(min_val):
            ApplicationServices.AXUIElementSetAttributeValue(
                ax_win, ApplicationServices.kAXMinimizedAttribute, False
            )
            unmin_deadline = min(deadline, time.monotonic() + 0.25)
            while time.monotonic() < unmin_deadline:
                _, cur_min = ApplicationServices.AXUIElementCopyAttributeValue(
                    ax_win, ApplicationServices.kAXMinimizedAttribute, None
                )
                if not bool(cur_min):
                    break
                time.sleep(0.025)

        # 3. Prime target window inside the app BEFORE process activation so Mission Control /
        #    Spaces swooshes directly to THIS window's Space rather than a different Space.
        raise_err = self._raise_and_focus_ax(ax_app, ax_win)
        if raise_err == AX_ERROR_INVALID_UI_ELEMENT:
            return ActivationOutcome(
                success=False,
                stale_window=True,
                error="Selected window closed during activation",
                elapsed_ms=(time.monotonic() - started) * 1000.0,
            )
        if raise_err == AX_ERROR_API_DISABLED:
            return ActivationOutcome(
                success=False,
                error="Accessibility permission is disabled.",
                elapsed_ms=(time.monotonic() - started) * 1000.0,
            )

        # 4. Activate target process (ignoring other apps, NOT raising all windows).
        self._activate_ns_running_app(running_app)

        # 5. Re-assert target window raise/main after process activation and verify outcome.
        self._raise_and_focus_ax(ax_app, ax_win)

        next_retry_at = time.monotonic() + 0.12
        last_ax_err = raise_err

        while time.monotonic() < deadline:
            if self._is_verified_focused(running_app, ax_app, ax_win):
                elapsed = (time.monotonic() - started) * 1000.0
                logger.info(
                    "Activated window for bundle=%s in %.1f ms",
                    window.bundle_id,
                    elapsed,
                )
                return ActivationOutcome(success=True, elapsed_ms=elapsed)

            now = time.monotonic()
            if now >= next_retry_at:
                self._activate_ns_running_app(running_app)
                last_ax_err = self._raise_and_focus_ax(ax_app, ax_win)
                next_retry_at = now + 0.14

            time.sleep(0.025)

        # On cross-Space / Fullscreen transitions, macOS Space swoosh animation (~350-600ms)
        # may still be finishing when `isActive()` becomes True. Accept if process is active
        # and no fatal AX error occurred, otherwise report controlled timeout.
        elapsed = (time.monotonic() - started) * 1000.0
        if running_app.isActive() and last_ax_err == AX_SUCCESS:
            logger.info(
                "Activated app/window (Space transition in progress) bundle=%s in %.1f ms",
                window.bundle_id,
                elapsed,
            )
            return ActivationOutcome(success=True, elapsed_ms=elapsed)

        if last_ax_err == AX_ERROR_CANNOT_COMPLETE:
            err_msg = "Accessibility activation timed out (target application may be unresponsive)."
        else:
            err_msg = f"Could not confirm focus on target window within {int(timeout_sec * 1000)} ms."
        logger.warning("Window activation incomplete bundle=%s: %s", window.bundle_id, err_msg)
        return ActivationOutcome(success=False, error=err_msg, elapsed_ms=elapsed)

    def activate_application_only(
        self,
        bundle_id: str,
        timeout_sec: float = 0.6,
    ) -> ActivationOutcome:
        """Activate a running windowless application without creating new documents (§2.5)."""
        started = time.monotonic()
        deadline = started + max(0.2, timeout_sec)

        ws = Cocoa.NSWorkspace.sharedWorkspace()
        apps = [
            a
            for a in ws.runningApplications()
            if not a.isTerminated() and str(a.bundleIdentifier() or "") == bundle_id
        ]
        if not apps:
            return ActivationOutcome(
                success=False,
                error=f"Application '{bundle_id}' is no longer running.",
                elapsed_ms=(time.monotonic() - started) * 1000.0,
            )

        target = apps[0]
        if target.isHidden():
            target.unhide()
        self._activate_ns_running_app(target)

        while time.monotonic() < deadline:
            if target.isActive():
                return ActivationOutcome(
                    success=True,
                    elapsed_ms=(time.monotonic() - started) * 1000.0,
                )
            time.sleep(0.025)

        return ActivationOutcome(
            success=bool(target.isActive()),
            error=None if target.isActive() else f"Timed out activating '{bundle_id}'.",
            elapsed_ms=(time.monotonic() - started) * 1000.0,
        )

    @staticmethod
    def _find_running_app(bundle_id: str, pid: int):
        ws = Cocoa.NSWorkspace.sharedWorkspace()
        for app in ws.runningApplications():
            if (
                not app.isTerminated()
                and int(app.processIdentifier()) == pid
                and str(app.bundleIdentifier() or "") == bundle_id
            ):
                return app
        return None

    @staticmethod
    def _activate_ns_running_app(running_app) -> None:
        running_app.activateWithOptions_(Cocoa.NSApplicationActivateIgnoringOtherApps)

    @staticmethod
    def _raise_and_focus_ax(ax_app, ax_win) -> int:
        ApplicationServices.AXUIElementSetAttributeValue(
            ax_win, ApplicationServices.kAXMainAttribute, True
        )
        ApplicationServices.AXUIElementSetAttributeValue(
            ax_win, ApplicationServices.kAXFocusedAttribute, True
        )
        ApplicationServices.AXUIElementSetAttributeValue(
            ax_app, ApplicationServices.kAXFocusedWindowAttribute, ax_win
        )
        err = int(
            ApplicationServices.AXUIElementPerformAction(
                ax_win, ApplicationServices.kAXRaiseAction
            )
        )
        return err

    @staticmethod
    def _is_verified_focused(running_app, ax_app, ax_win) -> bool:
        if not running_app.isActive():
            return False
        err, focused = ApplicationServices.AXUIElementCopyAttributeValue(
            ax_app, ApplicationServices.kAXFocusedWindowAttribute, None
        )
        if err == AX_SUCCESS and focused is not None:
            if bool(CoreFoundation.CFEqual(focused, ax_win)):
                return True
        err_main, is_main = ApplicationServices.AXUIElementCopyAttributeValue(
            ax_win, ApplicationServices.kAXMainAttribute, None
        )
        return err_main == AX_SUCCESS and bool(is_main)
