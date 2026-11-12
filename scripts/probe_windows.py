#!/usr/bin/env python3
"""Stage 0 Probe: Inspect macOS running apps, Spaces/Displays, AX windows, and HUD NSPanel."""

from __future__ import annotations

import argparse
import time

import Cocoa

from appjuggler.domain.models import AppBinding, Layer
from appjuggler.domain.window_slots import WindowSlotTable
from appjuggler.platforms.macos.app_registry import MacOSApplicationRegistry
from appjuggler.platforms.macos.permissions import MacOSPermissionMonitor
from appjuggler.platforms.macos.ui.overlay_panel import MacOSOverlayPresenter
from appjuggler.platforms.macos.window_registry import MacOSWindowRegistry


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect NSWorkspace applications, displays, and AX window slots."
    )
    parser.add_argument(
        "--bundle",
        default="com.apple.Terminal",
        help="Target bundle identifier to inspect windows for (default: com.apple.Terminal).",
    )
    parser.add_argument(
        "--all-apps",
        action="store_true",
        help="Enumerate windows across all running regular applications.",
    )
    parser.add_argument(
        "--test-panel-sec",
        type=float,
        default=0.0,
        help="Briefly display the non-activating FullScreenAuxiliary NSPanel for N seconds.",
    )
    args = parser.parse_args()

    perm = MacOSPermissionMonitor().check_status(prompt=False)
    app_reg = MacOSApplicationRegistry()
    win_reg = MacOSWindowRegistry()
    slot_table = WindowSlotTable()

    screens = Cocoa.NSScreen.screens()
    print("=== AppJuggler Stage 0: Displays, Apps & Windows Probe ===")
    print(f"Connected Displays  : {len(screens)}")
    for idx, scr in enumerate(screens, start=1):
        f = scr.frame()
        print(
            f"  Display #{idx}: {int(f.size.width)}x{int(f.size.height)} "
            f"at ({int(f.origin.x)}, {int(f.origin.y)}) scale={scr.backingScaleFactor():.1f}x"
        )

    print(f"Accessibility Trust : {perm.accessibility_trusted}")
    running = app_reg.running_apps()
    print(f"Regular Running Apps: {len(running)}")
    for rec in running:
        flags = []
        if rec.has_missing_bundle_id:
            flags.append("NO_BUNDLE_ID")
        if rec.has_duplicate_install_conflict:
            flags.append(f"DUPLICATE_INSTALLS={len(rec.candidate_paths)}")
        if rec.is_hidden:
            flags.append("HIDDEN")
        extra = f" [{', '.join(flags)}]" if flags else ""
        print(f"  - {rec.display_name:24s} | {rec.bundle_id or '<none>':36s} | pids={list(rec.pids)}{extra}")

    if not perm.accessibility_trusted:
        print(
            "\n[Note] AX window enumeration requires Accessibility permission "
            "(AXIsProcessTrusted=False; expected AXError=-25211 until granted)."
        )
    else:
        targets = (
            [r.bundle_id for r in running if r.bundle_id]
            if args.all_apps
            else [args.bundle]
        )
        print("\n=== AX Window Discovery ===")
        for bid in targets:
            wins = win_reg.discover_windows(bid)
            snap = slot_table.reconcile(bid, wins)
            print(
                f"Bundle: {bid} -> {len(snap.slots)} qualifying window(s) "
                f"(default_slot={snap.default_slot}, gen={snap.generation})"
            )
            for s in snap.slots:
                w = s.window
                print(
                    f"    Slot #{s.slot_number}: id={w.window_id} min={w.is_minimized} "
                    f"full={w.is_fullscreen} modal={w.is_modal} focus={w.is_focused} "
                    f"space={w.space_label or 'active'}"
                )

    if args.test_panel_sec > 0:
        app = Cocoa.NSApplication.sharedApplication()
        app.setActivationPolicy_(Cocoa.NSApplicationActivationPolicyAccessory)
        overlay = MacOSOverlayPresenter(app_reg)
        sample_bindings = (
            AppBinding(
                key="T",
                layer=Layer.BASE,
                bundle_id="com.apple.Terminal",
                display_name="Terminal",
            ),
            AppBinding(
                key="F",
                layer=Layer.BASE,
                bundle_id="com.apple.finder",
                display_name="Finder",
            ),
        )
        overlay.show_app_map(
            active_layer=Layer.BASE,
            bindings=sample_bindings,
            running_bundle_ids=app_reg.running_bundle_ids(),
            duplicate_conflicts=frozenset(),
            notice="Stage 0 Non-Activating NSPanel Probe",
        )
        end_at = time.monotonic() + args.test_panel_sec
        while time.monotonic() < end_at:
            Cocoa.NSRunLoop.currentRunLoop().runUntilDate_(
                Cocoa.NSDate.dateWithTimeIntervalSinceNow_(0.05)
            )
        overlay.hide()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
