"""Non-activating HUD NSPanel for keyboard map, window list, Quick Assign, and banners."""

from __future__ import annotations

from typing import Callable

import Cocoa
import objc

from appjuggler.contracts import ApplicationRegistry, OverlayPresenter
from appjuggler.domain.models import (
    ANSI_QWERTY_ROWS,
    AppBinding,
    AppRecord,
    Layer,
    WindowSlot,
)
from appjuggler.platforms.macos.ui import theme

# Keycap geometry (staggered ANSI QWERTY)
KEY_W = 84.0
KEY_H = 80.0
KEY_GAP_X = 8.0
KEY_GAP_Y = 8.0
ROW_STAGGER: tuple[float, float, float] = (0.0, 26.0, 68.0)

# Always-on-top modal HUD level & Space behavior (surfaces above Full Screen Spaces,
# floating utility panels, and popups without NSWindowCollectionBehaviorStationary).
HUD_WINDOW_LEVEL = int(Cocoa.NSScreenSaverWindowLevel) - 1
HUD_COLLECTION_BEHAVIOR = (
    Cocoa.NSWindowCollectionBehaviorCanJoinAllSpaces
    | Cocoa.NSWindowCollectionBehaviorFullScreenAuxiliary
    | Cocoa.NSWindowCollectionBehaviorIgnoresCycle
)


class KeyboardMapGridView(Cocoa.NSView):
    """Renders the 3-row ANSI QWERTY keyboard map; optionally clickable for slot editing."""

    def initWithFrame_(self, frame):
        self = objc.super(KeyboardMapGridView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._layer: Layer = Layer.BASE
        self._bindings_by_slot: dict[tuple[str, Layer], AppBinding] = {}
        self._running_bids: frozenset[str] = frozenset()
        self._conflict_bids: frozenset[str] = frozenset()
        self._icon_provider: Callable[[str, str | None], object | None] | None = None
        self._selected_key: str | None = None
        self._hovered_key: str | None = None
        self._on_key_clicked: Callable[[str], None] | None = None
        self._mouse_interactive: bool = True
        self._select_on_click: bool = True
        self._tracking_area = None
        self._key_rects: dict[str, Cocoa.NSRect] = {}
        return self

    def isFlipped(self) -> bool:
        return True

    def acceptsFirstMouse_(self, _event) -> bool:
        return bool(self._mouse_interactive and self._on_key_clicked is not None)

    def updateTrackingAreas(self) -> None:
        objc.super(KeyboardMapGridView, self).updateTrackingAreas()
        if self._tracking_area is not None:
            self.removeTrackingArea_(self._tracking_area)
            self._tracking_area = None
        if self._mouse_interactive and self._on_key_clicked is not None:
            opts = (
                Cocoa.NSTrackingMouseMoved
                | Cocoa.NSTrackingMouseEnteredAndExited
                | Cocoa.NSTrackingActiveAlways
                | Cocoa.NSTrackingInVisibleRect
            )
            area = Cocoa.NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
                Cocoa.NSZeroRect, opts, self, None
            )
            self.addTrackingArea_(area)
            self._tracking_area = area

    @objc.python_method
    def configure(
        self,
        *,
        active_layer: Layer,
        bindings: tuple[AppBinding, ...],
        running_bundle_ids: frozenset[str],
        duplicate_conflicts: frozenset[str] = frozenset(),
        icon_provider: Callable[[str, str | None], object | None] | None = None,
        selected_key: str | None = None,
        on_key_clicked: Callable[[str], None] | None = None,
        mouse_interactive: bool = True,
        select_on_click: bool = True,
    ) -> None:
        self._layer = active_layer
        self._bindings_by_slot = {b.slot_key: b for b in bindings}
        self._running_bids = running_bundle_ids
        self._conflict_bids = duplicate_conflicts
        if icon_provider is not None:
            self._icon_provider = icon_provider
        self._selected_key = selected_key
        self._on_key_clicked = on_key_clicked
        self._mouse_interactive = bool(mouse_interactive)
        self._select_on_click = bool(select_on_click)
        if not self._mouse_interactive or self._on_key_clicked is None:
            self._hovered_key = None
        self.updateTrackingAreas()
        self.setNeedsDisplay_(True)

    @objc.python_method
    def set_active_layer(self, layer: Layer) -> None:
        self._layer = layer
        self.setNeedsDisplay_(True)

    @objc.python_method
    def set_selected_key(self, key: str | None) -> None:
        self._selected_key = key.upper() if key else None
        self.setNeedsDisplay_(True)

    @objc.python_method
    def set_mouse_interactive(
        self,
        enabled: bool,
        on_key_clicked: Callable[[str], None] | None = None,
    ) -> None:
        self._mouse_interactive = bool(enabled)
        self._on_key_clicked = on_key_clicked if enabled else None
        if not self._mouse_interactive or self._on_key_clicked is None:
            self._hovered_key = None
        self.updateTrackingAreas()
        self.setNeedsDisplay_(True)

    @staticmethod
    def required_size() -> tuple[float, float]:
        width = 10 * KEY_W + 9 * KEY_GAP_X + 24.0
        height = 3 * KEY_H + 2 * KEY_GAP_Y + 16.0
        return width, height

    def drawRect_(self, _dirty_rect) -> None:
        self._key_rects.clear()
        start_x = 12.0
        start_y = 8.0

        for row_idx, keys in enumerate(ANSI_QWERTY_ROWS):
            stagger = ROW_STAGGER[row_idx]
            y = start_y + row_idx * (KEY_H + KEY_GAP_Y)
            for col_idx, key_char in enumerate(keys):
                x = start_x + stagger + col_idx * (KEY_W + KEY_GAP_X)
                rect = Cocoa.NSMakeRect(x, y, KEY_W, KEY_H)
                self._key_rects[key_char] = rect
                self._draw_keycap(key_char, rect)

    @objc.python_method
    def _draw_keycap(self, key_char: str, rect: Cocoa.NSRect) -> None:
        binding = self._bindings_by_slot.get((key_char, self._layer))
        is_selected = self._selected_key == key_char
        is_hovered = (
            self._mouse_interactive
            and self._on_key_clicked is not None
            and self._hovered_key == key_char
            and not is_selected
        )
        is_running = (
            binding is not None and binding.bundle_id in self._running_bids
        )
        has_conflict = (
            binding is not None and binding.bundle_id in self._conflict_bids
        )

        if is_selected:
            bg = theme.KEY_SELECTED_BG
            border = theme.KEY_SELECTED_BORDER
            lw = 2.0
        elif is_hovered and binding is not None:
            bg = theme.KEY_SELECTED_BG
            border = theme.KEY_SELECTED_BORDER
            lw = 1.8
        elif is_hovered:
            bg = theme.rgba(52, 62, 82, 0.72)
            border = theme.ACCENT_BLUE
            lw = 1.5
        elif binding is None:
            bg = theme.KEY_UNASSIGNED_BG
            border = theme.KEY_UNASSIGNED_BORDER
            lw = 1.0
        elif is_running:
            bg = theme.KEY_RUNNING_BG
            border = theme.KEY_RUNNING_BORDER
            lw = 1.4
        else:
            bg = theme.KEY_CLOSED_BG
            border = theme.KEY_CLOSED_BORDER
            lw = 1.0

        theme.fill_round_rect(rect, 10.0, bg, border, lw)

        label = f"⇧{key_char}" if self._layer is Layer.SHIFT else key_char
        label_color = (
            theme.ACCENT_SHIFT
            if self._layer is Layer.SHIFT and binding is not None
            else (theme.TEXT_PRIMARY if binding is not None else theme.TEXT_MUTED)
        )
        theme.draw_text(
            label,
            Cocoa.NSMakeRect(rect.origin.x + 8.0, rect.origin.y + 5.0, 36.0, 16.0),
            theme.font_mono(12.0, bold=True),
            label_color,
        )

        if binding is None:
            return

        dot_rect = Cocoa.NSMakeRect(
            rect.origin.x + KEY_W - 15.0, rect.origin.y + 8.0, 7.5, 7.5
        )
        dot_color = theme.STATUS_RUNNING if is_running else theme.STATUS_CLOSED
        theme.fill_round_rect(dot_rect, 3.75, dot_color)

        if has_conflict:
            theme.draw_text(
                "!",
                Cocoa.NSMakeRect(
                    rect.origin.x + KEY_W - 28.0, rect.origin.y + 4.0, 12.0, 14.0
                ),
                theme.font_mono(11.0, bold=True),
                theme.STATUS_WARNING,
                align=Cocoa.NSTextAlignmentCenter,
            )

        if not binding.show_window_picker:
            tilde_x = rect.origin.x + KEY_W - (40.0 if has_conflict else 29.0)
            theme.draw_text(
                "~",
                Cocoa.NSMakeRect(tilde_x, rect.origin.y + 4.0, 13.0, 14.0),
                theme.font_mono(11.5, bold=True),
                theme.ACCENT_BLUE,
                align=Cocoa.NSTextAlignmentCenter,
            )

        icon = (
            self._icon_provider(binding.bundle_id, binding.bundle_path)
            if self._icon_provider is not None
            else None
        )
        if icon is not None:
            icon_size = 32.0
            icon_rect = Cocoa.NSMakeRect(
                rect.origin.x + (KEY_W - icon_size) / 2.0,
                rect.origin.y + 21.0,
                icon_size,
                icon_size,
            )
            alpha = 1.0 if is_running else 0.40
            icon.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
                icon_rect,
                Cocoa.NSZeroRect,
                Cocoa.NSCompositingOperationSourceOver,
                alpha,
                True,
                None,
            )

        name_color = theme.TEXT_PRIMARY if is_running else theme.TEXT_SECONDARY
        theme.draw_text(
            binding.display_name,
            Cocoa.NSMakeRect(
                rect.origin.x + 4.0, rect.origin.y + KEY_H - 20.0, KEY_W - 8.0, 16.0
            ),
            theme.font_ui(10.5, bold=is_running),
            name_color,
            align=Cocoa.NSTextAlignmentCenter,
        )

    @objc.python_method
    def _hit_test_key(self, event) -> str | None:
        pt = self.convertPoint_fromView_(event.locationInWindow(), None)
        for key_char, rect in self._key_rects.items():
            if Cocoa.NSPointInRect(pt, rect):
                return key_char
        return None

    def mouseMoved_(self, event) -> None:
        if not self._mouse_interactive or self._on_key_clicked is None:
            return
        hovered = self._hit_test_key(event)
        if hovered != self._hovered_key:
            self._hovered_key = hovered
            self.setNeedsDisplay_(True)

    def mouseEntered_(self, event) -> None:
        self.mouseMoved_(event)

    def mouseExited_(self, _event) -> None:
        if self._hovered_key is not None:
            self._hovered_key = None
            self.setNeedsDisplay_(True)

    def mouseDown_(self, event) -> None:
        if not self._mouse_interactive or self._on_key_clicked is None:
            return
        key_char = self._hit_test_key(event)
        if key_char is not None:
            if self._select_on_click:
                self._selected_key = key_char
                self.setNeedsDisplay_(True)
            self._on_key_clicked(key_char)


class _NonActivatingHUDPanel(Cocoa.NSPanel):
    """Borderless non-activating NSPanel capable of appearing over native fullscreen Spaces."""

    def canBecomeKeyWindow(self) -> bool:
        return False

    def canBecomeMainWindow(self) -> bool:
        return False


class _HUDContentView(Cocoa.NSView):
    """Main HUD canvas hosting header, keyboard map, window list, Quick Assign, or banners."""

    def initWithFrame_(self, frame):
        self = objc.super(_HUDContentView, self).initWithFrame_(frame)
        if self is None:
            return None
        # Modes: "map" | "windows" | "launching" | "banner" | "assign_apps" | "assign_keymap"
        self.mode: str = "map"
        self.active_layer: Layer = Layer.BASE
        self.selected_binding: AppBinding | None = None
        self.slots: tuple[WindowSlot, ...] = ()
        self.selected_slot: int | None = None
        self.notice: str | None = None
        self.banner_level: str = "info"
        self.banner_message: str = ""
        self.icon_provider: Callable[[str, str | None], object | None] | None = None

        # Optional Quick Assign state (disabled by default)
        self.quick_assign_enabled: bool = False
        self.assign_apps: tuple[AppRecord, ...] = ()
        self.assign_selected_index: int = 0
        self.assign_bindings: tuple[AppBinding, ...] = ()
        self.assign_target_app: AppRecord | None = None
        self.assign_conflict_existing: AppBinding | None = None

        # Optional Mouse Hover & Click + Direct Jump + Window Dismiss state
        self.mouse_enabled: bool = False
        self.direct_jump_active: bool = False
        self.on_layer_pill_clicked: Callable[[], None] | None = None
        self.on_direct_jump_pill_clicked: Callable[[], None] | None = None
        self.on_window_slot_hovered: Callable[[int], None] | None = None
        self.on_window_slot_clicked: Callable[[int], None] | None = None
        self.on_window_slot_dismissed: Callable[[int], None] | None = None
        self.on_window_scroll: Callable[[int], None] | None = None
        self._tracking_area = None
        self._layer_pill_rect: Cocoa.NSRect | None = None
        self._direct_jump_pill_rect: Cocoa.NSRect | None = None
        self._window_row_rects: dict[int, Cocoa.NSRect] = {}
        self._window_dismiss_rects: dict[int, Cocoa.NSRect] = {}
        self._hovered_dismiss_slot: int | None = None
        self._scroll_accum: float = 0.0

        self.grid_view = KeyboardMapGridView.alloc().initWithFrame_(Cocoa.NSZeroRect)
        self.addSubview_(self.grid_view)
        return self

    def isFlipped(self) -> bool:
        return True

    def acceptsFirstMouse_(self, _event) -> bool:
        return bool(self.mouse_enabled or self.mode == "windows")

    def updateTrackingAreas(self) -> None:
        objc.super(_HUDContentView, self).updateTrackingAreas()
        if self._tracking_area is not None:
            self.removeTrackingArea_(self._tracking_area)
            self._tracking_area = None
        if self.mouse_enabled or self.mode == "windows":
            opts = (
                Cocoa.NSTrackingMouseMoved
                | Cocoa.NSTrackingMouseEnteredAndExited
                | Cocoa.NSTrackingActiveAlways
                | Cocoa.NSTrackingInVisibleRect
            )
            area = Cocoa.NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
                Cocoa.NSZeroRect, opts, self, None
            )
            self.addTrackingArea_(area)
            self._tracking_area = area

    @staticmethod
    def max_visible_window_rows() -> int:
        mouse_pt = Cocoa.NSEvent.mouseLocation()
        target_screen = None
        for scr in Cocoa.NSScreen.screens():
            if Cocoa.NSPointInRect(mouse_pt, scr.frame()):
                target_screen = scr
                break
        if target_screen is None:
            target_screen = Cocoa.NSScreen.mainScreen()
        avail_h = (
            float(target_screen.visibleFrame().size.height)
            if target_screen is not None
            else 900.0
        )
        return min(16, max(9, int((avail_h - 140.0) // 46.0)))

    def drawRect_(self, _dirty_rect) -> None:
        self._layer_pill_rect = None
        self._direct_jump_pill_rect = None
        self._window_row_rects.clear()
        self._window_dismiss_rects.clear()
        bounds = self.bounds()
        theme.fill_round_rect(
            Cocoa.NSInsetRect(bounds, 1.0, 1.0),
            16.0,
            theme.BG_PANEL,
            theme.BG_PANEL_BORDER,
            1.2,
        )

        if self.mode == "map":
            self._draw_map_chrome(bounds)
        elif self.mode == "assign_keymap":
            self._draw_assign_keymap_chrome(bounds)
        elif self.mode == "assign_apps":
            self._draw_assign_apps_list(bounds)
        elif self.mode == "windows":
            self._draw_window_list(bounds)
        elif self.mode == "launching":
            self._draw_launching(bounds)
        elif self.mode == "banner":
            self._draw_banner(bounds)

    @objc.python_method
    def _draw_map_chrome(self, bounds: Cocoa.NSRect) -> None:
        theme.draw_text(
            "AppJuggler",
            Cocoa.NSMakeRect(22.0, 14.0, 110.0, 22.0),
            theme.font_ui(15.0, bold=True),
            theme.TEXT_PRIMARY,
        )

        is_shift = self.active_layer is Layer.SHIFT
        pill_rect = Cocoa.NSMakeRect(130.0, 13.0, 162.0, 22.0)
        self._layer_pill_rect = pill_rect
        pill_bg = (
            theme.rgba(245, 166, 35, 0.22)
            if is_shift
            else theme.rgba(88, 154, 255, 0.20)
        )
        pill_border = theme.ACCENT_SHIFT if is_shift else theme.ACCENT_BLUE
        theme.fill_round_rect(pill_rect, 11.0, pill_bg, pill_border, 1.0)
        theme.draw_text(
            "SHIFT LAYER · ⇧A–Z" if is_shift else "BASE LAYER · A–Z",
            Cocoa.NSMakeRect(135.0, 16.0, 152.0, 16.0),
            theme.font_mono(11.0, bold=True),
            pill_border,
            align=Cocoa.NSTextAlignmentCenter,
        )

        dj_rect = Cocoa.NSMakeRect(300.0, 13.0, 138.0, 22.0)
        self._direct_jump_pill_rect = dj_rect
        if self.direct_jump_active:
            dj_bg = theme.rgba(52, 211, 153, 0.24)
            dj_border = theme.STATUS_RUNNING
            dj_fg = theme.STATUS_RUNNING
            dj_text = "~ DIRECT JUMP ON"
        else:
            dj_bg = theme.rgba(148, 163, 184, 0.12)
            dj_border = theme.rgba(148, 163, 184, 0.42)
            dj_fg = theme.TEXT_SECONDARY
            dj_text = "~ Direct Jump"
        theme.fill_round_rect(dj_rect, 11.0, dj_bg, dj_border, 1.0)
        theme.draw_text(
            dj_text,
            Cocoa.NSMakeRect(305.0, 16.0, 128.0, 16.0),
            theme.font_mono(10.5, bold=True),
            dj_fg,
            align=Cocoa.NSTextAlignmentCenter,
        )

        hint_parts: list[str] = []
        if self.mouse_enabled:
            hint_parts.append("Click/A–Z")
        hint_parts.append("⇧ Layer 2")
        if self.quick_assign_enabled:
            hint_parts.append("Tab Assign")
        hint_parts.extend([", Edit Assignments", "Esc"])
        header_hint = "  ·  ".join(hint_parts)
        theme.draw_text(
            header_hint,
            Cocoa.NSMakeRect(446.0, 16.0, bounds.size.width - 468.0, 18.0),
            theme.font_ui(11.5, bold=False),
            theme.TEXT_SECONDARY,
            align=Cocoa.NSTextAlignmentRight,
        )

        if self.notice:
            notice_rect = Cocoa.NSMakeRect(
                20.0, bounds.size.height - 36.0, bounds.size.width - 40.0, 24.0
            )
            theme.fill_round_rect(
                notice_rect,
                7.0,
                theme.rgba(251, 191, 36, 0.14),
                theme.rgba(251, 191, 36, 0.45),
                1.0,
            )
            theme.draw_text(
                self.notice,
                Cocoa.NSMakeRect(
                    30.0, bounds.size.height - 32.0, bounds.size.width - 60.0, 18.0
                ),
                theme.font_ui(12.0, bold=True),
                theme.STATUS_WARNING,
                align=Cocoa.NSTextAlignmentCenter,
            )

    @objc.python_method
    def _draw_assign_keymap_chrome(self, bounds: Cocoa.NSRect) -> None:
        app = self.assign_target_app
        app_name = app.display_name if app else "App"

        if app and app.bundle_id and self.icon_provider:
            icon = self.icon_provider(app.bundle_id, app.bundle_path)
            if icon is not None:
                icon.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
                    Cocoa.NSMakeRect(20.0, 11.0, 26.0, 26.0),
                    Cocoa.NSZeroRect,
                    Cocoa.NSCompositingOperationSourceOver,
                    1.0,
                    True,
                    None,
                )

        theme.draw_text(
            f"Assign '{app_name}'  →  Press Key A–Z",
            Cocoa.NSMakeRect(54.0, 14.0, 360.0, 22.0),
            theme.font_ui(15.0, bold=True),
            theme.STATUS_RUNNING,
        )

        is_shift = self.active_layer is Layer.SHIFT
        pill_rect = Cocoa.NSMakeRect(420.0, 13.0, 168.0, 22.0)
        self._layer_pill_rect = pill_rect
        pill_bg = (
            theme.rgba(245, 166, 35, 0.22)
            if is_shift
            else theme.rgba(88, 154, 255, 0.20)
        )
        pill_border = theme.ACCENT_SHIFT if is_shift else theme.ACCENT_BLUE
        theme.fill_round_rect(pill_rect, 11.0, pill_bg, pill_border, 1.0)
        theme.draw_text(
            "SHIFT LAYER · ⇧A–Z" if is_shift else "BASE LAYER · A–Z",
            Cocoa.NSMakeRect(426.0, 16.0, 156.0, 16.0),
            theme.font_mono(11.0, bold=True),
            pill_border,
            align=Cocoa.NSTextAlignmentCenter,
        )

        theme.draw_text(
            "Hold ⇧ Shift for Layer 2  ·  Esc Cancel",
            Cocoa.NSMakeRect(600.0, 16.0, bounds.size.width - 622.0, 18.0),
            theme.font_ui(12.0, bold=False),
            theme.TEXT_SECONDARY,
            align=Cocoa.NSTextAlignmentRight,
        )

        if self.assign_conflict_existing is not None:
            ex = self.assign_conflict_existing
            prefix = "⇧" if ex.layer is Layer.SHIFT else ""
            msg = (
                f"Slot {prefix}{ex.key} is currently assigned to '{ex.display_name}'. "
                f"Press {ex.key} again (or Enter) to replace with '{app_name}', or press another key."
            )
            notice_rect = Cocoa.NSMakeRect(
                20.0, bounds.size.height - 38.0, bounds.size.width - 40.0, 26.0
            )
            theme.fill_round_rect(
                notice_rect,
                7.0,
                theme.rgba(251, 191, 36, 0.20),
                theme.STATUS_WARNING,
                1.2,
            )
            theme.draw_text(
                msg,
                Cocoa.NSMakeRect(
                    30.0, bounds.size.height - 33.0, bounds.size.width - 60.0, 18.0
                ),
                theme.font_ui(12.0, bold=True),
                theme.STATUS_WARNING,
                align=Cocoa.NSTextAlignmentCenter,
            )

    @objc.python_method
    def _draw_assign_apps_list(self, bounds: Cocoa.NSRect) -> None:
        theme.draw_text(
            f"Assign Open App to Key  ({len(self.assign_apps)} open)",
            Cocoa.NSMakeRect(22.0, 16.0, 340.0, 22.0),
            theme.font_ui(15.0, bold=True),
            theme.STATUS_RUNNING,
        )
        theme.draw_text(
            "↑/↓ Select  ·  Enter or 1–9 Choose App  ·  , Settings  ·  Esc Cancel",
            Cocoa.NSMakeRect(340.0, 18.0, bounds.size.width - 360.0, 18.0),
            theme.font_ui(11.5, bold=False),
            theme.TEXT_SECONDARY,
            align=Cocoa.NSTextAlignmentRight,
        )

        # Build bundle_id -> list of current bound slot labels (e.g. ["T", "⇧S"])
        bound_labels: dict[str, list[str]] = {}
        for b in self.assign_bindings:
            lbl = f"⇧{b.key}" if b.layer is Layer.SHIFT else b.key
            bound_labels.setdefault(b.bundle_id, []).append(lbl)

        y = 50.0
        if self.notice:
            n_rect = Cocoa.NSMakeRect(20.0, y, bounds.size.width - 40.0, 28.0)
            theme.fill_round_rect(
                n_rect,
                8.0,
                theme.rgba(251, 191, 36, 0.16),
                theme.rgba(251, 191, 36, 0.55),
                1.0,
            )
            theme.draw_text(
                self.notice,
                Cocoa.NSMakeRect(30.0, y + 6.0, bounds.size.width - 60.0, 18.0),
                theme.font_ui(12.0, bold=True),
                theme.STATUS_WARNING,
            )
            y += 36.0

        max_visible = 9
        total = len(self.assign_apps)
        start_idx = 0
        if total > max_visible:
            start_idx = max(
                0, min(total - max_visible, self.assign_selected_index - max_visible // 2)
            )

        visible = self.assign_apps[start_idx : start_idx + max_visible]
        row_h = 40.0
        row_gap = 6.0

        for offset, rec in enumerate(visible):
            idx = start_idx + offset
            row_num = idx + 1
            is_sel = idx == self.assign_selected_index
            row_rect = Cocoa.NSMakeRect(18.0, y, bounds.size.width - 36.0, row_h)
            bg = theme.KEY_SELECTED_BG if is_sel else theme.KEY_CLOSED_BG
            border = theme.KEY_SELECTED_BORDER if is_sel else theme.KEY_CLOSED_BORDER
            theme.fill_round_rect(row_rect, 9.0, bg, border, 1.5 if is_sel else 1.0)

            has_direct_digit = 1 <= row_num <= 9
            badge_rect = Cocoa.NSMakeRect(28.0, y + 8.0, 30.0, 24.0)
            badge_bg = (
                theme.rgba(255, 255, 255, 0.18)
                if has_direct_digit
                else theme.rgba(255, 255, 255, 0.06)
            )
            theme.fill_round_rect(badge_rect, 6.0, badge_bg)
            theme.draw_text(
                str(row_num),
                Cocoa.NSMakeRect(28.0, y + 12.0, 30.0, 18.0),
                theme.font_mono(12.5, bold=has_direct_digit),
                theme.TEXT_PRIMARY if has_direct_digit else theme.TEXT_MUTED,
                align=Cocoa.NSTextAlignmentCenter,
            )

            if rec.bundle_id and self.icon_provider:
                icon = self.icon_provider(rec.bundle_id, rec.bundle_path)
                if icon is not None:
                    icon.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
                        Cocoa.NSMakeRect(66.0, y + 8.0, 24.0, 24.0),
                        Cocoa.NSZeroRect,
                        Cocoa.NSCompositingOperationSourceOver,
                        1.0,
                        True,
                        None,
                    )

            # Status pill on right: Current key(s) vs Unassigned
            if rec.has_missing_bundle_id or not rec.bundle_id:
                pill_text = "No Bundle ID"
                pill_color = theme.STATUS_ERROR
            elif rec.bundle_id in bound_labels:
                pill_text = f"Bound: {', '.join(bound_labels[rec.bundle_id])}"
                pill_color = theme.ACCENT_BLUE
            else:
                pill_text = "Unassigned"
                pill_color = theme.STATUS_WARNING

            pw = max(92.0, len(pill_text) * 7.2 + 20.0)
            px = row_rect.origin.x + row_rect.size.width - pw - 12.0
            p_rect = Cocoa.NSMakeRect(px, y + 9.0, pw, 22.0)
            theme.fill_round_rect(
                p_rect, 6.0, theme.rgba(15, 20, 30, 0.55), pill_color, 1.0
            )
            theme.draw_text(
                pill_text,
                Cocoa.NSMakeRect(px + 6.0, y + 12.0, pw - 12.0, 16.0),
                theme.font_ui(11.0, bold=True),
                pill_color,
                align=Cocoa.NSTextAlignmentCenter,
            )

            name_label = rec.display_name
            if idx == 0:
                name_label = f"{rec.display_name}  ·  Active"
            theme.draw_text(
                name_label,
                Cocoa.NSMakeRect(98.0, y + 11.0, max(120.0, px - 108.0), 20.0),
                theme.font_ui(13.5, bold=is_sel),
                theme.TEXT_PRIMARY,
            )
            y += row_h + row_gap

        if total > max_visible:
            theme.draw_text(
                f"Showing {start_idx + 1}–{min(total, start_idx + max_visible)} of {total} open apps (use ↑/↓ for all)",
                Cocoa.NSMakeRect(20.0, y + 2.0, bounds.size.width - 40.0, 18.0),
                theme.font_ui(11.0, bold=False),
                theme.TEXT_MUTED,
                align=Cocoa.NSTextAlignmentCenter,
            )

    @objc.python_method
    def _draw_window_list(self, bounds: Cocoa.NSRect) -> None:
        binding = self.selected_binding
        title = binding.display_name if binding else "Windows"

        if binding and self.icon_provider:
            icon = self.icon_provider(binding.bundle_id, binding.bundle_path)
            if icon is not None:
                icon.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
                    Cocoa.NSMakeRect(20.0, 14.0, 26.0, 26.0),
                    Cocoa.NSZeroRect,
                    Cocoa.NSCompositingOperationSourceOver,
                    1.0,
                    True,
                    None,
                )

        theme.draw_text(
            f"{title}  ({len(self.slots)} windows)",
            Cocoa.NSMakeRect(54.0, 17.0, 260.0, 22.0),
            theme.font_ui(15.0, bold=True),
            theme.TEXT_PRIMARY,
        )
        key_range = "1–9/A–Z" if any(s.slot_number >= 10 for s in self.slots) else "1–9"
        win_hint = (
            f"Hover/↑↓  ·  Click/Enter/{key_range} Activate  ·  × Hide  ·  Esc"
            if self.mouse_enabled
            else f"↑/↓ Select  ·  Enter/{key_range} Activate  ·  ⌫/× Hide  ·  Esc"
        )
        theme.draw_text(
            win_hint,
            Cocoa.NSMakeRect(300.0, 19.0, bounds.size.width - 320.0, 18.0),
            theme.font_ui(11.5, bold=False),
            theme.TEXT_SECONDARY,
            align=Cocoa.NSTextAlignmentRight,
        )

        y = 52.0
        if self.notice:
            n_rect = Cocoa.NSMakeRect(20.0, y, bounds.size.width - 40.0, 28.0)
            theme.fill_round_rect(
                n_rect,
                8.0,
                theme.rgba(251, 191, 36, 0.16),
                theme.rgba(251, 191, 36, 0.55),
                1.0,
            )
            theme.draw_text(
                self.notice,
                Cocoa.NSMakeRect(30.0, y + 6.0, bounds.size.width - 60.0, 18.0),
                theme.font_ui(12.0, bold=True),
                theme.STATUS_WARNING,
            )
            y += 36.0

        max_visible = self.max_visible_window_rows()
        total = len(self.slots)
        start_idx = 0
        if total > max_visible and self.selected_slot is not None:
            sel_indices = [
                i for i, s in enumerate(self.slots) if s.slot_number == self.selected_slot
            ]
            if sel_indices:
                sel_i = sel_indices[0]
                start_idx = max(0, min(total - max_visible, sel_i - max_visible // 2))

        visible_slots = self.slots[start_idx : start_idx + max_visible]
        row_h = 40.0
        row_gap = 6.0

        for slot in visible_slots:
            is_sel = self.selected_slot == slot.slot_number
            row_rect = Cocoa.NSMakeRect(18.0, y, bounds.size.width - 36.0, row_h)
            self._window_row_rects[slot.slot_number] = row_rect
            bg = theme.KEY_SELECTED_BG if is_sel else theme.KEY_CLOSED_BG
            border = theme.KEY_SELECTED_BORDER if is_sel else theme.KEY_CLOSED_BORDER
            theme.fill_round_rect(row_rect, 9.0, bg, border, 1.5 if is_sel else 1.0)

            has_direct_key = slot.has_direct_key
            is_letter_badge = 10 <= slot.slot_number <= 35
            badge_rect = Cocoa.NSMakeRect(28.0, y + 8.0, 30.0, 24.0)
            if is_letter_badge:
                badge_bg = theme.rgba(88, 154, 255, 0.26)
                badge_border = theme.rgba(88, 154, 255, 0.55)
            elif has_direct_key:
                badge_bg = theme.rgba(255, 255, 255, 0.18)
                badge_border = None
            else:
                badge_bg = theme.rgba(255, 255, 255, 0.06)
                badge_border = None
            theme.fill_round_rect(badge_rect, 6.0, badge_bg, badge_border, 1.0)
            theme.draw_text(
                slot.badge_label,
                Cocoa.NSMakeRect(28.0, y + 12.0, 30.0, 18.0),
                theme.font_mono(12.5, bold=has_direct_key),
                theme.TEXT_PRIMARY if has_direct_key else theme.TEXT_MUTED,
                align=Cocoa.NSTextAlignmentCenter,
            )

            # Far-right clickable '×' button to dismiss this entry from the quick-choice list
            close_size = 22.0
            close_x = row_rect.origin.x + row_rect.size.width - close_size - 10.0
            close_rect = Cocoa.NSMakeRect(close_x, y + 9.0, close_size, close_size)
            self._window_dismiss_rects[slot.slot_number] = Cocoa.NSMakeRect(
                close_x - 3.0, y + 4.0, close_size + 6.0, 32.0
            )
            is_x_hovered = self._hovered_dismiss_slot == slot.slot_number
            if is_x_hovered:
                x_bg = theme.rgba(248, 113, 113, 0.28)
                x_border = theme.STATUS_ERROR
                x_fg = theme.STATUS_ERROR
            else:
                x_bg = theme.rgba(15, 20, 30, 0.45)
                x_border = theme.rgba(148, 163, 184, 0.30)
                x_fg = theme.TEXT_SECONDARY
            theme.fill_round_rect(close_rect, 6.0, x_bg, x_border, 1.0)
            theme.draw_text(
                "×",
                Cocoa.NSMakeRect(close_x, y + 10.0, close_size, 18.0),
                theme.font_ui(13.5, bold=True),
                x_fg,
                align=Cocoa.NSTextAlignmentCenter,
            )

            badges: list[tuple[str, Cocoa.NSColor]] = []
            if slot.window.is_modal:
                badges.append(("Modal", theme.STATUS_MODAL))
            if slot.window.is_minimized:
                badges.append(("Minimized", theme.STATUS_WARNING))
            if slot.window.space_label:
                badges.append((slot.window.space_label, theme.STATUS_SPACE))
            elif slot.window.is_focused:
                badges.append(("Focused", theme.STATUS_RUNNING))

            right_edge = close_x - 8.0
            for b_text, b_color in reversed(badges):
                bw = max(58.0, len(b_text) * 7.2 + 16.0)
                bx = right_edge - bw
                b_rect = Cocoa.NSMakeRect(bx, y + 9.0, bw, 22.0)
                theme.fill_round_rect(
                    b_rect, 6.0, theme.rgba(15, 20, 30, 0.55), b_color, 1.0
                )
                theme.draw_text(
                    b_text,
                    Cocoa.NSMakeRect(bx + 4.0, y + 12.0, bw - 8.0, 16.0),
                    theme.font_ui(10.5, bold=True),
                    b_color,
                    align=Cocoa.NSTextAlignmentCenter,
                )
                right_edge = bx - 6.0

            title_w = max(120.0, right_edge - 72.0)
            theme.draw_text(
                slot.title,
                Cocoa.NSMakeRect(68.0, y + 11.0, title_w, 20.0),
                theme.font_ui(13.5, bold=is_sel),
                theme.TEXT_PRIMARY,
            )
            y += row_h + row_gap

        if total > max_visible:
            theme.draw_text(
                f"Showing {start_idx + 1}–{min(total, start_idx + max_visible)} of {total} windows (use ↑/↓ or A–Z for all)",
                Cocoa.NSMakeRect(20.0, y + 2.0, bounds.size.width - 40.0, 18.0),
                theme.font_ui(11.0, bold=False),
                theme.TEXT_MUTED,
                align=Cocoa.NSTextAlignmentCenter,
            )

    @objc.python_method
    def _draw_launching(self, bounds: Cocoa.NSRect) -> None:
        binding = self.selected_binding
        name = binding.display_name if binding else "Application"
        if binding and self.icon_provider:
            icon = self.icon_provider(binding.bundle_id, binding.bundle_path)
            if icon is not None:
                icon.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
                    Cocoa.NSMakeRect(24.0, 22.0, 40.0, 40.0),
                    Cocoa.NSZeroRect,
                    Cocoa.NSCompositingOperationSourceOver,
                    1.0,
                    True,
                    None,
                )
        theme.draw_text(
            f"Launching {name}…",
            Cocoa.NSMakeRect(78.0, 22.0, bounds.size.width - 98.0, 22.0),
            theme.font_ui(15.5, bold=True),
            theme.TEXT_PRIMARY,
        )
        theme.draw_text(
            "Observing window creation  ·  Press 1–9 / A–Z to preselect or Esc to cancel",
            Cocoa.NSMakeRect(78.0, 45.0, bounds.size.width - 98.0, 18.0),
            theme.font_ui(12.0, bold=False),
            theme.TEXT_SECONDARY,
        )

    @objc.python_method
    def _draw_banner(self, bounds: Cocoa.NSRect) -> None:
        accent = {
            "error": theme.STATUS_ERROR,
            "warning": theme.STATUS_WARNING,
            "success": theme.STATUS_RUNNING,
        }.get(self.banner_level, theme.ACCENT_BLUE)

        bar = Cocoa.NSMakeRect(16.0, 16.0, 5.0, bounds.size.height - 32.0)
        theme.fill_round_rect(bar, 2.5, accent)
        theme.draw_text(
            self.banner_message,
            Cocoa.NSMakeRect(32.0, 20.0, bounds.size.width - 48.0, bounds.size.height - 36.0),
            theme.font_ui(13.0, bold=True),
            theme.TEXT_PRIMARY,
        )

    def mouseMoved_(self, event) -> None:
        pt = self.convertPoint_fromView_(event.locationInWindow(), None)
        if self.mode == "windows":
            hovered_x: int | None = None
            for slot_num, d_rect in self._window_dismiss_rects.items():
                if Cocoa.NSPointInRect(pt, d_rect):
                    hovered_x = slot_num
                    break
            if hovered_x != self._hovered_dismiss_slot:
                self._hovered_dismiss_slot = hovered_x
                self.setNeedsDisplay_(True)
            if hovered_x is not None:
                return
            if self.mouse_enabled and self.on_window_slot_hovered is not None:
                for slot_num, rect in self._window_row_rects.items():
                    if Cocoa.NSPointInRect(pt, rect):
                        if self.selected_slot != slot_num:
                            self.on_window_slot_hovered(slot_num)
                        return

    def mouseEntered_(self, event) -> None:
        self.mouseMoved_(event)

    def mouseExited_(self, _event) -> None:
        if self._hovered_dismiss_slot is not None:
            self._hovered_dismiss_slot = None
            self.setNeedsDisplay_(True)

    def mouseDown_(self, event) -> None:
        pt = self.convertPoint_fromView_(event.locationInWindow(), None)
        if self.mode == "windows":
            for slot_num, d_rect in self._window_dismiss_rects.items():
                if Cocoa.NSPointInRect(pt, d_rect):
                    if self.on_window_slot_dismissed is not None:
                        self.on_window_slot_dismissed(slot_num)
                    return

        if not self.mouse_enabled:
            return
        if self.mode == "map" and self._direct_jump_pill_rect is not None:
            if Cocoa.NSPointInRect(pt, self._direct_jump_pill_rect):
                if self.on_direct_jump_pill_clicked is not None:
                    self.on_direct_jump_pill_clicked()
                return
        if self.mode in ("map", "assign_keymap") and self._layer_pill_rect is not None:
            if Cocoa.NSPointInRect(pt, self._layer_pill_rect):
                if self.on_layer_pill_clicked is not None:
                    self.on_layer_pill_clicked()
                return
        if self.mode == "windows" and self.on_window_slot_clicked is not None:
            for slot_num, rect in self._window_row_rects.items():
                if Cocoa.NSPointInRect(pt, rect):
                    self.on_window_slot_clicked(slot_num)
                    return

    def scrollWheel_(self, event) -> None:
        if (
            not self.mouse_enabled
            or self.mode != "windows"
            or self.on_window_scroll is None
            or len(self.slots) <= 1
        ):
            return
        dy = float(event.scrollingDeltaY())
        if abs(dy) < 0.05:
            return
        if event.hasPreciseScrollingDeltas():
            self._scroll_accum += dy
            if abs(self._scroll_accum) >= 22.0:
                step = 1 if self._scroll_accum < 0 else -1
                self._scroll_accum = 0.0
                self.on_window_scroll(step)
        else:
            step = 1 if dy < 0 else -1
            self.on_window_scroll(step)


class _BannerTimerTarget(Cocoa.NSObject):
    def initWithCallback_(self, callback: Callable[[], None]):
        self = objc.super(_BannerTimerTarget, self).init()
        if self is None:
            return None
        self._callback = callback
        return self

    def timerFired_(self, _timer) -> None:
        if self._callback is not None:
            self._callback()


class MacOSOverlayPresenter(OverlayPresenter):
    """Thread-safe AppKit HUD presenter (all UI mutations execute on the main thread)."""

    def __init__(self, app_registry: ApplicationRegistry) -> None:
        self._app_registry = app_registry
        self._panel: _NonActivatingHUDPanel | None = None
        self._content: _HUDContentView | None = None
        self._banner_timer = None
        self._timer_target = _BannerTimerTarget.alloc().initWithCallback_(
            self._on_banner_timer_fired
        )
        self._mouse_enabled: bool = False
        self._on_app_key_clicked: Callable[[str, Layer], None] | None = None
        self._on_layer_pill_clicked: Callable[[], None] | None = None
        self._on_direct_jump_pill_clicked: Callable[[], None] | None = None
        self._on_window_slot_hovered: Callable[[int], None] | None = None
        self._on_window_slot_clicked: Callable[[int], None] | None = None
        self._on_window_slot_dismissed: Callable[[int], None] | None = None
        self._on_window_scroll: Callable[[int], None] | None = None

    def set_mouse_callbacks(
        self,
        *,
        on_app_key_clicked: Callable[[str, Layer], None] | None = None,
        on_layer_pill_clicked: Callable[[], None] | None = None,
        on_direct_jump_pill_clicked: Callable[[], None] | None = None,
        on_window_slot_hovered: Callable[[int], None] | None = None,
        on_window_slot_clicked: Callable[[int], None] | None = None,
        on_window_slot_dismissed: Callable[[int], None] | None = None,
        on_window_scroll: Callable[[int], None] | None = None,
    ) -> None:
        self._on_app_key_clicked = on_app_key_clicked
        self._on_layer_pill_clicked = on_layer_pill_clicked
        self._on_direct_jump_pill_clicked = on_direct_jump_pill_clicked
        self._on_window_slot_hovered = on_window_slot_hovered
        self._on_window_slot_clicked = on_window_slot_clicked
        self._on_window_slot_dismissed = on_window_slot_dismissed
        self._on_window_scroll = on_window_scroll
        if self._content is not None:
            self._wire_content_callbacks(self._content)

    def set_mouse_enabled(self, enabled: bool) -> None:
        self._mouse_enabled = bool(enabled)
        if self._panel is not None and self._content is not None:
            self._apply_mouse_mode_on_main(
                interactive_mode=self._content.mode
                in ("map", "windows", "assign_keymap")
            )

    def _wire_content_callbacks(self, content: _HUDContentView) -> None:
        content.on_layer_pill_clicked = self._on_layer_pill_clicked
        content.on_direct_jump_pill_clicked = self._on_direct_jump_pill_clicked
        content.on_window_slot_hovered = self._on_window_slot_hovered
        content.on_window_slot_clicked = self._on_window_slot_clicked
        content.on_window_slot_dismissed = self._on_window_slot_dismissed
        content.on_window_scroll = self._on_window_scroll

    def _handle_hud_grid_key_clicked(self, key_char: str) -> None:
        if not self._mouse_enabled or self._on_app_key_clicked is None:
            return
        shift_down = bool(
            Cocoa.NSEvent.modifierFlags() & Cocoa.NSEventModifierFlagShift
        )
        layer = (
            Layer.SHIFT
            if (
                shift_down
                or (
                    self._content is not None
                    and self._content.active_layer is Layer.SHIFT
                )
            )
            else Layer.BASE
        )
        self._on_app_key_clicked(key_char, layer)

    def _apply_mouse_mode_on_main(self, *, interactive_mode: bool) -> None:
        if self._panel is None or self._content is None:
            return
        # Always accept clicks in "windows" mode so the right-side '×' dismiss button works
        # even if general HUD hover/click selection is toggled off.
        active = bool(
            (self._mouse_enabled and interactive_mode)
            or self._content.mode == "windows"
        )
        self._panel.setIgnoresMouseEvents_(not active)
        self._panel.setAcceptsMouseMovedEvents_(active)
        prev_mouse = self._content.mouse_enabled
        self._content.mouse_enabled = self._mouse_enabled
        self._content.updateTrackingAreas()
        if prev_mouse != self._mouse_enabled:
            if self._content.mode in ("map", "assign_keymap"):
                self._content.grid_view.set_mouse_interactive(
                    self._mouse_enabled,
                    on_key_clicked=(
                        self._handle_hud_grid_key_clicked if self._mouse_enabled else None
                    ),
                )
            self._content.setNeedsDisplay_(True)

    def _ensure_panel_on_main(self) -> tuple[_NonActivatingHUDPanel, _HUDContentView]:
        if self._panel is not None and self._content is not None:
            return self._panel, self._content

        rect = Cocoa.NSMakeRect(0.0, 0.0, 940.0, 340.0)
        style = (
            Cocoa.NSWindowStyleMaskBorderless
            | Cocoa.NSWindowStyleMaskNonactivatingPanel
        )
        panel = _NonActivatingHUDPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect,
            style,
            Cocoa.NSBackingStoreBuffered,
            False,
        )
        panel.setFloatingPanel_(True)
        panel.setBecomesKeyOnlyIfNeeded_(True)
        panel.setHidesOnDeactivate_(False)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(Cocoa.NSColor.clearColor())
        panel.setHasShadow_(True)
        panel.setAnimationBehavior_(Cocoa.NSWindowAnimationBehaviorNone)
        panel.setIgnoresMouseEvents_(not self._mouse_enabled)
        panel.setAcceptsMouseMovedEvents_(self._mouse_enabled)
        panel.setLevel_(HUD_WINDOW_LEVEL)
        panel.setCollectionBehavior_(HUD_COLLECTION_BEHAVIOR)

        content = _HUDContentView.alloc().initWithFrame_(rect)
        content.icon_provider = self._app_registry.app_icon_for_bundle
        content.mouse_enabled = self._mouse_enabled
        content.updateTrackingAreas()
        self._wire_content_callbacks(content)
        panel.setContentView_(content)

        self._panel = panel
        self._content = content
        return panel, content

    def _cancel_banner_timer(self) -> None:
        if self._banner_timer is not None:
            self._banner_timer.invalidate()
            self._banner_timer = None

    def _schedule_banner_timer(self, ms: int, clear_notice_only: bool) -> None:
        self._cancel_banner_timer()
        self._clear_notice_only = clear_notice_only
        self._banner_timer = (
            Cocoa.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                max(0.4, ms / 1000.0),
                self._timer_target,
                "timerFired:",
                None,
                False,
            )
        )

    def _on_banner_timer_fired(self) -> None:
        self._banner_timer = None
        if self._content is None or self._panel is None:
            return
        if getattr(self, "_clear_notice_only", False) and self._content.mode == "map":
            self._content.notice = None
            self._resize_and_center(self._panel, self._content, *self._map_panel_size(False))
            self._content.setNeedsDisplay_(True)
        elif self._content.mode == "banner":
            self._panel.orderOut_(None)

    @staticmethod
    def _map_panel_size(has_notice: bool) -> tuple[float, float]:
        gw, gh = KeyboardMapGridView.required_size()
        extra_bottom = 42.0 if has_notice else 12.0
        return gw + 16.0, gh + 50.0 + extra_bottom

    @staticmethod
    def _resize_and_center(
        panel: Cocoa.NSPanel, content: _HUDContentView, width: float, height: float
    ) -> None:
        mouse_pt = Cocoa.NSEvent.mouseLocation()
        target_screen = None
        for scr in Cocoa.NSScreen.screens():
            if Cocoa.NSPointInRect(mouse_pt, scr.frame()):
                target_screen = scr
                break
        if target_screen is None:
            target_screen = Cocoa.NSScreen.mainScreen()

        s_frame = (
            target_screen.visibleFrame()
            if target_screen is not None
            else Cocoa.NSMakeRect(0.0, 0.0, 1440.0, 900.0)
        )
        x = s_frame.origin.x + (s_frame.size.width - width) / 2.0
        y = s_frame.origin.y + (s_frame.size.height - height) * 0.56
        panel.setFrame_display_(Cocoa.NSMakeRect(x, y, width, height), True)
        content.setFrame_(Cocoa.NSMakeRect(0.0, 0.0, width, height))

    @staticmethod
    def _present_panel_front(panel: Cocoa.NSPanel) -> None:
        panel.setAnimationBehavior_(Cocoa.NSWindowAnimationBehaviorNone)
        panel.setCollectionBehavior_(HUD_COLLECTION_BEHAVIOR)
        panel.setLevel_(HUD_WINDOW_LEVEL)
        panel.orderFrontRegardless()

    def is_interactive_visible(self) -> bool:
        return bool(
            self._panel is not None
            and self._panel.isVisible()
            and self._content is not None
            and self._content.mode != "banner"
        )

    def contains_screen_point(self, pt: Cocoa.NSPoint) -> bool:
        if self._panel is None or not self._panel.isVisible():
            return False
        return bool(Cocoa.NSPointInRect(pt, self._panel.frame()))

    def reassert_frontmost(self) -> None:
        if self._panel is not None and self._panel.isVisible():
            self._present_panel_front(self._panel)

    def show_app_map(
        self,
        active_layer: Layer,
        bindings: tuple[AppBinding, ...],
        running_bundle_ids: frozenset[str],
        duplicate_conflicts: frozenset[str],
        notice: str | None = None,
        quick_assign_enabled: bool = False,
        direct_jump: bool = False,
    ) -> None:
        self._cancel_banner_timer()
        panel, content = self._ensure_panel_on_main()
        content.mode = "map"
        content.active_layer = active_layer
        content.direct_jump_active = bool(direct_jump)
        content.notice = notice
        content.quick_assign_enabled = quick_assign_enabled
        self._apply_mouse_mode_on_main(interactive_mode=True)

        gw, gh = KeyboardMapGridView.required_size()
        pw, ph = self._map_panel_size(bool(notice))
        self._resize_and_center(panel, content, pw, ph)

        content.grid_view.setHidden_(False)
        content.grid_view.setFrame_(Cocoa.NSMakeRect(8.0, 46.0, gw, gh))
        content.grid_view.configure(
            active_layer=active_layer,
            bindings=bindings,
            running_bundle_ids=running_bundle_ids,
            duplicate_conflicts=duplicate_conflicts,
            icon_provider=self._app_registry.app_icon_for_bundle,
            selected_key=None,
            on_key_clicked=self._handle_hud_grid_key_clicked if self._mouse_enabled else None,
            mouse_interactive=self._mouse_enabled,
            select_on_click=False,
        )
        content.setNeedsDisplay_(True)
        self._present_panel_front(panel)

    def update_app_map_layer(
        self, active_layer: Layer, direct_jump: bool = False
    ) -> None:
        if self._panel is None or self._content is None:
            return
        if self._content.mode not in ("map", "assign_keymap"):
            return
        self._content.active_layer = active_layer
        self._content.direct_jump_active = bool(direct_jump)
        self._content.grid_view.set_active_layer(active_layer)
        self._content.setNeedsDisplay_(True)

    def show_quick_assign_apps(
        self,
        apps: tuple[AppRecord, ...],
        selected_index: int,
        bindings: tuple[AppBinding, ...],
        notice: str | None = None,
    ) -> None:
        self._cancel_banner_timer()
        panel, content = self._ensure_panel_on_main()
        content.mode = "assign_apps"
        content.assign_apps = apps
        content.assign_selected_index = selected_index
        content.assign_bindings = bindings
        content.notice = notice
        content.grid_view.setHidden_(True)
        self._apply_mouse_mode_on_main(interactive_mode=False)

        visible_rows = min(9, max(1, len(apps)))
        extra_notice = 36.0 if notice else 0.0
        extra_footer = 22.0 if len(apps) > 9 else 0.0
        height = 64.0 + extra_notice + visible_rows * 46.0 + extra_footer
        self._resize_and_center(panel, content, 700.0, height)
        content.setNeedsDisplay_(True)
        self._present_panel_front(panel)

    def update_quick_assign_app_selection(
        self,
        selected_index: int,
        notice: str | None = None,
    ) -> None:
        if self._panel is None or self._content is None:
            return
        if self._content.mode != "assign_apps":
            return
        prev_had_notice = bool(self._content.notice)
        self._content.assign_selected_index = selected_index
        if notice is not None:
            self._content.notice = notice
        if bool(self._content.notice) != prev_had_notice:
            self.show_quick_assign_apps(
                apps=self._content.assign_apps,
                selected_index=selected_index,
                bindings=self._content.assign_bindings,
                notice=self._content.notice,
            )
            return
        self._content.setNeedsDisplay_(True)

    def show_quick_assign_keymap(
        self,
        app: AppRecord,
        active_layer: Layer,
        bindings: tuple[AppBinding, ...],
        running_bundle_ids: frozenset[str],
        duplicate_conflicts: frozenset[str],
        conflict_existing: AppBinding | None = None,
    ) -> None:
        self._cancel_banner_timer()
        panel, content = self._ensure_panel_on_main()
        content.mode = "assign_keymap"
        content.assign_target_app = app
        content.active_layer = active_layer
        content.assign_conflict_existing = conflict_existing
        self._apply_mouse_mode_on_main(interactive_mode=True)

        gw, gh = KeyboardMapGridView.required_size()
        pw, ph = self._map_panel_size(conflict_existing is not None)
        self._resize_and_center(panel, content, pw, ph)

        content.grid_view.setHidden_(False)
        content.grid_view.setFrame_(Cocoa.NSMakeRect(8.0, 46.0, gw, gh))
        content.grid_view.configure(
            active_layer=active_layer,
            bindings=bindings,
            running_bundle_ids=running_bundle_ids,
            duplicate_conflicts=duplicate_conflicts,
            icon_provider=self._app_registry.app_icon_for_bundle,
            selected_key=conflict_existing.key if conflict_existing else None,
            on_key_clicked=self._handle_hud_grid_key_clicked if self._mouse_enabled else None,
            mouse_interactive=self._mouse_enabled,
            select_on_click=False,
        )
        content.setNeedsDisplay_(True)
        self._present_panel_front(panel)

    def show_launch_pending(self, binding: AppBinding) -> None:
        self._cancel_banner_timer()
        panel, content = self._ensure_panel_on_main()
        content.mode = "launching"
        content.selected_binding = binding
        content.grid_view.setHidden_(True)
        self._apply_mouse_mode_on_main(interactive_mode=False)
        self._resize_and_center(panel, content, 540.0, 86.0)
        content.setNeedsDisplay_(True)
        self._present_panel_front(panel)

    def show_window_list(
        self,
        binding: AppBinding,
        slots: tuple[WindowSlot, ...],
        selected_slot: int | None,
        notice: str | None = None,
    ) -> None:
        self._cancel_banner_timer()
        panel, content = self._ensure_panel_on_main()
        content.mode = "windows"
        content.selected_binding = binding
        content.slots = slots
        content.selected_slot = selected_slot
        content.notice = notice
        content._hovered_dismiss_slot = None
        content._scroll_accum = 0.0
        content.grid_view.setHidden_(True)
        self._apply_mouse_mode_on_main(interactive_mode=True)

        max_vis = content.max_visible_window_rows()
        visible_rows = min(max_vis, max(1, len(slots)))
        extra_notice = 36.0 if notice else 0.0
        extra_footer = 22.0 if len(slots) > max_vis else 0.0
        height = 66.0 + extra_notice + visible_rows * 46.0 + extra_footer
        self._resize_and_center(panel, content, 720.0, height)
        content.setNeedsDisplay_(True)
        self._present_panel_front(panel)

    def update_window_selection(
        self,
        selected_slot: int | None,
        notice: str | None = None,
    ) -> None:
        if self._panel is None or self._content is None:
            return
        if self._content.mode != "windows":
            return
        prev_had_notice = bool(self._content.notice)
        self._content.selected_slot = selected_slot
        if notice is not None:
            self._content.notice = notice
        if bool(self._content.notice) != prev_had_notice and self._content.selected_binding:
            self.show_window_list(
                binding=self._content.selected_binding,
                slots=self._content.slots,
                selected_slot=selected_slot,
                notice=self._content.notice,
            )
            return
        self._content.setNeedsDisplay_(True)

    def show_status_banner(
        self,
        level: str,
        message: str,
        auto_hide_ms: int = 2400,
        keep_app_map: bool = False,
    ) -> None:
        panel, content = self._ensure_panel_on_main()
        if keep_app_map and content.mode == "map":
            content.notice = message
            pw, ph = self._map_panel_size(True)
            self._resize_and_center(panel, content, pw, ph)
            content.setNeedsDisplay_(True)
            panel.orderFrontRegardless()
            self._schedule_banner_timer(auto_hide_ms, clear_notice_only=True)
            return

        content.mode = "banner"
        content.banner_level = level
        content.banner_message = message
        content.grid_view.setHidden_(True)
        self._apply_mouse_mode_on_main(interactive_mode=False)
        self._resize_and_center(panel, content, 620.0, 66.0)
        content.setNeedsDisplay_(True)
        self._present_panel_front(panel)
        self._schedule_banner_timer(auto_hide_ms, clear_notice_only=False)

    def hide(self) -> None:
        self._cancel_banner_timer()
        if self._panel is not None:
            self._panel.orderOut_(None)
