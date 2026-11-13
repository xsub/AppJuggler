"""Normalized input events, backend feedback events, and state machine commands."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from appjuggler.domain.models import AppBinding, AppRecord, Layer, WindowSlot


class KeyKind(str, Enum):
    """Normalized logical classification of hardware keyboard events."""

    LETTER = "letter"
    DIGIT = "digit"
    ARROW_UP = "arrow_up"
    ARROW_DOWN = "arrow_down"
    ENTER = "enter"
    ESCAPE = "escape"
    TAB = "tab"
    COMMA = "comma"
    TILDE = "tilde"
    DELETE = "delete"
    CAPS_LOCK = "caps_lock"
    SHIFT_MODIFIER = "shift_modifier"
    OTHER_MODIFIER = "other_modifier"
    OTHER_KEY = "other_key"


class InputAction(str, Enum):
    """Whether the low-level input tap should forward or swallow an event."""

    PASS_THROUGH = "pass_through"
    SWALLOW = "swallow"


@dataclass(frozen=True)
class HardwareKeyEvent:
    """Platform-neutral representation of a key or modifier transition."""

    kind: KeyKind
    keycode: int
    timestamp_ns: int
    is_key_down: bool = True
    is_autorepeat: bool = False
    symbol: str | None = None
    shift_held: bool = False
    caps_lock_on: bool = False
    other_modifiers_held: bool = False
    caps_lock_toggled: bool = False


# --- Commands emitted by NavigationController ---


@dataclass(frozen=True)
class ShowAppMapCommand:
    op_token: int
    active_layer: Layer
    direct_jump: bool = False


@dataclass(frozen=True)
class UpdateAppMapLayerCommand:
    op_token: int
    active_layer: Layer
    direct_jump: bool = False


@dataclass(frozen=True)
class InspectAppWindowsCommand:
    op_token: int
    binding: AppBinding
    direct_jump: bool = False


@dataclass(frozen=True)
class LaunchAppCommand:
    op_token: int
    binding: AppBinding


@dataclass(frozen=True)
class ShowWindowListCommand:
    op_token: int
    binding: AppBinding
    slots: tuple[WindowSlot, ...]
    selected_slot: int | None
    generation: int
    notice: str | None = None


@dataclass(frozen=True)
class UpdateWindowSelectionCommand:
    op_token: int
    selected_slot: int | None
    generation: int
    notice: str | None = None


@dataclass(frozen=True)
class ActivateWindowCommand:
    op_token: int
    binding: AppBinding
    slot: WindowSlot
    generation: int
    from_launch: bool = False


@dataclass(frozen=True)
class ActivateAppNoWindowsCommand:
    op_token: int
    binding: AppBinding
    from_launch: bool = False


@dataclass(frozen=True)
class ShowStatusBannerCommand:
    level: str  # "info" | "warning" | "error"
    message: str
    auto_hide_ms: int = 2400
    keep_app_map: bool = False


@dataclass(frozen=True)
class HideOverlayCommand:
    reason: str


# --- Quick Assign (Hold Caps Lock 2s / Tab in HUD) Commands ---


@dataclass(frozen=True)
class RequestOpenAppsForAssignCommand:
    op_token: int


@dataclass(frozen=True)
class ShowQuickAssignAppListCommand:
    op_token: int
    apps: tuple[AppRecord, ...]
    selected_index: int
    notice: str | None = None


@dataclass(frozen=True)
class UpdateQuickAssignAppSelectionCommand:
    op_token: int
    selected_index: int
    notice: str | None = None


@dataclass(frozen=True)
class ShowQuickAssignKeyMapCommand:
    op_token: int
    app: AppRecord
    active_layer: Layer
    conflict_existing: AppBinding | None = None


@dataclass(frozen=True)
class CommitQuickAssignCommand:
    op_token: int
    app: AppRecord
    key: str
    layer: Layer
    allow_replace: bool


@dataclass(frozen=True)
class OpenFullAssignmentWindowCommand:
    pass


@dataclass(frozen=True)
class DismissWindowSlotCommand:
    op_token: int
    binding: AppBinding
    window_id: str
    slot_number: int


NavCommand = (
    ShowAppMapCommand
    | UpdateAppMapLayerCommand
    | InspectAppWindowsCommand
    | LaunchAppCommand
    | ShowWindowListCommand
    | UpdateWindowSelectionCommand
    | ActivateWindowCommand
    | ActivateAppNoWindowsCommand
    | ShowStatusBannerCommand
    | HideOverlayCommand
    | RequestOpenAppsForAssignCommand
    | ShowQuickAssignAppListCommand
    | UpdateQuickAssignAppSelectionCommand
    | ShowQuickAssignKeyMapCommand
    | CommitQuickAssignCommand
    | OpenFullAssignmentWindowCommand
    | DismissWindowSlotCommand
)


@dataclass(frozen=True)
class StepResult:
    """Return value of processing a hardware event in NavigationController."""

    action: InputAction
    commands: tuple[NavCommand, ...] = ()


# --- Asynchronous backend events fed back into NavigationController ---


@dataclass(frozen=True)
class AppWindowsResolved:
    op_token: int
    bundle_id: str
    slots: tuple[WindowSlot, ...]
    default_slot: int | None
    generation: int
    app_is_running: bool


@dataclass(frozen=True)
class WindowListRefreshed:
    bundle_id: str
    slots: tuple[WindowSlot, ...]
    generation: int


@dataclass(frozen=True)
class WindowSlotDismissed:
    op_token: int
    bundle_id: str
    dismissed_slot: int
    slots: tuple[WindowSlot, ...]
    default_slot: int | None
    generation: int


@dataclass(frozen=True)
class LaunchCompleted:
    op_token: int
    bundle_id: str
    slots: tuple[WindowSlot, ...] = ()
    default_slot: int | None = None
    generation: int = 0
    error: str | None = None
    missing_bundle: bool = False


@dataclass(frozen=True)
class ActivationCompleted:
    op_token: int
    success: bool
    stale_window: bool = False
    refreshed_slots: tuple[WindowSlot, ...] = ()
    refreshed_generation: int = 0
    error: str | None = None


@dataclass(frozen=True)
class CapsHoldTriggered:
    pass


@dataclass(frozen=True)
class OpenAppsForAssignResolved:
    op_token: int
    apps: tuple[AppRecord, ...]
    default_index: int = 0


@dataclass(frozen=True)
class EnterAssignmentEditing:
    pass


@dataclass(frozen=True)
class ExitAssignmentEditing:
    pass


@dataclass(frozen=True)
class SystemInterruptEvent:
    reason: str


# --- Optional Mouse HUD Selection Events (gated on enable_mouse_selection) ---


@dataclass(frozen=True)
class MouseClickAppSlot:
    key: str
    layer: Layer


@dataclass(frozen=True)
class MouseToggleLayer:
    pass


@dataclass(frozen=True)
class MouseToggleDirectJump:
    pass


@dataclass(frozen=True)
class MouseHoverWindowSlot:
    slot_number: int


@dataclass(frozen=True)
class MouseClickWindowSlot:
    slot_number: int


@dataclass(frozen=True)
class MouseDismissWindowSlot:
    slot_number: int


@dataclass(frozen=True)
class MouseScrollWindowSelection:
    delta: int


BackendEvent = (
    AppWindowsResolved
    | WindowListRefreshed
    | WindowSlotDismissed
    | LaunchCompleted
    | ActivationCompleted
    | CapsHoldTriggered
    | OpenAppsForAssignResolved
    | EnterAssignmentEditing
    | ExitAssignmentEditing
    | SystemInterruptEvent
    | MouseClickAppSlot
    | MouseToggleLayer
    | MouseToggleDirectJump
    | MouseHoverWindowSlot
    | MouseClickWindowSlot
    | MouseDismissWindowSlot
    | MouseScrollWindowSelection
)
