# Linux Support Roadmap (`TODO`)

AppJuggler's core engine (`src/appjuggler/domain/` and `src/appjuggler/persistence/`) has **zero macOS imports** and already runs unchanged on Linux (`pyproject.toml` scopes `pyobjc-*` to `sys_platform == 'darwin'`).

Stub implementations of all six platform contracts live in [`src/appjuggler/platforms/linux/stub.py`](../src/appjuggler/platforms/linux/stub.py). This document specifies how to complete native Linux support across **X11** and **Wayland**.

---

## 1. Adapter Mapping (`contracts.py` → Linux Subsystems)

| Contract | X11 Backend | Wayland Backend |
| --- | --- | --- |
| **`InputAdapter`** | `XInput2` (`XI_RawKeyPress` / `XI_RawKeyRelease`) for passive double-Caps Lock observation + `XGrabKeyboard` while the modal HUD is open; XKB (`xkbcommon`) with `mods=0` for layout-independent base letter `A–Z`. | `/dev/input/event*` (`python-evdev`) + `uinput` grab while modal HUD is open (works identically across GNOME, KDE KWin, Sway, Hyprland; requires `input`/`uinput` udev rule). |
| **`ApplicationRegistry`** | Desktop Entry ID (`<app>.desktop`, e.g. `org.mozilla.firefox.desktop`) used as `bundle_id` in `config.json`. Scan `$XDG_DATA_DIRS/applications/*.desktop`; launch via `Gio.DesktopAppInfo.launch()`. | Same XDG `.desktop` registry and `Gio.DesktopAppInfo` launcher. |
| **`WindowRegistry`** | EWMH `_NET_CLIENT_LIST`, `WM_CLASS` / `_GTK_APPLICATION_ID` / `_NET_WM_PID` correlation to `.desktop` ID, `_NET_WM_STATE_HIDDEN` (minimized), `_NET_WM_STATE_FULLSCREEN`, `_NET_WM_STATE_MODAL`, `_NET_WM_DESKTOP` (workspace label). | Compositor-specific discovery adapters:<br>• **wlroots (Sway / Hyprland / River)**: `zwlr_foreign_toplevel_management_v1` (plus Hyprland/Sway IPC socket for workspace numbers)<br>• **KDE Plasma (KWin)**: KWin DBus scripting API<br>• **GNOME Shell**: tiny companion GNOME Shell extension exposing `org.appjuggler.Windows` over session DBus (since stock Mutter restricts foreign window enumeration). |
| **`WindowActivator`** | Send EWMH `_NET_ACTIVE_WINDOW` client message (and `_NET_CURRENT_DESKTOP` if needed) + `XSync` focus verification with bounded 800 ms timeout. | `zwlr_foreign_toplevel_handle_v1.activate(seat)` + `xdg_activation_v1` token (wlroots), KWin DBus activate, or GNOME companion extension ` window.activate(global.get_current_time())`. |
| **`OverlayPresenter`** | GTK4 (`PyGObject`) borderless `override-redirect` / `_NET_WM_WINDOW_TYPE_NOTIFICATION` non-activating window centered on pointer monitor. | GTK4 + **`gtk4-layer-shell`** (`zwlr_layer_surface_v1` on `ZWLR_LAYER_SHELL_V1_LAYER_OVERLAY` with `KEYBOARD_INTERACTIVITY_NONE` when input is handled by `InputAdapter`, or `EXCLUSIVE` if compositor-driven). |
| **`PermissionMonitor`** | Check `$DISPLAY` connectivity and XInput2 availability; detect screensaver/lock via `org.freedesktop.ScreenSaver` / `loginctl` `LockedHint`. | Check read/write access to `/dev/input/event*` & `/dev/uinput`, Wayland socket `$WAYLAND_DISPLAY`, compositor capability, and `org.freedesktop.login1.Session.LockedHint`. |

---

## 2. Implementation Milestones for Linux

- [ ] **Milestone L0 (Linux Probes)**: Add `scripts/probe_linux_caps.py` (`evdev` `KEY_CAPSLOCK` value `1` press vs `0` release vs `2` repeat + LED state `/sys/class/leds/*::capslock/brightness`) and `scripts/probe_linux_windows.py` (X11 EWMH + Wayland compositor detection).
- [ ] **Milestone L1 (X11 Vertical Prototype)**: Implement `X11InputAdapter`, `XdgApplicationRegistry`, `EwmhWindowRegistry`, `EwmhWindowActivator`, and GTK4 HUD overlay; verify `Caps ×2 → T → 1..9` on X11.
- [ ] **Milestone L2 (Wayland wlroots / KDE / GNOME)**: Implement `EvdevInputAdapter`, `gtk4-layer-shell` overlay, and `wlr-foreign-toplevel` + KWin/GNOME window bridges.
- [ ] **Milestone L3 (Packaging)**: Desktop entry, systemd user unit (`appjuggler.service`), and udev rule documentation for `evdev`/`uinput` permissions.
