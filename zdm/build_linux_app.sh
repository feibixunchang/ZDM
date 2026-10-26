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
# The script cd's into $SRC before invoking PyInstaller, so a *relative* build
# dir (e.g. ../build-1.15.17) used to make "$BUILD_DIR/pyinstaller.log"
# disappear after the cd and abort the build. Canonicalize it once, here.
mkdir -p "$BUILD_DIR"
BUILD_DIR="$(cd "$BUILD_DIR" && pwd)"
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
# paramiko 是 sftp:// 通道的唯一外部依赖（FTP/FTPS 用标准库）。发布包必须带：
# 装好的 .deb 里没 pip，用户在下载页补不了 —— --selfcheck 会把这件事当硬条件。
python -m pip install --quiet "PyQt5>=5.15,<5.16" "pyinstaller>=6.3" "paramiko>=3.4"
python - <<'PYEOF'
import PyInstaller
from PyQt5.QtCore import PYQT_VERSION_STR, QT_VERSION_STR
print(f"    PyQt5 {PYQT_VERSION_STR} | Qt {QT_VERSION_STR} | PyInstaller {PyInstaller.__version__}")
PYEOF

# ---------------------------------------------------------------- freeze
# YouTube 的签名/n 参数需要 JS 运行时（deno）。随包带上，用户装完即可用；
# 版本固定在 fetch_bin.py 里，避免上游更新悄悄改变发布内容。
if [ ! -x "$SRC/bin/deno" ]; then
    python3 "$SRC/fetch_bin.py" --dest "$SRC/bin" --os linux
fi
# 流媒体引擎（N_m3u8DL-RE）同样随包：spec 从 bin/ 取，构建期没备货就打出一个
# 只能靠 ffmpeg 下 m3u8 的包。版本号、URL、sha256 都钉在 fetch_bin.py 里。
if [ ! -x "$SRC/bin/N_m3u8DL-RE" ]; then
    python3 "$SRC/fetch_bin.py" --dest "$SRC/bin" --os linux --tool re
fi
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
if not r.get("extension_ui18n"): bad.append("extension/ui18n.js missing (英文界面会整块失效)")
if not r.get("theme_module"): bad.append("theme.py not importable")
if not r.get("pyqt5"): bad.append("PyQt5 not importable")
# 这两个是「装完在用户机器上才发现」的典型：YouTube 全靠随包 yt-dlp，
# B站 DASH 靠随包 ffmpeg 合音轨。构建期少一个，用户那边就是
# 「YouTube 不能下」或「下载成功但没声音」。
if not r.get("ytdlp"): bad.append("bundled yt-dlp binary missing")
if not r.get("ffmpeg"): bad.append("bundled ffmpeg missing (DASH 音轨无法合流)")
# deno 是 YouTube 的 JS 运行时；没有它 YouTube 只回故事板（"page needs to be reloaded"）。
if not r.get("deno"): bad.append("bundled deno/JS runtime missing (YouTube 解析会失败)")
# 流媒体引擎没有也一样：包能启动，但 m3u8 全退化成 ffmpeg（无分片续传）。
if not r.get("m3u8dl_re"): bad.append("bundled N_m3u8DL-RE missing (%s)" % r.get("m3u8dl_re_error"))
# ftp:// 只用标准库，少了说明冻结时把 zdm/ftp.py 丢了；sftp:// 少 paramiko 的话，
# 用户要到粘链接那一刻才知道这包不支持。
if not r.get("ftp_channel"): bad.append("ftp channel not importable (%s)" % r.get("ftp_error"))
if not r.get("sftp"): bad.append("paramiko not bundled (sftp:// unavailable)")
if not r.get("firefox_ext"): bad.append("Firefox .xpi packaging broken (%s)" % r.get("firefox_ext_error"))
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
# Firefox 的扩展 ID 是清单里的字符串（browser_specific_settings.gecko.id），
# 唯一真相在 zdm/zdm/firefox_ext.py 的 GECKO_ID。deb 里的清单是构建期写死的，
# 所以这里当场比对一次：抄错了不会有任何报错，只会变成「Firefox 点下载没反应、
# Chrome 一切正常」，那种 bug 只能等用户报障才发现。
GECKO_ID="zdm-connector@feibixunchang.github.io"
if [ -f "$SRC/zdm/firefox_ext.py" ] &&
   ! grep -q "^GECKO_ID = \"$GECKO_ID\"" "$SRC/zdm/firefox_ext.py"; then
    echo "ERROR: 本脚本里的 Firefox 扩展 ID 与 zdm/firefox_ext.py 的 GECKO_ID 不一致" >&2
    exit 1
fi
# 本文件里写出的每一个 Firefox 扩展 ID 都要是上面这一个（含下面嵌进 portable
# 包里的那份 install.sh —— 那段是引号 heredoc，构建期不展开，只能靠这里兜住）。
# 匹配串由 GECKO_ID 拼出来，避免这条断言把自己也扫进去。
# `|| true` 是必需的，不是装饰：脚本开头是 set -euo pipefail，而「一处都没抄错」时
# grep -vx 没有任何输出、返回 1，pipefail 把这个非零状态带出命令替换，赋值语句本身
# 就成了失败命令 —— 于是断言在一切正常的那条路上把整场构建静默杀掉（CI 上就是
# 「==> staging the .deb」之后什么也没有、exit 1）。
BAD_GECKO="$(grep -o "${GECKO_ID%%@*}@[A-Za-z0-9._-]*" "$SRC/build_linux_app.sh" 2>/dev/null | grep -vx "$GECKO_ID" | sort -u || true)"
if [ -n "$BAD_GECKO" ]; then
    echo "ERROR: build_linux_app.sh 里有与 GECKO_ID 不一致的 Firefox 扩展 ID：$BAD_GECKO" >&2
    exit 1
fi
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
  "allowed_origins": ["chrome-extension://$EXT_ID/"],
  "allowed_extensions": ["$GECKO_ID"]
}
MANIFEST
done
# Firefox 读的是自己的目录（system-wide 那个），清单同一份：Chrome 会忽略
# allowed_extensions，Firefox 忽略 allowed_origins，两边各取所需。
install -Dm644 /dev/null "$STAGE/usr/lib/mozilla/native-messaging-hosts/com.zdm.launcher.json"
cat >"$STAGE/usr/lib/mozilla/native-messaging-hosts/com.zdm.launcher.json" <<MANIFEST
{
  "name": "com.zdm.launcher",
  "description": "ZDM 下载管理器启动器",
  "path": "/opt/zdm/zdm-native-host",
  "type": "stdio",
  "allowed_extensions": ["$GECKO_ID"]
}
MANIFEST

# System-wide default handler for the zdm:// scheme (the extension's fallback
# wake-up path) is registered by postinst *without* shipping
# /etc/xdg/mimeapps.list. Shipping that file made dpkg replace whatever the
# admin/other packages had put there, silently wiping every other scheme's
# default (e.g. mailto:, x-scheme-handler/http). postinst merges our single
# line into the existing file instead.

install -m644 "$REPO/LICENSE.txt" "$STAGE/usr/share/doc/zdm/copyright"
install -m644 "$SRC/README.md" "$STAGE/usr/share/doc/zdm/README.md"
[ -f "$SRC/bin/DENO-LICENSE.md" ] && install -m644 "$SRC/bin/DENO-LICENSE.md" "$STAGE/usr/share/doc/zdm/DENO-LICENSE.md" || true
[ -f "$SRC/bin/RE-LICENSE.txt" ] && install -m644 "$SRC/bin/RE-LICENSE.txt" "$STAGE/usr/share/doc/zdm/RE-LICENSE.txt" || true

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

# Claim zdm:// for the extension's fallback wake-up path, merging into the
# existing system mimeapps.list instead of replacing it (see the staging
# comment above). Only our single key is touched; every other default and
# section survives.
MIMEAPPS=/etc/xdg/mimeapps.list
if [ -f "$MIMEAPPS" ] && ! grep -q '^x-scheme-handler/zdm=' "$MIMEAPPS"; then
    if grep -q '^\[Default Applications\]' "$MIMEAPPS"; then
        TMP="$MIMEAPPS.zdm.tmp"
        if awk '
            /^\[Default Applications\]/ {
                print
                if (!done) { print "x-scheme-handler/zdm=zdm.desktop"; done = 1 }
                next
            }
            { print }
            END {
                if (!done) {
                    print ""
                    print "[Default Applications]"
                    print "x-scheme-handler/zdm=zdm.desktop"
                }
            }
        ' "$MIMEAPPS" >"$TMP" 2>/dev/null; then
            cat "$TMP" >"$MIMEAPPS"
        fi
        rm -f "$TMP"
    else
        printf '\n[Default Applications]\nx-scheme-handler/zdm=zdm.desktop\n' >>"$MIMEAPPS"
    fi
elif [ ! -f "$MIMEAPPS" ]; then
    mkdir -p /etc/xdg
    printf '[Default Applications]\nx-scheme-handler/zdm=zdm.desktop\n' >"$MIMEAPPS"
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
if [ -n "$TU" ] && command -v xdg-mime >/dev/null 2>&1; then
    su -s /bin/sh -c "xdg-mime default zdm.desktop x-scheme-handler/zdm" "$TU" \
        >/dev/null 2>&1 || true
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
# The desktop entry launches the /usr/bin symlink, so its argv[0] is
# "/usr/bin/zdm-desktop", not "/opt/zdm/ZDM" — a pattern that only matches the
# real path leaves the old GUI running after an upgrade (it then talks to the
# new service with old code). Kill both spellings.
pkill -f '^/usr/bin/zdm-desktop( |$)' >/dev/null 2>&1 || true
pkill -f '^/usr/bin/zdm( |$)' >/dev/null 2>&1 || true
# Drop the browser integration we registered in postinst: a stale host
# manifest makes Chrome launch a binary that no longer exists.
rm -f /etc/opt/chrome/native-messaging-hosts/com.zdm.launcher.json \
      /etc/chromium/native-messaging-hosts/com.zdm.launcher.json \
      /usr/lib/mozilla/native-messaging-hosts/com.zdm.launcher.json
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
# 升级时旧版本很可能还在跑：桌面端是单实例，服务还占着 9000 端口。文件
# 换掉后旧进程继续执行**旧代码**，新装的版本永远用不上 —— 免 root 安装
# 最典型的「更新了下载还是失败」就是这么来的。先停掉再覆盖。
if [ -x "$DEST/ZDM" ]; then
    pkill -f "$DEST/ZDM" >/dev/null 2>&1 || true
    sleep 0.5
fi
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
  "allowed_origins": ["chrome-extension://mpodbnemeeghjpllgphpemhoogddfhjg/"],
  "allowed_extensions": ["zdm-connector@feibixunchang.github.io"]
}
MANIFEST
done
# Firefox：同一份清单，只是认 allowed_extensions、读自己的目录。父目录不存在
# 就当没装 Firefox，不给干净机器造 ~/.mozilla。
if [ -d "$HOME/.mozilla" ]; then
    mkdir -p "$HOME/.mozilla/native-messaging-hosts"
    cat >"$HOME/.mozilla/native-messaging-hosts/com.zdm.launcher.json" <<MANIFEST
{
  "name": "com.zdm.launcher",
  "description": "ZDM 下载管理器启动器",
  "path": "$DEST/zdm-native-host",
  "type": "stdio",
  "allowed_extensions": ["zdm-connector@feibixunchang.github.io"]
}
MANIFEST
fi
mkdir -p "$CFG"
# Prefer xdg-mime (it writes a proper default); the manual merge below is the
# fallback for systems without xdg-utils. Never append a second
# [Default Applications] header - duplicate sections are invalid and some DEs
# drop the whole file.
if command -v xdg-mime >/dev/null 2>&1; then
    xdg-mime default zdm.desktop x-scheme-handler/zdm >/dev/null 2>&1 || true
fi
MIMEAPPS="$CFG/mimeapps.list"
if [ -f "$MIMEAPPS" ] && ! grep -q '^x-scheme-handler/zdm=' "$MIMEAPPS"; then
    if grep -q '^\[Default Applications\]' "$MIMEAPPS"; then
        TMP="$MIMEAPPS.zdm.tmp"
        if awk '
            /^\[Default Applications\]/ {
                print
                if (!done) { print "x-scheme-handler/zdm=zdm.desktop"; done = 1 }
                next
            }
            { print }
            END {
                if (!done) {
                    print ""
                    print "[Default Applications]"
                    print "x-scheme-handler/zdm=zdm.desktop"
                }
            }
        ' "$MIMEAPPS" >"$TMP" 2>/dev/null; then
            cat "$TMP" >"$MIMEAPPS"
        fi
        rm -f "$TMP"
    else
        printf '\n[Default Applications]\nx-scheme-handler/zdm=zdm.desktop\n' >>"$MIMEAPPS"
    fi
elif [ ! -f "$MIMEAPPS" ]; then
    printf '[Default Applications]\nx-scheme-handler/zdm=zdm.desktop\n' >"$MIMEAPPS"
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
