#!/usr/bin/env bash
# Build a macOS .dmg installer for ZDM.
# This script MUST run on macOS (it uses /usr/bin/hdiutil, which is macOS-only).
#
#   ./build_macos_dmg.sh            # produces ZDM-1.0.0.dmg in the current dir
#   ZDM_VERSION=1.2.0 ./build_macos_dmg.sh
#
# The repo already ships a ready-made ZDM.app (see zdm/install.sh, which builds it
# on install). This script just wraps ZDM.app + the browser extension + a shortcut
# to /Applications into a distributable disk image.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION="${ZDM_VERSION:-1.0.0}"
APP_NAME="ZDM"
DMG_NAME="${APP_NAME}-${VERSION}.dmg"
STAGING="$(mktemp -d)/${APP_NAME}"
APPS="/Applications"

echo "==> staging $APP_NAME.app + extension + Apps shortcut"
mkdir -p "$STAGING"
# Prefer a freshly built app bundle if present, else the one shipped in the repo.
if [ -d "$SRC/ZDM.app" ]; then
  cp -R "$SRC/ZDM.app" "$STAGING/"
else
  echo "!! ZDM.app not found next to this script; run zdm/install.sh on this Mac first" >&2
  exit 1
fi
[ -d "$SRC/extension" ] && cp -R "$SRC/extension" "$STAGING/"
[ -f "$SRC/README.md" ] && cp "$SRC/README.md" "$STAGING/"
# Make the .app easy to install: drop it into /Applications via a symlink in the DMG.
ln -s "$APPS" "$STAGING/Applications"

echo "==> creating $DMG_NAME with hdiutil"
hdiutil create -volname "$APP_NAME" -srcfolder "$STAGING" -ov -format UDZO "$DMG_NAME"
echo "==> done: $(pwd)/$DMG_NAME"
rm -rf "$(dirname "$STAGING")"
