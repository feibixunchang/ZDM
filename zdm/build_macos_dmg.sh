#!/usr/bin/env bash
# Build a macOS .dmg installer for ZDM.
# This script MUST run on macOS (it uses /usr/bin/hdiutil, which is macOS-only).
#
#   ./build_macos_dmg.sh            # produces ZDM-1.13.0-<arch>.dmg in the current dir
#   ZDM_VERSION=1.13.0 ./build_macos_dmg.sh
#
# The repo already ships a ready-made ZDM.app (see zdm/install.sh, which builds it
# on install). This script just wraps ZDM.app + the browser extension + a shortcut
# to /Applications into a distributable disk image.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 版本号默认读 zdm/zdm/__init__.py（唯一真源）。
VERSION="${ZDM_VERSION:-$(sed -n 's/^__version__[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$SRC/zdm/__init__.py" 2>/dev/null | head -1)}"
VERSION="${VERSION:-0.0.0}"
APP_NAME="ZDM"
# The bundled launcher embeds one CPython framework, so the DMG is arm64- or
# x64-only - never universal. Tag the file and let update.py pick by arch.
case "$(uname -m)" in
  arm64|aarch64) PKG_ARCH=arm64 ;;
  x86_64|amd64)  PKG_ARCH=x64 ;;
  *)             PKG_ARCH="$(uname -m)" ;;
esac
DMG_NAME="${APP_NAME}-${VERSION}-${PKG_ARCH}.dmg"
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
# Ship the full install/usage guide inside the DMG when available.
if [ -f "$SRC/docs/ZDM_安装与使用说明.md" ]; then
  cp "$SRC/docs/ZDM_安装与使用说明.md" "$STAGING/安装与使用说明.md"
elif [ -f "$SRC/../docs/ZDM_安装与使用说明.md" ]; then
  cp "$SRC/../docs/ZDM_安装与使用说明.md" "$STAGING/安装与使用说明.md"
fi
# Make the .app easy to install: drop it into /Applications via a symlink in the DMG.
ln -s "$APPS" "$STAGING/Applications"

echo "==> creating $DMG_NAME with hdiutil"
hdiutil create -volname "$APP_NAME" -srcfolder "$STAGING" -ov -format UDZO "$DMG_NAME"
echo "==> done: $(pwd)/$DMG_NAME"
rm -rf "$(dirname "$STAGING")"
