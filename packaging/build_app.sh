#!/usr/bin/env bash
# Build "ActionCheese Sync.app" (macOS) into dist/.
#
#   packaging/build_app.sh              # bundle ffmpeg -> app runs anywhere
#   packaging/build_app.sh --no-ffmpeg  # smaller; recipient needs ffmpeg installed
#
# LICENCE NOTE: Homebrew's ffmpeg is built with GPL components, so the bundled
# app is subject to the GPL -- anyone you give it to is entitled to the
# corresponding source. This project is GPL-3.0 with its source public, so that
# obligation is already met and the bundled build can be shared freely.
set -euo pipefail

cd "$(dirname "$0")/.."
VENV="${VENV:-.venv}"
BUNDLE_FFMPEG=1
[[ "${1:-}" == "--no-ffmpeg" ]] && BUNDLE_FFMPEG=0

if [[ ! -x "$VENV/bin/python" ]]; then
  echo "error: no virtualenv at $VENV (run: uv venv && uv pip install -e '.[dev]')" >&2
  exit 1
fi
"$VENV/bin/python" -c "import PyInstaller" 2>/dev/null || {
  echo "installing pyinstaller…"; "$VENV/bin/python" -m pip install -q pyinstaller; }

export ACSYNC_VERSION
ACSYNC_VERSION="$("$VENV/bin/python" -c 'import syncmyvids; print(syncmyvids.__version__)')"

if [[ "$BUNDLE_FFMPEG" == "1" ]]; then
  FF="$(command -v ffmpeg || true)"
  FP="$(command -v ffprobe || true)"
  if [[ -z "$FF" || -z "$FP" ]]; then
    echo "error: ffmpeg/ffprobe not on PATH; install them or pass --no-ffmpeg" >&2
    exit 1
  fi
  # Homebrew binaries link against dylibs in the Cellar. Resolve them so the
  # app does not break the moment it lands on a machine without Homebrew.
  if otool -L "$FF" | grep -q "@@HOMEBREW\|/opt/homebrew\|/usr/local/Cellar"; then
    echo "note: ffmpeg links to Homebrew dylibs; PyInstaller will collect them."
  fi
  export ACSYNC_BUNDLE_FFMPEG=1 ACSYNC_FFMPEG="$FF" ACSYNC_FFPROBE="$FP"
  echo "bundling ffmpeg from $FF"
else
  unset ACSYNC_BUNDLE_FFMPEG || true
  echo "not bundling ffmpeg; the recipient must have it installed"
fi

rm -rf build dist
"$VENV/bin/pyinstaller" --noconfirm --clean \
  --distpath dist --workpath build \
  packaging/actioncheese-sync.spec

if [[ -d "dist/ActionCheese Sync.app" ]]; then
  echo
  echo "built dist/ActionCheese Sync.app ($(du -sh "dist/ActionCheese Sync.app" | cut -f1))"
  echo
  echo "To share it, zip the bundle (this preserves the executable bits):"
  echo "  ditto -c -k --keepParent 'dist/ActionCheese Sync.app' dist/ActionCheeseSync-mac.zip"
  echo
  echo "It is unsigned, so on another Mac the first launch needs:"
  echo "  right-click the app -> Open -> Open"
  echo "or:  xattr -dr com.apple.quarantine '/Applications/ActionCheese Sync.app'"
else
  echo "error: build produced no .app" >&2
  exit 1
fi
