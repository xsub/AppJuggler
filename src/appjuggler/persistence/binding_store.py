"""Validated, atomic persistent configuration store (BindingStore)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from typing import Any

from appjuggler.domain.models import AppBinding, Layer, TriggerConfig

SCHEMA_VERSION = 1


class ConfigValidationError(Exception):
    """Raised when configuration JSON violates schema v1."""


class SlotConflictError(Exception):
    """Raised when attempting to overwrite an occupied (key, layer) slot without explicit consent."""

    def __init__(self, existing: AppBinding, incoming: AppBinding) -> None:
        self.existing = existing
        self.incoming = incoming
        super().__init__(
            f"Slot {existing.layer.value}:{existing.key} is already assigned to "
            f"{existing.display_name} ({existing.bundle_id})."
        )


class CorruptedConfigLockError(Exception):
    """Raised when attempting to overwrite a corrupted config file without explicit backup/repair."""


@dataclass(frozen=True)
class AppConfig:
    """In-memory validated representation of config.json."""

    schema_version: int = SCHEMA_VERSION
    trigger: TriggerConfig = TriggerConfig()
    auto_fullscreen_on_switch: bool = False
    auto_maximize_on_switch: bool = False
    enable_mouse_selection: bool = False
    bindings: tuple[AppBinding, ...] = ()


def default_config_path() -> Path:
    """Return platform-appropriate default path for AppJuggler's config.json."""
    env_override = os.environ.get("APPJUGGLER_CONFIG")
    if env_override:
        return Path(env_override).expanduser()

    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "AppJuggler"
            / "config.json"
        )

    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg_config).expanduser() if xdg_config else (Path.home() / ".config")
    return base / "appjuggler" / "config.json"


def default_initial_bindings() -> tuple[AppBinding, ...]:
    """Default initial bindings created only when starting without an existing config file."""
    if sys.platform == "darwin":
        return (
            AppBinding(
                key="T",
                layer=Layer.BASE,
                bundle_id="com.apple.Terminal",
                display_name="Terminal",
                bundle_path="/System/Applications/Utilities/Terminal.app",
            ),
        )
    return ()


def parse_config_dict(raw: Any) -> AppConfig:
    """Validate and parse raw decoded JSON into an immutable AppConfig."""
    if not isinstance(raw, dict):
        raise ConfigValidationError("Root of configuration file must be a JSON object.")

    version = raw.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ConfigValidationError(
            f"Unsupported schema_version {version!r}; expected {SCHEMA_VERSION}."
        )

    raw_trigger = raw.get("trigger", {"kind": "double_caps_lock", "interval_ms": 300})
    if not isinstance(raw_trigger, dict):
        raise ConfigValidationError("'trigger' must be a JSON object.")
    raw_enable_hold = raw_trigger.get("enable_caps_hold_assign", False)
    if not isinstance(raw_enable_hold, bool):
        raise ConfigValidationError("'trigger.enable_caps_hold_assign' must be a boolean.")
    try:
        trigger = TriggerConfig(
            kind=str(raw_trigger.get("kind", "double_caps_lock")),
            interval_ms=int(raw_trigger.get("interval_ms", 300)),
            enable_caps_hold_assign=raw_enable_hold,
            hold_assign_ms=int(raw_trigger.get("hold_assign_ms", 2000)),
        )
    except (TypeError, ValueError) as exc:
        raise ConfigValidationError(f"Invalid trigger settings: {exc}") from exc

    raw_bindings = raw.get("bindings", [])
    if not isinstance(raw_bindings, list):
        raise ConfigValidationError("'bindings' must be a JSON array.")

    seen_slots: dict[tuple[str, Layer], AppBinding] = {}
    parsed_bindings: list[AppBinding] = []

    for idx, entry in enumerate(raw_bindings):
        if not isinstance(entry, dict):
            raise ConfigValidationError(f"bindings[{idx}] must be a JSON object.")
        # Ensure we never persist transient PIDs or window handles.
        forbidden = {"pid", "pids", "window_id", "window_handle"} & set(entry.keys())
        if forbidden:
            raise ConfigValidationError(
                f"bindings[{idx}] contains forbidden transient keys: {sorted(forbidden)}"
            )
        try:
            layer = Layer(str(entry.get("layer", "base")).lower())
            bundle_path = entry.get("bundle_path")
            if bundle_path is not None and not isinstance(bundle_path, str):
                raise ValueError("bundle_path must be a string or null")
            binding = AppBinding(
                key=str(entry["key"]),
                layer=layer,
                bundle_id=str(entry["bundle_id"]),
                display_name=str(entry["display_name"]),
                bundle_path=bundle_path or None,
            )
        except KeyError as exc:
            raise ConfigValidationError(
                f"bindings[{idx}] missing required field {exc}"
            ) from exc
        except ValueError as exc:
            raise ConfigValidationError(f"bindings[{idx}] invalid: {exc}") from exc

        if binding.slot_key in seen_slots:
            prev = seen_slots[binding.slot_key]
            raise ConfigValidationError(
                f"Duplicate slot {binding.layer.value}:{binding.key} in bindings "
                f"({prev.bundle_id} vs {binding.bundle_id})."
            )
        seen_slots[binding.slot_key] = binding
        parsed_bindings.append(binding)

    raw_auto_fs = raw.get(
        "auto_fullscreen_on_switch",
        raw_trigger.get("auto_fullscreen_on_switch", False),
    )
    if not isinstance(raw_auto_fs, bool):
        raise ConfigValidationError("'auto_fullscreen_on_switch' must be a boolean.")

    raw_auto_max = raw.get(
        "auto_maximize_on_switch",
        raw_trigger.get("auto_maximize_on_switch", False),
    )
    if not isinstance(raw_auto_max, bool):
        raise ConfigValidationError("'auto_maximize_on_switch' must be a boolean.")

    if raw_auto_max and raw_auto_fs:
        raw_auto_fs = False

    raw_mouse_sel = raw.get(
        "enable_mouse_selection",
        raw_trigger.get("enable_mouse_selection", False),
    )
    if not isinstance(raw_mouse_sel, bool):
        raise ConfigValidationError("'enable_mouse_selection' must be a boolean.")

    parsed_bindings.sort(key=lambda b: (0 if b.layer is Layer.BASE else 1, b.key))
    return AppConfig(
        schema_version=SCHEMA_VERSION,
        trigger=trigger,
        auto_fullscreen_on_switch=raw_auto_fs,
        auto_maximize_on_switch=raw_auto_max,
        enable_mouse_selection=raw_mouse_sel,
        bindings=tuple(parsed_bindings),
    )


def serialize_config_dict(config: AppConfig) -> dict[str, Any]:
    """Convert AppConfig to a clean JSON-serializable dictionary."""
    bindings_out: list[dict[str, Any]] = []
    sorted_bindings = sorted(
        config.bindings, key=lambda b: (0 if b.layer is Layer.BASE else 1, b.key)
    )
    for b in sorted_bindings:
        item: dict[str, Any] = {
            "key": b.key,
            "layer": b.layer.value,
            "bundle_id": b.bundle_id,
            "display_name": b.display_name,
        }
        if b.bundle_path:
            item["bundle_path"] = b.bundle_path
        bindings_out.append(item)

    trigger_out: dict[str, Any] = {
        "kind": config.trigger.kind,
        "interval_ms": config.trigger.interval_ms,
    }
    if config.trigger.enable_caps_hold_assign:
        trigger_out["enable_caps_hold_assign"] = True
        trigger_out["hold_assign_ms"] = config.trigger.hold_assign_ms
    elif config.trigger.hold_assign_ms != 2000:
        trigger_out["hold_assign_ms"] = config.trigger.hold_assign_ms

    out: dict[str, Any] = {
        "schema_version": config.schema_version,
        "trigger": trigger_out,
    }
    if config.auto_fullscreen_on_switch:
        out["auto_fullscreen_on_switch"] = True
    if config.auto_maximize_on_switch:
        out["auto_maximize_on_switch"] = True
    if config.enable_mouse_selection:
        out["enable_mouse_selection"] = True
    out["bindings"] = bindings_out
    return out


class BindingStore:
    """Thread-safe configuration manager with atomic writes and corruption protection."""

    def __init__(
        self,
        path: Path | None = None,
        seed_defaults_if_missing: bool = True,
    ) -> None:
        self._lock = threading.RLock()
        self._path: Path = path or default_config_path()
        self._seed_defaults = seed_defaults_if_missing
        self._config: AppConfig = AppConfig()
        self._load_error: str | None = None
        self._last_stat_sig: tuple[int, int, int] | None = None
        self.load()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def config(self) -> AppConfig:
        with self._lock:
            return self._config

    @property
    def bindings(self) -> tuple[AppBinding, ...]:
        with self._lock:
            return self._config.bindings

    @property
    def trigger(self) -> TriggerConfig:
        with self._lock:
            return self._config.trigger

    @property
    def auto_fullscreen_on_switch(self) -> bool:
        with self._lock:
            return self._config.auto_fullscreen_on_switch

    @property
    def auto_maximize_on_switch(self) -> bool:
        with self._lock:
            return self._config.auto_maximize_on_switch

    @property
    def enable_mouse_selection(self) -> bool:
        with self._lock:
            return self._config.enable_mouse_selection

    @property
    def load_error(self) -> str | None:
        """Non-None if the on-disk config file was corrupted and refused silent overwrite."""
        with self._lock:
            return self._load_error

    def load(self) -> AppConfig:
        """Load configuration from disk, or initialize defaults if file does not exist."""
        with self._lock:
            if not self._path.exists():
                self._load_error = None
                initial_bindings = (
                    default_initial_bindings() if self._seed_defaults else ()
                )
                self._config = AppConfig(bindings=initial_bindings)
                self._write_atomic_locked(self._config)
                return self._config

            try:
                raw_text = self._path.read_text(encoding="utf-8")
                raw_json = json.loads(raw_text)
                parsed = parse_config_dict(raw_json)
            except (OSError, json.JSONDecodeError, ConfigValidationError) as exc:
                # Spec §8: Never silently overwrite a damaged configuration file with an empty one!
                self._load_error = f"Corrupted configuration at {self._path}: {exc}"
                return self._config

            self._config = parsed
            self._load_error = None
            self._last_stat_sig = self._stat_signature_unlocked()
            return self._config

    def reload_if_modified(self) -> bool:
        """Reload config.json if its on-disk mtime/inode/size changed externally."""
        with self._lock:
            sig = self._stat_signature_unlocked()
            if sig is None or sig == self._last_stat_sig:
                return False
            self.load()
            return self._load_error is None

    def _stat_signature_unlocked(self) -> tuple[int, int, int] | None:
        try:
            st = self._path.stat()
            return (st.st_mtime_ns, st.st_ino, st.st_size)
        except OSError:
            return None

    def backup_corrupted_and_reset(self) -> Path | None:
        """Explicitly back up a damaged config file and initialize a clean configuration."""
        with self._lock:
            backup_path: Path | None = None
            if self._path.exists():
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                backup_path = self._path.with_name(f"{self._path.stem}.corrupt.{stamp}.json")
                os.replace(self._path, backup_path)

            self._load_error = None
            initial_bindings = default_initial_bindings() if self._seed_defaults else ()
            self._config = AppConfig(bindings=initial_bindings)
            self._write_atomic_locked(self._config)
            return backup_path

    def get_slot(self, key: str, layer: Layer) -> AppBinding | None:
        norm = key.upper()
        with self._lock:
            for b in self._config.bindings:
                if b.key == norm and b.layer is layer:
                    return b
            return None

    def bindings_for_bundle(self, bundle_id: str) -> tuple[AppBinding, ...]:
        with self._lock:
            return tuple(b for b in self._config.bindings if b.bundle_id == bundle_id)

    def assign(
        self,
        binding: AppBinding,
        *,
        allow_replace: bool = False,
        remove_previous_for_bundle: bool = False,
    ) -> AppConfig:
        """Assign an app to a (key, layer) slot with explicit conflict checking."""
        with self._lock:
            self._ensure_not_corrupted_locked()
            existing = self.get_slot(binding.key, binding.layer)
            if (
                existing is not None
                and existing.bundle_id != binding.bundle_id
                and not allow_replace
            ):
                raise SlotConflictError(existing=existing, incoming=binding)

            updated: list[AppBinding] = []
            for item in self._config.bindings:
                if item.slot_key == binding.slot_key:
                    continue
                if remove_previous_for_bundle and item.bundle_id == binding.bundle_id:
                    continue
                updated.append(item)
            updated.append(binding)
            updated.sort(key=lambda b: (0 if b.layer is Layer.BASE else 1, b.key))

            new_cfg = replace(self._config, bindings=tuple(updated))
            self._write_atomic_locked(new_cfg)
            self._config = new_cfg
            return new_cfg

    def remove_slot(self, key: str, layer: Layer) -> AppConfig:
        """Explicitly clear a (key, layer) binding slot."""
        norm = key.upper()
        with self._lock:
            self._ensure_not_corrupted_locked()
            updated = tuple(
                b
                for b in self._config.bindings
                if not (b.key == norm and b.layer is layer)
            )
            new_cfg = replace(self._config, bindings=updated)
            self._write_atomic_locked(new_cfg)
            self._config = new_cfg
            return new_cfg

    def set_trigger_interval_ms(self, interval_ms: int) -> AppConfig:
        """Update and persist the double-Caps trigger interval in milliseconds."""
        with self._lock:
            self._ensure_not_corrupted_locked()
            new_trigger = replace(self._config.trigger, interval_ms=interval_ms)
            new_cfg = replace(self._config, trigger=new_trigger)
            self._write_atomic_locked(new_cfg)
            self._config = new_cfg
            return new_cfg

    def set_caps_hold_assign_enabled(self, enabled: bool) -> AppConfig:
        """Update and persist the optional 2-second Caps Lock hold quick-assign toggle."""
        with self._lock:
            self._ensure_not_corrupted_locked()
            new_trigger = replace(
                self._config.trigger, enable_caps_hold_assign=bool(enabled)
            )
            new_cfg = replace(self._config, trigger=new_trigger)
            self._write_atomic_locked(new_cfg)
            self._config = new_cfg
            return new_cfg

    def set_auto_fullscreen_on_switch(self, enabled: bool) -> AppConfig:
        """Update and persist whether activated target windows automatically enter Full Screen."""
        with self._lock:
            self._ensure_not_corrupted_locked()
            flag = bool(enabled)
            new_cfg = replace(
                self._config,
                auto_fullscreen_on_switch=flag,
                auto_maximize_on_switch=(
                    False if flag else self._config.auto_maximize_on_switch
                ),
            )
            self._write_atomic_locked(new_cfg)
            self._config = new_cfg
            return new_cfg

    def set_auto_maximize_on_switch(self, enabled: bool) -> AppConfig:
        """Update and persist whether activated target windows instantly fill visibleFrame on the same Space."""
        with self._lock:
            self._ensure_not_corrupted_locked()
            flag = bool(enabled)
            new_cfg = replace(
                self._config,
                auto_maximize_on_switch=flag,
                auto_fullscreen_on_switch=(
                    False if flag else self._config.auto_fullscreen_on_switch
                ),
            )
            self._write_atomic_locked(new_cfg)
            self._config = new_cfg
            return new_cfg

    def set_enable_mouse_selection(self, enabled: bool) -> AppConfig:
        """Update and persist whether optional mouse hover & click selection is active in HUD."""
        with self._lock:
            self._ensure_not_corrupted_locked()
            new_cfg = replace(self._config, enable_mouse_selection=bool(enabled))
            self._write_atomic_locked(new_cfg)
            self._config = new_cfg
            return new_cfg

    def _ensure_not_corrupted_locked(self) -> None:
        if self._load_error is not None:
            raise CorruptedConfigLockError(self._load_error)

    def _write_atomic_locked(self, config: AppConfig) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(serialize_config_dict(config), indent=2, ensure_ascii=False) + "\n"

        fd, tmp_name = tempfile.mkstemp(
            dir=str(self._path.parent),
            prefix=".config.",
            suffix=".tmp",
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self._path)
            self._last_stat_sig = self._stat_signature_unlocked()
        except Exception:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
