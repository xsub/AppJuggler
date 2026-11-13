"""macOS System Status Bar (menu bar) item and Permissions Diagnostics dialog."""

from __future__ import annotations

from typing import Callable

import Cocoa
import objc

from appjuggler.contracts import PermissionMonitor, PermissionStatus


class _StatusMenuTarget(Cocoa.NSObject):
    def initWithCallbacks_(self, callbacks: dict[str, Callable[[], None]]):
        self = objc.super(_StatusMenuTarget, self).init()
        if self is None:
            return None
        self._cb = callbacks
        return self

    def openNavigator_(self, _sender) -> None:
        self._cb["open_navigator"]()

    def editAssignments_(self, _sender) -> None:
        self._cb["edit_assignments"]()

    def showPermissions_(self, _sender) -> None:
        self._cb["show_permissions"]()

    def reloadConfig_(self, _sender) -> None:
        self._cb["reload_config"]()

    def toggleAutoFullscreen_(self, _sender) -> None:
        self._cb["toggle_auto_fullscreen"]()

    def toggleAutoMaximize_(self, _sender) -> None:
        self._cb["toggle_auto_maximize"]()

    def toggleMouseSelection_(self, _sender) -> None:
        self._cb["toggle_mouse_selection"]()

    def openAnimationsWindow_(self, _sender) -> None:
        self._cb["open_animations"]()

    def quitApp_(self, _sender) -> None:
        self._cb["quit_app"]()


class StatusBarController:
    """Owns the persistent macOS menu bar extra and permission repair dialog."""

    def __init__(
        self,
        permission_monitor: PermissionMonitor,
        on_open_navigator: Callable[[], None],
        on_edit_assignments: Callable[[], None],
        on_reload_config: Callable[[], None],
        on_quit: Callable[[], None],
        on_toggle_auto_fullscreen: Callable[[], None] | None = None,
        on_toggle_auto_maximize: Callable[[], None] | None = None,
        on_toggle_mouse_selection: Callable[[], None] | None = None,
        on_open_animations: Callable[[], None] | None = None,
    ) -> None:
        self._permission_monitor = permission_monitor
        self._target = _StatusMenuTarget.alloc().initWithCallbacks_(
            {
                "open_navigator": on_open_navigator,
                "edit_assignments": on_edit_assignments,
                "show_permissions": self.show_permissions_dialog,
                "reload_config": on_reload_config,
                "toggle_auto_fullscreen": on_toggle_auto_fullscreen or (lambda: None),
                "toggle_auto_maximize": on_toggle_auto_maximize or (lambda: None),
                "toggle_mouse_selection": on_toggle_mouse_selection or (lambda: None),
                "open_animations": on_open_animations or (lambda: None),
                "quit_app": on_quit,
            }
        )
        self._status_item = None
        self._state_menu_item = None
        self._unassigned_menu_item = None
        self._auto_fs_menu_item = None
        self._auto_max_menu_item = None
        self._mouse_sel_menu_item = None
        self._last_rendered_snapshot: tuple[bool, bool, int, bool, bool, bool] | None = None
        self._last_icon_ready: bool | None = None

    def install(self) -> None:
        bar = Cocoa.NSStatusBar.systemStatusBar()
        # Use compact square length (28pt vs 48pt text) so notched MacBook Pros
        # with crowded menu bars fit the icon before clipping behind the camera housing.
        item = bar.statusItemWithLength_(Cocoa.NSSquareStatusItemLength)
        btn = item.button()
        if btn is not None:
            self._apply_button_icon(btn, ready=True)
            btn.setToolTip_(
                "AppJuggler — Caps×2: Navigate | Caps×2 then ',': Edit Assignments"
            )

        menu = Cocoa.NSMenu.alloc().initWithTitle_("AppJuggler")

        state_item = Cocoa.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Status: Starting…", None, ""
        )
        state_item.setEnabled_(False)
        menu.addItem_(state_item)
        self._state_menu_item = state_item

        unassigned_item = Cocoa.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Unassigned running apps: 0", None, ""
        )
        unassigned_item.setEnabled_(False)
        menu.addItem_(unassigned_item)
        self._unassigned_menu_item = unassigned_item

        menu.addItem_(Cocoa.NSMenuItem.separatorItem())

        nav_item = Cocoa.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Open Navigator Panel (Caps Lock ×2)", "openNavigator:", ""
        )
        nav_item.setTarget_(self._target)
        menu.addItem_(nav_item)

        edit_item = Cocoa.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Edit Assignments…", "editAssignments:", ","
        )
        edit_item.setTarget_(self._target)
        menu.addItem_(edit_item)

        max_item = Cocoa.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Instant Maximize Target Window (0 ms)", "toggleAutoMaximize:", ""
        )
        max_item.setTarget_(self._target)
        menu.addItem_(max_item)
        self._auto_max_menu_item = max_item

        fs_item = Cocoa.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Switch Target Window to Full Screen", "toggleAutoFullscreen:", ""
        )
        fs_item.setTarget_(self._target)
        menu.addItem_(fs_item)
        self._auto_fs_menu_item = fs_item

        mouse_item = Cocoa.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Mouse Hover & Click in HUD", "toggleMouseSelection:", ""
        )
        mouse_item.setTarget_(self._target)
        menu.addItem_(mouse_item)
        self._mouse_sel_menu_item = mouse_item

        anim_item = Cocoa.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Disable Window Animations…", "openAnimationsWindow:", ""
        )
        anim_item.setTarget_(self._target)
        menu.addItem_(anim_item)

        perm_item = Cocoa.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Permissions & Diagnostics…", "showPermissions:", ""
        )
        perm_item.setTarget_(self._target)
        menu.addItem_(perm_item)

        reload_item = Cocoa.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Reload Configuration", "reloadConfig:", "r"
        )
        reload_item.setTarget_(self._target)
        menu.addItem_(reload_item)

        menu.addItem_(Cocoa.NSMenuItem.separatorItem())

        quit_item = Cocoa.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Quit AppJuggler", "quitApp:", "q"
        )
        quit_item.setTarget_(self._target)
        menu.addItem_(quit_item)

        item.setMenu_(menu)
        self._status_item = item

    @staticmethod
    def _apply_button_icon(btn, ready: bool) -> None:
        symbol_name = "keyboard" if ready else "exclamationmark.triangle"
        img = Cocoa.NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            symbol_name, "AppJuggler"
        )
        if img is not None:
            img.setTemplate_(True)
            btn.setImage_(img)
            btn.setTitle_("")
        else:
            btn.setTitle_("⌨" if ready else "⚠")

    def update_status(
        self,
        perm_status: PermissionStatus,
        unassigned_count: int,
        auto_fullscreen: bool = False,
        auto_maximize: bool = False,
        mouse_selection: bool = False,
    ) -> None:
        if self._status_item is None:
            return
        snap = (
            bool(perm_status.is_ready),
            bool(perm_status.screen_locked),
            int(unassigned_count),
            bool(auto_fullscreen),
            bool(auto_maximize),
            bool(mouse_selection),
        )
        if snap == self._last_rendered_snapshot:
            return
        self._last_rendered_snapshot = snap

        btn = self._status_item.button()
        if btn is not None and self._last_icon_ready != perm_status.is_ready:
            self._last_icon_ready = perm_status.is_ready
            self._apply_button_icon(btn, ready=perm_status.is_ready)

        if self._auto_max_menu_item is not None:
            self._auto_max_menu_item.setState_(
                Cocoa.NSControlStateValueOn
                if auto_maximize
                else Cocoa.NSControlStateValueOff
            )

        if self._auto_fs_menu_item is not None:
            self._auto_fs_menu_item.setState_(
                Cocoa.NSControlStateValueOn
                if auto_fullscreen
                else Cocoa.NSControlStateValueOff
            )

        if self._mouse_sel_menu_item is not None:
            self._mouse_sel_menu_item.setState_(
                Cocoa.NSControlStateValueOn
                if mouse_selection
                else Cocoa.NSControlStateValueOff
            )

        if self._state_menu_item is not None:
            if perm_status.is_ready:
                self._state_menu_item.setTitle_(
                    "Ready · Caps×2 Jump · ',' Edit Assignments"
                )
            elif perm_status.screen_locked:
                self._state_menu_item.setTitle_("Paused · Screen Locked")
            else:
                self._state_menu_item.setTitle_(
                    "⚠️ Paused · Accessibility Permission Needed"
                )

        if self._unassigned_menu_item is not None:
            self._unassigned_menu_item.setTitle_(
                f"Unassigned running apps: {unassigned_count}"
            )

    def show_permissions_dialog(self, prompt_system: bool = False) -> None:
        status = self._permission_monitor.check_status(prompt=prompt_system)
        Cocoa.NSApp.activateIgnoringOtherApps_(True)

        alert = Cocoa.NSAlert.alloc().init()
        if status.is_ready:
            alert.setAlertStyle_(Cocoa.NSAlertStyleInformational)
            alert.setMessageText_("AppJuggler Permissions Ready")
            alert.setInformativeText_(
                f"• Accessibility API: Granted\n"
                f"• Active Keyboard Tap (CGEventTap): Active\n"
                f"• Target Identity: {status.executable_description}\n\n"
                "Double-tap Caps Lock at any time to open the keyboard navigator."
            )
            alert.addButtonWithTitle_("OK")
            alert.runModal()
            return

        alert.setAlertStyle_(Cocoa.NSAlertStyleWarning)
        alert.setMessageText_("Permissions Required for Keyboard Navigation")
        remediation = "\n".join(f"• {s}" for s in status.remediation_steps)
        alert.setInformativeText_(
            f"AppJuggler pauses keyboard capture when macOS permissions are missing "
            f"(it never fakes active state or buffers keystrokes).\n\n"
            f"• Accessibility Trusted: {'Yes' if status.accessibility_trusted else 'No'}\n"
            f"• Active Keyboard Tap: {'Yes' if status.input_tap_available else 'No'}\n"
            f"• Process Identity: {status.executable_description}\n\n"
            f"How to enable:\n{remediation}\n\n"
            "AppJuggler will detect the permission automatically within 2 seconds once enabled."
        )
        alert.addButtonWithTitle_("Open Accessibility Settings")
        alert.addButtonWithTitle_("Open Input Monitoring")
        alert.addButtonWithTitle_("Later")

        choice = alert.runModal()
        if choice == Cocoa.NSAlertFirstButtonReturn:
            self._permission_monitor.check_status(prompt=True)
            self._permission_monitor.open_accessibility_settings()
        elif choice == Cocoa.NSAlertSecondButtonReturn:
            self._permission_monitor.open_input_monitoring_settings()
