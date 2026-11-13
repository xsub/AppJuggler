"""Version and build of the running AppJuggler 1.9.

Versions follow the calendar: "2611" means the code comes from a commit dated November 2026.
The build is that commit's short hash, with "-dirty" when tracked files have uncommitted
changes. An AppJuggler.app bundle carries both in its Info.plist (stamped by setup_app.py);
a source checkout reads them from git; anything else reports "dev" / "unknown".
"""

from __future__ import annotations

import functools
import subprocess
from dataclasses import dataclass
from pathlib import Path

from appjuggler import APP_BUNDLE_ID

# Fixed number after the name; window and HUD titles show it slightly greyed.
PRODUCT_NUMBER = "1.9"
PRODUCT_NAME = f"AppJuggler {PRODUCT_NUMBER}"
COPYRIGHT = "(c) 2026 Pawel Suchanecki and AppJuggler Contributors · MIT License"
SOURCE_URL = "https://github.com/xsub/AppJuggler"
NATIVE_VERSION_URL = "https://PuffyClouds.Works/AppJuggler2"
# Info.plist key holding the build commit (the same key AppJuggler 2 uses).
BUILD_COMMIT_KEY = "AJBuildCommit"


@dataclass(frozen=True)
class BuildInfo:
    version: str
    build: str


UNKNOWN = BuildInfo(version="dev", build="unknown")


def version_line(info: BuildInfo) -> str:
    return f"{PRODUCT_NAME} | Version: {info.version} | Build: {info.build}"


def console_banner(info: BuildInfo) -> str:
    """The line AppJuggler introduces itself with on the console and in --version."""
    return f"{version_line(info)} | {COPYRIGHT}"


def git_build_info(repo_root: Path) -> BuildInfo | None:
    """Version and build of the git checkout at `repo_root`, or None without git or a repo."""

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            check=True,
            timeout=3,
        ).stdout.strip()

    try:
        version = git("log", "-1", "--format=%cd", "--date=format:%y%m")
        build = git("rev-parse", "--short", "HEAD")
        if git("status", "--porcelain", "--untracked-files=no"):
            build += "-dirty"
    except (OSError, subprocess.SubprocessError):
        return None
    if not version or not build:
        return None
    return BuildInfo(version=version, build=build)


def bundle_build_info() -> BuildInfo | None:
    """Values stamped into AppJuggler.app; None outside AppJuggler's own bundle."""
    try:
        from Foundation import NSBundle
    except ImportError:
        return None
    bundle = NSBundle.mainBundle()
    if bundle is None or bundle.bundleIdentifier() != APP_BUNDLE_ID:
        return None
    version = bundle.objectForInfoDictionaryKey_("CFBundleShortVersionString")
    build = bundle.objectForInfoDictionaryKey_(BUILD_COMMIT_KEY)
    if not version or not build:
        return None
    return BuildInfo(version=str(version), build=str(build))


def source_checkout_root() -> Path | None:
    """The repository this package runs from, when it runs from a source checkout."""
    root = Path(__file__).resolve().parents[2]  # <root>/src/appjuggler/build_info.py
    if (root / "pyproject.toml").is_file() and (root / ".git").exists():
        return root
    return None


@functools.cache
def current() -> BuildInfo:
    """Version and build of the running copy (looked up once)."""
    info = bundle_build_info()
    if info is None:
        root = source_checkout_root()
        info = git_build_info(root) if root is not None else None
    return info or UNKNOWN
