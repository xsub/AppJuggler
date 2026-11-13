"""Interactive Assignment Configuration Window ('Edit Assignments')."""

from __future__ import annotations

import importlib.resources
import math
import time
from pathlib import Path
from typing import Callable

import Cocoa
import objc

from appjuggler import CONTACT_EMAIL, build_info
from appjuggler.contracts import ApplicationRegistry
from appjuggler.domain.models import AppBinding, AppRecord, Layer
from appjuggler.persistence.binding_store import (
    BindingStore,
    CorruptedConfigLockError,
    SlotConflictError,
)
from appjuggler.platforms.macos.animations import read_macos_animation_status
from appjuggler.platforms.macos.ui import theme
from appjuggler.platforms.macos.ui.overlay_panel import KeyboardMapGridView


class _ClickableView(Cocoa.NSView):
    """View that runs a callback when clicked, with a pointing-hand cursor and a button role
    for VoiceOver. Without a callback it behaves as a plain view."""

    def initWithFrame_(self, frame):
        self = objc.super(_ClickableView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._on_click: Callable[[], None] | None = None
        return self

    @objc.python_method
    def set_on_click(self, callback: Callable[[], None] | None) -> None:
        self._on_click = callback
        win = self.window()
        if win is not None:
            win.invalidateCursorRectsForView_(self)

    def acceptsFirstMouse_(self, _event) -> bool:
        return self._on_click is not None

    def mouseDown_(self, event) -> None:
        if self._on_click is None:
            objc.super(_ClickableView, self).mouseDown_(event)
            return
        self._on_click()

    def resetCursorRects(self) -> None:
        if self._on_click is not None:
            self.addCursorRect_cursor_(self.bounds(), Cocoa.NSCursor.pointingHandCursor())

    def isAccessibilityElement(self) -> bool:
        return self._on_click is not None or bool(
            objc.super(_ClickableView, self).isAccessibilityElement()
        )

    def accessibilityRole(self):
        if self._on_click is not None:
            return Cocoa.NSAccessibilityButtonRole
        return objc.super(_ClickableView, self).accessibilityRole()

    def accessibilityPerformPress(self) -> bool:
        if self._on_click is None:
            return False
        self._on_click()
        return True


class _RoundedCardView(_ClickableView):
    """Pure-AppKit rounded card container (avoids CGColor pointer bridging warnings)."""

    def initWithFrame_(self, frame):
        self = objc.super(_RoundedCardView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._bg_color = Cocoa.NSColor.whiteColor()
        self._border_color = None
        self._radius = 8.0
        self._logo_image = None
        return self

    @objc.python_method
    def configure(
        self,
        *,
        bg_color,
        border_color=None,
        radius: float = 8.0,
        logo_image=None,
    ) -> None:
        self._bg_color = bg_color
        self._border_color = border_color
        self._radius = radius
        self._logo_image = logo_image
        self.setNeedsDisplay_(True)

    def drawRect_(self, _dirty_rect) -> None:
        bounds = Cocoa.NSInsetRect(self.bounds(), 0.5, 0.5)
        theme.fill_round_rect(bounds, self._radius, self._bg_color, self._border_color, 1.0)
        if self._logo_image is not None:
            Cocoa.NSGraphicsContext.saveGraphicsState()
            ctx = Cocoa.NSGraphicsContext.currentContext()
            if ctx is not None:
                ctx.setImageInterpolation_(Cocoa.NSImageInterpolationHigh)
            clip = Cocoa.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                bounds, self._radius, self._radius
            )
            clip.addClip()
            pad_rect = Cocoa.NSInsetRect(bounds, 5.0, 2.5)
            img_sz = self._logo_image.size()
            iw = float(img_sz.width)
            ih = float(img_sz.height)
            pw = float(pad_rect.size.width)
            ph = float(pad_rect.size.height)
            if iw > 0.0 and ih > 0.0 and pw > 0.0 and ph > 0.0:
                scale = min(pw / iw, ph / ih)
                dw = iw * scale
                dh = ih * scale
                img_rect = Cocoa.NSMakeRect(
                    float(pad_rect.origin.x) + (pw - dw) * 0.5,
                    float(pad_rect.origin.y) + (ph - dh) * 0.5,
                    dw,
                    dh,
                )
            else:
                img_rect = pad_rect
            self._logo_image.drawInRect_fromRect_operation_fraction_(
                img_rect,
                Cocoa.NSZeroRect,
                Cocoa.NSCompositingOperationSourceOver,
                1.0,
            )
            Cocoa.NSGraphicsContext.restoreGraphicsState()


class _HighQualityImageView(_ClickableView):
    """Draws an NSImage crisply with high interpolation and aspect-fit centering."""

    def initWithFrame_(self, frame):
        self = objc.super(_HighQualityImageView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._image = None
        return self

    @objc.python_method
    def set_image(self, image) -> None:
        self._image = image
        self.setNeedsDisplay_(True)

    def drawRect_(self, _dirty_rect) -> None:
        if self._image is None:
            return
        bounds = self.bounds()
        img_sz = self._image.size()
        iw = float(img_sz.width)
        ih = float(img_sz.height)
        bw = float(bounds.size.width)
        bh = float(bounds.size.height)
        if iw <= 0.0 or ih <= 0.0 or bw <= 0.0 or bh <= 0.0:
            return
        scale = min(bw / iw, bh / ih)
        dw = iw * scale
        dh = ih * scale
        dst = Cocoa.NSMakeRect(
            float(bounds.origin.x) + (bw - dw) * 0.5,
            float(bounds.origin.y) + (bh - dh) * 0.5,
            dw,
            dh,
        )
        Cocoa.NSGraphicsContext.saveGraphicsState()
        ctx = Cocoa.NSGraphicsContext.currentContext()
        if ctx is not None:
            ctx.setImageInterpolation_(Cocoa.NSImageInterpolationHigh)
        self._image.drawInRect_fromRect_operation_fraction_(
            dst,
            Cocoa.NSZeroRect,
            Cocoa.NSCompositingOperationSourceOver,
            1.0,
        )
        Cocoa.NSGraphicsContext.restoreGraphicsState()


class _ClickToFocusTextField(Cocoa.NSTextField):
    """Editable text field that refuses default/Tab focus and activates only when clicked."""

    def initWithFrame_(self, frame):
        self = objc.super(_ClickToFocusTextField, self).initWithFrame_(frame)
        if self is None:
            return None
        self.setRefusesFirstResponder_(True)
        return self

    def mouseDown_(self, event) -> None:
        self.setRefusesFirstResponder_(False)
        win = self.window()
        if win is not None:
            win.makeFirstResponder_(self)
        objc.super(_ClickToFocusTextField, self).mouseDown_(event)

    def textDidEndEditing_(self, notification) -> None:
        objc.super(_ClickToFocusTextField, self).textDidEndEditing_(notification)
        self.setRefusesFirstResponder_(True)


class _EditorWindow(Cocoa.NSWindow):
    """Assignment editor window that prevents accidental typing into numeric timing fields."""

    def initWithContentRect_styleMask_backing_defer_(
        self, content_rect, style_mask, backing, defer
    ):
        self = objc.super(
            _EditorWindow, self
        ).initWithContentRect_styleMask_backing_defer_(
            content_rect, style_mask, backing, defer
        )
        if self is None:
            return None
        self._click_focus_field = None
        self._on_key_char: Callable[[str], None] | None = None
        return self

    @objc.python_method
    def configure_editor_input(
        self,
        *,
        click_focus_field,
        on_key_char: Callable[[str], None] | None = None,
    ) -> None:
        self._click_focus_field = click_focus_field
        self._on_key_char = on_key_char

    def sendEvent_(self, event) -> None:
        if (
            self._click_focus_field is not None
            and isinstance(self.firstResponder(), Cocoa.NSTextView)
            and int(event.type())
            in (
                int(Cocoa.NSEventTypeLeftMouseDown),
                int(Cocoa.NSEventTypeRightMouseDown),
                int(Cocoa.NSEventTypeOtherMouseDown),
            )
        ):
            local_pt = self._click_focus_field.convertPoint_fromView_(
                event.locationInWindow(), None
            )
            if not Cocoa.NSPointInRect(local_pt, self._click_focus_field.bounds()):
                self.makeFirstResponder_(None)
                self._click_focus_field.setRefusesFirstResponder_(True)
        objc.super(_EditorWindow, self).sendEvent_(event)

    def keyDown_(self, event) -> None:
        if isinstance(self.firstResponder(), Cocoa.NSTextView):
            objc.super(_EditorWindow, self).keyDown_(event)
            return
        if int(event.keyCode()) == 53:
            self.close()
            return
        mods = int(event.modifierFlags())
        disallowed = int(
            Cocoa.NSEventModifierFlagCommand
            | Cocoa.NSEventModifierFlagControl
            | Cocoa.NSEventModifierFlagOption
        )
        if not (mods & disallowed) and self._on_key_char is not None:
            chars = str(event.charactersIgnoringModifiers() or "").upper()
            if len(chars) == 1 and "A" <= chars <= "Z":
                self._on_key_char(chars)
                return


class _FullWidthCaptionView(Cocoa.NSView):
    """Draws an uppercase caption tracked smoothly across the exact full width [0..width]."""

    def initWithFrame_(self, frame):
        self = objc.super(_FullWidthCaptionView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._text: str = ""
        self._font = theme.font_ui(10.5, bold=True)
        self._color = Cocoa.NSColor.secondaryLabelColor()
        return self

    @objc.python_method
    def configure(self, text: str, font=None, color=None) -> None:
        self._text = text
        if font is not None:
            self._font = font
        if color is not None:
            self._color = color
        self.setNeedsDisplay_(True)

    def drawRect_(self, _dirty_rect) -> None:
        if not self._text:
            return
        bounds = self.bounds()
        attrs = {
            Cocoa.NSFontAttributeName: self._font,
            Cocoa.NSForegroundColorAttributeName: self._color,
        }
        chars = list(self._text)
        widths = [
            float(Cocoa.NSString.stringWithString_(ch).sizeWithAttributes_(attrs).width)
            for ch in chars
        ]
        total_w = float(bounds.size.width) - 1.0
        sum_w = sum(widths)
        gap = (total_w - sum_w) / max(1, len(chars) - 1) if len(chars) > 1 else 0.0
        char_h = float(
            Cocoa.NSString.stringWithString_("A").sizeWithAttributes_(attrs).height
        )
        y = (float(bounds.size.height) - char_h) / 2.0
        x = 0.5
        for ch, w in zip(chars, widths):
            Cocoa.NSString.stringWithString_(ch).drawAtPoint_withAttributes_(
                Cocoa.NSMakePoint(x, y), attrs
            )
            x += w + gap


class _CopyableAddressButton(Cocoa.NSButton):
    """Contact address under the logo: a click copies it to the clipboard and confirms in place."""

    CONFIRMATION_TEXT = "Copied to clipboard"
    CONFIRMATION_SECONDS = 1.6
    FONT_SIZE = 10.5

    def initWithFrame_(self, frame):
        self = objc.super(_CopyableAddressButton, self).initWithFrame_(frame)
        if self is None:
            return None
        self._address: str = ""
        self._pasteboard = None
        self._hovered: bool = False
        self._showing_confirmation: bool = False
        self._tracking_area = None
        self.setBordered_(False)
        self.setButtonType_(Cocoa.NSButtonTypeMomentaryPushIn)
        self.setFocusRingType_(Cocoa.NSFocusRingTypeNone)
        self.setToolTip_("Click to copy")
        self.setTarget_(self)
        self.setAction_("copyAddress:")
        return self

    @objc.python_method
    def configure(self, address: str, pasteboard=None) -> None:
        """Set the address to show; `pasteboard` defaults to the general (system) clipboard."""
        self._address = address
        self._pasteboard = pasteboard
        self.setAccessibilityLabel_(f"Copy {address} to the clipboard")
        self._refresh_title()

    def acceptsFirstMouse_(self, _event) -> bool:
        return True

    def resetCursorRects(self) -> None:
        self.addCursorRect_cursor_(self.bounds(), Cocoa.NSCursor.pointingHandCursor())

    def updateTrackingAreas(self) -> None:
        objc.super(_CopyableAddressButton, self).updateTrackingAreas()
        if self._tracking_area is not None:
            self.removeTrackingArea_(self._tracking_area)
        opts = (
            Cocoa.NSTrackingMouseEnteredAndExited
            | Cocoa.NSTrackingActiveAlways
            | Cocoa.NSTrackingInVisibleRect
        )
        area = Cocoa.NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            Cocoa.NSZeroRect, opts, self, None
        )
        self.addTrackingArea_(area)
        self._tracking_area = area

    def mouseEntered_(self, _event) -> None:
        self._hovered = True
        self._refresh_title()

    def mouseExited_(self, _event) -> None:
        self._hovered = False
        self._refresh_title()

    def copyAddress_(self, _sender) -> None:
        if not self._address:
            return
        pb = self._pasteboard
        if pb is None:
            pb = Cocoa.NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_(self._address, Cocoa.NSPasteboardTypeString)

        self._showing_confirmation = True
        self._refresh_title()
        Cocoa.NSObject.cancelPreviousPerformRequestsWithTarget_selector_object_(
            self, "endConfirmation:", None
        )
        self.performSelector_withObject_afterDelay_(
            "endConfirmation:", None, self.CONFIRMATION_SECONDS
        )

    def endConfirmation_(self, _arg) -> None:
        self._showing_confirmation = False
        self._refresh_title()

    @objc.python_method
    def _refresh_title(self) -> None:
        confirming = self._showing_confirmation
        text = self.CONFIRMATION_TEXT if confirming else self._address
        if confirming:
            color = Cocoa.NSColor.systemGreenColor()
        elif self._hovered:
            color = Cocoa.NSColor.labelColor()
        else:
            color = Cocoa.NSColor.secondaryLabelColor()

        # Shrink slightly if the text would not fit the column.
        font = theme.font_ui(self.FONT_SIZE, bold=confirming)
        avail_w = float(self.bounds().size.width)
        natural_w = float(
            Cocoa.NSString.stringWithString_(text)
            .sizeWithAttributes_({Cocoa.NSFontAttributeName: font})
            .width
        )
        if natural_w > avail_w > 0.0:
            font = theme.font_ui(self.FONT_SIZE * avail_w / natural_w, bold=confirming)

        style = Cocoa.NSMutableParagraphStyle.alloc().init()
        style.setAlignment_(Cocoa.NSTextAlignmentCenter)
        attrs = {
            Cocoa.NSFontAttributeName: font,
            Cocoa.NSForegroundColorAttributeName: color,
            Cocoa.NSParagraphStyleAttributeName: style,
        }
        if self._hovered and not confirming:
            attrs[Cocoa.NSUnderlineStyleAttributeName] = Cocoa.NSUnderlineStyleSingle
        self.setAttributedTitle_(
            Cocoa.NSAttributedString.alloc().initWithString_attributes_(text, attrs)
        )


class _LinkButton(Cocoa.NSButton):
    """A grey prefix followed by a URL; a click opens the URL in the default browser."""

    FONT_SIZE = 12.0

    def initWithFrame_(self, frame):
        self = objc.super(_LinkButton, self).initWithFrame_(frame)
        if self is None:
            return None
        self._prefix: str = ""
        self._url: str = ""
        self._opener: Callable[[str], None] | None = None
        self._hovered: bool = False
        self._tracking_area = None
        self.setBordered_(False)
        self.setButtonType_(Cocoa.NSButtonTypeMomentaryChange)
        self.setFocusRingType_(Cocoa.NSFocusRingTypeNone)
        self.setToolTip_("Open in your browser")
        self.setTarget_(self)
        self.setAction_("openURL:")
        return self

    @objc.python_method
    def configure(
        self, prefix: str, url: str, opener: Callable[[str], None] | None = None
    ) -> None:
        """`opener` defaults to the default browser (tests pass a recorder)."""
        self._prefix = prefix
        self._url = url
        self._opener = opener
        self._refresh_title()
        self.sizeToFit()

    def resetCursorRects(self) -> None:
        self.addCursorRect_cursor_(self.bounds(), Cocoa.NSCursor.pointingHandCursor())

    def updateTrackingAreas(self) -> None:
        objc.super(_LinkButton, self).updateTrackingAreas()
        if self._tracking_area is not None:
            self.removeTrackingArea_(self._tracking_area)
        opts = (
            Cocoa.NSTrackingMouseEnteredAndExited
            | Cocoa.NSTrackingActiveAlways
            | Cocoa.NSTrackingInVisibleRect
        )
        area = Cocoa.NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            Cocoa.NSZeroRect, opts, self, None
        )
        self.addTrackingArea_(area)
        self._tracking_area = area

    def mouseEntered_(self, _event) -> None:
        self._hovered = True
        self._refresh_title()

    def mouseExited_(self, _event) -> None:
        self._hovered = False
        self._refresh_title()

    def openURL_(self, _sender) -> None:
        if not self._url:
            return
        if self._opener is not None:
            self._opener(self._url)
            return
        Cocoa.NSWorkspace.sharedWorkspace().openURL_(Cocoa.NSURL.URLWithString_(self._url))

    @objc.python_method
    def _refresh_title(self) -> None:
        font = theme.font_ui(self.FONT_SIZE)
        title = Cocoa.NSMutableAttributedString.alloc().initWithString_attributes_(
            self._prefix,
            {
                Cocoa.NSFontAttributeName: font,
                Cocoa.NSForegroundColorAttributeName: Cocoa.NSColor.secondaryLabelColor(),
            },
        )
        url_attrs = {
            Cocoa.NSFontAttributeName: font,
            Cocoa.NSForegroundColorAttributeName: Cocoa.NSColor.linkColor(),
        }
        if self._hovered:
            url_attrs[Cocoa.NSUnderlineStyleAttributeName] = Cocoa.NSUnderlineStyleSingle
        title.appendAttributedString_(
            Cocoa.NSAttributedString.alloc().initWithString_attributes_(self._url, url_attrs)
        )
        self.setAttributedTitle_(title)


def _about_popover_content(
    info: build_info.BuildInfo, opener: Callable[[str], None] | None = None
) -> Cocoa.NSView:
    """Version pop-up contents: version and build, license, then source and native links."""
    title = Cocoa.NSTextField.labelWithString_(build_info.version_line(info))
    title.setFont_(theme.font_ui(12.5, bold=True))
    title.setTextColor_(Cocoa.NSColor.labelColor())
    title.setSelectable_(True)  # lets users copy the build for a bug report
    title.sizeToFit()

    license_lbl = Cocoa.NSTextField.labelWithString_(build_info.COPYRIGHT)
    license_lbl.setFont_(theme.font_ui(11.5))
    license_lbl.setTextColor_(Cocoa.NSColor.secondaryLabelColor())
    license_lbl.sizeToFit()

    source_link = _LinkButton.alloc().initWithFrame_(Cocoa.NSZeroRect)
    source_link.configure("Source code: ", build_info.SOURCE_URL, opener)
    native_link = _LinkButton.alloc().initWithFrame_(Cocoa.NSZeroRect)
    native_link.configure("Native version: ", build_info.NATIVE_VERSION_URL, opener)

    # Fixed frames, top to bottom. Labels and borderless buttons pad their text by about
    # 2pt per side, hence the 2pt and 4pt corrections.
    rows = [title, license_lbl, source_link, native_link]
    gaps_after = [3.0, 10.0, 3.0]
    pad_x, pad_y = 16.0, 14.0
    width = max(float(v.frame().size.width) for v in rows) - 4.0 + 2 * pad_x
    height = 2 * pad_y + sum(float(v.frame().size.height) for v in rows) + sum(gaps_after)
    container = Cocoa.NSView.alloc().initWithFrame_(Cocoa.NSMakeRect(0.0, 0.0, width, height))
    y = height - pad_y
    for index, view in enumerate(rows):
        y -= float(view.frame().size.height)
        view.setFrameOrigin_(Cocoa.NSMakePoint(pad_x - 2.0, y))
        container.addSubview_(view)
        if index < len(gaps_after):
            y -= gaps_after[index]
    return container


class _AboutPopover(Cocoa.NSObject):
    """Version pop-up shown under a logo in Edit Assignments when the logo is clicked."""

    def init(self):
        self = objc.super(_AboutPopover, self).init()
        if self is None:
            return None
        self._popover = None
        self._last_closed = float("-inf")
        return self

    @objc.python_method
    def toggle_below(self, view) -> None:
        """Show the pop-up under `view`, or close it when it is already open."""
        if self._popover is not None and self._popover.isShown():
            self._popover.performClose_(None)
            return
        # A click on the logo first closes the open pop-up (it is transient); that click
        # should leave it closed rather than open it again.
        if time.monotonic() - self._last_closed < 0.3:
            return

        content = _about_popover_content(build_info.current())
        controller = Cocoa.NSViewController.alloc().init()
        controller.setView_(content)
        controller.setPreferredContentSize_(content.frame().size)
        popover = Cocoa.NSPopover.alloc().init()
        popover.setBehavior_(Cocoa.NSPopoverBehaviorTransient)
        popover.setDelegate_(self)
        popover.setContentViewController_(controller)
        popover.showRelativeToRect_ofView_preferredEdge_(
            view.bounds(), view, Cocoa.NSRectEdgeMinY
        )
        self._popover = popover

    @objc.python_method
    def close(self) -> None:
        if self._popover is not None:
            self._popover.performClose_(None)

    def popoverDidClose_(self, _notification) -> None:
        self._last_closed = time.monotonic()


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

    def windowDidBecomeKey_(self, _notification) -> None:
        self._cb["on_focus"]()

    def windowDidResignKey_(self, _notification) -> None:
        self._cb["on_blur"]()

    def windowDidMiniaturize_(self, _notification) -> None:
        self._cb["on_blur"]()

    def windowWillClose_(self, _notification) -> None:
        self._cb["on_close"]()

    def appDidResignActive_(self, _notification) -> None:
        self._cb["on_app_resign_active"]()

    def control_textView_doCommandBySelector_(
        self, _control, _text_view, command_selector
    ) -> bool:
        sel = str(command_selector)
        if sel == "insertNewline:":
            self._cb["on_save_interval"]()
            return True
        if sel == "cancelOperation:":
            self._cb["on_cancel_interval_edit"]()
            return True
        return False

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

    def autoMaximizeToggled_(self, _sender) -> None:
        self._cb["on_toggle_auto_maximize"]()

    def mouseSelectionToggled_(self, _sender) -> None:
        self._cb["on_toggle_mouse_selection"]()

    def slotWindowPickerToggled_(self, _sender) -> None:
        self._cb["on_toggle_slot_window_picker"]()

    def openAnimationsClicked_(self, _sender) -> None:
        self._cb["on_open_animations"]()

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
        on_open_animations_window: Callable[[Cocoa.NSWindow | None], None] | None = None,
    ) -> None:
        self._store = store
        self._app_registry = app_registry
        self._on_enter_editing = on_enter_editing
        self._on_exit_editing = on_exit_editing
        self._on_config_updated = on_config_updated
        self._on_quit_app = on_quit_app or (lambda: Cocoa.NSApp.terminate_(None))
        self._on_open_animations_window = on_open_animations_window

        self._window: Cocoa.NSWindow | None = None
        self._delegate: _AssignmentWindowDelegate | None = None
        self._grid_view: KeyboardMapGridView | None = None
        self._editing_active: bool = False

        self._layer: Layer = Layer.BASE
        self._selected_key: str = "T"
        self._show_all_apps: bool = False
        self._candidate_apps: list[AppRecord] = []

        # Controls
        self._layer_seg = None
        self._filter_seg = None
        self._slot_summary_label = None
        self._slot_window_picker_checkbox = None
        self._app_popup = None
        self._path_popup = None
        self._conflict_label = None
        self._interval_field = None
        self._auto_fullscreen_checkbox = None
        self._auto_maximize_checkbox = None
        self._mouse_selection_checkbox = None
        self._caps_hold_checkbox = None
        self._anim_status_label = None
        self._status_label = None
        self._repair_btn = None
        self._status_w: float = 0.0
        self._status_w_beside_repair: float = 0.0
        self._about_popover = _AboutPopover.alloc().init()

    def _mark_editing_active(self) -> None:
        if not self._editing_active:
            self._editing_active = True
            self._on_enter_editing()

    def _mark_editing_inactive(self) -> None:
        if self._editing_active:
            self._editing_active = False
            self._on_exit_editing()

    def _defocus_interval_field(self) -> None:
        if self._window is not None:
            self._window.makeFirstResponder_(None)
        if self._interval_field is not None:
            self._interval_field.setRefusesFirstResponder_(True)

    def _clear_transient_feedback(self) -> None:
        if self._status_label is not None and not self._store.load_error:
            self._status_label.setStringValue_("")
            self._status_label.setToolTip_(None)

    def show_window(self) -> None:
        if self._window is None:
            self._build_window()
        self._clear_transient_feedback()
        self._defocus_interval_field()
        self.refresh_data()
        assert self._window is not None
        self._window.setCollectionBehavior_(
            Cocoa.NSWindowCollectionBehaviorMoveToActiveSpace
            | Cocoa.NSWindowCollectionBehaviorFullScreenAuxiliary
        )
        self._window.setLevel_(Cocoa.NSFloatingWindowLevel)
        self._mark_editing_active()
        Cocoa.NSApp.activateIgnoringOtherApps_(True)
        self._window.makeKeyAndOrderFront_(None)
        self._defocus_interval_field()

    def close_if_open(self) -> None:
        if self._window is not None and self._window.isVisible():
            self._window.close()
        else:
            self._clear_transient_feedback()
            self._defocus_interval_field()
            self._mark_editing_inactive()

    def is_visible(self) -> bool:
        return bool(self._window is not None and self._window.isVisible())

    def _build_window(self) -> None:
        gw, gh = KeyboardMapGridView.required_size()
        win_w = gw + 40.0
        win_h = gh + 470.0
        style = (
            Cocoa.NSWindowStyleMaskTitled
            | Cocoa.NSWindowStyleMaskClosable
            | Cocoa.NSWindowStyleMaskMiniaturizable
        )
        win = _EditorWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            Cocoa.NSMakeRect(160.0, 120.0, win_w, win_h),
            style,
            Cocoa.NSBackingStoreBuffered,
            False,
        )
        win.setTitle_("AppJuggler — Edit Assignments")
        win.setReleasedWhenClosed_(False)
        win.setHidesOnDeactivate_(False)
        win.setAutorecalculatesKeyViewLoop_(False)
        win.setInitialFirstResponder_(None)
        win.setLevel_(Cocoa.NSFloatingWindowLevel)
        win.setCollectionBehavior_(
            Cocoa.NSWindowCollectionBehaviorMoveToActiveSpace
            | Cocoa.NSWindowCollectionBehaviorFullScreenAuxiliary
        )
        win.center()

        self._delegate = _AssignmentWindowDelegate.alloc().initWithCallbacks_(
            {
                "on_focus": self._handle_focus,
                "on_blur": self._handle_blur,
                "on_close": self._handle_close,
                "on_app_resign_active": self._handle_app_resign_active,
                "on_layer_changed": self._handle_layer_changed,
                "on_filter_changed": self._handle_filter_changed,
                "on_app_selected": self._handle_app_selected,
                "on_assign": self._handle_assign,
                "on_clear_slot": self._handle_clear_slot,
                "on_browse_app": self._handle_browse_app,
                "on_refresh_apps": self.refresh_data,
                "on_save_interval": self._handle_save_interval,
                "on_cancel_interval_edit": self._handle_cancel_interval_edit,
                "on_toggle_auto_fullscreen": self._handle_toggle_auto_fullscreen,
                "on_toggle_auto_maximize": self._handle_toggle_auto_maximize,
                "on_toggle_mouse_selection": self._handle_toggle_mouse_selection,
                "on_toggle_slot_window_picker": self._handle_toggle_slot_window_picker,
                "on_toggle_caps_hold": self._handle_toggle_caps_hold,
                "on_open_animations": self._handle_open_animations,
                "on_repair_config": self._handle_repair_config,
                "on_quit_app": self._on_quit_app,
            }
        )
        win.setDelegate_(self._delegate)
        Cocoa.NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            self._delegate,
            "appDidResignActive:",
            Cocoa.NSApplicationDidResignActiveNotification,
            None,
        )

        content = win.contentView()

        # --- Top Header: Left (AppJuggler Logo + Name + Subtitle) | Right (AI - Engineered By + Puffy Clouds + contact address) ---
        col_w = 138.0
        col_x = win_w - 20.0 - col_w
        hdr_sep_y = win_h - 116.0
        # Right column, bottom to top: contact address, logo, caption. The 80pt stack is centred
        # on the 88pt app icon and kept on whole points so it stays crisp on non-Retina displays.
        address_h = 15.0
        address_y = hdr_sep_y + 15.0
        badge_y = address_y + address_h + 4.0
        badge_h = 42.0
        caption_y = badge_y + badge_h + 4.0

        app_logo_img = self._load_app_icon()
        app_logo_view = _HighQualityImageView.alloc().initWithFrame_(
            Cocoa.NSMakeRect(11.0, hdr_sep_y + 10.5, 88.0, 88.0)
        )
        app_logo_view.set_image(app_logo_img)
        self._show_version_on_click(app_logo_view)
        content.addSubview_(app_logo_view)

        title_lbl = Cocoa.NSTextField.labelWithString_("AppJuggler")
        title_lbl.setFont_(theme.font_ui(24.0, bold=True))
        title_lbl.setTextColor_(Cocoa.NSColor.labelColor())
        title_lbl.setFrame_(
            Cocoa.NSMakeRect(104.0, hdr_sep_y + 55.0, col_x - 120.0, 30.0)
        )
        content.addSubview_(title_lbl)

        subtitle_lbl = Cocoa.NSTextField.labelWithString_(
            "Jump straight to any app or window from your keyboard."
        )
        subtitle_lbl.setFont_(theme.font_ui(13.5, bold=False))
        subtitle_lbl.setTextColor_(Cocoa.NSColor.secondaryLabelColor())
        subtitle_lbl.setFrame_(
            Cocoa.NSMakeRect(105.0, hdr_sep_y + 30.0, col_x - 122.0, 20.0)
        )
        content.addSubview_(subtitle_lbl)

        eng_lbl = _FullWidthCaptionView.alloc().initWithFrame_(
            Cocoa.NSMakeRect(col_x, caption_y, col_w, 15.0)
        )
        eng_lbl.configure(
            "AI - ENGINEERED BY",
            font=theme.font_ui(10.0, bold=True),
            color=Cocoa.NSColor.secondaryLabelColor(),
        )
        content.addSubview_(eng_lbl)

        logo_img = self._load_puffy_clouds_logo()
        logo_card = _RoundedCardView.alloc().initWithFrame_(
            Cocoa.NSMakeRect(col_x, badge_y, col_w, badge_h)
        )
        logo_card.configure(
            bg_color=Cocoa.NSColor.whiteColor(),
            border_color=theme.rgba(148, 163, 184, 0.40),
            radius=7.0,
            logo_image=logo_img,
        )
        if logo_img is None:
            fallback_lbl = Cocoa.NSTextField.labelWithString_("PUFFY CLOUDS")
            fallback_lbl.setFont_(theme.font_ui(11.0, bold=True))
            fallback_lbl.setTextColor_(Cocoa.NSColor.blackColor())
            fallback_lbl.setAlignment_(Cocoa.NSTextAlignmentCenter)
            fallback_lbl.setFrame_(Cocoa.NSMakeRect(4.0, 12.0, col_w - 8.0, 18.0))
            logo_card.addSubview_(fallback_lbl)
        self._show_version_on_click(logo_card)
        content.addSubview_(logo_card)

        address_btn = _CopyableAddressButton.alloc().initWithFrame_(
            Cocoa.NSMakeRect(col_x, address_y, col_w, address_h)
        )
        address_btn.configure(CONTACT_EMAIL)
        content.addSubview_(address_btn)

        hdr_sep = Cocoa.NSBox.alloc().initWithFrame_(
            Cocoa.NSMakeRect(20.0, hdr_sep_y, win_w - 40.0, 1.0)
        )
        hdr_sep.setBoxType_(Cocoa.NSBoxSeparator)
        content.addSubview_(hdr_sep)

        # Layer switcher + instructions row
        top_y = hdr_sep_y - 38.0
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
            "Click or press A–Z to select any key slot · Esc closes"
        )
        hint.setFrame_(Cocoa.NSMakeRect(438.0, top_y + 2.0, win_w - 458.0, 22.0))
        hint.setTextColor_(Cocoa.NSColor.secondaryLabelColor())
        hint.setAlignment_(Cocoa.NSTextAlignmentRight)
        content.addSubview_(hint)

        # Visual Keyboard Map (dark container frame)
        map_y = top_y - 10.0 - gh
        map_box = _RoundedCardView.alloc().initWithFrame_(
            Cocoa.NSMakeRect(20.0, map_y, gw, gh)
        )
        map_box.configure(
            bg_color=theme.BG_PANEL,
            border_color=theme.BG_PANEL_BORDER,
            radius=12.0,
        )

        grid = KeyboardMapGridView.alloc().initWithFrame_(
            Cocoa.NSMakeRect(0.0, 0.0, gw, gh)
        )
        map_box.addSubview_(grid)
        content.addSubview_(map_box)
        self._grid_view = grid

        # Selected slot summary + per-slot Window/Tab List toggle + Clear button
        row1_y = map_y - 40.0
        slot_lbl = Cocoa.NSTextField.labelWithString_("Selected Slot: T (Base)")
        slot_lbl.setFrame_(Cocoa.NSMakeRect(22.0, row1_y + 4.0, win_w - 384.0, 22.0))
        slot_lbl.setFont_(theme.font_ui(13.5, bold=True))
        slot_lbl.setLineBreakMode_(Cocoa.NSLineBreakByTruncatingMiddle)
        content.addSubview_(slot_lbl)
        self._slot_summary_label = slot_lbl

        slot_picker_chk = Cocoa.NSButton.checkboxWithTitle_target_action_(
            "Show Window / Tab List",
            self._delegate,
            "slotWindowPickerToggled:",
        )
        slot_picker_chk.setFrame_(
            Cocoa.NSMakeRect(win_w - 352.0, row1_y + 3.0, 202.0, 22.0)
        )
        slot_picker_chk.setState_(Cocoa.NSControlStateValueOn)
        content.addSubview_(slot_picker_chk)
        self._slot_window_picker_checkbox = slot_picker_chk

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
        assign_btn.setFrame_(Cocoa.NSMakeRect(win_w - 186.0, row3_y, 166.0, 28.0))
        content.addSubview_(assign_btn)

        # Conflict / bundle metadata notice row
        row4_y = row3_y - 26.0
        conf_lbl = Cocoa.NSTextField.labelWithString_("")
        conf_lbl.setFrame_(Cocoa.NSMakeRect(114.0, row4_y, win_w - 134.0, 20.0))
        conf_lbl.setFont_(theme.font_ui(11.5, bold=False))
        conf_lbl.setTextColor_(Cocoa.NSColor.secondaryLabelColor())
        content.addSubview_(conf_lbl)
        self._conflict_label = conf_lbl

        # Horizontal divider separating binding editor from options & diagnostics footer
        sep = Cocoa.NSBox.alloc().initWithFrame_(
            Cocoa.NSMakeRect(20.0, 146.0, win_w - 40.0, 1.0)
        )
        sep.setBoxType_(Cocoa.NSBoxSeparator)
        content.addSubview_(sep)

        # Optional checkboxes + Animations button/status + Double-Caps timing
        opts_top_y = 114.0
        opts_bot_y = 86.0
        anim_row_y = 48.0
        row5_y = 12.0

        max_chk = Cocoa.NSButton.checkboxWithTitle_target_action_(
            "Instant Maximize (Same Space — 0 ms)",
            self._delegate,
            "autoMaximizeToggled:",
        )
        max_chk.setFrame_(Cocoa.NSMakeRect(22.0, opts_top_y, 304.0, 22.0))
        max_chk.setState_(
            Cocoa.NSControlStateValueOn
            if self._store.auto_maximize_on_switch
            else Cocoa.NSControlStateValueOff
        )
        content.addSubview_(max_chk)
        self._auto_maximize_checkbox = max_chk

        fs_chk = Cocoa.NSButton.checkboxWithTitle_target_action_(
            "Switch Target Window to Full Screen",
            self._delegate,
            "autoFullscreenToggled:",
        )
        fs_chk.setFrame_(Cocoa.NSMakeRect(340.0, opts_top_y, 290.0, 22.0))
        fs_chk.setState_(
            Cocoa.NSControlStateValueOn
            if self._store.auto_fullscreen_on_switch
            else Cocoa.NSControlStateValueOff
        )
        content.addSubview_(fs_chk)
        self._auto_fullscreen_checkbox = fs_chk

        mouse_chk = Cocoa.NSButton.checkboxWithTitle_target_action_(
            "Mouse Hover & Click in HUD",
            self._delegate,
            "mouseSelectionToggled:",
        )
        mouse_chk.setFrame_(Cocoa.NSMakeRect(22.0, opts_bot_y, 260.0, 22.0))
        mouse_chk.setState_(
            Cocoa.NSControlStateValueOn
            if self._store.enable_mouse_selection
            else Cocoa.NSControlStateValueOff
        )
        content.addSubview_(mouse_chk)
        self._mouse_selection_checkbox = mouse_chk

        hold_chk = Cocoa.NSButton.checkboxWithTitle_target_action_(
            "Hold Caps 2s Quick Assign",
            self._delegate,
            "capsHoldToggled:",
        )
        hold_chk.setFrame_(Cocoa.NSMakeRect(340.0, opts_bot_y, 260.0, 22.0))
        hold_chk.setState_(
            Cocoa.NSControlStateValueOn
            if self._store.trigger.enable_caps_hold_assign
            else Cocoa.NSControlStateValueOff
        )
        content.addSubview_(hold_chk)
        self._caps_hold_checkbox = hold_chk

        anim_btn = Cocoa.NSButton.buttonWithTitle_target_action_(
            "Disable Window Animations…",
            self._delegate,
            "openAnimationsClicked:",
        )
        anim_btn.setFrame_(Cocoa.NSMakeRect(20.0, anim_row_y, 224.0, 28.0))
        content.addSubview_(anim_btn)

        anim_lbl = Cocoa.NSTextField.labelWithString_("")
        anim_lbl.setFont_(theme.font_ui(11.5, bold=False))
        anim_lbl.setFrame_(Cocoa.NSMakeRect(252.0, anim_row_y + 4.0, win_w - 272.0, 20.0))
        content.addSubview_(anim_lbl)
        self._anim_status_label = anim_lbl

        # Bottom row: Double-Caps trigger interval (click-only focus!) + status feedback + repair + Quit
        trig_lbl = Cocoa.NSTextField.labelWithString_("Double-Caps (ms):")
        trig_lbl.setFrame_(Cocoa.NSMakeRect(22.0, row5_y + 3.0, 124.0, 22.0))
        content.addSubview_(trig_lbl)

        iv_field = _ClickToFocusTextField.alloc().initWithFrame_(
            Cocoa.NSMakeRect(148.0, row5_y + 2.0, 58.0, 24.0)
        )
        iv_field.setStringValue_(str(self._store.trigger.interval_ms))
        iv_field.setDelegate_(self._delegate)
        content.addSubview_(iv_field)
        self._interval_field = iv_field

        iv_btn = Cocoa.NSButton.buttonWithTitle_target_action_(
            "Save Timing", self._delegate, "saveIntervalClicked:"
        )
        iv_btn.setFrame_(Cocoa.NSMakeRect(212.0, row5_y, 98.0, 28.0))
        content.addSubview_(iv_btn)

        # Quit sits at the far right. Feedback text and the repair button (hidden unless the
        # config file is damaged) share the space to its left.
        status_x = 322.0
        quit_w = 150.0
        quit_x = win_w - 20.0 - quit_w
        self._status_w = quit_x - 12.0 - status_x

        status_lbl = Cocoa.NSTextField.labelWithString_("")
        status_lbl.setFrame_(
            Cocoa.NSMakeRect(status_x, row5_y + 3.0, self._status_w, 22.0)
        )
        status_lbl.setFont_(theme.font_ui(12.0, bold=True))
        status_lbl.setLineBreakMode_(Cocoa.NSLineBreakByTruncatingTail)
        content.addSubview_(status_lbl)
        self._status_label = status_lbl

        repair_btn = Cocoa.NSButton.buttonWithTitle_target_action_(
            "Backup & Reset Damaged Config", self._delegate, "repairConfigClicked:"
        )
        # As wide as its title needs: a fixed 210pt used to cut the title short.
        repair_w = max(210.0, math.ceil(float(repair_btn.cell().cellSize().width)))
        repair_x = quit_x - 10.0 - repair_w
        self._status_w_beside_repair = repair_x - 12.0 - status_x
        repair_btn.setFrame_(Cocoa.NSMakeRect(repair_x, row5_y, repair_w, 28.0))
        repair_btn.setHidden_(True)
        content.addSubview_(repair_btn)
        self._repair_btn = repair_btn

        quit_btn = Cocoa.NSButton.buttonWithTitle_target_action_(
            "Quit AppJuggler", self._delegate, "quitAppClicked:"
        )
        quit_btn.setFrame_(Cocoa.NSMakeRect(quit_x, row5_y, quit_w, 28.0))
        content.addSubview_(quit_btn)

        win.configure_editor_input(
            click_focus_field=iv_field,
            on_key_char=self._handle_key_typed,
        )
        win.makeFirstResponder_(None)
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
        self._sync_mode_checkboxes_and_anim_summary()

        damaged = bool(self._store.load_error)
        self._repair_btn.setHidden_(not damaged)
        # The repair button takes the right part of the status area while it is visible.
        self._status_label.setFrameSize_(
            Cocoa.NSMakeSize(
                self._status_w_beside_repair if damaged else self._status_w, 22.0
            )
        )
        if damaged:
            self._set_feedback("Config damaged — not overwritten", is_error=True)

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
        if self._slot_window_picker_checkbox is not None:
            self._slot_window_picker_checkbox.setState_(
                Cocoa.NSControlStateValueOn
                if (existing is None or existing.show_window_picker)
                else Cocoa.NSControlStateValueOff
            )
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
            mode_note = "" if existing.show_window_picker else "  ·  [~ Direct Jump]"
            text = (
                f"Selected Slot: {prefix}{self._selected_key} ({self._layer.label})  →  "
                f"{existing.display_name} ({existing.bundle_id}){mode_note}{state_note}"
            )
        self._slot_summary_label.setStringValue_(text)

    def _handle_key_clicked(self, key_char: str) -> None:
        self._defocus_interval_field()
        self._selected_key = key_char
        self._update_slot_summary()

    def _handle_key_typed(self, key_char: str) -> None:
        self._selected_key = key_char
        if self._grid_view is not None:
            self._grid_view.set_selected_key(key_char)
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

        picker_enabled = (
            True
            if self._slot_window_picker_checkbox is None
            else bool(
                self._slot_window_picker_checkbox.state()
                == Cocoa.NSControlStateValueOn
            )
        )
        new_binding = AppBinding(
            key=self._selected_key,
            layer=self._layer,
            bundle_id=rec.bundle_id,
            display_name=rec.display_name,
            bundle_path=chosen_path,
            show_window_picker=picker_enabled,
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
        self._defocus_interval_field()
        self._set_feedback(f"Saved double-Caps interval: {ms} ms", is_error=False)
        self._on_config_updated()

    def _handle_cancel_interval_edit(self) -> None:
        if self._interval_field is not None:
            self._interval_field.setStringValue_(str(self._store.trigger.interval_ms))
        self._defocus_interval_field()

    def _handle_focus(self) -> None:
        self._mark_editing_active()
        if self._window is not None and not isinstance(
            self._window.firstResponder(), Cocoa.NSTextView
        ):
            self._defocus_interval_field()
        self._sync_mode_checkboxes_and_anim_summary()

    def _handle_blur(self) -> None:
        self._defocus_interval_field()
        self._mark_editing_inactive()

    def _handle_app_resign_active(self) -> None:
        self._clear_transient_feedback()
        self._defocus_interval_field()

    def _sync_mode_checkboxes_and_anim_summary(self) -> None:
        if self._auto_maximize_checkbox is not None:
            self._auto_maximize_checkbox.setState_(
                Cocoa.NSControlStateValueOn
                if self._store.auto_maximize_on_switch
                else Cocoa.NSControlStateValueOff
            )
        if self._auto_fullscreen_checkbox is not None:
            self._auto_fullscreen_checkbox.setState_(
                Cocoa.NSControlStateValueOn
                if self._store.auto_fullscreen_on_switch
                else Cocoa.NSControlStateValueOff
            )
        if self._mouse_selection_checkbox is not None:
            self._mouse_selection_checkbox.setState_(
                Cocoa.NSControlStateValueOn
                if self._store.enable_mouse_selection
                else Cocoa.NSControlStateValueOff
            )
        if self._caps_hold_checkbox is not None:
            self._caps_hold_checkbox.setState_(
                Cocoa.NSControlStateValueOn
                if self._store.trigger.enable_caps_hold_assign
                else Cocoa.NSControlStateValueOff
            )
        if self._anim_status_label is not None:
            st = read_macos_animation_status()
            if self._store.auto_fullscreen_on_switch:
                self._anim_status_label.setStringValue_(
                    "⚠ Native Full Screen creates Spaces (~500 ms slide/fade) — click for 0 ms guide"
                )
                self._anim_status_label.setTextColor_(Cocoa.NSColor.systemOrangeColor())
            elif st.all_programmable_disabled:
                self._anim_status_label.setStringValue_(
                    "✓ macOS Window Animations Disabled · Same-Space 0 ms Switching Active"
                )
                self._anim_status_label.setTextColor_(Cocoa.NSColor.systemGreenColor())
            else:
                self._anim_status_label.setStringValue_(
                    "Click to disable macOS window/Dock animations & view 0 ms Space guide"
                )
                self._anim_status_label.setTextColor_(
                    Cocoa.NSColor.secondaryLabelColor()
                )

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
        self._sync_mode_checkboxes_and_anim_summary()
        state_txt = "enabled (creates Spaces)" if enabled else "disabled"
        self._set_feedback(
            f"Switch Target Window to Full Screen {state_txt}", is_error=False
        )
        self._on_config_updated()

    def _handle_toggle_auto_maximize(self) -> None:
        if self._auto_maximize_checkbox is None:
            return
        enabled = bool(
            self._auto_maximize_checkbox.state() == Cocoa.NSControlStateValueOn
        )
        try:
            self._store.set_auto_maximize_on_switch(enabled)
        except Exception as exc:
            self._set_feedback(f"Could not save option: {exc}", is_error=True)
            return
        self._sync_mode_checkboxes_and_anim_summary()
        state_txt = "enabled" if enabled else "disabled"
        self._set_feedback(
            f"Instant Maximize (Same Space — 0 ms) {state_txt}", is_error=False
        )
        self._on_config_updated()

    def _handle_open_animations(self) -> None:
        if self._on_open_animations_window is not None:
            self._on_open_animations_window(self._window)

    def _handle_toggle_mouse_selection(self) -> None:
        if self._mouse_selection_checkbox is None:
            return
        enabled = bool(
            self._mouse_selection_checkbox.state() == Cocoa.NSControlStateValueOn
        )
        try:
            self._store.set_enable_mouse_selection(enabled)
        except Exception as exc:
            self._set_feedback(f"Could not save option: {exc}", is_error=True)
            return
        state_txt = "enabled" if enabled else "disabled"
        self._set_feedback(f"Mouse Hover & Click in HUD {state_txt}", is_error=False)
        self._on_config_updated()

    def _handle_toggle_slot_window_picker(self) -> None:
        if self._slot_window_picker_checkbox is None:
            return
        enabled = bool(
            self._slot_window_picker_checkbox.state() == Cocoa.NSControlStateValueOn
        )
        prefix = "⇧" if self._layer is Layer.SHIFT else ""
        existing = self._store.get_slot(self._selected_key, self._layer)
        if existing is None:
            mode_txt = "Window / Tab List" if enabled else "Direct Jump (~)"
            self._set_feedback(
                f"Slot {prefix}{self._selected_key}: will assign with {mode_txt}",
                is_error=False,
            )
            return

        try:
            self._store.set_slot_window_picker(
                self._selected_key, self._layer, enabled
            )
        except CorruptedConfigLockError as exc:
            self._show_alert("Configuration Locked", str(exc))
            return
        except Exception as exc:
            self._set_feedback(f"Could not save slot option: {exc}", is_error=True)
            return

        mode_txt = (
            "Show Window / Tab List"
            if enabled
            else "Direct Jump (skips window list)"
        )
        self._set_feedback(
            f"{prefix}{self._selected_key} ({existing.display_name}): {mode_txt}",
            is_error=False,
        )
        self._on_config_updated()
        self.refresh_data()

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

    def _show_version_on_click(self, view: _ClickableView) -> None:
        """Clicking a logo shows the version pop-up under it."""
        view.setAccessibilityLabel_("Version information")
        view.set_on_click(lambda: self._about_popover.toggle_below(view))

    def _handle_close(self) -> None:
        self._about_popover.close()
        self._clear_transient_feedback()
        self._defocus_interval_field()
        self._mark_editing_inactive()

    def _set_feedback(self, msg: str, is_error: bool) -> None:
        if self._status_label is None:
            return
        self._status_label.setStringValue_(msg)
        self._status_label.setTextColor_(
            Cocoa.NSColor.systemRedColor()
            if is_error
            else Cocoa.NSColor.systemGreenColor()
        )
        # The label truncates long messages; the tooltip keeps the full text reachable.
        self._status_label.setToolTip_(msg or None)

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
    def _load_app_icon():
        try:
            raw = (
                importlib.resources.files("appjuggler.assets")
                .joinpath("app_icon.png")
                .read_bytes()
            )
            ns_data = Cocoa.NSData.dataWithBytes_length_(raw, len(raw))
            return Cocoa.NSImage.alloc().initWithData_(ns_data)
        except Exception:
            return None

    @staticmethod
    def _load_puffy_clouds_logo():
        try:
            raw = (
                importlib.resources.files("appjuggler.assets")
                .joinpath("puffy_clouds_logo.png")
                .read_bytes()
            )
            ns_data = Cocoa.NSData.dataWithBytes_length_(raw, len(raw))
            return Cocoa.NSImage.alloc().initWithData_(ns_data)
        except Exception:
            return None

    @staticmethod
    def _show_alert(title: str, message: str) -> None:
        alert = Cocoa.NSAlert.alloc().init()
        alert.setAlertStyle_(Cocoa.NSAlertStyleWarning)
        alert.setMessageText_(title)
        alert.setInformativeText_(message)
        alert.addButtonWithTitle_("OK")
        alert.runModal()
