"""macOS window, Dock, Finder, and Space animation inspection & one-click tuning."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import subprocess
from typing import Any, Callable

import Cocoa
import CoreFoundation

logger = logging.getLogger(__name__)

TERMINAL_DISABLE_COMMANDS = """\
# Disable AppKit window open/close pop & fade animations
defaults write -g NSAutomaticWindowAnimationsEnabled -bool false

# Near-instant Cocoa window & sheet resize animations
defaults write -g NSWindowResizeTime -float 0.001

# Faster Dock window minimize/restore ('scale' instead of 'genie')
defaults write com.apple.dock mineffect -string "scale"

# Faster Mission Control & zero Dock autohide delay
defaults write com.apple.dock expose-animation-duration -float 0.1
defaults write com.apple.dock launchanim -bool false
defaults write com.apple.dock autohide-delay -float 0
defaults write com.apple.dock autohide-time-modifier -float 0

# Disable Finder window & Info panel animations
defaults write com.apple.finder DisableAllAnimations -bool true

# Apply Dock and Finder changes immediately
killall Dock Finder
"""

_FAST_DEFAULTS_SPEC: tuple[tuple[str, str, str, str], ...] = (
    ("-g", "NSAutomaticWindowAnimationsEnabled", "-bool", "false"),
    ("-g", "NSWindowResizeTime", "-float", "0.001"),
    ("com.apple.dock", "mineffect", "-string", "scale"),
    ("com.apple.dock", "expose-animation-duration", "-float", "0.1"),
    ("com.apple.dock", "launchanim", "-bool", "false"),
    ("com.apple.dock", "autohide-delay", "-float", "0"),
    ("com.apple.dock", "autohide-time-modifier", "-float", "0"),
    ("com.apple.finder", "DisableAllAnimations", "-bool", "true"),
)


@dataclass(frozen=True)
class MacOSAnimationStatus:
    """Snapshot of macOS user-space window/Dock/Finder animation settings."""

    window_open_close_disabled: bool
    window_resize_fast: bool
    dock_scale_minimize: bool
    mission_control_fast: bool
    dock_launch_anim_disabled: bool
    finder_animations_disabled: bool
    reduce_motion_enabled: bool

    @property
    def appkit_window_optimized(self) -> bool:
        return self.window_open_close_disabled and self.window_resize_fast

    @property
    def dock_and_finder_optimized(self) -> bool:
        return (
            self.dock_scale_minimize
            and self.mission_control_fast
            and self.dock_launch_anim_disabled
            and self.finder_animations_disabled
        )

    @property
    def all_programmable_disabled(self) -> bool:
        return self.appkit_window_optimized and self.dock_and_finder_optimized


def _read_cf_pref(domain: str | object, key: str) -> Any:
    if domain == "-g":
        return CoreFoundation.CFPreferencesCopyValue(
            key,
            CoreFoundation.kCFPreferencesAnyApplication,
            CoreFoundation.kCFPreferencesCurrentUser,
            CoreFoundation.kCFPreferencesAnyHost,
        )
    return CoreFoundation.CFPreferencesCopyAppValue(key, domain)


def _sync_cf_prefs() -> None:
    for app_id in (
        CoreFoundation.kCFPreferencesAnyApplication,
        "com.apple.dock",
        "com.apple.finder",
    ):
        try:
            CoreFoundation.CFPreferencesAppSynchronize(app_id)
        except Exception:
            pass


def read_macos_animation_status() -> MacOSAnimationStatus:
    """Read live macOS animation settings in <0.5 ms via CoreFoundation & NSWorkspace."""
    _sync_cf_prefs()

    win_anim = _read_cf_pref("-g", "NSAutomaticWindowAnimationsEnabled")
    win_open_close_disabled = win_anim is not None and not bool(win_anim)

    resize_val = _read_cf_pref("-g", "NSWindowResizeTime")
    try:
        win_resize_fast = resize_val is not None and float(resize_val) <= 0.01
    except (TypeError, ValueError):
        win_resize_fast = False

    mineffect = _read_cf_pref("com.apple.dock", "mineffect")
    dock_scale = str(mineffect or "").strip().lower() == "scale"

    expose_dur = _read_cf_pref("com.apple.dock", "expose-animation-duration")
    try:
        mc_fast = expose_dur is not None and float(expose_dur) <= 0.15
    except (TypeError, ValueError):
        mc_fast = False

    launchanim = _read_cf_pref("com.apple.dock", "launchanim")
    launch_disabled = launchanim is not None and not bool(launchanim)

    finder_dis = _read_cf_pref("com.apple.finder", "DisableAllAnimations")
    finder_disabled = bool(finder_dis)

    try:
        reduce_motion = bool(
            Cocoa.NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceMotion()
        )
    except Exception:
        reduce_motion = False

    return MacOSAnimationStatus(
        window_open_close_disabled=win_open_close_disabled,
        window_resize_fast=win_resize_fast,
        dock_scale_minimize=dock_scale,
        mission_control_fast=mc_fast,
        dock_launch_anim_disabled=launch_disabled,
        finder_animations_disabled=finder_disabled,
        reduce_motion_enabled=reduce_motion,
    )


def apply_fast_macos_animations(
    *,
    restart_dock_finder: bool = True,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[bool, str]:
    """Write macOS defaults disabling AppKit window, Dock, Mission Control, and Finder animations."""
    try:
        for domain, key, val_type, val in _FAST_DEFAULTS_SPEC:
            cmd = ["defaults", "write", domain, key, val_type, val]
            res = runner(cmd, capture_output=True, text=True, timeout=5.0, check=False)
            if res.returncode != 0:
                err = (res.stderr or res.stdout or f"exit {res.returncode}").strip()
                return False, f"Failed writing {domain} {key}: {err}"

        if restart_dock_finder:
            for proc in ("Dock", "Finder"):
                runner(
                    ["killall", proc],
                    capture_output=True,
                    text=True,
                    timeout=5.0,
                    check=False,
                )
        _sync_cf_prefs()
        return (
            True,
            "Disabled macOS window, Dock & Finder animations (restart running apps for NSWindow open/close effect).",
        )
    except Exception as exc:
        logger.exception("Error applying fast macOS animation defaults")
        return False, f"Could not apply macOS defaults: {exc}"


def restore_default_macos_animations(
    *,
    restart_dock_finder: bool = True,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[bool, str]:
    """Remove custom macOS animation defaults and restore standard system animations."""
    try:
        for domain, key, _val_type, _val in _FAST_DEFAULTS_SPEC:
            runner(
                ["defaults", "delete", domain, key],
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )
        if restart_dock_finder:
            for proc in ("Dock", "Finder"):
                runner(
                    ["killall", proc],
                    capture_output=True,
                    text=True,
                    timeout=5.0,
                    check=False,
                )
        _sync_cf_prefs()
        return True, "Restored standard macOS window, Dock & Finder animations."
    except Exception as exc:
        logger.exception("Error restoring macOS animation defaults")
        return False, f"Could not restore macOS defaults: {exc}"


def open_reduce_motion_settings() -> None:
    """Open System Settings → Accessibility → Display (Reduce Motion / Zredukuj ruch)."""
    ws = Cocoa.NSWorkspace.sharedWorkspace()
    for url_str in (
        "x-apple.systempreferences:com.apple.Accessibility-Settings.extension?Seeing_Display",
        "x-apple.systempreferences:com.apple.preference.universalaccess?Seeing_Display",
    ):
        url = Cocoa.NSURL.URLWithString_(url_str)
        if url is not None and ws.openURL_(url):
            return
