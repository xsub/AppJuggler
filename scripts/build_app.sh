#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Error: Virtualenv Python not found at $PYTHON_BIN" >&2
  echo "Run: python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'" >&2
  exit 1
fi

echo "==> Cleaning previous build artifacts..."
rm -rf "$ROOT_DIR/build" "$ROOT_DIR/dist"

echo "==> Building AppJuggler.app via py2app..."
PYTHONPATH="$ROOT_DIR/src" "$PYTHON_BIN" setup_app.py py2app >/dev/null

APP_BUNDLE="$ROOT_DIR/dist/AppJuggler.app"
if [[ ! -d "$APP_BUNDLE" ]]; then
  echo "Error: Expected bundle not produced at $APP_BUNDLE" >&2
  exit 1
fi

echo "==> Applying local ad-hoc signature for stable TCC identity..."
codesign --force --deep --sign - "$APP_BUNDLE"

echo "==> Verifying bundle signature and Info.plist..."
codesign --verify --deep --strict "$APP_BUNDLE"
/usr/libexec/PlistBuddy -c "Print :CFBundleIdentifier" "$APP_BUNDLE/Contents/Info.plist"

echo "==> Built successfully: $APP_BUNDLE"
echo "    Note: After rebuilding, if macOS Accessibility stops responding, toggle AppJuggler OFF/ON in System Settings → Privacy & Security → Accessibility."
