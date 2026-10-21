#!/usr/bin/env bash
# Build the *self-contained* Linux desktop app and package it as a real .deb.
#
# Why this script exists
# ----------------------
# The old Linux packaging shipped ~350 KB of .py files plus a 4-line shell
# wrapper that ran the system's python3. That "package" was a shell in the
# literal sense: it contained no interpreter, no PyQt5 and no Qt, declared only
# `Depends: python3`, and tried to pip-install PyQt5 as root in postinst (where
# it lands in /root/.local and is invisible to the desktop user, and on PEP-668
# distros fails outright). On a clean machine the window never opened and the
# app silently fell back to the browser UI.
#
# This script produces what the macOS .dmg has always been: one bundle with its
# own CPython, PyQt5/Qt and resources. The output is
#
#   $BUILD_DIR/ZDM/ZDM              real ELF executable (PyInstaller onedir)
#   $BUILD_DIR/ZDM/_internal/...    CPython + PyQt5 + web/ + extension/
#   zdm_<version>_<arch>.deb        installs to /opt/zdm, no pip, no system python
#   zdm-linux-<arch>.tar.gz         same tree, user-level install (no root)
#
# Usage:
#   ./build_linux_app.sh [build_dir] [version]
#
# Requires (CI installs these): python3 >= 3.9 with venv, network access to PyPI,
# and dpkg-deb for the .deb (p7zip not needed any more).
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"       # .../zdm/
REPO="$(cd "$SRC/.." && pwd)"                             # repo root
BUILD_DIR="${1:-$(pwd)/linux-build}"
# 版本号默认读 zdm/zdm/__init__.py（唯一真源）。写死在这里会漂：
# v1.14.7 就出现过 build_dist.py=1.13.0 / 后端=1.14.0，包名和 /api 报的版本对不上。
VERSION="${2:-$(sed -n 's/^__version__[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$SRC/zdm/__init__.py" 2>/dev/null | head -1)}"
VERSION="${VERSION:-0.0.0}"

DIST="$BUILD_DIR/dist"
PYBUILD="$BUILD_DIR/pybuild"
VENV="$BUILD_DIR/build-venv"
APP="$DIST/ZDM"                                            # onedir root
STAGE="$BUILD_DIR/deb-root"

echo "==> repo=$REPO"
echo "==> build dir=$BUILD_DIR"
echo "==> version=$VERSION"

case "$(uname -m)" in
    x86_64)  DEFAULT_ARCH=amd64 ;;
    aarch64) DEFAULT_ARCH=arm64 ;;
    armv7l)  DEFAULT_ARCH=armhf ;;
    *)       DEFAULT_ARCH="$(uname -m)" ;;
esac
ARCH="${ZDM_DEB_ARCH:-$(dpkg --print-architecture 2>/dev/null || echo "$DEFAULT_ARCH")}"
echo "==> deb architecture=$ARCH (host $(uname -m))"

# ---------------------------------------------------------------- toolchain
PY=""
for c in python3 python3.12 python3.11 python3.10 /usr/bin/python3; do
    command -v "$c" >/dev/null 2>&1 || continue
    "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null || continue
    "$c" -c 'import venv' 2>/dev/null || continue
    PY="$c"; break
done
[ -n "$PY" ] || { echo "ERROR: python3 >= 3.9 with venv/ensurepip is required" >&2; exit 1; }
echo "==> python: $PY ($("$PY" -V 2>&1))"

if ! command -v dpkg-deb >/dev/null 2>&1; then
    echo "ERROR: dpkg-deb not found; the .deb can only be built on Debian/Ubuntu." >&2
    exit 1
fi

rm -rf "$DIST" "$PYBUILD" "$VENV" "$STAGE"
mkdir -p "$DIST" "$PYBUILD"

# ---------------------------------------------------------------- build venv
# A throwaway venv keeps the build reproducible and keeps PyQt5/PyInstaller out
# of the system interpreter (PEP 668 would refuse either way).
echo "==> creating build venv"
# Debian/Ubuntu may ship venv without ensurepip (python3-venv split); create a
# pip-less venv and bootstrap pip so the build also works outside CI.
if "$PY" -c 'import ensurepip' 2>/dev/null; then
    "$PY" -m venv "$VENV"
else
    "$PY" -m venv --without-pip "$VENV"
fi
if ! "$VENV/bin/python" -m pip --version >/dev/null 2>&1; then
    echo "==> bootstrapping pip (venv has no ensurepip)"
    curl -fsSL https://bootstrap.pypa.io/get-pip.py -o "$BUILD_DIR/get-pip.py"
    "$VENV/bin/python" "$BUILD_DIR/get-pip.py" --quiet --disable-pip-version-check
fi
# shellcheck disable=SC1091
. "$VENV/bin/activate"
python -m pip install --upgrade --quiet pip wheel
echo "==> installing PyQt5 + PyInstaller"
python -m pip install --quiet "PyQt5>=5.15,<5.16" "pyinstaller>=6.3"
python - <<'PYEOF'
import PyInstaller
from PyQt5.QtCore import PYQT_VERSION_STR, QT_VERSION_STR
print(f"    PyQt5 {PYQT_VERSION_STR} | Qt {QT_VERSION_STR} | PyInstaller {PyInstaller.__version__}")
PYEOF

# ---------------------------------------------------------------- freeze
echo "==> freezing (PyInstaller onedir)"
cd "$SRC"
export PYTHONDONTWRITEBYTECODE=1
pyinstaller --clean --noconfirm \
    --distpath "$DIST" --workpath "$PYBUILD" \
    "$SRC/zdm.spec" >"$BUILD_DIR/pyinstaller.log" 2>&1 || {
        echo "ERROR: pyinstaller failed; tail of $BUILD_DIR/pyinstaller.log:" >&2
        tail -40 "$BUILD_DIR/pyinstaller.log" >&2
        exit 1
    }

[ -x "$APP/ZDM" ] || { echo "ERROR: $APP/ZDM missing" >&2; exit 1; }
echo "==> bundle: $(du -sh "$APP" | cut -f1) at $APP"
file "$APP/ZDM" | sed 's/^/    /'

# ---------------------------------------------------------------- self checks
echo "==> self-check: interpreter, PyQt5, resources"
SELFCHECK="$("$APP/ZDM" --selfcheck)" || {
    echo "ERROR: --selfcheck failed (bundle cannot start)" >&2
    echo "$SELFCHECK" >&2
    exit 1
}
echo "$SELFCHECK"
python - "$SELFCHECK" "$VERSION" <<'PYEOF'
import json, sys
r = json.loads(sys.argv[1])
want = sys.argv[2]
bad = []
if not r.get("frozen"): bad.append("not running frozen")
if not r.get("web_index"): bad.append("web/index.html missing")
if not r.get("extension_manifest"): bad.append("extension/manifest.json missing")
if not r.get("extension_sniffer"): bad.append("extension/sniffer.js missing (media sniffer)")
if not r.get("theme_module"): bad.append("theme.py not importable")
if not r.get("pyqt5"): bad.append("PyQt5 not importable")
# 这两个是「装完在用户机器上才发现」的典型：YouTube 全靠随包 yt-dlp，
# B站 DASH 靠随包 ffmpeg 合音轨。构建期少一个，用户那边就是
# 「YouTube 不能下」或「下载成功但没声音」。
if not r.get("ytdlp"): bad.append("bundled yt-dlp binary missing")
if not r.get("ffmpeg"): bad.append("bundled ffmpeg missing (DASH 音轨无法合流)")
if r.get("version") != want:
    bad.append(f"version mismatch: bundle {r.get('version')} != build {want}")
if bad:
    print("SELF-CHECK FAILED:", "; ".join(bad)); sys.exit(1)
print("    frozen bundle OK (python %s, PyQt5 %s, Qt %s)" % (r["python"], r["pyqt5"], r["qt"]))
PYEOF

echo "==> self-check: no unresolved shared libraries"
missing=0
while IFS= read -r f; do
    if file "$f" | grep -q ELF && ldd "$f" 2>/dev/null | grep -q "not found"; then
        echo "    MISSING LIBS in $f:" >&2
        ldd "$f" 2>/dev/null | grep "not found" >&2
        missing=1
    fi
done < <(find "$APP" \( -name "*.so*" -o -name ZDM \) -type f | head -120)
[ "$missing" = 0 ] || { echo "ERROR: bundle references libraries that are not present" >&2; exit 1; }
echo "    all bundled objects resolve"

echo "==> self-check: window launches (headless)"
SMOKE="$BUILD_DIR/smoke"
mkdir -p "$SMOKE"
ln -sfn "$APP/ZDM" "$SMOKE/zdm-desktop"
# --smoke quits by itself after ~3.5 s; a first-run window must actually map.
# The argv[0] basename selects GUI mode, exactly like the installed /usr/bin link.
if command -v xvfb-run >/dev/null 2>&1; then
    smoke_cmd=(xvfb-run -a "$SMOKE/zdm-desktop" 9111 --smoke)
else
    echo "    xvfb-run not found, using Qt's offscreen platform"
    smoke_cmd=(env QT_QPA_PLATFORM=offscreen "$SMOKE/zdm-desktop" 9111 --smoke)
fi
if timeout 90 "${smoke_cmd[@]}"; then
    echo "    GUI started and exited cleanly"
else
    echo "ERROR: GUI smoke test failed" >&2
    exit 1
fi

# ---------------------------------------------------------------- .deb
echo "==> staging the .deb"
APPDIR="$STAGE/opt/zdm"
mkdir -p "$APPDIR" "$STAGE/usr/bin" \
         "$STAGE/usr/share/applications" \
         "$STAGE/usr/share/doc/zdm"
cp -a "$APP/." "$APPDIR/"
find "$APPDIR" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true

# One ELF, three names: the CLI (`zdm`), the desktop client (`zdm-desktop`,
# which zdm/desktop.py already looks up on PATH) and the raw bundle.
ln -sfn /opt/zdm/ZDM "$STAGE/usr/bin/zdm"
ln -sfn /opt/zdm/ZDM "$STAGE/usr/bin/zdm-desktop"

for size in 16 24 32 48 64 128 256 512; do
    src="$SRC/extension/icons/icon${size}.png"
    [ -f "$src" ] || continue
    if [ "$size" = 256 ]; then dst="256x256"; else dst="${size}x${size}"; fi
    install -Dm644 "$src" "$STAGE/usr/share/icons/hicolor/$dst/apps/zdm.png"
done

cat >"$STAGE/usr/share/applications/zdm.desktop" <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=ZDM 下载管理器
Name[en]=ZDM Download Manager
GenericName=下载管理器
Comment=多线程下载管理器（浏览器扩展可接管下载）
Exec=/usr/bin/zdm-desktop %u
TryExec=/usr/bin/zdm-desktop
Icon=zdm
Terminal=false
Categories=Network;FileTransfer;
Keywords=download;zdm;下载;
MimeType=x-scheme-handler/zdm;
StartupNotify=true
StartupWMClass=ZDM
DESKTOP

# Chrome native-messaging host: without it the extension's connectNative()
# fails on every cold start and "browser takeover" silently degrades to the
# zdm:// fallback (which needs the scheme handler below to exist at all).
# The manifest path cannot carry arguments on Linux, so it points at a tiny
# wrapper that adds "--native-host" - the bundled ZDM ELF speaks the protocol.
EXT_ID="mpodbnemeeghjpllgphpemhoogddfhjg"   # id pinned by extension/manifest.json's key
cat >"$APPDIR/zdm-native-host" <<'WRAPPER'
#!/bin/sh
# Chrome/Chromium native-messaging host shim for the frozen bundle.
exec /opt/zdm/ZDM --native-host "$@"
WRAPPER
chmod 0755 "$APPDIR/zdm-native-host"
for hostdir in /etc/opt/chrome/native-messaging-hosts /etc/chromium/native-messaging-hosts; do
    install -Dm644 /dev/null "$STAGE$hostdir/com.zdm.launcher.json"
    cat >"$STAGE$hostdir/com.zdm.launcher.json" <<MANIFEST
{
  "name": "com.zdm.launcher",
  "description": "ZDM 下载管理器启动器",
  "path": "/opt/zdm/zdm-native-host",
  "type": "stdio",
  "allowed_origins": ["chrome-extension://$EXT_ID/"]
}
MANIFEST
done

# System-wide default handler for the zdm:// scheme (the extension's fallback
# wake-up path). /etc/xdg/mimeapps.list is the XDG system default location.
install -Dm644 /dev/null "$STAGE/etc/xdg/mimeapps.list"
cat >"$STAGE/etc/xdg/mimeapps.list" <<'MIME'
[Default Applications]
x-scheme-handler/zdm=zdm.desktop
MIME

install -m644 "$REPO/LICENSE.txt" "$STAGE/usr/share/doc/zdm/copyright"
install -m644 "$SRC/README.md" "$STAGE/usr/share/doc/zdm/README.md"

INSTALLED_KB="$(du -sk "$STAGE" --exclude=DEBIAN | awk '{print $1}')"
mkdir -p "$STAGE/DEBIAN"
cat >"$STAGE/DEBIAN/control" <<CONTROL
Package: zdm
Version: $VERSION
Section: net
Priority: optional
Architecture: $ARCH
Installed-Size: $INSTALLED_KB
Maintainer: ZDM <noreply@example.com>
Depends: libgl1, libglib2.0-0, libegl1, libfontconfig1, libfreetype6, libdbus-1-3, libx11-6, libx11-xcb1, libxau6, libxdmcp6, libxext6, libxrender1, libxi6, libxfixes3, libsm6, libice6, libxkbcommon0, libxkbcommon-x11-0, libxcb1, libxcb-icccm4, libxcb-image0, libxcb-keysyms1, libxcb-randr0, libxcb-render-util0, libxcb-shape0, libxcb-xinerama0, libxcb-xkb1
Recommends: xdg-utils
Description: ZDM 跨平台多线程下载管理器
 多线程分块、断点续传、暂停/继续、队列、限速；自带浏览器扩展接管下载；
 桌面端单实例 + 系统托盘（开机自启/退出）。
 .
 本包自带 Python 运行时与 PyQt5/Qt，无需系统 python3 或 pip 即可运行。
CONTROL

cat >"$STAGE/DEBIAN/postinst" <<'POSTINST'
#!/bin/sh
set -e
# No pip, no interpreter check: everything the app needs is inside /opt/zdm.
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q /usr/share/applications || true
fi
if command -v update-mime-database >/dev/null 2>&1; then
    update-mime-database /usr/share/mime >/dev/null 2>&1 || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f -t /usr/share/icons/hicolor >/dev/null 2>&1 || true
fi
if command -v xdg-desktop-menu >/dev/null 2>&1; then
    xdg-desktop-menu forceupdate >/dev/null 2>&1 || true
fi

# 升级后旧版 serve 进程还握着 9000 端口跑旧代码：新界面 ping 得到它就
# 不会重启，表现成「装了新版还是旧行为/下不动」。先杀掉旧进程，再以
# 触发安装的桌面用户身份把新 serve 拉起来 —— 用户不用再开终端敲
# `zdm start --daemon`（实测升级路径）。纯 root 环境（容器/CI）不启动：
# serve 以 root 跑会写坏下载目录属主。
pkill -f "/opt/zdm/ZDM" >/dev/null 2>&1 || true
TU=""
for v in "${SUDO_USER:-}" "${PKEXEC_USER:-}"; do
    if [ -n "$v" ] && [ "$v" != "root" ]; then TU="$v"; fi
done
# 双击 deb 常由 pkexec/图形包管理器装：SUDO_USER/PKEXEC_USER 都可能
# 没有 —— 兜底扫 /run/user/<uid>，第一个非 root 的图形会话就是目标用户。
if [ -z "$TU" ]; then
    for d in /run/user/*/; do
        [ -d "$d" ] || continue
        u=$(stat -c %U "$d" 2>/dev/null || true)
        if [ -n "$u" ] && [ "$u" != "root" ] && [ "$u" != "UNKNOWN" ]; then TU="$u"; break; fi
    done
fi
if [ -n "$TU" ] && [ -x "/opt/zdm/ZDM" ]; then
    su -s /bin/sh -c "nohup /opt/zdm/ZDM start --daemon >/dev/null 2>&1" "$TU" || true
fi
exit 0
POSTINST
chmod 0755 "$STAGE/DEBIAN/postinst"

cat >"$STAGE/DEBIAN/prerm" <<'PRERM'
#!/bin/sh
set -e
# Stop a running desktop client so no file stays mapped from /opt/zdm.
pkill -f '/opt/zdm/ZDM' >/dev/null 2>&1 || true
# Drop the browser integration we registered in postinst: a stale host
# manifest makes Chrome launch a binary that no longer exists.
rm -f /etc/opt/chrome/native-messaging-hosts/com.zdm.launcher.json \
      /etc/chromium/native-messaging-hosts/com.zdm.launcher.json
exit 0
PRERM
chmod 0755 "$STAGE/DEBIAN/prerm"

DEB="$BUILD_DIR/zdm_${VERSION}_${ARCH}.deb"
echo "==> building $DEB"
# -Zxz keeps the package readable by older dpkg releases; zstd (the default on
# recent dpkg) is not understood by Ubuntu 18.04-era tooling.
dpkg-deb --build --root-owner-group -Zxz "$STAGE" "$DEB" >/dev/null
echo "    $(du -h "$DEB" | cut -f1)  $DEB"

# verify the control fields really landed where we think they did
dpkg-deb --field "$DEB" Package Version Architecture Installed-Size | sed 's/^/    /'
BUNDLED="$(dpkg-deb --contents "$DEB" | grep -c '/opt/zdm/_internal/' || true)"
[ "$BUNDLED" -gt 100 ] || {
    echo "ERROR: the .deb does not contain a bundled runtime ($BUNDLED files under /opt/zdm/_internal)" >&2
    exit 1
}
echo "    bundle files under /opt/zdm/_internal: $BUNDLED"

# ---------------------------------------------------------------- portable
echo "==> portable tarball (no root)"
TARROOT="$BUILD_DIR/portable"
rm -rf "$TARROOT"
mkdir -p "$TARROOT/zdm-linux"
cp -a "$APP/." "$TARROOT/zdm-linux/"
# Same native-messaging shim as the .deb ships, so the extension's
# connectNative() also works from the portable install.
cat >"$TARROOT/zdm-linux/zdm-native-host" <<'WRAPPER'
#!/bin/sh
# Chrome/Chromium native-messaging host shim: resolves its own location so the
# bundle can live anywhere (~/.local/share/zdm, a USB stick, ...).
HERE="$(cd "$(dirname "$0")" && pwd)"
exec "$HERE/ZDM" --native-host "$@"
WRAPPER
chmod 0755 "$TARROOT/zdm-linux/zdm-native-host"
cat >"$TARROOT/zdm-linux/install.sh" <<'USERINSTALL'
#!/bin/sh
# User-level install: no root, no dependencies. Copies the bundle to
# ~/.local/share/zdm and adds launchers to ~/.local/bin and the app menu.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="${XDG_DATA_HOME:-$HOME/.local/share}/zdm"
BIN="$HOME/.local/bin"
CFG="${XDG_CONFIG_HOME:-$HOME/.config}"
mkdir -p "$DEST" "$BIN" "${XDG_DATA_HOME:-$HOME/.local/share}/applications"
rm -rf "$DEST.new" "$DEST.old"
cp -a "$HERE/." "$DEST.new"
mv -f "$DEST" "$DEST.old" 2>/dev/null || true
mv "$DEST.new" "$DEST"; rm -rf "$DEST.old"
ln -sfn "$DEST/ZDM" "$BIN/zdm"
ln -sfn "$DEST/ZDM" "$BIN/zdm-desktop"
cat >"${XDG_DATA_HOME:-$HOME/.local/share}/applications/zdm.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=ZDM 下载管理器
Comment=多线程下载管理器（浏览器扩展可接管下载）
Exec=$BIN/zdm-desktop %u
Icon=$DEST/_internal/extension/icons/icon256.png
Terminal=false
Categories=Network;FileTransfer;
MimeType=x-scheme-handler/zdm;
StartupNotify=true
StartupWMClass=ZDM
EOF
# Register the native-messaging host (per user) so the browser extension can
# wake ZDM with connectNative(), and claim the zdm:// scheme.
for hostroot in "$CFG/google-chrome/NativeMessagingHosts" "$CFG/chromium/NativeMessagingHosts"; do
    mkdir -p "$hostroot"
    cat >"$hostroot/com.zdm.launcher.json" <<MANIFEST
{
  "name": "com.zdm.launcher",
  "description": "ZDM 下载管理器启动器",
  "path": "$DEST/zdm-native-host",
  "type": "stdio",
  "allowed_origins": ["chrome-extension://mpodbnemeeghjpllgphpemhoogddfhjg/"]
}
MANIFEST
done
mkdir -p "$CFG"
if ! grep -q 'x-scheme-handler/zdm' "$CFG/mimeapps.list" 2>/dev/null; then
    printf '[Default Applications]\nx-scheme-handler/zdm=zdm.desktop\n' >>"$CFG/mimeapps.list"
fi
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q "${XDG_DATA_HOME:-$HOME/.local/share}/applications" >/dev/null 2>&1 || true
fi
echo "installed to $DEST"
echo "run: $BIN/zdm-desktop   (or 'zdm gui' for the CLI)"
case ":$PATH:" in *":$BIN:"*) ;; *) echo "note: add $BIN to PATH to use the 'zdm' command";; esac
USERINSTALL
chmod +x "$TARROOT/zdm-linux/install.sh"
TARBALL="$BUILD_DIR/zdm-linux-${ARCH}.tar.gz"
tar -czf "$TARBALL" -C "$TARROOT" zdm-linux
echo "    $(du -h "$TARBALL" | cut -f1)  $TARBALL"

deactivate 2>/dev/null || true

echo
echo "==> done"
ls -lah "$BUILD_DIR" | grep -E "\.deb|\.tar\.gz" || true
