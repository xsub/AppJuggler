"""macOS hardware keycode & layout-aware base-letter decoder.

Separates physical virtual keycode from logical letter A–Z (§2.3):
- Resolves base unshifted/un-caps-locked letters on the main thread via Carbon's
  `TISCopyCurrentKeyboardLayoutInputSource` + `UCKeyTranslate(modifierKeyState=0)`.
- Falls back to standard ANSI QWERTY virtual keycodes when a non-Latin input source
  (e.g. Cyrillic, Hebrew, Kana) does not produce A–Z on base keys.
- Decodes events inside the CGEventTap callback via pure O(1) dictionary lookups
  with zero framework calls and complete independence from Caps Lock state.
"""

from __future__ import annotations

import ctypes
from ctypes import POINTER, byref, c_int32, c_uint8, c_uint16, c_uint32, c_ulong, c_void_p

from appjuggler.domain.events import HardwareKeyEvent, KeyKind

# macOS HIToolbox virtual keycodes (Events.h)
VK_CAPS_LOCK = 57
VK_ESCAPE = 53
VK_TAB = 48
VK_COMMA = 43
VK_GRAVE = 50
VK_ISO_SECTION = 10
VK_DELETE = 51
VK_FORWARD_DELETE = 117
VK_RETURN = 36
VK_KEYPAD_ENTER = 76
VK_UP_ARROW = 126
VK_DOWN_ARROW = 125

VK_SHIFT_KEYS: frozenset[int] = frozenset({56, 60})
VK_OTHER_MODIFIER_KEYS: frozenset[int] = frozenset({54, 55, 58, 59, 61, 62, 63})

ANSI_KEYCODE_TO_LETTER: dict[int, str] = {
    0: "A",
    1: "S",
    2: "D",
    3: "F",
    4: "H",
    5: "G",
    6: "Z",
    7: "X",
    8: "C",
    9: "V",
    11: "B",
    12: "Q",
    13: "W",
    14: "E",
    15: "R",
    16: "Y",
    17: "T",
    31: "O",
    32: "U",
    34: "I",
    35: "P",
    37: "L",
    38: "J",
    40: "K",
    45: "N",
    46: "M",
}

KEYCODE_TO_DIGIT: dict[int, str] = {
    # Main number row
    18: "1",
    19: "2",
    20: "3",
    21: "4",
    23: "5",
    22: "6",
    26: "7",
    28: "8",
    25: "9",
    29: "0",
    # Numeric keypad
    83: "1",
    84: "2",
    85: "3",
    86: "4",
    87: "5",
    88: "6",
    89: "7",
    91: "8",
    92: "9",
    82: "0",
}

# CGEventFlags bitmasks
FLAG_ALPHA_SHIFT = 0x00010000  # Caps Lock
FLAG_SHIFT = 0x00020000
FLAG_CONTROL = 0x00040000
FLAG_ALTERNATE = 0x00080000  # Option
FLAG_COMMAND = 0x00100000
OTHER_MODIFIERS_MASK = FLAG_CONTROL | FLAG_ALTERNATE | FLAG_COMMAND


def query_current_layout_letter_map() -> tuple[dict[int, str], str, bool]:
    """Build `{keycode: 'A'..'Z'}` from the active macOS keyboard layout (main thread only).

    Returns `(keycode_to_letter, layout_name, used_ansi_fallback)`.
    """
    try:
        carbon = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")

        carbon.TISCopyCurrentKeyboardLayoutInputSource.restype = c_void_p
        carbon.TISGetInputSourceProperty.argtypes = [c_void_p, c_void_p]
        carbon.TISGetInputSourceProperty.restype = c_void_p
        carbon.LMGetKbdType.restype = c_uint8
        carbon.UCKeyTranslate.argtypes = [
            c_void_p,
            c_uint16,
            c_uint16,
            c_uint32,
            c_uint32,
            c_uint32,
            POINTER(c_uint32),
            c_ulong,
            POINTER(c_ulong),
            POINTER(c_uint16),
        ]
        carbon.UCKeyTranslate.restype = c_int32

        cf.CFDataGetBytePtr.argtypes = [c_void_p]
        cf.CFDataGetBytePtr.restype = c_void_p
        cf.CFRelease.argtypes = [c_void_p]

        prop_layout = c_void_p.in_dll(carbon, "kTISPropertyUnicodeKeyLayoutData")
        prop_name = c_void_p.in_dll(carbon, "kTISPropertyLocalizedName")

        src = carbon.TISCopyCurrentKeyboardLayoutInputSource()
        if not src:
            return dict(ANSI_KEYCODE_TO_LETTER), "ANSI Fallback", True

        try:
            layout_name = "macOS Layout"
            name_ref = carbon.TISGetInputSourceProperty(src, prop_name)
            if name_ref:
                try:
                    import objc

                    layout_name = str(objc.objc_object(c_void_p=name_ref))
                except Exception:
                    pass

            layout_data = carbon.TISGetInputSourceProperty(src, prop_layout)
            if not layout_data:
                return dict(ANSI_KEYCODE_TO_LETTER), layout_name, True

            data_ptr = cf.CFDataGetBytePtr(layout_data)
            if not data_ptr:
                return dict(ANSI_KEYCODE_TO_LETTER), layout_name, True

            kbd_type = carbon.LMGetKbdType()
            resolved: dict[int, str] = {}
            for kc in range(51):
                if kc in KEYCODE_TO_DIGIT or kc in (VK_RETURN, VK_ESCAPE):
                    continue
                dead_key = c_uint32(0)
                actual_len = c_ulong(0)
                buf = (c_uint16 * 4)()
                # modifierKeyState=0 ensures Shift and Caps Lock are completely ignored;
                # optionBits=1 (kUCKeyTranslateNoDeadKeysBit) resolves dead keys cleanly.
                status = carbon.UCKeyTranslate(
                    data_ptr,
                    kc,
                    0,
                    0,
                    kbd_type,
                    1,
                    byref(dead_key),
                    4,
                    byref(actual_len),
                    buf,
                )
                if status == 0 and actual_len.value == 1:
                    ch = chr(buf[0]).upper()
                    if "A" <= ch <= "Z":
                        resolved[kc] = ch

            # Require at least 20 ASCII Latin letters (standard Latin layouts have 26);
            # otherwise fall back to physical ANSI QWERTY positions for non-Latin scripts.
            if len(set(resolved.values())) >= 20:
                return resolved, layout_name, False
            return dict(ANSI_KEYCODE_TO_LETTER), f"{layout_name} (ANSI fallback)", True
        finally:
            cf.CFRelease(src)
    except Exception:
        return dict(ANSI_KEYCODE_TO_LETTER), "ANSI Fallback", True


class KeyEventDecoder:
    """Zero-allocation-overhead hardware event decoder with Caps Lock toggle filtering."""

    def __init__(
        self,
        initial_caps_on: bool = False,
        letter_map: dict[int, str] | None = None,
    ) -> None:
        self._last_caps_on: bool = initial_caps_on
        self._letter_map: dict[int, str] = (
            dict(letter_map) if letter_map is not None else dict(ANSI_KEYCODE_TO_LETTER)
        )
        self.layout_description: str = "ANSI QWERTY"
        self.using_ansi_fallback: bool = False

    def refresh_from_system_layout(self) -> None:
        """Refresh cached keycode->letter dictionary on the main thread."""
        mapping, name, fallback = query_current_layout_letter_map()
        # Atomic reference replacement for lock-free reading in event tap thread.
        self._letter_map = mapping
        self.layout_description = name
        self.using_ansi_fallback = fallback

    def sync_caps_state(self, caps_on: bool) -> None:
        self._last_caps_on = caps_on

    def decode_flags_changed(
        self, keycode: int, flags: int, timestamp_ns: int
    ) -> HardwareKeyEvent:
        """Decode a `kCGEventFlagsChanged` event, distinguishing genuine Caps Lock toggles."""
        caps_on = bool(flags & FLAG_ALPHA_SHIFT)
        shift_held = bool(flags & FLAG_SHIFT)
        other_mods = bool(flags & OTHER_MODIFIERS_MASK)

        if keycode == VK_CAPS_LOCK:
            toggled = caps_on != self._last_caps_on
            self._last_caps_on = caps_on
            return HardwareKeyEvent(
                kind=KeyKind.CAPS_LOCK,
                keycode=keycode,
                timestamp_ns=timestamp_ns,
                is_key_down=True,
                symbol=None,
                shift_held=shift_held,
                caps_lock_on=caps_on,
                other_modifiers_held=other_mods,
                caps_lock_toggled=toggled,
            )

        # Keep Caps Lock state synchronized on any other modifier event as well.
        self._last_caps_on = caps_on

        if keycode in VK_SHIFT_KEYS:
            return HardwareKeyEvent(
                kind=KeyKind.SHIFT_MODIFIER,
                keycode=keycode,
                timestamp_ns=timestamp_ns,
                is_key_down=shift_held,
                shift_held=shift_held,
                caps_lock_on=caps_on,
                other_modifiers_held=other_mods,
            )

        return HardwareKeyEvent(
            kind=KeyKind.OTHER_MODIFIER,
            keycode=keycode,
            timestamp_ns=timestamp_ns,
            is_key_down=other_mods,
            shift_held=shift_held,
            caps_lock_on=caps_on,
            other_modifiers_held=other_mods,
        )

    def decode_key_event(
        self,
        keycode: int,
        flags: int,
        is_key_down: bool,
        is_autorepeat: bool,
        timestamp_ns: int,
    ) -> HardwareKeyEvent:
        """Decode a `kCGEventKeyDown` or `kCGEventKeyUp` event."""
        caps_on = bool(flags & FLAG_ALPHA_SHIFT)
        shift_held = bool(flags & FLAG_SHIFT)
        other_mods = bool(flags & OTHER_MODIFIERS_MASK)
        self._last_caps_on = caps_on

        if keycode == VK_ESCAPE:
            kind = KeyKind.ESCAPE
            symbol = None
        elif keycode == VK_TAB:
            kind = KeyKind.TAB
            symbol = None
        elif keycode in (VK_RETURN, VK_KEYPAD_ENTER):
            kind = KeyKind.ENTER
            symbol = None
        elif keycode == VK_UP_ARROW:
            kind = KeyKind.ARROW_UP
            symbol = None
        elif keycode == VK_DOWN_ARROW:
            kind = KeyKind.ARROW_DOWN
            symbol = None
        elif keycode in (VK_DELETE, VK_FORWARD_DELETE):
            kind = KeyKind.DELETE
            symbol = None
        elif keycode in KEYCODE_TO_DIGIT:
            kind = KeyKind.DIGIT
            symbol = KEYCODE_TO_DIGIT[keycode]
        else:
            letter = self._letter_map.get(keycode)
            if letter is not None:
                kind = KeyKind.LETTER
                symbol = letter
            elif keycode == VK_COMMA:
                kind = KeyKind.COMMA
                symbol = None
            elif keycode in (VK_GRAVE, VK_ISO_SECTION):
                kind = KeyKind.TILDE
                symbol = "~"
            else:
                kind = KeyKind.OTHER_KEY
                symbol = None

        return HardwareKeyEvent(
            kind=kind,
            keycode=keycode,
            timestamp_ns=timestamp_ns,
            is_key_down=is_key_down,
            is_autorepeat=is_autorepeat,
            symbol=symbol,
            shift_held=shift_held,
            caps_lock_on=caps_on,
            other_modifiers_held=other_mods,
        )
