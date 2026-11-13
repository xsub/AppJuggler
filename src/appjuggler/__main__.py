"""CLI & GUI entry point for AppJuggler."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import sys

from appjuggler import build_info
from appjuggler.persistence.binding_store import BindingStore, default_config_path


def _configure_logging(verbose: bool) -> None:
    # Spec §4.1: Diagnostic logs cover state transitions, timings, and errors only —
    # never raw keystrokes or full window titles.
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _run_doctor(config_path: Path) -> int:
    """Print non-invasive environment, permission, and configuration diagnostics."""
    store = BindingStore(path=config_path)
    report: dict[str, object] = {
        "version": build_info.current().version,
        "build": build_info.current().build,
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "config_path": str(store.path),
        "config_load_error": store.load_error,
        "trigger_interval_ms": store.trigger.interval_ms,
        "bindings_count": len(store.bindings),
    }

    if sys.platform == "darwin":
        from appjuggler.platforms.macos.app_registry import MacOSApplicationRegistry
        from appjuggler.platforms.macos.keymap import query_current_layout_letter_map
        from appjuggler.platforms.macos.permissions import MacOSPermissionMonitor

        perm = MacOSPermissionMonitor().check_status(prompt=False)
        _, layout_name, fallback = query_current_layout_letter_map()
        reg = MacOSApplicationRegistry()
        running = reg.running_apps()
        report.update(
            {
                "accessibility_trusted": perm.accessibility_trusted,
                "input_tap_available": perm.input_tap_available,
                "screen_locked": perm.screen_locked,
                "running_as_app_bundle": perm.running_as_app_bundle,
                "keyboard_layout": layout_name,
                "ansi_fallback_active": fallback,
                "regular_running_apps": len(running),
                "remediation_steps": list(perm.remediation_steps),
            }
        )
    elif sys.platform.startswith("linux"):
        from appjuggler.platforms.linux.stub import LinuxPermissionMonitor

        perm = LinuxPermissionMonitor().check_status(prompt=False)
        report.update(
            {
                "linux_adapter_status": "stub_todo",
                "remediation_steps": list(perm.remediation_steps),
            }
        )

    print(json.dumps(report, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    # Filter macOS LaunchServices `-psn_...` argument injected when launched via Finder/.app
    cleaned_argv = [
        a for a in (argv if argv is not None else sys.argv[1:]) if not a.startswith("-psn_")
    ]

    parser = argparse.ArgumentParser(
        prog="appjuggler",
        description="AppJuggler: Jump straight to any app or window from your keyboard.",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Print the version, build and license, then exit.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"Custom path to config.json (default: {default_config_path()})",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="Run environment, permission, keyboard layout, and config diagnostics and exit.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose diagnostic logging (never logs typed text).",
    )

    args = parser.parse_args(cleaned_argv)
    if args.version:
        # Printed directly: argparse's version action would wrap the line.
        print(build_info.console_banner(build_info.current()))
        return 0
    _configure_logging(args.verbose)
    cfg_path = args.config or default_config_path()

    if args.doctor:
        return _run_doctor(cfg_path)

    # Introduce ourselves on the console before anything else.
    logging.getLogger("appjuggler").info(build_info.console_banner(build_info.current()))

    if sys.platform == "darwin":
        from appjuggler.platforms.macos.runtime import MacOSAppRuntime

        store = BindingStore(path=cfg_path)
        runtime = MacOSAppRuntime(store=store)
        return runtime.run()

    if sys.platform.startswith("linux"):
        from appjuggler.platforms.linux.stub import LinuxPermissionMonitor

        status = LinuxPermissionMonitor().check_status()
        for line in status.remediation_steps:
            print(line, file=sys.stderr)
        return 2

    print(f"Unsupported platform: {sys.platform}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
