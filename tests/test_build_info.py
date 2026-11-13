"""Unit tests for the AppJuggler 1 version and build information."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from appjuggler import build_info
from appjuggler.build_info import BuildInfo


def test_console_banner_introduces_product_version_build_and_license() -> None:
    info = BuildInfo(version="2611", build="ae57acf")
    assert build_info.version_line(info) == "AppJuggler 1 | Version: 2611 | Build: ae57acf"
    assert build_info.console_banner(info) == (
        "AppJuggler 1 | Version: 2611 | Build: ae57acf"
        " | (c) 2026 Pawel Suchanecki and AppJuggler Contributors · MIT License"
    )


def _git(repo: Path, *args: str) -> str:
    env = dict(
        os.environ,
        GIT_AUTHOR_DATE="2026-11-13T12:00:00+0000",
        GIT_COMMITTER_DATE="2026-11-13T12:00:00+0000",
    )
    return subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    ).stdout.strip()


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_git_build_info_uses_commit_month_and_marks_uncommitted_changes(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    (tmp_path / "file.txt").write_text("one\n", encoding="utf-8")
    _git(tmp_path, "add", "file.txt")
    _git(tmp_path, "commit", "-q", "-m", "first")
    commit = _git(tmp_path, "rev-parse", "--short", "HEAD")

    assert build_info.git_build_info(tmp_path) == BuildInfo(version="2611", build=commit)

    # Untracked files do not count; changes to tracked files do.
    (tmp_path / "notes.txt").write_text("scratch\n", encoding="utf-8")
    assert build_info.git_build_info(tmp_path) == BuildInfo(version="2611", build=commit)
    (tmp_path / "file.txt").write_text("two\n", encoding="utf-8")
    assert build_info.git_build_info(tmp_path) == BuildInfo(
        version="2611", build=f"{commit}-dirty"
    )


def test_git_build_info_is_none_outside_a_repository(tmp_path: Path) -> None:
    assert build_info.git_build_info(tmp_path) is None


def test_bundle_values_count_only_inside_appjugglers_own_bundle() -> None:
    # The test runner is Python, not AppJuggler.app: its Info.plist must not be reported.
    assert build_info.bundle_build_info() is None


def test_current_always_reports_a_version_and_build() -> None:
    info = build_info.current()
    assert info.version and info.build
