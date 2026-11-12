"""Unit tests for BindingStore schema validation, conflict safety, and corruption protection."""

from __future__ import annotations

from pathlib import Path
import pytest

from appjuggler.domain.models import AppBinding, Layer
from appjuggler.persistence.binding_store import (
    BindingStore,
    ConfigValidationError,
    CorruptedConfigLockError,
    SlotConflictError,
    parse_config_dict,
)


def test_store_roundtrip_both_layers_and_slot_conflict_protection(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.json"
    store = BindingStore(path=cfg_file, seed_defaults_if_missing=False)
    assert store.bindings == ()

    b_term = AppBinding(key="T", layer=Layer.BASE, bundle_id="com.apple.Terminal", display_name="Terminal")
    b_text = AppBinding(key="T", layer=Layer.SHIFT, bundle_id="com.apple.TextEdit", display_name="TextEdit")
    store.assign(b_term)
    store.assign(b_text)
    store.set_trigger_interval_ms(280)

    # Occupied slot MUST NOT be silently overwritten
    b_safari_conflict = AppBinding(key="T", layer=Layer.BASE, bundle_id="com.apple.Safari", display_name="Safari")
    with pytest.raises(SlotConflictError):
        store.assign(b_safari_conflict, allow_replace=False)

    # Restart store from disk: both layers and trigger interval persist intact
    reloaded = BindingStore(path=cfg_file, seed_defaults_if_missing=False)
    assert reloaded.trigger.interval_ms == 280
    assert reloaded.get_slot("T", Layer.BASE) == b_term
    assert reloaded.get_slot("T", Layer.SHIFT) == b_text

    # Explicit replacement and explicit removal succeed
    reloaded.assign(b_safari_conflict, allow_replace=True)
    assert reloaded.get_slot("T", Layer.BASE) == b_safari_conflict
    reloaded.remove_slot("T", Layer.BASE)
    assert reloaded.get_slot("T", Layer.BASE) is None
    assert reloaded.get_slot("T", Layer.SHIFT) == b_text


def test_corrupted_config_is_never_silently_overwritten(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.json"
    damaged_payload = "{ broken json: [1, 2, "
    cfg_file.write_text(damaged_payload, encoding="utf-8")

    store = BindingStore(path=cfg_file, seed_defaults_if_missing=True)
    assert store.load_error is not None
    # File on disk MUST remain untouched!
    assert cfg_file.read_text(encoding="utf-8") == damaged_payload

    # Mutations are blocked until explicit backup & reset
    with pytest.raises(CorruptedConfigLockError):
        store.set_trigger_interval_ms(250)

    backup = store.backup_corrupted_and_reset()
    assert backup is not None and backup.exists()
    assert backup.read_text(encoding="utf-8") == damaged_payload
    assert store.load_error is None


def test_schema_rejects_transient_pids_and_duplicate_slots() -> None:
    with pytest.raises(ConfigValidationError, match="forbidden transient keys"):
        parse_config_dict(
            {
                "schema_version": 1,
                "bindings": [
                    {
                        "key": "T",
                        "layer": "base",
                        "bundle_id": "com.apple.Terminal",
                        "display_name": "Terminal",
                        "pid": 1234,
                    }
                ],
            }
        )

    with pytest.raises(ConfigValidationError, match="Duplicate slot"):
        parse_config_dict(
            {
                "schema_version": 1,
                "bindings": [
                    {"key": "T", "layer": "base", "bundle_id": "a.one", "display_name": "One"},
                    {"key": "T", "layer": "base", "bundle_id": "b.two", "display_name": "Two"},
                ],
            }
        )


def test_reload_if_modified_detects_external_edits(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.json"
    store = BindingStore(path=cfg_path, seed_defaults_if_missing=False)
    assert store.reload_if_modified() is False

    cfg_path.write_text(
        '{"schema_version": 1, "bindings": [{"key": "F", "layer": "base", "bundle_id": "org.mozilla.firefox", "display_name": "Firefox"}]}\n',
        encoding="utf-8",
    )
    assert store.reload_if_modified() is True
    assert store.get_slot("F", Layer.BASE) is not None
    assert store.reload_if_modified() is False

    # Caps-hold quick assign is disabled by default and omitted from clean config JSON
    assert store.trigger.enable_caps_hold_assign is False
    assert "enable_caps_hold_assign" not in cfg_path.read_text(encoding="utf-8")

    store.set_caps_hold_assign_enabled(True)
    assert store.trigger.enable_caps_hold_assign is True
    assert '"enable_caps_hold_assign": true' in cfg_path.read_text(encoding="utf-8")

    store.set_caps_hold_assign_enabled(False)
    assert store.trigger.enable_caps_hold_assign is False
    assert "enable_caps_hold_assign" not in cfg_path.read_text(encoding="utf-8")

    # Auto-fullscreen option is disabled by default, persisted when enabled, and preserved on assign
    assert store.auto_fullscreen_on_switch is False
    assert "auto_fullscreen_on_switch" not in cfg_path.read_text(encoding="utf-8")

    store.set_auto_fullscreen_on_switch(True)
    assert store.auto_fullscreen_on_switch is True
    assert '"auto_fullscreen_on_switch": true' in cfg_path.read_text(encoding="utf-8")

    store.assign(
        AppBinding(key="T", layer=Layer.BASE, bundle_id="com.apple.Terminal", display_name="Terminal")
    )
    assert store.auto_fullscreen_on_switch is True

    store.set_auto_fullscreen_on_switch(False)
    assert store.auto_fullscreen_on_switch is False
    assert "auto_fullscreen_on_switch" not in cfg_path.read_text(encoding="utf-8")
