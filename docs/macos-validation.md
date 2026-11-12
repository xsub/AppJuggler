# macOS Empirical Validation & System Behavior Matrix

Recorded on the target hardware during Stage 0–4 implementation (`2026-11-12`).

## 1. Validated Target Environment

| Property | Measured Value |
| --- | --- |
| **Hardware** | MacBook Pro (`Mac15,6`), Apple M3 Pro (11 CPU cores, 36 GB RAM) |
| **OS** | macOS `15.7.9` (`Build 24G830`, `arm64`) |
| **Display** | Built-in Liquid Retina XDR (`3024 × 1964` physical, `1512 × 982 @ 2.0x` logical) |
| **Active Keyboard Layout** | `Polish Pro` (`KeyboardLayout ID = 30788`, QWERTY-based Latin layout; `UCKeyTranslate` resolved all 26 letters A–Z without fallback) |
| **Mission Control / Spaces** | `workspaces-auto-swoosh = true` (default), `spans-displays = false` (displays have separate Spaces) |
| **Runtime & Packaging** | Python `3.12.13` + `PyObjC 13.0` + `py2app 0.28.10` (`dist/AppJuggler.app`, ad-hoc signed `io.appjuggler.AppJuggler`) |

---

## 2. Empirical Stage 0 Findings & Resulting Engineering Decisions

### 2.1. Caps Lock Representation (`CGEventTap` vs `IOHIDManager`)
- Pressing Caps Lock emits `kCGEventFlagsChanged` with `keycode == 57` (`0x39`), **not** `keyDown`/`keyUp`.
- Each genuine Caps Lock press toggles the `kCGEventFlagMaskAlphaShift` (`0x00010000`) bit relative to the previous state (`OFF → ON → OFF` when initially off; `ON → OFF → ON` when initially on).
- Filtering strictly on `curr_alpha_shift != last_alpha_shift` counts **only distinct Caps Lock state transitions**, preventing false single-tap triggers on external USB/Bluetooth keyboards that emit redundant `flagsChanged(keycode=57)` events on key release.
- **Optional 2-Second Physical Hold (`IOHIDManager`, disabled by default)**: Normal execution runs **only** `CGEventTap`. Only when `trigger.enable_caps_hold_assign = true` is explicitly enabled does `MacOSInputAdapter` attach an `IOHIDManager` (`UsagePage = 0x07, Usage = 0x39`) listener for physical switch-contact holds.
- **MacBook Pro Camera Notch & Menu Bar Overflow**: On 14" MacBook Pro (`1512pt` wide, camera notch centered around `X ≈ 656..856`), having ~19 third-party status bar items pushes leftmost `NSStatusItem`s behind the notch where macOS hides them. AppJuggler uses compact `NSSquareStatusItemLength` (`keyboard` SF Symbol) AND exposes `Edit Assignments…` directly from the keyboard via `Caps ×2 → ,`.
- Note on Apple built-in keyboards: Apple's HID firmware applies a brief (~70 ms) anti-accidental-press filter before turning Caps Lock ON from OFF; normal deliberate taps toggle cleanly, while ultra-brief `<50 ms` grazes are ignored by macOS hardware before reaching WindowServer (leaving LED unchanged).

### 2.2. Fullscreen Spaces & Off-Space Windows (`kAXWindowsAttribute` vs `kAXMainWindowAttribute`)
- **Measured on macOS 15.7.9** across `Safari`, `Claude`, `Terminal` (in native Fullscreen Spaces), and `Firefox` / `Finder` (on inactive Desktop Spaces):
  - `kAXWindowsAttribute` returns **only** windows on the **currently active Space** plus **minimized** windows — it returns `0` windows when an app's sole window is in a native Fullscreen Space or another Desktop Space!
  - However, `kAXMainWindowAttribute` and `kAXFocusedWindowAttribute` **still return the live off-Space / Fullscreen `AXUIElement`** (`AXRole == 'AXWindow'`, valid `AXParent`, `AXFullScreen == True/False`)!
  - Furthermore, any `AXUIElement` handle discovered earlier in the AppJuggler session remains valid (`CFEqual`, `AXRole`, `AXParent`) and raisable via `AXRaise` even after switching away from its Space.
- **Adapter implementation (`MacOSWindowRegistry`)**:
  - Merges `[kAXMainWindowAttribute, kAXFocusedWindowAttribute] + kAXWindowsAttribute + live session-cached AXUIElements` (deduplicated via `CoreFoundation.CFEqual`).
  - Also solves apps like `Terminal.app` that expose auxiliary `('AXWindow', 'AXUnknown')` children ahead of their `AXStandardWindow` in `kAXWindowsAttribute`.

### 2.3. Minimized Windows Reporting `AXSubrole == 'AXDialog'` on macOS 15
- **Measured on `Finder` and `PreviewMD`**: on macOS 15.7.9, standard windows minimized to the Dock change `AXSubrole` from `'AXStandardWindow'` to `'AXDialog'` while `AXModal` remains `False`.
- **Adapter implementation**: `is_modal` checks `AXModal == True` or `AXSubrole in ('AXSystemDialog', 'AXSheet')` / `AXRole in ('AXDialog', 'AXSheet')` so minimized standard windows are badged `Minimized` rather than falsely badged `Modal`.

### 2.4. Duplicate Application Bundle Conflicts
- Live scan via `NSWorkspace.URLsForApplicationsWithBundleIdentifier_` detected real multi-copy installations on the machine (`com.openai.codex` ×2, `com.spotify.client` ×2).
- Both the HUD map (`!` indicator) and **Edit Assignments…** surface duplicate installations explicitly and let the user select the exact `.app` launch path hint rather than guessing.

### 2.5. `py2app 0.28.10` + `setuptools 84` Compatibility
- PEP 621 `[project].dependencies` in `pyproject.toml` causes `setuptools >= 61` to populate `distribution.install_requires`, which `py2app 0.28.10` rejects (`error: install_requires is no longer supported`).
- `setup_app.py` subclasses `py2app` (`CleanPy2App`) to clear `distribution.install_requires` prior to `finalize_options()`, producing a verified, ad-hoc signed `dist/AppJuggler.app`.

---

## 3. Scenario & Limitation Matrix (Acceptance Criterion 11)

| Scenario | Behavior | Documented Limitations |
| --- | --- | --- |
| **Same Space (multi-window)** | Instant `AXRaise` + `kAXMainAttribute` + `activateIgnoringOtherApps`; session slot numbers `1..N` remain fixed across focus and title changes. | Slot numbers are session-scoped and intentionally reset if the target app or AppJuggler restarts. |
| **Other Desktop Space** | App's main/focused window and any window previously seen during the session appear with an `Other Space` badge; selecting one triggers macOS's native Space transition. | IfAppJuggler starts cold while an app has *multiple* unfocused windows on an unvisited Desktop Space, macOS public AX only exposes that app's `MainWindow`/`FocusedWindow` until that Space is visited once in the session (no private CGS Space APIs used). Requires Mission Control *"When switching to an application, switch to a Space with open windows for the application"* (`workspaces-auto-swoosh = true`, macOS default). |
| **Native Fullscreen Space** | HUD `NSPanel` (`NonactivatingPanel \| CanJoinAllSpaces \| FullScreenAuxiliary`, level `NSPopUpMenuWindowLevel`) renders directly over Fullscreen apps without exiting fullscreen or stealing focus. | Cross-Space jumps use macOS's standard Space swoosh animation (~350–600 ms); AppJuggler does not inject code or disable SIP to bypass WindowServer animations. |
| **Hidden App (`Cmd+H`)** | `MacOSWindowActivator` calls `running_app.unhide()` before raising the chosen window. | None. |
| **Minimized Window** | Badged `Minimized` in the window list; selecting it sets `kAXMinimizedAttribute = False`, waits up to 250 ms for Dock unminimize, then raises and focuses it. | Dock genie/scale animation duration is governed by macOS Dock settings. |
| **Modal Dialog / Sheet** | Included in window discovery (`AXModal == True` / `AXSheet` / `AXSystemDialog`) and badged `Modal` rather than filtered out. | An app-modal dialog blocks selecting sibling background windows of that same app until dismissed in the app itself. |
| **Multiple Monitors** | HUD `NSPanel` centers on whichever `NSScreen` currently contains the mouse pointer (`NSEvent.mouseLocation()`), falling back to `NSScreen.mainScreen()`. | Validated structurally via `NSScreen` geometry calculation; single built-in display attached during Stage 0 hardware run. |
| **Electron / CEF Windowless or Custom Windows** | Apps with 0 AX windows (e.g. closed window while app stays running, or unexposed custom surface) trigger `activate_application_only` without synthesizing `Cmd+N`. | Some Electron/CEF builds delay exposing AX trees until Accessibility is queried or an app window is opened. |
