#!/usr/bin/env python3
"""Stage 0 Probe: Measure Caps Lock CGEventTap representation and double-tap intervals.

Privacy note: Never records or prints typed characters — non-Caps keys are logged
only as generic candidate-invalidation events.
"""

from __future__ import annotations

import argparse
import platform
import sys
import time

import Quartz

from appjuggler.platforms.macos.keymap import (
    FLAG_ALPHA_SHIFT,
    VK_CAPS_LOCK,
    KeyEventDecoder,
)
from appjuggler.platforms.macos.permissions import MacOSPermissionMonitor


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure Caps Lock hardware events on macOS.")
    parser.add_argument(
        "--seconds",
        type=float,
        default=15.0,
        help="Listening duration in seconds (default: 15).",
    )
    parser.add_argument(
        "--interval-ms",
        type=int,
        default=300,
        help="Candidate double-tap threshold in ms (default: 300).",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Print environment/layout/tap capability report without interactive loop.",
    )
    args = parser.parse_args()

    perm = MacOSPermissionMonitor().check_status(prompt=False)
    init_flags = int(
        Quartz.CGEventSourceFlagsState(Quartz.kCGEventSourceStateCombinedSessionState)
    )
    init_caps = bool(init_flags & FLAG_ALPHA_SHIFT)
    decoder = KeyEventDecoder(initial_caps_on=init_caps)
    decoder.refresh_from_system_layout()

    print("=== AppJuggler Stage 0: Caps Lock Probe ===")
    print(f"macOS Version       : {platform.mac_ver()[0]} ({platform.machine()})")
    print(f"Keyboard Layout     : {decoder.layout_description} (ANSI fallback={decoder.using_ansi_fallback})")
    print(f"Initial Caps Lock   : {'ON' if init_caps else 'OFF'}")
    print(f"Accessibility Trust : {perm.accessibility_trusted}")
    print(f"Active Tap Capable  : {perm.input_tap_available}")

    if args.check_only:
        return 0

    if not perm.input_tap_available:
        print("\n[!] Cannot open CGEventTap without Accessibility / Input Monitoring permission.")
        for step in perm.remediation_steps:
            print(f"    - {step}")
        return 1

    last_toggle_ns: list[int | None] = [None]
    measured_deltas_ms: list[float] = []

    def _callback(_proxy, event_type, event, _refcon):
        now_ns = time.monotonic_ns()
        kc = int(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode))
        flags = int(Quartz.CGEventGetFlags(event))

        if event_type == Quartz.kCGEventFlagsChanged and kc == VK_CAPS_LOCK:
            ev = decoder.decode_flags_changed(kc, flags, now_ns)
            if ev.caps_lock_toggled:
                prev = last_toggle_ns[0]
                if prev is not None:
                    dt_ms = (now_ns - prev) / 1_000_000.0
                    hit = dt_ms <= args.interval_ms
                    if dt_ms <= 1200.0:
                        measured_deltas_ms.append(dt_ms)
                    print(
                        f"  [ACTUATION] CapsLock -> {'ON ' if ev.caps_lock_on else 'OFF'} | "
                        f"dt={dt_ms:6.1f} ms | {'TRIGGER!' if hit else 'single'}"
                    )
                    last_toggle_ns[0] = None if hit else now_ns
                else:
                    print(
                        f"  [ACTUATION] CapsLock -> {'ON ' if ev.caps_lock_on else 'OFF'} | (1st tap candidate)"
                    )
                    last_toggle_ns[0] = now_ns
            else:
                print(
                    f"  [IGNORED  ] Non-toggling flagsChanged(keycode=57, caps={'ON' if ev.caps_lock_on else 'OFF'})"
                )
        elif event_type == Quartz.kCGEventKeyDown:
            decoder.decode_key_event(kc, flags, True, False, now_ns)
            if last_toggle_ns[0] is not None:
                print("  [INVALIDAT] Intervening keyDown cancelled pending Caps candidate")
                last_toggle_ns[0] = None
        return event

    mask = Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged) | Quartz.CGEventMaskBit(
        Quartz.kCGEventKeyDown
    )
    tap = Quartz.CGEventTapCreate(
        Quartz.kCGSessionEventTap,
        Quartz.kCGHeadInsertEventTap,
        Quartz.kCGEventTapOptionListenOnly,
        mask,
        _callback,
        None,
    )
    if tap is None:
        print("[!] Failed to create listen-only CGEventTap.")
        return 1

    src = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
    rl = Quartz.CFRunLoopGetCurrent()
    Quartz.CFRunLoopAddSource(rl, src, Quartz.kCFRunLoopCommonModes)
    Quartz.CGEventTapEnable(tap, True)

    print(f"\nListening for {args.seconds:.0f}s — try single Caps, double Caps, and Caps->Letter->Caps...")
    Quartz.CFRunLoopRunInMode(Quartz.kCFRunLoopDefaultMode, args.seconds, False)
    Quartz.CFMachPortInvalidate(tap)

    if measured_deltas_ms:
        print(
            f"\nSummary: {len(measured_deltas_ms)} pair(s) measured | "
            f"min={min(measured_deltas_ms):.1f} ms | max={max(measured_deltas_ms):.1f} ms"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
