"""macOS WindowActivator with bounded timeouts, unhide/unminimize, and verification."""

from __future__ import annotations

import logging
import time
from typing import Callable

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
                # Re-assert target window once process is active so multi-window apps
                # (e.g. Finder with several windows across Spaces) lock focus onto THIS window.
                self._raise_and_focus_ax(ax_app, ax_win)
                if make_fullscreen:
                    self.ensure_window_fullscreen(window, timeout_sec=1.6)
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
            self._raise_and_focus_ax(ax_app, ax_win)
            if make_fullscreen:
                self.ensure_window_fullscreen(window, timeout_sec=1.6)
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

    def ensure_window_fullscreen(
        self,
        window: DiscoveredWindow,
        is_cancelled: Callable[[], bool] | None = None,
        timeout_sec: float = 1.6,
        allow_window_reacquire: bool = False,
    ) -> bool:
        """Put a non-modal activated window into Full Screen with cross-Space & cold-launch verification.

        Handles two real-world macOS timing challenges:
        1. Cross-Space switches: WindowServer ignores fullscreen transitions while the ~500 ms
           Space swoosh animation is in flight.
        2. Newly launched apps: Electron/SwiftUI/Cocoa apps may take 200-1200 ms after creating
           an initial window (or swap out a splash window) before `AXFullScreen` / `AXFullScreenButton`
           becomes settable and enabled.
        """
        if window.is_modal and not allow_window_reacquire:
            return False

        cancelled = is_cancelled or (lambda: False)
        if cancelled():
            return False

        cur_win = window
        ax_win = cur_win.native_handle
        ax_app = ApplicationServices.AXUIElementCreateApplication(cur_win.pid)
        ApplicationServices.AXUIElementSetMessagingTimeout(ax_win, 0.30)
        ApplicationServices.AXUIElementSetMessagingTimeout(ax_app, 0.30)

        started_fs = time.monotonic()
        deadline = started_fs + max(0.35, timeout_sec)
        native_grace_sec = 1.1 if allow_window_reacquire else 0.65
        last_btn_press_at = -10.0
        last_menu_press_at = -10.0

        while time.monotonic() < deadline:
            if cancelled():
                return False

            running_app = self._find_running_app(cur_win.bundle_id, cur_win.pid)
            win_alive = running_app is not None and self._window_registry.is_window_alive(cur_win)

            err_role, raw_role = (
                ApplicationServices.AXUIElementCopyAttributeValue(
                    ax_win, ApplicationServices.kAXRoleAttribute, None
                )
                if win_alive
                else (-1, None)
            )
            _, raw_subrole = (
                ApplicationServices.AXUIElementCopyAttributeValue(
                    ax_win, ApplicationServices.kAXSubroleAttribute, None
                )
                if win_alive
                else (-1, None)
            )
            subrole = str(raw_subrole or "")
            non_standard = (
                not win_alive
                or err_role != AX_SUCCESS
                or str(raw_role or "") != "AXWindow"
                or cur_win.is_modal
                or subrole in ("AXSystemDialog", "AXSheet", "AXFloatingWindow", "AXUnknown")
            )

            if non_standard:
                if not allow_window_reacquire:
                    return False
                replacement = self._reacquire_standard_window(cur_win.bundle_id)
                if replacement is None:
                    time.sleep(0.14)
                    continue
                cur_win = replacement
                ax_win = cur_win.native_handle
                ax_app = ApplicationServices.AXUIElementCreateApplication(cur_win.pid)
                ApplicationServices.AXUIElementSetMessagingTimeout(ax_win, 0.30)
                ApplicationServices.AXUIElementSetMessagingTimeout(ax_app, 0.30)
                running_app = self._find_running_app(cur_win.bundle_id, cur_win.pid)
                if running_app is None:
                    return False

            err_fs, cur_fs = ApplicationServices.AXUIElementCopyAttributeValue(
                ax_win, "AXFullScreen", None
            )
            if err_fs == AX_SUCCESS and bool(cur_fs):
                return True

            err_set, is_settable = ApplicationServices.AXUIElementIsAttributeSettable(
                ax_win, "AXFullScreen", None
            )
            can_set_ax_fs = err_set == AX_SUCCESS and bool(is_settable)

            err_btn, fs_btn = ApplicationServices.AXUIElementCopyAttributeValue(
                ax_win, ApplicationServices.kAXFullScreenButtonAttribute, None
            )
            has_fs_btn = err_btn == AX_SUCCESS and fs_btn is not None

            win_rect = self._read_ax_rect(ax_win)
            scr, primary_h = self._screen_for_ax_rect(win_rect)
            scr_frame = scr.frame() if scr is not None else None
            if (
                not can_set_ax_fs
                and not has_fs_btn
                and win_rect is not None
                and scr_frame is not None
                and win_rect[2] >= float(scr_frame.size.width) - 8.0
                and win_rect[3] >= float(scr_frame.size.height) - 8.0
            ):
                return True

            # Keep the selected window raised and main in multi-window apps (like Finder)
            # after any incoming Space swoosh or cold launch lands.
            if not running_app.isActive():
                self._activate_ns_running_app(running_app)
            self._raise_and_focus_ax(ax_app, ax_win)

            # 1. Try setting idempotent AXFullScreen = True.
            if can_set_ax_fs:
                ApplicationServices.AXUIElementSetAttributeValue(
                    ax_win, "AXFullScreen", True
                )
                if self._wait_until_ax_fullscreen(ax_win, cancelled, 0.16):
                    return True

            # 2. If AXFullScreen is still False, press native AXFullScreenButton
            #    (strictly gated on subrole == 'AXFullScreenButton' & enabled == True
            #    with cooldown so it can never toggle back out of Full Screen).
            now = time.monotonic()
            if has_fs_btn and (now - last_btn_press_at) >= 0.65:
                _, btn_sub = ApplicationServices.AXUIElementCopyAttributeValue(
                    fs_btn, ApplicationServices.kAXSubroleAttribute, None
                )
                _, btn_en = ApplicationServices.AXUIElementCopyAttributeValue(
                    fs_btn, ApplicationServices.kAXEnabledAttribute, None
                )
                _, cur_fs_again = ApplicationServices.AXUIElementCopyAttributeValue(
                    ax_win, "AXFullScreen", None
                )
                if (
                    not bool(cur_fs_again)
                    and str(btn_sub or "") == "AXFullScreenButton"
                    and bool(btn_en)
                ):
                    if (
                        ApplicationServices.AXUIElementPerformAction(
                            fs_btn, ApplicationServices.kAXPressAction
                        )
                        == AX_SUCCESS
                    ):
                        last_btn_press_at = time.monotonic()
                        if self._wait_until_ax_fullscreen(ax_win, cancelled, 0.22):
                            return True

            # 3. Electron / custom-titlebar apps without AXFullScreen settable or button yet.
            if not can_set_ax_fs and not has_fs_btn:
                if (time.monotonic() - last_menu_press_at) >= 0.75:
                    if self._trigger_menubar_enter_fullscreen(ax_app):
                        last_menu_press_at = time.monotonic()
                        if self._wait_until_ax_fullscreen(ax_win, cancelled, 0.25):
                            return True
                        continue

                # Give newly created windows time to attach fullscreen capabilities before
                # falling back to visibleFrame maximize.
                if (time.monotonic() - started_fs) < native_grace_sec and (
                    time.monotonic() + 0.16
                ) < deadline:
                    if allow_window_reacquire:
                        newer = self._reacquire_standard_window(cur_win.bundle_id)
                        if newer is not None and newer.window_id != cur_win.window_id:
                            cur_win = newer
                            ax_win = cur_win.native_handle
                    time.sleep(0.14)
                    continue

                # 4. Graceful maximize fallback only when native Full Screen remains unsupported.
                if scr is not None and primary_h > 0.0:
                    self._fill_screen_visible_frame(ax_win, scr, primary_h)
                return True

            time.sleep(0.14)

        _, final_fs = ApplicationServices.AXUIElementCopyAttributeValue(
            ax_win, "AXFullScreen", None
        )
        return bool(final_fs)

    def _reacquire_standard_window(self, bundle_id: str) -> DiscoveredWindow | None:
        discovered = self._window_registry.discover_windows(bundle_id, timeout_sec=0.35)
        for win in discovered:
            if not win.is_modal and not win.is_minimized:
                return win
        return discovered[0] if discovered else None

    @staticmethod
    def _wait_until_ax_fullscreen(
        ax_win, cancelled: Callable[[], bool], wait_sec: float
    ) -> bool:
        end = time.monotonic() + wait_sec
        while time.monotonic() < end:
            if cancelled():
                return False
            err, fs_val = ApplicationServices.AXUIElementCopyAttributeValue(
                ax_win, "AXFullScreen", None
            )
            if err == AX_SUCCESS and bool(fs_val):
                return True
            time.sleep(0.035)
        return False

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
                _, mark = ApplicationServices.AXUIElementCopyAttributeValue(
                    mi, "AXMenuItemMarkChar", None
                )
                ident_str = str(ident or "")
                title_str = str(title or "")
                mark_str = str(mark or "").strip()
                if title_str.startswith(
                    (
                        "Exit Full Screen",
                        "Leave Full Screen",
                        "Opuść tryb pełnoekranowy",
                        "Wyjdź z trybu pełnoekranowego",
                    )
                ):
                    return False
                if mark_str and (
                    ident_str
                    in (
                        "enterFullScreenMode:",
                        "toggleFullScreen:",
                        "toggleFullScreenMode:",
                    )
                    or "Full Screen" in title_str
                    or "pełnoekranow" in title_str.lower()
                    or "pełny ekran" in title_str.lower()
                ):
                    return False
                if ident_str in (
                    "enterFullScreenMode:",
                    "toggleFullScreen:",
                    "toggleFullScreenMode:",
                ) or title_str.startswith(
                    (
                        "Enter Full Screen",
                        "Toggle Full Screen",
                        "Wejdź do trybu pełnoekranowego",
                        "Przełącz tryb pełnoekranowy",
                        "Pełny ekran",
                    )
                ):
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
