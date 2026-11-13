"""Unit tests for macOS window animation inspection & defaults management."""

from __future__ import annotations

import subprocess
import sys
import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS animation defaults tests")


def test_apply_and_restore_fast_macos_animations_invokes_expected_commands(monkeypatch) -> None:
    from appjuggler.platforms.macos import animations

    recorded: list[list[str]] = []

    def fake_runner(cmd, **_kwargs):
        recorded.append(list(cmd))
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(animations, "_sync_cf_prefs", lambda: None)

    ok, msg = animations.apply_fast_macos_animations(
        restart_dock_finder=True, runner=fake_runner
    )
    assert ok is True
    assert "Disabled macOS window" in msg
    assert ["defaults", "write", "-g", "NSAutomaticWindowAnimationsEnabled", "-bool", "false"] in recorded
    assert ["defaults", "write", "-g", "NSWindowResizeTime", "-float", "0.001"] in recorded
    assert ["defaults", "write", "com.apple.dock", "mineffect", "-string", "scale"] in recorded
    assert ["killall", "Dock"] in recorded
    assert ["killall", "Finder"] in recorded

    recorded.clear()
    ok_res, _ = animations.restore_default_macos_animations(
        restart_dock_finder=True, runner=fake_runner
    )
    assert ok_res is True
    assert ["defaults", "delete", "-g", "NSAutomaticWindowAnimationsEnabled"] in recorded
    assert ["defaults", "delete", "com.apple.dock", "mineffect"] in recorded
    assert ["killall", "Dock"] in recorded


def test_animation_status_properties() -> None:
    from appjuggler.platforms.macos.animations import MacOSAnimationStatus

    st = MacOSAnimationStatus(
        window_open_close_disabled=True,
        window_resize_fast=True,
        dock_scale_minimize=True,
        mission_control_fast=True,
        dock_launch_anim_disabled=True,
        finder_animations_disabled=True,
        reduce_motion_enabled=False,
    )
    assert st.appkit_window_optimized is True
    assert st.dock_and_finder_optimized is True
    assert st.all_programmable_disabled is True
