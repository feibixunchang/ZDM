#!/usr/bin/env bash
# Build the macOS ZDM.app bundle and package it as a .dmg.
#
# This is the CI-friendly counterpart of what zdm/install.sh does when installing
# on a Mac: it compiles the native launcher (zdm_launcher.m) into a real Mach-O
# and embeds the Python runtime, so ZDM.app keeps a proper macOS app identity
# (own Dock icon, working applicationShouldHandleReopen:).
#
# Unlike install.sh this script
#   * never touches ~/Library or ~/.local (safe to run on a CI runner),
#   * writes everything under a build directory you pass in,
#   * picks the Python framework robustly (CLT / Xcode / Homebrew / pyenv),
#   * produces ZDM-<version>.dmg ready to upload.
#
# Usage:
#   ./build_macos_app.sh <build_dir> <version>
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # .../zdm/
REPO="$(cd "$SRC/.." && pwd)"                         # repo root
BUILD_DIR="${1:-$(pwd)/macos-build}"
VERSION="${2:-1.11.0}"
APP="$BUILD_DIR/ZDM.app"
STAGE="$BUILD_DIR/dmg-root"

echo "==> repo=$REPO"
echo "==> build dir=$BUILD_DIR"
echo "==> version=$VERSION"

# ---------------------------------------------------------------- python lookup
# Find an interpreter that (a) is >= 3.9 and (b) has a Python.framework we can
# link against and embed. Prefer a framework-backed interpreter so the produced
# launcher can actually load libpython at runtime.
pick_python() {
    for c in /usr/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
        command -v "$c" >/dev/null 2>&1 || [ -x "$c" ] || continue
        "$c" -c 'import sys; sys.exit(0 if sys.version_info>=(3,9) else 1)' 2>/dev/null || continue
        echo "$c"; return
    done
    echo ""
}

PY="$(pick_python)"
[ -n "$PY" ] || { echo "ERROR: no python3 >= 3.9 found" >&2; exit 1; }
echo "==> python: $PY ($("$PY" -V 2>&1))"

PYVER="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
BASE_PREFIX="$("$PY" -c 'import sys; print(getattr(sys,"base_prefix",sys.prefix))')"
echo "==> python version=$PYVER  base_prefix=$BASE_PREFIX"

# Locate a Python.framework dir that contains BOTH the dylib and the headers.
# Note: sys.base_prefix may already point at .../PythonX.framework/Versions/X.Y
# (the dylib/Headers live directly under that), while Homebrew/other builds may
# point at the prefix root. We probe several shapes and keep the first that works.
FW_DIR=""
probe() {  # $1 = candidate framework-root; checks $1/Versions/$PYVER
    local root="$1" fw
    [ -n "$root" ] || return 1
    fw="$root/Versions/$PYVER"
    if [ -f "$fw/lib/libpython$PYVER.dylib" ] && [ -f "$fw/Headers/Python.h" ]; then
        FW_DIR="$fw"; return 0
    fi
    # maybe base_prefix already IS the Versions/X.Y dir
    if [ -f "$root/lib/libpython$PYVER.dylib" ] && [ -f "$root/Headers/Python.h" ]; then
        FW_DIR="$root"; return 0
    fi
    return 1
}

probe "$BASE_PREFIX"
[ -n "$FW_DIR" ] || probe "$BASE_PREFIX/../.."
[ -n "$FW_DIR" ] || probe "$BASE_PREFIX/.."
[ -n "$FW_DIR" ] || probe "$BASE_PREFIX/Frameworks/Python$PYVER.framework"
[ -n "$FW_DIR" ] || probe "/Library/Developer/CommandLineTools/Library/Frameworks/Python$PYVER.framework"
[ -n "$FW_DIR" ] || probe "/Applications/Xcode.app/Contents/Developer/Library/Frameworks/Python$PYVER.framework"
[ -n "$FW_DIR" ] || {
    echo "ERROR: no Python.framework with libpython$PYVER.dylib + Headers found." >&2
    echo "       base_prefix=$BASE_PREFIX" >&2
    echo "       Install the Xcode command line tools: xcode-select --install" >&2
    exit 1
}
# framework ROOT (for copying the whole thing into the bundle)
FW_ROOT="$(cd "$FW_DIR/../.." 2>/dev/null && pwd || true)"
if [ ! -d "$FW_ROOT/Python$PYVER.framework" ] && [ -d "$FW_DIR/../.." ]; then
    FW_ROOT="$(cd "$FW_DIR/../.." && pwd)"
fi
echo "==> using framework version dir: $FW_DIR"
echo "==> framework root: $FW_ROOT"

# ---------------------------------------------------------------- build the app
rm -rf "$BUILD_DIR"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources" "$APP/Contents/Frameworks"

echo "==> staging runtime into ZDM.app"
# Everything the app needs at runtime lives under Contents/Resources (it holds
# both the code and the venv, so sys.path setup stays consistent). The launcher
# discovers this dir at runtime from its own executable path; the value compiled
# in below is only a last-resort fallback for exotic layouts, so shipping the
# app somewhere else never depends on it.
RES="$APP/Contents/Resources"
for item in zdm web extension; do
    cp -R "$SRC/$item" "$RES/"
done
# Top-level runtime modules that live next to gui.py but NOT inside the zdm/
# package directory (e.g. theme.py). Missing these makes `import theme` fail at
# startup, so list every such module explicitly.
cp "$SRC/zdm.py" "$SRC/gui.py" "$SRC/theme.py" "$RES/"
cp "$SRC/README.md" "$RES/" 2>/dev/null || true
cp "$SRC/LICENSE.txt" "$RES/" 2>/dev/null || true
find "$APP" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

# Fail fast if a module gui.py imports at top level is missing.
echo "==> verifying required runtime modules are present"
for mod in gui.py theme.py zdm.py; do
    [ -f "$RES/$mod" ] || { echo "ERROR: $mod missing from $RES"; exit 1; }
done

# PyQt5 is required by the desktop client; install it into a throwaway venv that
# the app will use at runtime (bundled, so the end user needs nothing).
echo "==> creating venv with PyQt5 + PyObjC"
VENV="$RES/venv"
"$PY" -m venv "$VENV"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet PyQt5 pyobjc-framework-Cocoa
# pip can "succeed" while installing somewhere unexpected, and a venv without
# PyQt5 only shows up much later as an app that refuses to start. Check now.
"$VENV/bin/python" -c "import PyQt5, os; print('    PyQt5:', os.path.dirname(PyQt5.__file__))" \
    || { echo "ERROR: PyQt5 is not importable from the build venv"; exit 1; }

# Now that a Python exists, actually import every top-level module gui.py needs.
# This is the check that catches a module being forgotten in the copy lists.
echo "==> import check for runtime modules"
( cd "$RES" && "$VENV/bin/python" -c "
import importlib.util as u, sys
missing = [m for m in ('theme', 'gui') if u.find_spec(m) is None]
if missing:
    print('missing modules:', missing); sys.exit(1)
import theme
assert theme.LIGHT and theme.DARK
assert theme.build_stylesheet(theme.DARK)
from zdm import update, desktop
assert update.is_newer('1.11.0', '1.9.1')
assert callable(desktop.ensure_desktop)
print('    theme + gui + update + desktop importable, both palettes render')
" ) || { echo "ERROR: runtime module import check failed"; exit 1; }

echo "==> embedding Python framework (for the native launcher to link against)"
# IMPORTANT: the framework directory MUST be named PythonX.framework (no minor
# version), because the launcher's rpath is @executable_path/../Frameworks/PythonX.framework/...
# Naming it "Python3.9.framework" makes dyld fail with "Library not loaded".
FW_BUNDLE_NAME="Python$(echo "$PYVER" | cut -d. -f1).framework"
rm -rf "$APP/Contents/Frameworks/$FW_BUNDLE_NAME"
cp -R "$FW_ROOT" "$APP/Contents/Frameworks/$FW_BUNDLE_NAME"
# make the dylib's install name @rpath-based so it resolves inside the bundle
EMBED_FW="$APP/Contents/Frameworks/$FW_BUNDLE_NAME/Versions/$PYVER"
if [ -f "$EMBED_FW/lib/libpython$PYVER.dylib" ]; then
    install_name_tool -id "@rpath/$FW_BUNDLE_NAME/Versions/$PYVER/Python$(echo "$PYVER" | cut -d. -f1)" \
        "$EMBED_FW/lib/libpython$PYVER.dylib" 2>/dev/null || true
fi
echo "    embedded: $FW_BUNDLE_NAME"

# ------------------------------------------------ make the venv relocatable
# `python -m venv` writes absolute paths: venv/bin/python is a symlink chain into
# the interpreter that created it (/Applications/Xcode.app/... on a dev machine)
# and pyvenv.cfg pins `home` to the same place. Both break the moment the .app is
# installed on a Mac without Xcode -- the service process dies instantly and the
# user sees "the GUI opens but every download fails".
#
# Fix: leave PyQt5 where pip installed it (Resources/venv/...), but swap the
# interpreter for a *relative* symlink to the embedded framework and drop
# pyvenv.cfg. The framework's own site-packages gets a .pth file pointing back at
# the venv's site-packages, so nothing inside the bundle refers to an absolute
# path and the whole .app can be moved anywhere.
echo "==> making the bundled interpreter relocatable"
VENV_SP="$VENV/lib/python$PYVER/site-packages"
[ -d "$VENV_SP/PyQt5" ] || { echo "ERROR: PyQt5 missing from $VENV_SP before relink"; exit 1; }

EMBED_SP="$EMBED_FW/lib/python$PYVER/site-packages"
mkdir -p "$EMBED_SP"
REL_SP="$(python3 -c "import os,sys;print(os.path.relpath(os.path.realpath(sys.argv[1]), os.path.realpath(sys.argv[2])))" \
    "$VENV_SP" "$EMBED_SP")" || { echo "ERROR: cannot compute the site-packages link path"; exit 1; }
printf '%s\n' "$REL_SP" > "$EMBED_SP/zdm-venv.pth"

rm -rf "$VENV/bin" "$VENV/pyvenv.cfg" "$VENV/pyvenv.cfg.orig"
mkdir -p "$VENV/bin"
# ../../../ from Resources/venv/bin == Contents/, then into Frameworks/
REL_FW="../../../Frameworks/$FW_BUNDLE_NAME/Versions/$PYVER"
for name in "python" "python3" "python$PYVER"; do
    ln -sf "$REL_FW/bin/python$PYVER" "$VENV/bin/$name"
done

# Prove it: the interpreter must live inside this bundle, report a prefix inside
# it, and still find PyQt5 -- with no absolute build-machine path involved.
BUNDLE_REAL="$(cd "$APP" && pwd -P)"
PY_REAL="$(python3 -c "import os,sys;print(os.path.realpath(sys.argv[1]))" "$VENV/bin/python")"
case "$PY_REAL" in
    "$BUNDLE_REAL"/*) ;;
    *) echo "ERROR: venv interpreter points outside the bundle: $PY_REAL"; exit 1 ;;
esac
"$VENV/bin/python" - <<'PYCHECK' || { echo "ERROR: bundled interpreter cannot stand on its own"; exit 1; }
import os, sys
from PyQt5.QtCore import QT_VERSION_STR
for p in (sys.prefix, sys.base_prefix):
    assert "Xcode" not in p, p
    assert "/Contents/" in os.path.realpath(p), p
print("    interpreter is self-contained (Python %s, Qt %s)" % (sys.version.split()[0], QT_VERSION_STR))
PYCHECK

echo "==> compiling native launcher (this is what makes ZDM.app a real app)"
PY_MAJOR="$(echo "$PYVER" | cut -d. -f1)"
clang -o "$APP/Contents/MacOS/ZDM" "$SRC/zdm_launcher.m" \
    -I"$FW_DIR/Headers" "$FW_DIR/lib/libpython$PYVER.dylib" \
    -DZDM_APP_DIR="\"$APP/Contents/Resources\"" \
    -Wl,-rpath,"@executable_path/../Frameworks/Python$PY_MAJOR.framework/Versions/$PYVER/lib" \
    -Wl,-rpath,"@executable_path/../Frameworks"
chmod +x "$APP/Contents/MacOS/ZDM"

echo "==> building app icon (full iconset incl. @2x)"
ICONSET="$BUILD_DIR/ZDM.iconset"
rm -rf "$ICONSET"; mkdir -p "$ICONSET"
SRC_PNG="$SRC/extension/icons/icon512.png"
for logical in 16 32 128 256 512; do
    sips -z "$logical" "$logical" "$SRC_PNG" --out "$ICONSET/icon_${logical}x${logical}.png" >/dev/null 2>&1 || true
    sips -z $((logical*2)) $((logical*2)) "$SRC_PNG" --out "$ICONSET/icon_${logical}x${logical}@2x.png" >/dev/null 2>&1 || true
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/zdm.icns"

cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>ZDM</string>
  <key>CFBundleDisplayName</key><string>ZDM 下载管理器</string>
  <key>CFBundleIdentifier</key><string>com.zdm.desktop</string>
  <key>CFBundleExecutable</key><string>ZDM</string>
  <key>CFBundleIconFile</key><string>zdm</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
EOF

# ad-hoc sign so Gatekeeper lets users open it without extra clicks
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || true
xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true

# ---------------------------------------------------------------- self-check
echo "==> self-check"
file "$APP/Contents/MacOS/ZDM" | grep -q "Mach-O" || { echo "ERROR: launcher is not a Mach-O"; exit 1; }
echo "    launcher: $(file -b "$APP/Contents/MacOS/ZDM" | cut -d: -f1)"
echo "    icon:     $(file -b "$APP/Contents/Resources/zdm.icns" | cut -d, -f1-2)"
echo "    version:  $(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$APP/Contents/Info.plist")"

# dyld must be able to resolve the embedded framework, otherwise the app dies
# immediately at launch with "Library not loaded: @rpath/PythonX.framework/...".
if ! otool -L "$APP/Contents/MacOS/ZDM" | grep -q "Python$PY_MAJOR.framework"; then
    echo "ERROR: launcher does not reference the embedded Python framework"; exit 1
fi
if [ ! -f "$APP/Contents/Frameworks/$FW_BUNDLE_NAME/Versions/$PYVER/Python$PY_MAJOR" ]; then
    echo "ERROR: embedded framework binary missing:" \
         "$APP/Contents/Frameworks/$FW_BUNDLE_NAME/Versions/$PYVER/Python$PY_MAJOR"; exit 1
fi

# Relocation test: the shipped .app is moved to /Applications (or anywhere else)
# by the user, so everything inside it must resolve relative to its own location
# -- never via a path baked in at build time. Getting this wrong is fatal twice
# over: a stale resource path means the app dies before showing a window, and a
# venv still pointing at the build machine's Python means the service never
# starts ("GUI opens but every download fails"). Rename the finished bundle,
# then ask both the launcher and the interpreter where they think they live.
echo "==> relocation test"
RELOC_ROOT="$BUILD_DIR/relocated"
RELOC="$RELOC_ROOT/ZDM Relocated.app"
rm -rf "$RELOC_ROOT"; mkdir -p "$RELOC_ROOT"
BUG_RES="$(cd "$RES" && pwd -P)"   # captured before the move, see the hint below
# never leave the bundle renamed if a check aborts the build midway
restore_app() { [ -d "$RELOC" ] && mv "$RELOC" "$APP"; return 0; }
trap restore_app EXIT
mv "$APP" "$RELOC"

RELOC_RES="$(cd "$RELOC/Contents/Resources" && pwd -P)"
GOT_RES="$(env -u ZDM_APP_DIR "$RELOC/Contents/MacOS/ZDM" --zdm-print-app-dir 2>/dev/null | head -1)"
reloc_ok=1
if [ "$GOT_RES" != "$RELOC_RES" ]; then
    echo "ERROR: launcher resolved the wrong resource dir after relocation"
    echo "       expected: $RELOC_RES"
    echo "       got:      ${GOT_RES:-(nothing)}"
    [ "$GOT_RES" = "$BUG_RES" ] && echo "       (it fell back to the build directory -- compiled-in path wins over runtime discovery)"
    reloc_ok=0
fi

# The interpreter must survive the move too: a venv that still points at the
# build machine's system Python works here and dies on the user's Mac.
if [ "$reloc_ok" = "1" ] && ! "$RELOC/Contents/Resources/venv/bin/python" -c "
import os, sys
from PyQt5.QtCore import QT_VERSION_STR
here = os.path.realpath(sys.prefix)
assert here.startswith(os.path.realpath('$RELOC')), here
" 2>"$RELOC_ROOT/py.log"; then
    [ -s "$RELOC_ROOT/py.log" ] && { echo "ERROR: relocated interpreter is not self-contained:"; cat "$RELOC_ROOT/py.log"; }
    reloc_ok=0
fi

# always put the bundle back where the rest of the script expects it
mv "$RELOC" "$APP"
trap - EXIT
rm -rf "$RELOC_ROOT"
[ "$reloc_ok" = "1" ] || exit 1
echo "    launcher + interpreter both work from the relocated path ✔"

# dyld smoke test: running the launcher must not produce a "Library not loaded"
# error. We accept it exiting with the single-instance message (when a developer
# already runs ZDM on the same port) but never a dyld failure.
echo "==> dyld load test"
DYLD_LOG="$BUILD_DIR/dyld-test.log"
"$APP/Contents/MacOS/ZDM" >"$DYLD_LOG" 2>&1 &
DYLD_PID=$!
sleep 6
kill "$DYLD_PID" 2>/dev/null || true
if grep -q "Library not loaded" "$DYLD_LOG"; then
    echo "ERROR: dyld could not resolve the embedded framework:"; cat "$DYLD_LOG"; exit 1
fi
echo "    dyld resolved the embedded framework ✔"

# Actually launch the app and make sure it survives and can talk to the service.
# NOTE: we run the bundled gui.py on an isolated port so the smoke test does not
# collide with a ZDM instance the developer may already be running on 9000 (the
# single-instance socket would make the fresh process exit immediately).
echo "==> launch smoke test (12s, isolated port 9555)"
TEST_PORT="${ZDM_SMOKE_PORT:-9555}"
SMOKE_DIR="$BUILD_DIR/smoke"
mkdir -p "$SMOKE_DIR"
# a throwaway service for the smoke test
"$RES/venv/bin/python" "$RES/zdm.py" serve --port "$TEST_PORT" \
    >"$SMOKE_DIR/service.log" 2>&1 &
SVC_PID=$!
sleep 3

LAUNCH_LOG="$BUILD_DIR/launch-test.log"
QT_QPA_PLATFORM=offscreen "$RES/venv/bin/python" "$RES/gui.py" "$TEST_PORT" \
    >"$LAUNCH_LOG" 2>&1 &
GUI_PID=$!
sleep 12

cleanup() { kill "$GUI_PID" "$SVC_PID" 2>/dev/null || true; }
trap cleanup EXIT

ok=1
if kill -0 "$GUI_PID" 2>/dev/null; then
    echo "    gui stayed alive for 12s ✔"
else
    echo "ERROR: gui exited early. Output:"; cat "$LAUNCH_LOG"; ok=0
fi
if grep -qE "Library not loaded|Terminating app|unrecognized selector|Segmentation fault|ModuleNotFoundError|fatal error|Traceback" "$LAUNCH_LOG"; then
    echo "ERROR: errors in gui output:"; cat "$LAUNCH_LOG"; ok=0
fi
if [ -s "$LAUNCH_LOG" ]; then
    echo "    gui output:"; head -5 "$LAUNCH_LOG" | sed 's/^/      /'
else
    echo "    gui output: (clean)"
fi
[ "$ok" = "1" ] || exit 1

# ---------------------------------------------------------------- dmg
# The smoke test above actually runs the app, which leaves runtime droppings
# inside the bundle: dock.log plus compiled bytecode. Strip them so the shipped
# .app is byte-clean (writing into Contents/Resources also invalidates the
# signature).
echo "==> cleaning bundle droppings"
rm -f "$APP/Contents/Resources/dock.log"
find "$APP" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$APP" -name "*.pyc" -delete 2>/dev/null || true

echo "==> staging .dmg contents"
rm -rf "$STAGE"; mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
[ -d "$SRC/extension" ] && cp -R "$SRC/extension" "$STAGE/"
cp "$SRC/README.md" "$STAGE/" 2>/dev/null || true
[ -f "$REPO/docs/ZDM_安装与使用说明.md" ] && cp "$REPO/docs/ZDM_安装与使用说明.md" "$STAGE/安装与使用说明.md"
ln -s /Applications "$STAGE/Applications"

DMG="$BUILD_DIR/ZDM-$VERSION.dmg"
echo "==> creating $DMG"
hdiutil create -volname ZDM -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null
echo "==> done: $DMG"