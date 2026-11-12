# AppJuggler

> **Jump straight to any app or window from your keyboard.**

AppJuggler replaces hunting through `Cmd+Tab`, the Dock, and Mission Control with muscle-memory keyboard addresses:

1. **Double-tap `Caps Lock`** (`Caps Lock ×2`, default `300 ms`) — opens the non-activating keyboard map overlay while naturally restoring your original Caps Lock state and LED.
2. **Press `A–Z` (or `⇧Shift + A–Z`)** — selects one of **52 persistent application slots** (keyed by `bundle_id`, never lost when apps quit or macOS restarts).
3. **Jump immediately or pick a window**:
   - **1 window**: activates immediately — no extra keystroke needed.
   - **Multiple windows**: press **`1–9`** for instant direct jump, or **`↑` / `↓` + `Enter`** (supports `>9` windows), or **`Esc`** to cancel.
   - **Closed app**: launches the app asynchronously, observes window creation without fixed `sleep` delays, and activates the target once ready.

Fast sequences such as `Caps ×2 → T → 3` are processed immediately by an O(1) lock-free/microsecond state machine on a dedicated `CGEventTap` thread — you never have to wait for panel animations or window enumeration to finish before typing the chord.

---

## Platform Status

| Platform | Status | Details |
| --- | --- | --- |
| **macOS (13+ / Apple Silicon & Intel)** | **Implemented (MVP Stages 0–4)** | PyObjC (`Quartz` `CGEventTap`, `NSWorkspace`, `AXUIElement`, non-activating `FullScreenAuxiliary` `NSPanel`, menu bar extra, `.app` bundle via `py2app`). |
| **Linux (X11 & Wayland)** | **TODO (Contracts & Stubs Ready)** | Core domain (`NavigationController`, `WindowSlotTable`, `BindingStore`) is 100% OS-independent and tested. OS adapter stubs live in `src/appjuggler/platforms/linux/`; see [docs/linux-roadmap.md](docs/linux-roadmap.md). |

---

## Quickstart from a Clean Checkout (macOS)

### Prerequisites
- macOS 13.0 or newer (validated on macOS 15.7.9, Apple M3 Pro)
- Python 3.11+ (`python3.12` recommended for prebuilt `PyObjC` and `py2app` wheels)

### 1. Set up the virtual environment and install

```bash
git clone https://github.com/xsub/AppJuggler.git
cd AppJuggler
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

### 2. Verify unit tests and system diagnostics

```bash
pytest -v
appjuggler --doctor
```

### 3. Run AppJuggler from the terminal (development mode)

```bash
appjuggler
```

- Look for the compact **keyboard icon** in the macOS menu bar (or press **`Caps Lock ×2 → ,`** anytime if your MacBook camera notch hides overflow menu bar items).
- When running from a terminal, macOS Privacy & Security binds Accessibility permission to **your Terminal / IDE**. Open **`Permissions & Diagnostics…`** from the menu bar icon (or grant your Terminal / IDE directly in **System Settings → Privacy & Security → Accessibility**). AppJuggler detects granted permissions automatically without restarting.
- Out of the box, **`T`** (Base layer) is pre-bound to **Terminal** (`com.apple.Terminal`). Try pressing **`Caps Lock ×2 → T`**!

### 4. Build a standalone `AppJuggler.app` bundle

```bash
./scripts/build_app.sh
open dist/AppJuggler.app
```

- Produces an ad-hoc signed menu-bar bundle (`LSUIElement=true`, `CFBundleIdentifier=io.appjuggler.AppJuggler`) at `dist/AppJuggler.app`.
- Grant **AppJuggler.app** access in **System Settings → Privacy & Security → Accessibility**. (If you rebuild `AppJuggler.app` later, macOS TCC may require toggling its Accessibility switch OFF and back ON.)

---

## Assigning Apps to Keys

### 1. From Anywhere on the Keyboard — Hold `Caps Lock` for 2 Seconds
1. **Hold `Caps Lock` continuously for 2 seconds** (detected via raw `IOHIDManager` physical switch state, so normal single/double Caps taps are unaffected).
2. The **Assign Open App to Key** overlay appears over your current desktop or Fullscreen Space, listing all currently open apps (with your **currently active app pre-selected at `#1`**, plus `Unassigned` vs `Bound: X` badges).
3. Press **`Enter`** (or **`↑` / `↓` + `Enter`**, or **`1–9`**) to pick an open app.
4. The **Visual Keyboard Map** appears — press **`A–Z`** (or hold **`⇧ Shift` + `A–Z`** for Layer 2) to bind it immediately!
   - If the slot is already occupied by another app, press the **same key again** (or `Enter`) to confirm replacing it, or press any other free key.

*(Shortcut: when the normal `Caps ×2` Navigator Map is open, you can also press **`Tab`** to enter Quick Assign Open App, or press **`,`** to open the full GUI **Edit Assignments** window — no menu bar click needed, even if your MacBook camera notch hides rightmost status bar icons.)*

### 2. From the GUI Window (`Caps ×2 → ,` or Menu Bar Keyboard Icon)
- Open **`Edit Assignments…`** either by pressing **`Caps Lock ×2 → ,`** or clicking the compact keyboard icon in the macOS menu bar.
- Click **Base Layer (`A–Z`)** or **Shift Layer (`⇧A–Z`)**, click any keycap on the visual keyboard map, choose an app from **Unassigned Running Apps** / **All Running & Installed** / **Choose `.app`…**, and click **Assign to Slot**.
- Configuration is stored atomically at `~/Library/Application Support/AppJuggler/config.json` (macOS) or `~/.config/appjuggler/config.json` (Linux).

---

## Stage 0 System Probes

```bash
# Measure live Caps Lock flagsChanged toggles and double-tap intervals (ms)
.venv/bin/python scripts/probe_caps.py --seconds 15

# Inspect displays, running regular apps, duplicate-bundle conflicts, and AX windows across Spaces
.venv/bin/python scripts/probe_windows.py --all-apps
```

---

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — Cross-platform architecture, state machine invariants, and real-time threading model
- [docs/macos-validation.md](docs/macos-validation.md) — Empirical macOS 15.7.9 validation results, Spaces/Fullscreen behavior, and known public-API boundaries
- [docs/linux-roadmap.md](docs/linux-roadmap.md) — Architecture and implementation plan for Linux (X11 & Wayland) adapters
- [docs/PROJECT_HANDOFF.md](docs/PROJECT_HANDOFF.md) — Original Polish product & engineering specification
