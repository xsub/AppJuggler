"""Core domain models for AppJuggler."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Layer(str, Enum):
    """Keyboard binding layer (Base A-Z vs Shift+A-Z)."""

    BASE = "base"
    SHIFT = "shift"

    @property
    def label(self) -> str:
        return "Base (A–Z)" if self is Layer.BASE else "Shift (⇧A–Z)"


class NavState(str, Enum):
    """Logical navigation states defined in the specification + Quick Assign."""

    IDLE = "Idle"
    APP_SELECTION = "AppSelection"
    WINDOW_SELECTION = "WindowSelection"
    LAUNCH_PENDING = "LaunchPending"
    ASSIGNMENT_EDITING = "AssignmentEditing"
    ACTIVATION_PENDING = "ActivationPending"
    QUICK_ASSIGN_APP = "QuickAssignApp"
    QUICK_ASSIGN_KEY = "QuickAssignKey"


VALID_KEYS: tuple[str, ...] = tuple(chr(c) for c in range(ord("A"), ord("Z") + 1))

ANSI_QWERTY_ROWS: tuple[tuple[str, ...], ...] = (
    ("Q", "W", "E", "R", "T", "Y", "U", "I", "O", "P"),
    ("A", "S", "D", "F", "G", "H", "J", "K", "L"),
    ("Z", "X", "C", "V", "B", "N", "M"),
)


@dataclass(frozen=True)
class TriggerConfig:
    """Trigger gesture configuration."""

    kind: str = "double_caps_lock"
    interval_ms: int = 300
    hold_assign_ms: int = 2000

    def __post_init__(self) -> None:
        if self.kind != "double_caps_lock":
            raise ValueError(f"Unsupported trigger kind: {self.kind!r}")
        if not (50 <= self.interval_ms <= 1500):
            raise ValueError(
                f"trigger.interval_ms must be between 50 and 1500 ms, got {self.interval_ms}"
            )
        if not (500 <= self.hold_assign_ms <= 5000):
            raise ValueError(
                f"trigger.hold_assign_ms must be between 500 and 5000 ms, got {self.hold_assign_ms}"
            )


@dataclass(frozen=True)
class AppBinding:
    """Persistent binding mapping a (key, layer) slot to an application bundle ID."""

    key: str
    layer: Layer
    bundle_id: str
    display_name: str
    bundle_path: str | None = None

    def __post_init__(self) -> None:
        normalized_key = self.key.upper()
        if normalized_key not in VALID_KEYS:
            raise ValueError(f"Invalid binding key {self.key!r}; expected A-Z.")
        object.__setattr__(self, "key", normalized_key)
        if not isinstance(self.layer, Layer):
            object.__setattr__(self, "layer", Layer(self.layer))
        if not self.bundle_id or not self.bundle_id.strip():
            raise ValueError("bundle_id must be a non-empty string.")
        if not self.display_name or not self.display_name.strip():
            raise ValueError("display_name must be a non-empty string.")

    @property
    def slot_key(self) -> tuple[str, Layer]:
        return (self.key, self.layer)


@dataclass(frozen=True)
class AppRecord:
    """Runtime snapshot of a running or installed application."""

    bundle_id: str | None
    display_name: str
    is_running: bool = False
    pids: tuple[int, ...] = ()
    bundle_path: str | None = None
    candidate_paths: tuple[str, ...] = ()
    is_hidden: bool = False

    @property
    def has_missing_bundle_id(self) -> bool:
        return not self.bundle_id

    @property
    def has_duplicate_install_conflict(self) -> bool:
        return len(self.candidate_paths) > 1


@dataclass(frozen=True)
class DiscoveredWindow:
    """Adapter-reported window snapshot with an opaque session handle."""

    window_id: str
    bundle_id: str
    pid: int
    title: str
    is_minimized: bool = False
    is_fullscreen: bool = False
    is_modal: bool = False
    is_focused: bool = False
    space_label: str | None = None
    creation_hint: int = 0
    native_handle: Any = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class WindowSlot:
    """Session-stable numbered slot assigned to a live window."""

    slot_number: int
    window: DiscoveredWindow

    @property
    def window_id(self) -> str:
        return self.window.window_id

    @property
    def title(self) -> str:
        return self.window.title


@dataclass(frozen=True)
class ActivationOutcome:
    """Bounded-time outcome of attempting to activate an app or window."""

    success: bool
    stale_window: bool = False
    error: str | None = None
    elapsed_ms: float = 0.0
