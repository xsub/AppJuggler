"""Visual theme colors, fonts, and drawing helpers for AppJuggler's AppKit HUD."""

from __future__ import annotations

import Cocoa


def rgba(r: int, g: int, b: int, a: float = 1.0) -> Cocoa.NSColor:
    return Cocoa.NSColor.colorWithCalibratedRed_green_blue_alpha_(
        r / 255.0, g / 255.0, b / 255.0, a
    )


# HUD Surfaces
BG_PANEL = rgba(18, 20, 26, 0.94)
BG_PANEL_BORDER = rgba(255, 255, 255, 0.14)
BG_HEADER = rgba(28, 31, 40, 0.95)

# Keycaps
KEY_UNASSIGNED_BG = rgba(32, 36, 46, 0.68)
KEY_UNASSIGNED_BORDER = rgba(255, 255, 255, 0.07)

KEY_CLOSED_BG = rgba(38, 43, 56, 0.88)
KEY_CLOSED_BORDER = rgba(255, 255, 255, 0.15)

KEY_RUNNING_BG = rgba(46, 56, 78, 0.96)
KEY_RUNNING_BORDER = rgba(110, 168, 254, 0.55)

KEY_SELECTED_BG = rgba(52, 95, 185, 0.96)
KEY_SELECTED_BORDER = rgba(145, 198, 255, 0.95)

# Accents & Status
ACCENT_BLUE = rgba(88, 154, 255, 1.0)
ACCENT_SHIFT = rgba(245, 166, 35, 1.0)
STATUS_RUNNING = rgba(52, 211, 153, 1.0)
STATUS_CLOSED = rgba(148, 163, 184, 0.55)
STATUS_WARNING = rgba(251, 191, 36, 1.0)
STATUS_ERROR = rgba(248, 113, 113, 1.0)
STATUS_MODAL = rgba(244, 114, 182, 1.0)
STATUS_SPACE = rgba(56, 189, 248, 1.0)

# Typography colors
TEXT_PRIMARY = rgba(248, 250, 252, 1.0)
TEXT_SECONDARY = rgba(168, 179, 196, 1.0)
TEXT_MUTED = rgba(110, 120, 138, 0.85)


def font_ui(size: float, bold: bool = False) -> Cocoa.NSFont:
    weight = Cocoa.NSFontWeightSemibold if bold else Cocoa.NSFontWeightRegular
    return Cocoa.NSFont.systemFontOfSize_weight_(size, weight)


def font_mono(size: float, bold: bool = True) -> Cocoa.NSFont:
    weight = Cocoa.NSFontWeightBold if bold else Cocoa.NSFontWeightMedium
    return Cocoa.NSFont.monospacedSystemFontOfSize_weight_(size, weight)


def fill_round_rect(
    rect: Cocoa.NSRect,
    radius: float,
    fill_color: Cocoa.NSColor,
    stroke_color: Cocoa.NSColor | None = None,
    line_width: float = 1.0,
) -> None:
    path = Cocoa.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        rect, radius, radius
    )
    fill_color.setFill()
    path.fill()
    if stroke_color is not None:
        stroke_color.setStroke()
        path.setLineWidth_(line_width)
        path.stroke()


def draw_text(
    text: str,
    rect: Cocoa.NSRect,
    font: Cocoa.NSFont,
    color: Cocoa.NSColor,
    align: int = Cocoa.NSTextAlignmentLeft,
) -> None:
    style = Cocoa.NSMutableParagraphStyle.alloc().init()
    style.setAlignment_(align)
    style.setLineBreakMode_(Cocoa.NSLineBreakByTruncatingTail)
    attrs = {
        Cocoa.NSFontAttributeName: font,
        Cocoa.NSForegroundColorAttributeName: color,
        Cocoa.NSParagraphStyleAttributeName: style,
    }
    ns_str = Cocoa.NSString.stringWithString_(text)
    ns_str.drawInRect_withAttributes_(rect, attrs)
