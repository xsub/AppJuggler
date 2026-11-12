"""Interactive Assignment Configuration Window ('Edit Assignments')."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import Cocoa
import objc

from appjuggler.contracts import ApplicationRegistry
from appjuggler.domain.models import AppBinding, AppRecord, Layer
from appjuggler.persistence.binding_store import (
    BindingStore,
    CorruptedConfigLockError,
    SlotConflictError,
)
from appjuggler.platforms.macos.ui import theme
from appjuggler.platforms.macos.ui.overlay_panel import KeyboardMapGridView


class _AssignmentWindowDelegate(Cocoa.NSObject):
    def initWithCallbacks_(
        self,
        callbacks: dict[str, Callable],
    ):
        self = objc.super(_AssignmentWindowDelegate, self).init()
        if self is None:
            return None
        self._cb = callbacks
        return self

    def windowWillClose_(self, _notification) -> None:
        self._cb["on_close"]()

    def layerChanged_(self, sender) -> None:
        self._cb["on_layer_changed"](int(sender.selectedSegment()))

    def filterChanged_(self, sender) -> None:
        self._cb["on_filter_changed"](int(sender.selectedSegment()))

    def appPopupChanged_(self, _sender) -> None:
        self._cb["on_app_selected"]()

    def assignClicked_(self, _sender) -> None:
        self._cb["on_assign"]()

    def clearSlotClicked_(self, _sender) -> None:
        self._cb["on_clear_slot"]()

    def browseAppClicked_(self, _sender) -> None:
        self._cb["on_browse_app"]()

    def refreshAppsClicked_(self, _sender) -> None:
        self._cb["on_refresh_apps"]()

    def saveIntervalClicked_(self, _sender) -> None:
        self._cb["on_save_interval"]()

    def capsHoldToggled_(self, _sender) -> None:
        self._cb["on_toggle_caps_hold"]()

    def autoFullscreenToggled_(self, _sender) -> None:
        self._cb["on_toggle_auto_fullscreen"]()

    def repairConfigClicked_(self, _sender) -> None:
        self._cb["on_repair_config"]()

    def quitAppClicked_(self, _sender) -> None:
        self._cb["on_quit_app"]()


class AssignmentEditorController:
    """Manages the normal-activating 'Edit Assignments' configuration window (§2.6)."""

    def __init__(
        self,
        store: BindingStore,
        app_registry: ApplicationRegistry,
        on_enter_editing: Callable[[], None],
        on_exit_editing: Callable[[], None],
        on_config_updated: Callable[[], None],
        on_quit_app: Callable[[], None] | None = None,
    ) -> None:
        self._store = store
        self._app_registry = app_registry
        self._on_enter_editing = on_enter_editing
        self._on_exit_editing = on_exit_editing
        self._on_config_updated = on_config_updated
        self._on_quit_app = on_quit_app or (lambda: Cocoa.NSApp.terminate_(None))

        self._window: Cocoa.NSWindow | None = None
        self._delegate: _AssignmentWindowDelegate | None = None
        self._grid_view: KeyboardMapGridView | None = None

        self._layer: Layer = Layer.BASE
        self._selected_key: str = "T"
        self._show_all_apps: bool = False
        self._candidate_apps: list[AppRecord] = []

        # Controls
        self._layer_seg = None
        self._filter_seg = None
        self._slot_summary_label = None
        self._app_popup = None
        self._path_popup = None
        self._conflict_label = None
        self._interval_field = None
        self._auto_fullscreen_checkbox = None
        self._caps_hold_checkbox = None
        self._status_label = None
        self._repair_btn = None

    def show_window(self) -> None:
        self._on_enter_editing()
        if self._window is None:
            self._build_window()
        self.refresh_data()
        assert self._window is not None
        Cocoa.NSApp.activateIgnoringOtherApps_(True)
        self._window.makeKeyAndOrderFront_(None)

    def is_visible(self) -> bool:
        return bool(self._window is not None and self._window.isVisible())

    def _build_window(self) -> None:
        gw, gh = KeyboardMapGridView.required_size()
        win_w = gw + 40.0
        win_h = gh + 324.0
        style = (
            Cocoa.NSWindowStyleMaskTitled
            | Cocoa.NSWindowStyleMaskClosable
            | Cocoa.NSWindowStyleMaskMiniaturizable
        )
        win = Cocoa.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            Cocoa.NSMakeRect(160.0, 140.0, win_w, win_h),
            style,
            Cocoa.NSBackingStoreBuffered,
            False,
        )
        win.setTitle_("AppJuggler — Edit Assignments")
        win.setReleasedWhenClosed_(False)

        self._delegate = _AssignmentWindowDelegate.alloc().initWithCallbacks_(
            {
                "on_close": self._handle_close,
                "on_layer_changed": self._handle_layer_changed,
                "on_filter_changed": self._handle_filter_changed,
                "on_app_selected": self._handle_app_selected,
                "on_assign": self._handle_assign,
                "on_clear_slot": self._handle_clear_slot,
                "on_browse_app": self._handle_browse_app,
                "on_refresh_apps": self.refresh_data,
                "on_save_interval": self._handle_save_interval,
                "on_toggle_auto_fullscreen": self._handle_toggle_auto_fullscreen,
                "on_toggle_caps_hold": self._handle_toggle_caps_hold,
                "on_repair_config": self._handle_repair_config,
                "on_quit_app": self._on_quit_app,
            }
        )
        win.setDelegate_(self._delegate)

        content = win.contentView()

        # Top row: Layer switcher + instructions
        top_y = win_h - 44.0
        lbl_layer = Cocoa.NSTextField.labelWithString_("Keyboard Layer:")
        lbl_layer.setFrame_(Cocoa.NSMakeRect(20.0, top_y + 2.0, 110.0, 22.0))
        lbl_layer.setFont_(theme.font_ui(13.0, bold=True))
        content.addSubview_(lbl_layer)

        seg = Cocoa.NSSegmentedControl.segmentedControlWithLabels_trackingMode_target_action_(
            ["Base Layer (A–Z)", "Shift Layer (⇧A–Z)"],
            Cocoa.NSSegmentSwitchTrackingSelectOne,
            self._delegate,
            "layerChanged:",
        )
        seg.setSelectedSegment_(0)
        seg.setFrame_(Cocoa.NSMakeRect(132.0, top_y, 290.0, 26.0))
        content.addSubview_(seg)
        self._layer_seg = seg

        hint = Cocoa.NSTextField.labelWithString_(
            "Click any key below to inspect or change its persistent application binding."
        )
        hint.setFrame_(Cocoa.NSMakeRect(438.0, top_y + 2.0, win_w - 458.0, 22.0))
        hint.setTextColor_(Cocoa.NSColor.secondaryLabelColor())
        content.addSubview_(hint)

        # Visual Keyboard Map (dark container frame)
        map_y = win_h - 54.0 - gh
        map_box = Cocoa.NSView.alloc().initWithFrame_(
            Cocoa.NSMakeRect(20.0, map_y, gw, gh)
        )
        map_box.setWantsLayer_(True)
        map_box.layer().setBackgroundColor_(theme.BG_PANEL.CGColor())
        map_box.layer().setCornerRadius_(12.0)

        grid = KeyboardMapGridView.alloc().initWithFrame_(
            Cocoa.NSMakeRect(0.0, 0.0, gw, gh)
        )
        map_box.addSubview_(grid)
        content.addSubview_(map_box)
        self._grid_view = grid

        # Selected slot summary + Clear button
        row1_y = map_y - 40.0
        slot_lbl = Cocoa.NSTextField.labelWithString_("Selected Slot: T (Base)")
        slot_lbl.setFrame_(Cocoa.NSMakeRect(22.0, row1_y + 4.0, win_w - 200.0, 22.0))
        slot_lbl.setFont_(theme.font_ui(13.5, bold=True))
        content.addSubview_(slot_lbl)
        self._slot_summary_label = slot_lbl

        clear_btn = Cocoa.NSButton.buttonWithTitle_target_action_(
            "Clear Slot", self._delegate, "clearSlotClicked:"
        )
        clear_btn.setFrame_(Cocoa.NSMakeRect(win_w - 140.0, row1_y, 120.0, 28.0))
        content.addSubview_(clear_btn)

        # App source filter + App selector popup + Browse + Refresh
        row2_y = row1_y - 42.0
        f_lbl = Cocoa.NSTextField.labelWithString_("Application Source:")
        f_lbl.setFrame_(Cocoa.NSMakeRect(22.0, row2_y + 4.0, 132.0, 22.0))
        content.addSubview_(f_lbl)

        f_seg = Cocoa.NSSegmentedControl.segmentedControlWithLabels_trackingMode_target_action_(
            ["Unassigned Running Apps", "All Running & Installed"],
            Cocoa.NSSegmentSwitchTrackingSelectOne,
            self._delegate,
            "filterChanged:",
        )
        f_seg.setSelectedSegment_(0)
        f_seg.setFrame_(Cocoa.NSMakeRect(156.0, row2_y + 1.0, 340.0, 26.0))
        content.addSubview_(f_seg)
        self._filter_seg = f_seg

        ref_btn = Cocoa.NSButton.buttonWithTitle_target_action_(
            "Refresh List", self._delegate, "refreshAppsClicked:"
        )
        ref_btn.setFrame_(Cocoa.NSMakeRect(508.0, row2_y, 110.0, 28.0))
        content.addSubview_(ref_btn)

        browse_btn = Cocoa.NSButton.buttonWithTitle_target_action_(
            "Choose .app…", self._delegate, "browseAppClicked:"
        )
        browse_btn.setFrame_(Cocoa.NSMakeRect(626.0, row2_y, 125.0, 28.0))
        content.addSubview_(browse_btn)

        # App dropdown + Path hint dropdown + Assign button
        row3_y = row2_y - 42.0
        app_lbl = Cocoa.NSTextField.labelWithString_("Target App:")
        app_lbl.setFrame_(Cocoa.NSMakeRect(22.0, row3_y + 4.0, 90.0, 22.0))
        content.addSubview_(app_lbl)

        popup = Cocoa.NSPopUpButton.alloc().initWithFrame_pullsDown_(
            Cocoa.NSMakeRect(112.0, row3_y, 360.0, 28.0), False
        )
        popup.setTarget_(self._delegate)
        popup.setAction_("appPopupChanged:")
        content.addSubview_(popup)
        self._app_popup = popup

        path_popup = Cocoa.NSPopUpButton.alloc().initWithFrame_pullsDown_(
            Cocoa.NSMakeRect(480.0, row3_y, win_w - 676.0, 28.0), False
        )
        content.addSubview_(path_popup)
        self._path_popup = path_popup

        assign_btn = Cocoa.NSButton.buttonWithTitle_target_action_(
            "Assign to Slot", self._delegate, "assignClicked:"
        )
        assign_btn.setBezelStyle_(Cocoa.NSBezelStyleRounded)
        assign_btn.setKeyEquivalent_("\r")
        assign_btn.setFrame_(Cocoa.NSMakeRect(win_w - 186.0, row3_y, 166.0, 28.0))
        content.addSubview_(assign_btn)

        # Conflict / bundle metadata notice row
        row4_y = row3_y - 28.0
        conf_lbl = Cocoa.NSTextField.labelWithString_("")
        conf_lbl.setFrame_(Cocoa.NSMakeRect(114.0, row4_y, win_w - 134.0, 20.0))
        conf_lbl.setFont_(theme.font_ui(11.5, bold=False))
        conf_lbl.setTextColor_(Cocoa.NSColor.secondaryLabelColor())
        content.addSubview_(conf_lbl)
        self._conflict_label = conf_lbl

        # Options row: Switch Target Window to Full Screen + Optional 2s Caps-hold Quick Assign
        opts_y = 54.0
        fs_chk = Cocoa.NSButton.checkboxWithTitle_target_action_(
            "Switch Target Window to Full Screen",
            self._delegate,
            "autoFullscreenToggled:",
        )
        fs_chk.setFrame_(Cocoa.NSMakeRect(22.0, opts_y, 285.0, 22.0))
        fs_chk.setState_(
            Cocoa.NSControlStateValueOn
            if self._store.auto_fullscreen_on_switch
            else Cocoa.NSControlStateValueOff
        )
        content.addSubview_(fs_chk)
        self._auto_fullscreen_checkbox = fs_chk

        hold_chk = Cocoa.NSButton.checkboxWithTitle_target_action_(
            "Hold Caps 2s Quick Assign",
            self._delegate,
            "capsHoldToggled:",
        )
        hold_chk.setFrame_(Cocoa.NSMakeRect(322.0, opts_y, 215.0, 22.0))
        hold_chk.setState_(
            Cocoa.NSControlStateValueOn
            if self._store.trigger.enable_caps_hold_assign
            else Cocoa.NSControlStateValueOff
        )
        content.addSubview_(hold_chk)
        self._caps_hold_checkbox = hold_chk

        # Bottom row: Double-Caps trigger interval + status feedback + repair + quit
        row5_y = 14.0
        trig_lbl = Cocoa.NSTextField.labelWithString_("Double-Caps (ms):")
        trig_lbl.setFrame_(Cocoa.NSMakeRect(22.0, row5_y + 3.0, 124.0, 22.0))
        content.addSubview_(trig_lbl)

        iv_field = Cocoa.NSTextField.alloc().initWithFrame_(
            Cocoa.NSMakeRect(148.0, row5_y + 2.0, 58.0, 24.0)
        )
        iv_field.setStringValue_(str(self._store.trigger.interval_ms))
        content.addSubview_(iv_field)
        self._interval_field = iv_field

        iv_btn = Cocoa.NSButton.buttonWithTitle_target_action_(
            "Save Timing", self._delegate, "saveIntervalClicked:"
        )
        iv_btn.setFrame_(Cocoa.NSMakeRect(212.0, row5_y, 98.0, 28.0))
        content.addSubview_(iv_btn)

        status_lbl = Cocoa.NSTextField.labelWithString_("")
        status_lbl.setFrame_(Cocoa.NSMakeRect(322.0, row5_y + 3.0, win_w - 480.0, 22.0))
        status_lbl.setFont_(theme.font_ui(12.0, bold=True))
        content.addSubview_(status_lbl)
        self._status_label = status_lbl

        repair_btn = Cocoa.NSButton.buttonWithTitle_target_action_(
            "Backup & Reset Damaged Config", self._delegate, "repairConfigClicked:"
        )
        repair_btn.setFrame_(Cocoa.NSMakeRect(win_w - 368.0, row5_y, 210.0, 28.0))
        repair_btn.setHidden_(True)
        content.addSubview_(repair_btn)
        self._repair_btn = repair_btn

        quit_btn = Cocoa.NSButton.buttonWithTitle_target_action_(
            "Quit AppJuggler", self._delegate, "quitAppClicked:"
        )
        quit_btn.setFrame_(Cocoa.NSMakeRect(win_w - 150.0, row5_y, 130.0, 28.0))
        content.addSubview_(quit_btn)

        self._window = win

    def refresh_data(self) -> None:
        if self._window is None or self._grid_view is None:
            return

        running_records = self._app_registry.running_apps()
        installed_records = self._app_registry.installed_apps()
        running_bids = self._app_registry.running_bundle_ids()
        bindings = self._store.bindings
        bound_bids = {b.bundle_id for b in bindings}

        conflicts = frozenset(
            r.bundle_id
            for r in installed_records
            if r.bundle_id and r.has_duplicate_install_conflict
        )

        self._grid_view.configure(
            active_layer=self._layer,
            bindings=bindings,
            running_bundle_ids=running_bids,
            duplicate_conflicts=conflicts,
            icon_provider=self._app_registry.app_icon_for_bundle,
            selected_key=self._selected_key,
            on_key_clicked=self._handle_key_clicked,
        )

        self._interval_field.setStringValue_(str(self._store.trigger.interval_ms))
        if self._auto_fullscreen_checkbox is not None:
            self._auto_fullscreen_checkbox.setState_(
                Cocoa.NSControlStateValueOn
                if self._store.auto_fullscreen_on_switch
                else Cocoa.NSControlStateValueOff
            )
        if self._caps_hold_checkbox is not None:
            self._caps_hold_checkbox.setState_(
                Cocoa.NSControlStateValueOn
                if self._store.trigger.enable_caps_hold_assign
                else Cocoa.NSControlStateValueOff
            )

        if self._store.load_error:
            self._status_label.setStringValue_("Config file error — protected from overwrite")
            self._status_label.setTextColor_(Cocoa.NSColor.systemRedColor())
            self._repair_btn.setHidden_(False)
        else:
            self._repair_btn.setHidden_(True)

        # Populate candidate apps list
        if not self._show_all_apps:
            # Unassigned running apps (including edge-case processes missing bundle_id)
            self._candidate_apps = [
                r
                for r in running_records
                if r.bundle_id is None or r.bundle_id not in bound_bids
            ]
        else:
            merged = list(installed_records)
            # Include any running processes lacking a bundle_id so edge case is never hidden
            merged.extend([r for r in running_records if r.bundle_id is None])
            self._candidate_apps = merged

        self._app_popup.removeAllItems()
        if not self._candidate_apps:
            self._app_popup.addItemWithTitle_(
                "(No unassigned running apps — switch to 'All Running & Installed')"
            )
            self._app_popup.setEnabled_(False)
        else:
            self._app_popup.setEnabled_(True)
            for rec in self._candidate_apps:
                tags: list[str] = []
                if rec.is_running:
                    tags.append("running")
                if rec.has_missing_bundle_id:
                    tags.append("NO BUNDLE ID — CONFLICT")
                elif rec.has_duplicate_install_conflict:
                    tags.append(f"CONFLICT: {len(rec.candidate_paths)} copies")
                suffix = f"  [{', '.join(tags)}]" if tags else ""
                bid_txt = rec.bundle_id or "missing-bundle-id"
                # NSPopUpButton deduplicates titles with addItemWithTitle_, so use menu().addItem
                title = f"{rec.display_name} ({bid_txt}){suffix}"
                item = Cocoa.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                    title, None, ""
                )
                self._app_popup.menu().addItem_(item)

        self._update_slot_summary()
        self._handle_app_selected()

    def _update_slot_summary(self) -> None:
        prefix = "⇧" if self._layer is Layer.SHIFT else ""
        existing = self._store.get_slot(self._selected_key, self._layer)
        if existing is None:
            text = f"Selected Slot: {prefix}{self._selected_key} ({self._layer.label})  —  [Empty / Available]"
        else:
            paths = self._app_registry.resolve_bundle_urls(
                existing.bundle_id, hint_path=existing.bundle_path
            )
            state_note = ""
            if not paths:
                state_note = "  ⚠️ BUNDLE MISSING ON DISK"
            elif len(paths) > 1:
                state_note = f"  ⚠️ MULTIPLE INSTALLS ({len(paths)} copies)"
            text = (
                f"Selected Slot: {prefix}{self._selected_key} ({self._layer.label})  →  "
                f"{existing.display_name} ({existing.bundle_id}){state_note}"
            )
        self._slot_summary_label.setStringValue_(text)

    def _handle_key_clicked(self, key_char: str) -> None:
        self._selected_key = key_char
        self._update_slot_summary()

    def _handle_layer_changed(self, seg_idx: int) -> None:
        self._layer = Layer.SHIFT if seg_idx == 1 else Layer.BASE
        if self._grid_view is not None:
            self._grid_view.set_active_layer(self._layer)
        self._update_slot_summary()

    def _handle_filter_changed(self, seg_idx: int) -> None:
        self._show_all_apps = seg_idx == 1
        self.refresh_data()

    def _handle_app_selected(self) -> None:
        self._path_popup.removeAllItems()
        if not self._candidate_apps:
            self._path_popup.addItemWithTitle_("(No bundle path)")
            self._path_popup.setEnabled_(False)
            self._conflict_label.setStringValue_("")
            return

        idx = int(self._app_popup.indexOfSelectedItem())
        if idx < 0 or idx >= len(self._candidate_apps):
            return
        rec = self._candidate_apps[idx]

        if rec.has_missing_bundle_id:
            self._path_popup.addItemWithTitle_(rec.bundle_path or "(Unbundled executable)")
            self._path_popup.setEnabled_(False)
            self._conflict_label.setStringValue_(
                "⚠️ Edge-case conflict: This process has no macOS CFBundleIdentifier and cannot be bound persistently without guessing."
            )
            self._conflict_label.setTextColor_(Cocoa.NSColor.systemOrangeColor())
            return

        paths = rec.candidate_paths or ((rec.bundle_path,) if rec.bundle_path else ())
        if not paths:
            self._path_popup.addItemWithTitle_("(Default LaunchServices lookup)")
            self._path_popup.setEnabled_(False)
        else:
            for p in paths:
                item = Cocoa.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                    p, None, ""
                )
                self._path_popup.menu().addItem_(item)
            self._path_popup.setEnabled_(len(paths) > 1)

        if rec.has_duplicate_install_conflict:
            self._conflict_label.setStringValue_(
                f"⚠️ Multiple installations share bundle_id '{rec.bundle_id}'. Select preferred launch bundle path above."
            )
            self._conflict_label.setTextColor_(Cocoa.NSColor.systemOrangeColor())
        else:
            self._conflict_label.setStringValue_(
                f"Bundle ID: {rec.bundle_id}  ·  Path hint: {paths[0] if paths else 'LaunchServices'}"
            )
            self._conflict_label.setTextColor_(Cocoa.NSColor.secondaryLabelColor())

    def _handle_assign(self) -> None:
        if not self._candidate_apps:
            self._set_feedback("Select an application first.", is_error=True)
            return
        idx = int(self._app_popup.indexOfSelectedItem())
        if idx < 0 or idx >= len(self._candidate_apps):
            return
        rec = self._candidate_apps[idx]
        if rec.has_missing_bundle_id or not rec.bundle_id:
            self._show_alert(
                "Cannot Bind Application Without Bundle Identifier",
                f"'{rec.display_name}' does not declare a macOS CFBundleIdentifier. "
                "AppJuggler requires a stable bundle identifier rather than guessing process names.",
            )
            return

        chosen_path: str | None = rec.bundle_path
        if self._path_popup.numberOfItems() > 0:
            p_title = str(self._path_popup.titleOfSelectedItem() or "")
            if p_title.startswith("/"):
                chosen_path = p_title

        new_binding = AppBinding(
            key=self._selected_key,
            layer=self._layer,
            bundle_id=rec.bundle_id,
            display_name=rec.display_name,
            bundle_path=chosen_path,
        )

        try:
            self._store.assign(
                new_binding,
                allow_replace=False,
                remove_previous_for_bundle=False,
            )
        except SlotConflictError as conflict:
            if not self._confirm_replace(conflict.existing, new_binding):
                return
            self._store.assign(
                new_binding,
                allow_replace=True,
                remove_previous_for_bundle=False,
            )
        except CorruptedConfigLockError as exc:
            self._show_alert("Configuration Locked", str(exc))
            return

        prefix = "⇧" if self._layer is Layer.SHIFT else ""
        self._set_feedback(
            f"Saved {prefix}{self._selected_key} → {new_binding.display_name}",
            is_error=False,
        )
        self._on_config_updated()
        self.refresh_data()

    def _handle_clear_slot(self) -> None:
        existing = self._store.get_slot(self._selected_key, self._layer)
        if existing is None:
            self._set_feedback("Slot is already empty.", is_error=False)
            return
        try:
            self._store.remove_slot(self._selected_key, self._layer)
        except CorruptedConfigLockError as exc:
            self._show_alert("Configuration Locked", str(exc))
            return
        prefix = "⇧" if self._layer is Layer.SHIFT else ""
        self._set_feedback(f"Cleared slot {prefix}{self._selected_key}", is_error=False)
        self._on_config_updated()
        self.refresh_data()

    def _handle_browse_app(self) -> None:
        panel = Cocoa.NSOpenPanel.openPanel()
        panel.setCanChooseFiles_(True)
        panel.setCanChooseDirectories_(False)
        panel.setAllowsMultipleSelection_(False)
        panel.setAllowedFileTypes_(["app"])
        panel.setDirectoryURL_(Cocoa.NSURL.fileURLWithPath_("/Applications"))
        if panel.runModal() != Cocoa.NSModalResponseOK:
            return
        url = panel.URL()
        if url is None or not url.path():
            return
        app_path = Path(str(url.path())).resolve()
        bundle = Cocoa.NSBundle.bundleWithPath_(str(app_path))
        raw_bid = bundle.bundleIdentifier() if bundle is not None else None
        if not raw_bid:
            self._show_alert(
                "Missing Bundle Identifier",
                f"'{app_path.name}' does not have a valid CFBundleIdentifier in Info.plist.",
            )
            return
        info = (bundle.localizedInfoDictionary() or bundle.infoDictionary() or {})
        name = str(
            info.get("CFBundleDisplayName")
            or info.get("CFBundleName")
            or app_path.stem
        )
        rec = AppRecord(
            bundle_id=str(raw_bid),
            display_name=name,
            is_running=self._app_registry.is_bundle_running(str(raw_bid)),
            bundle_path=str(app_path),
            candidate_paths=self._app_registry.resolve_bundle_urls(
                str(raw_bid), hint_path=str(app_path)
            ),
        )
        self._candidate_apps.insert(0, rec)
        self._app_popup.setEnabled_(True)
        item = Cocoa.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            f"{rec.display_name} ({rec.bundle_id})", None, ""
        )
        self._app_popup.menu().insertItem_atIndex_(item, 0)
        self._app_popup.selectItemAtIndex_(0)
        self._handle_app_selected()

    def _handle_save_interval(self) -> None:
        raw = str(self._interval_field.stringValue() or "").strip()
        try:
            ms = int(raw)
            self._store.set_trigger_interval_ms(ms)
        except (ValueError, Exception) as exc:
            self._set_feedback(f"Invalid interval: {exc}", is_error=True)
            return
        self._set_feedback(f"Saved double-Caps interval: {ms} ms", is_error=False)
        self._on_config_updated()

    def _handle_toggle_auto_fullscreen(self) -> None:
        if self._auto_fullscreen_checkbox is None:
            return
        enabled = bool(
            self._auto_fullscreen_checkbox.state() == Cocoa.NSControlStateValueOn
        )
        try:
            self._store.set_auto_fullscreen_on_switch(enabled)
        except Exception as exc:
            self._set_feedback(f"Could not save option: {exc}", is_error=True)
            return
        state_txt = "enabled" if enabled else "disabled"
        self._set_feedback(
            f"Switch Target Window to Full Screen {state_txt}", is_error=False
        )
        self._on_config_updated()

    def _handle_toggle_caps_hold(self) -> None:
        if self._caps_hold_checkbox is None:
            return
        enabled = bool(self._caps_hold_checkbox.state() == Cocoa.NSControlStateValueOn)
        try:
            self._store.set_caps_hold_assign_enabled(enabled)
        except Exception as exc:
            self._set_feedback(f"Could not save option: {exc}", is_error=True)
            return
        state_txt = "enabled" if enabled else "disabled"
        self._set_feedback(f"2s Caps-hold Quick Assign {state_txt}", is_error=False)
        self._on_config_updated()

    def _handle_repair_config(self) -> None:
        backup = self._store.backup_corrupted_and_reset()
        self._on_config_updated()
        self.refresh_data()
        self._set_feedback(
            f"Backed up damaged file to {backup.name if backup else 'disk'}",
            is_error=False,
        )

    def _handle_close(self) -> None:
        self._on_exit_editing()

    def _set_feedback(self, msg: str, is_error: bool) -> None:
        if self._status_label is None:
            return
        self._status_label.setStringValue_(msg)
        self._status_label.setTextColor_(
            Cocoa.NSColor.systemRedColor()
            if is_error
            else Cocoa.NSColor.systemGreenColor()
        )

    @staticmethod
    def _confirm_replace(existing: AppBinding, incoming: AppBinding) -> bool:
        prefix = "⇧" if existing.layer is Layer.SHIFT else ""
        alert = Cocoa.NSAlert.alloc().init()
        alert.setMessageText_(f"Replace slot {prefix}{existing.key}?")
        alert.setInformativeText_(
            f"Slot {prefix}{existing.key} ({existing.layer.label}) is currently assigned to "
            f"'{existing.display_name}' ({existing.bundle_id}).\n\n"
            f"Replace it with '{incoming.display_name}' ({incoming.bundle_id})?"
        )
        alert.addButtonWithTitle_("Replace Assignment")
        alert.addButtonWithTitle_("Cancel")
        return alert.runModal() == Cocoa.NSAlertFirstButtonReturn

    @staticmethod
    def _show_alert(title: str, message: str) -> None:
        alert = Cocoa.NSAlert.alloc().init()
        alert.setAlertStyle_(Cocoa.NSAlertStyleWarning)
        alert.setMessageText_(title)
        alert.setInformativeText_(message)
        alert.addButtonWithTitle_("OK")
        alert.runModal()
