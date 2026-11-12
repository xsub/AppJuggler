"""macOS WindowActivator with bounded timeouts, unhide/unminimize, and verification."""

from __future__ import annotations

import logging
import time

import ApplicationServices
import Cocoa
import CoreFoundation
import Quartz

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
        make_fullscreen: bool = False,
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
                if make_fullscreen:
                    self._ensure_window_fullscreen(ax_app, ax_win, window)
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
            if make_fullscreen:
                self._ensure_window_fullscreen(ax_app, ax_win, window)
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

    def _ensure_window_fullscreen(
        self, ax_app, ax_win, window: DiscoveredWindow
    ) -> None:
        """Put a non-modal activated window into Full Screen if not already Full Screen."""
        if window.is_modal:
            return

        err_role, raw_role = ApplicationServices.AXUIElementCopyAttributeValue(
            ax_win, ApplicationServices.kAXRoleAttribute, None
        )
        if err_role != AX_SUCCESS or str(raw_role or "") != "AXWindow":
            return

        _, raw_subrole = ApplicationServices.AXUIElementCopyAttributeValue(
            ax_win, ApplicationServices.kAXSubroleAttribute, None
        )
        subrole = str(raw_subrole or "")
        if subrole in ("AXSystemDialog", "AXSheet", "AXFloatingWindow", "AXUnknown"):
            return

        err_fs, cur_fs = ApplicationServices.AXUIElementCopyAttributeValue(
            ax_win, "AXFullScreen", None
        )
        if err_fs == AX_SUCCESS and bool(cur_fs):
            return

        # 1. Preferred path: directly set idempotent AXFullScreen = True.
        err_set, is_settable = ApplicationServices.AXUIElementIsAttributeSettable(
            ax_win, "AXFullScreen", None
        )
        if err_set == AX_SUCCESS and bool(is_settable):
            err_write = ApplicationServices.AXUIElementSetAttributeValue(
                ax_win, "AXFullScreen", True
            )
            if err_write == AX_SUCCESS:
                return

        # Measure current AX window geometry and match to its NSScreen.
        win_rect = self._read_ax_rect(ax_win)
        scr, primary_h = self._screen_for_ax_rect(win_rect)
        scr_frame = scr.frame() if scr is not None else None
        if (
            win_rect is not None
            and scr_frame is not None
            and win_rect[2] >= float(scr_frame.size.width) - 8.0
            and win_rect[3] >= float(scr_frame.size.height) - 8.0
        ):
            # Already spans the full display (e.g. Electron window in Full Screen).
            return

        # 2. Dedicated Full Screen titlebar button if exposed.
        err_btn, fs_btn = ApplicationServices.AXUIElementCopyAttributeValue(
            ax_win, ApplicationServices.kAXFullScreenButtonAttribute, None
        )
        if err_btn == AX_SUCCESS and fs_btn is not None:
            if (
                ApplicationServices.AXUIElementPerformAction(
                    fs_btn, ApplicationServices.kAXPressAction
                )
                == AX_SUCCESS
            ):
                return

        # 3. Electron / custom-titlebar apps (e.g. VS Code / Antigravity) that expose
        #    'Toggle Full Screen' / 'Enter Full Screen' in View or Window menu.
        if self._trigger_menubar_enter_fullscreen(ax_app):
            return

        # 4. Graceful maximize fallback for windows that do not support native macOS
        #    Full Screen Spaces (e.g. Activity Monitor): fill the screen's visibleFrame.
        if scr is not None and primary_h > 0.0:
            self._fill_screen_visible_frame(ax_win, scr, primary_h)

    @staticmethod
    def _trigger_menubar_enter_fullscreen(ax_app) -> bool:
        err, menubar = ApplicationServices.AXUIElementCopyAttributeValue(
            ax_app, ApplicationServices.kAXMenuBarAttribute, None
        )
        if err != AX_SUCCESS or menubar is None:
            return False
        err_c, top_items = ApplicationServices.AXUIElementCopyAttributeValue(
            menubar, ApplicationServices.kAXChildrenAttribute, None
        )
        if err_c != AX_SUCCESS or not top_items:
            return False

        for top_item in top_items:
            _, top_title = ApplicationServices.AXUIElementCopyAttributeValue(
                top_item, ApplicationServices.kAXTitleAttribute, None
            )
            if str(top_title or "") not in ("View", "Window", "Widok", "Okno"):
                continue
            _, submenus = ApplicationServices.AXUIElementCopyAttributeValue(
                top_item, ApplicationServices.kAXChildrenAttribute, None
            )
            if not submenus:
                continue
            _, menu_items = ApplicationServices.AXUIElementCopyAttributeValue(
                submenus[0], ApplicationServices.kAXChildrenAttribute, None
            )
            for mi in menu_items or []:
                _, enabled = ApplicationServices.AXUIElementCopyAttributeValue(
                    mi, ApplicationServices.kAXEnabledAttribute, None
                )
                if not bool(enabled):
                    continue
                _, ident = ApplicationServices.AXUIElementCopyAttributeValue(
                    mi, ApplicationServices.kAXIdentifierAttribute, None
                )
                _, title = ApplicationServices.AXUIElementCopyAttributeValue(
                    mi, ApplicationServices.kAXTitleAttribute, None
                )
                ident_str = str(ident or "")
                title_str = str(title or "")
                if title_str.startswith(("Exit Full Screen", "Leave Full Screen")):
                    return False
                if ident_str in (
                    "enterFullScreenMode:",
                    "toggleFullScreen:",
                    "toggleFullScreenMode:",
                ) or title_str.startswith("Enter Full Screen"):
                    act_err = ApplicationServices.AXUIElementPerformAction(
                        mi, ApplicationServices.kAXPressAction
                    )
                    if act_err == AX_SUCCESS:
                        return True
        return False

    @staticmethod
    def _read_ax_rect(ax_win) -> tuple[float, float, float, float] | None:
        err_p, pos_val = ApplicationServices.AXUIElementCopyAttributeValue(
            ax_win, ApplicationServices.kAXPositionAttribute, None
        )
        err_s, sz_val = ApplicationServices.AXUIElementCopyAttributeValue(
            ax_win, ApplicationServices.kAXSizeAttribute, None
        )
        if err_p != AX_SUCCESS or err_s != AX_SUCCESS or pos_val is None or sz_val is None:
            return None
        ok_p, pt = ApplicationServices.AXValueGetValue(
            pos_val, ApplicationServices.kAXValueCGPointType, None
        )
        ok_s, sz = ApplicationServices.AXValueGetValue(
            sz_val, ApplicationServices.kAXValueCGSizeType, None
        )
        if not (ok_p and ok_s):
            return None
        return (float(pt.x), float(pt.y), float(sz.width), float(sz.height))

    @staticmethod
    def _screen_for_ax_rect(
        win_rect: tuple[float, float, float, float] | None,
    ) -> tuple[object | None, float]:
        screens = list(Cocoa.NSScreen.screens() or [])
        if not screens:
            return None, 0.0
        primary_h = float(screens[0].frame().size.height)
        if win_rect is None:
            return (Cocoa.NSScreen.mainScreen() or screens[0]), primary_h

        cx = win_rect[0] + win_rect[2] * 0.5
        cy_ax = win_rect[1] + win_rect[3] * 0.5
        # Convert AX top-left Y to AppKit bottom-left Y
        cy_cocoa = primary_h - cy_ax
        pt = Cocoa.NSMakePoint(cx, cy_cocoa)
        for scr in screens:
            if Cocoa.NSPointInRect(pt, scr.frame()):
                return scr, primary_h
        return (Cocoa.NSScreen.mainScreen() or screens[0]), primary_h

    @staticmethod
    def _fill_screen_visible_frame(ax_win, scr, primary_h: float) -> None:
        _, set_p = ApplicationServices.AXUIElementIsAttributeSettable(
            ax_win, ApplicationServices.kAXPositionAttribute, None
        )
        _, set_s = ApplicationServices.AXUIElementIsAttributeSettable(
            ax_win, ApplicationServices.kAXSizeAttribute, None
        )
        if not (bool(set_p) and bool(set_s)):
            return
        vis = scr.visibleFrame()
        ax_x = float(vis.origin.x)
        ax_y = primary_h - (float(vis.origin.y) + float(vis.size.height))
        ax_w = float(vis.size.width)
        ax_h = float(vis.size.height)
        v_pos = ApplicationServices.AXValueCreate(
            ApplicationServices.kAXValueCGPointType, Quartz.CGPoint(ax_x, ax_y)
        )
        v_sz = ApplicationServices.AXValueCreate(
            ApplicationServices.kAXValueCGSizeType, Quartz.CGSize(ax_w, ax_h)
        )
        ApplicationServices.AXUIElementSetAttributeValue(
            ax_win, ApplicationServices.kAXPositionAttribute, v_pos
        )
        ApplicationServices.AXUIElementSetAttributeValue(
            ax_win, ApplicationServices.kAXSizeAttribute, v_sz
        )
