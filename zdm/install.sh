#!/usr/bin/env bash
# ZDM installer for Linux and macOS - user level, no root required.
# Windows users: use install.ps1 instead.
#
#   ./install.sh                 # install, autostart, start service
#   ./install.sh --systemd       # Linux: use a systemd user service
#   ./install.sh --no-autostart  # install without autostart
#   ./install.sh --uninstall     # remove
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 版本号只有一个来源：zdm/__init__.py 的 __version__（/api 报的也是它）。
# 以前这里写死 1.13.0，发版后就出现「Finder 里显示 1.13.0、程序里 1.14.x」的矛盾。
VERSION="$(sed -n 's/^__version__[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$ROOT/zdm/__init__.py" 2>/dev/null | head -1)"
VERSION="${VERSION:-0.0.0}"
PY="${PYTHON:-}"
OS="$(uname -s)"
MODE="autostart"

for arg in "$@"; do
  case "$arg" in
    --systemd) MODE="systemd" ;;
    --no-autostart) MODE="none" ;;
    --uninstall) MODE="uninstall" ;;
    -h|--help)
      echo "用法: ./install.sh [--systemd|--no-autostart|--uninstall]"; exit 0 ;;
  esac
done

# ---------------------------------------------------------------- python
pick_python() {
  if [ -n "$PY" ] && command -v "$PY" >/dev/null; then echo "$PY"; return; fi
  local c ok first=""
  # system interpreters first, and prefer one that already has PyQt5
  for c in /usr/bin/python3 /usr/local/bin/python3 /opt/homebrew/bin/python3 python3 python; do
    command -v "$c" >/dev/null 2>&1 || [ -x "$c" ] || continue
    ok=$("$c" -c 'import sys; print(1 if sys.version_info >= (3,9) else 0)' 2>/dev/null || echo 0)
    [ "$ok" = "1" ] || continue
    if "$c" -c "import PyQt5.QtWidgets" >/dev/null 2>&1; then echo "$c"; return; fi
    [ -z "$first" ] && first="$c"
  done
  echo "$first"
}

install_pyqt() {
  local py="$1"
  if "$py" -c "import PyQt5.QtWidgets" >/dev/null 2>&1; then
    echo "==> PyQt5 已就绪"
  else
    echo "==> 安装 PyQt5（桌面端需要）"
    "$py" -m pip install --user PyQt5 >/dev/null 2>&1 \
      || "$py" -m pip install --user --break-system-packages PyQt5 >/dev/null 2>&1 \
      || echo "==> PyQt5 安装失败，桌面端将自动回退到网页界面"
  fi
  # macOS: PyObjC lets the desktop client hook Cocoa's
  # applicationShouldHandleReopen: so clicking the Dock icon restores a window
  # that was closed to the tray. Without it we fall back to a state poller.
  if [ "$(uname -s)" = "Darwin" ]; then
    if "$py" -c "import objc, AppKit" >/dev/null 2>&1; then
      echo "==> PyObjC 已就绪（Dock 图标唤醒窗口）"
    else
      "$py" -m pip install --user pyobjc-framework-Cocoa >/dev/null 2>&1 \
        || "$py" -m pip install --user --break-system-packages pyobjc-framework-Cocoa >/dev/null 2>&1 \
        || echo "==> PyObjC 安装失败，Dock 唤醒将退化为轮询方式（仍可用）"
    fi
  fi
}

# ---------------------------------------------------------------- Linux
linux_uninstall() {
  "$HOME/.local/bin/zdm" stop 2>/dev/null || true
  rm -rf "$HOME/.local/share/zdm" "$HOME/.local/bin/zdm" \
         "$HOME/.local/share/applications/zdm.desktop" \
         "$HOME/.local/share/icons/hicolor/128x128/apps/zdm.png" \
         "$HOME/.config/autostart/zdm.desktop" \
         "$HOME/.config/systemd/user/zdm.service"
  systemctl --user daemon-reload 2>/dev/null || true
  echo "已卸载（配置在 ~/.config/zdm，已下载文件保留）"
}

install_native_host() {
  # Chrome/Chromium native messaging lets the extension start ZDM without
  # showing an "open zdm://" dialog. The host name and the extension id must
  # match extension/background.js + the manifest "key".
  local APP_DIR="$1" EXT_ID="$2"
  local HOST="$APP_DIR/zdm_native_host.py"
  chmod +x "$HOST" 2>/dev/null || true
  local cfg
  for cfg in \
    "$HOME/.config/google-chrome" \
    "$HOME/.config/chromium" \
    "$HOME/.config/microsoft-edge" \
    "$HOME/.config/BraveSoftware/Brave-Browser" \
    "$HOME/.config/vivaldi"; do
    [ -d "$cfg" ] || continue
    mkdir -p "$cfg/NativeMessagingHosts"
    cat > "$cfg/NativeMessagingHosts/com.zdm.launcher.json" <<EOF
{
  "name": "com.zdm.launcher",
  "description": "ZDM download manager launcher",
  "path": "$HOST",
  "type": "stdio",
  "allowed_origins": ["chrome-extension://$EXT_ID/"]
}
EOF
  done
  echo "==> 已注册浏览器原生消息主机 com.zdm.launcher（扩展 ID $EXT_ID）"
}

install_linux() {
  local PREFIX="${PREFIX:-$HOME/.local}"
  local APP_DIR="$PREFIX/share/zdm"
  local BIN_DIR="$PREFIX/bin"

  echo "==> 安装到 $APP_DIR"
  # 升级时旧进程很可能还在跑：桌面端是单实例、服务还占着 9000 端口，
  # 文件换掉后它们继续执行**旧代码**，新装的版本永远用不上 —— 这就是
  # 「更新了还是老毛病 / 下载还是失败」的典型原因。先停掉再覆盖。
  "$BIN_DIR/zdm" stop >/dev/null 2>&1 || true
  pkill -f "$APP_DIR/zdm.py" >/dev/null 2>&1 || true
  pkill -f "$APP_DIR/gui.py" >/dev/null 2>&1 || true
  sleep 0.5
  mkdir -p "$APP_DIR" "$BIN_DIR" "$PREFIX/share/applications" \
           "$PREFIX/share/icons/hicolor/128x128/apps"
  rm -rf "$APP_DIR/zdm" "$APP_DIR/web" "$APP_DIR/extension"
  cp -r "$ROOT/zdm" "$ROOT/web" "$ROOT/extension" "$ROOT/zdm.py" "$ROOT/gui.py" \
        "$ROOT/theme.py" "$ROOT/zdm_native_host.py" "$APP_DIR/"
  cp "$ROOT/README.md" "$APP_DIR/" 2>/dev/null || true
  find "$APP_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
  # gui.py imports theme.py from the app root (it is NOT inside the zdm package),
  # so a missing copy means the desktop client cannot start at all.
  [ -f "$APP_DIR/theme.py" ] || { echo "!! 安装不完整：theme.py 缺失" >&2; exit 1; }

  cat > "$BIN_DIR/zdm" <<EOF
#!/bin/sh
exec "$PY" "$APP_DIR/zdm.py" "\$@"
EOF
  chmod +x "$BIN_DIR/zdm" "$APP_DIR/zdm.py"

  # Direct desktop-client launcher. ``zdm gui`` asks the same module for a
  # launcher, so this wrapper must run gui.py itself (no recursion).
  cat > "$BIN_DIR/zdm-gui" <<EOF
#!/bin/sh
exec "$PY" "$APP_DIR/gui.py" "\$@"
EOF
  chmod +x "$BIN_DIR/zdm-gui"
  cp "$ROOT/extension/icons/icon128.png" "$PREFIX/share/icons/hicolor/128x128/apps/zdm.png"

  cat > "$PREFIX/share/applications/zdm.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=ZDM 下载管理器
Comment=多线程下载管理器（浏览器扩展可接管下载）
Exec=$BIN_DIR/zdm-gui
Icon=zdm
Terminal=false
Categories=Network;FileTransfer;
Keywords=download;zdm;下载;
StartupNotify=true
EOF

  case "$MODE" in
    autostart)
      mkdir -p "$HOME/.config/autostart"
      cat > "$HOME/.config/autostart/zdm.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=ZDM Service
Comment=ZDM 下载服务后台常驻
Exec=$BIN_DIR/zdm start --daemon --no-open
Icon=zdm
Terminal=false
X-GNOME-Autostart-enabled=true
NoDisplay=true
EOF
      echo "==> 已设置开机自启（autostart）"
      ;;
    systemd)
      mkdir -p "$HOME/.config/systemd/user"
      cat > "$HOME/.config/systemd/user/zdm.service" <<EOF
[Unit]
Description=ZDM download manager service
After=network-online.target

[Service]
Type=simple
ExecStart=$BIN_DIR/zdm start --no-open
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF
      systemctl --user daemon-reload || true
      systemctl --user enable --now zdm.service || true
      echo "==> 已注册 systemd user service"
      ;;
  esac
  update-desktop-database "$PREFIX/share/applications" 2>/dev/null || true
  gtk-update-icon-cache -f -t "$PREFIX/share/icons/hicolor" 2>/dev/null || true
  install_pyqt "$PY"
  local EXT_ID
  EXT_ID="$("$PY" - "$ROOT/extension/manifest.json" <<'PYEOF'
import base64, hashlib, json, sys
m = json.load(open(sys.argv[1], encoding="utf-8"))
h = hashlib.sha256(base64.b64decode(m["key"])).hexdigest()[:32]
print("".join(chr(ord("a") + int(c, 16)) for c in h))
PYEOF
)"
  install_native_host "$APP_DIR" "$EXT_ID"
  "$BIN_DIR/zdm" start --daemon >/dev/null 2>&1 || true
  EXT_DIR="$APP_DIR/extension"
}

# ---------------------------------------------------------------- macOS
mac_uninstall() {
  launchctl bootout "gui/$(id -u)/com.zdm.service" 2>/dev/null || launchctl unload -w "$HOME/Library/LaunchAgents/com.zdm.service.plist" 2>/dev/null || true
  rm -rf "$HOME/Library/Application Support/ZDM" \
         "$HOME/Library/LaunchAgents/com.zdm.service.plist" \
         "$HOME/Applications/ZDM.app" "$HOME/.local/bin/zdm"
  echo "已卸载（配置在 ~/Library/Application Support/ZDM，已下载文件保留）"
}

install_mac() {
  local APP_DIR="$HOME/Library/Application Support/ZDM"
  local LA="$HOME/Library/LaunchAgents"
  local APPS="$HOME/Applications"
  local APP="$APPS/ZDM.app"

  echo "==> 安装到 $APP_DIR"
  mkdir -p "$APP_DIR" "$LA" "$APPS" "$HOME/.local/bin"
  rm -rf "$APP_DIR/zdm" "$APP_DIR/web" "$APP_DIR/extension"
  cp -r "$ROOT/zdm" "$ROOT/web" "$ROOT/extension" "$ROOT/zdm.py" "$ROOT/gui.py" \
        "$ROOT/theme.py" "$ROOT/zdm_native_host.py" "$APP_DIR/"
  cp "$ROOT/README.md" "$APP_DIR/" 2>/dev/null || true
  find "$APP_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
  # gui.py imports theme.py from the app root (it is NOT inside the zdm package),
  # so a missing copy means the desktop client cannot start at all.
  [ -f "$APP_DIR/theme.py" ] || { echo "!! 安装不完整：theme.py 缺失" >&2; exit 1; }

  mkdir -p "$HOME/.local/bin"
  cat > "$HOME/.local/bin/zdm" <<EOF
#!/bin/sh
exec "$PY" "$APP_DIR/zdm.py" "\$@"
EOF
  chmod +x "$HOME/.local/bin/zdm" "$APP_DIR/zdm.py"

  # Same direct launcher as on Linux (used when ZDM.app is missing/moved).
  cat > "$HOME/.local/bin/zdm-gui" <<EOF
#!/bin/sh
exec "$PY" "$APP_DIR/gui.py" "\$@"
EOF
  chmod +x "$HOME/.local/bin/zdm-gui"

  # launchd agent: starts on login and restarts on crash
  cat > "$LA/com.zdm.service.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.zdm.service</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$APP_DIR/zdm.py</string>
    <string>serve</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$APP_DIR/zdm.log</string>
  <key>StandardErrorPath</key><string>$APP_DIR/zdm.log</string>
</dict>
</plist>
EOF
  # macOS 13+ removed `launchctl load`; use bootstrap + kickstart (fallback to load on older macOS)
  launchctl bootout "gui/$(id -u)/com.zdm.service" 2>/dev/null || true
  if launchctl bootstrap "gui/$(id -u)" "$LA/com.zdm.service.plist" 2>/dev/null; then
    launchctl kickstart -k "gui/$(id -u)/com.zdm.service" 2>/dev/null || true
  else
    launchctl unload -w "$LA/com.zdm.service.plist" 2>/dev/null || true
    launchctl load -w "$LA/com.zdm.service.plist" 2>/dev/null || true
  fi
  echo "==> 已注册 LaunchAgent（登录自启 + 崩溃重启）"

  # icon
  local ICNS="$APP_DIR/zdm.icns"
  if command -v iconutil >/dev/null && command -v sips >/dev/null; then
    local ICONSET
    ICONSET="$(mktemp -d)/zdm.iconset"
    mkdir -p "$ICONSET"
    # A valid .iconset needs the standard names AND every @2x (retina) variant.
    # Missing @2x files produce a malformed .icns and macOS falls back to the
    # generic placeholder icon in the Dock / Launchpad.
    local SRC="$APP_DIR/extension/icons/icon512.png"
    local logical icon_name
    for logical in 16 32 128 256 512; do
      icon_name="icon_${logical}x${logical}.png"
      sips -z "$logical" "$logical" "$SRC" \
        --out "$ICONSET/$icon_name" >/dev/null 2>&1 || true
      sips -z "$((logical * 2))" "$((logical * 2))" "$SRC" \
        --out "$ICONSET/icon_${logical}x${logical}@2x.png" >/dev/null 2>&1 || true
    done
    iconutil -c icns "$ICONSET" -o "$ICNS" >/dev/null 2>&1 || true
    rm -rf "$(dirname "$ICONSET")"
  fi

  # Build a REAL native .app bundle.
  #
  # A "script based" bundle (Contents/MacOS/<exe> being a shell script that starts
  # python) does NOT keep a stable macOS app identity: the Dock / LaunchServices
  # key off the *running process image*, which ends up being Python.app, not
  # ZDM.app. That causes a generic Dock icon and a Dock click that never reaches
  # the Qt window. To fix this we build ZDM.app/Contents/MacOS/ZDM as a genuine
  # Mach-O launcher that embeds the CPython interpreter *in the same process* (via
  # the Python C API) and runs the Qt client from there. The process is then
  # ZDM.app, so the Dock icon, the reopen callback, etc. all work natively.
  #
  # A DMG install already put a complete bundle in /Applications. Building a
  # second one in ~/Applications would show up as a duplicate icon in the
  # Launchpad and confuse the user about which one is the real app, so skip it
  # and keep using the installed bundle.
  if [ -x "/Applications/ZDM.app/Contents/MacOS/ZDM" ]; then
    echo "==> 检测到 /Applications/ZDM.app 已安装，保留它（不再重复创建 ~/Applications/ZDM.app）"
    rm -rf "$APP"
    APP="/Applications/ZDM.app"
  fi
  mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources" "$APP/Contents/Frameworks"
  [ -f "$ICNS" ] && cp "$ICNS" "$APP/Contents/Resources/zdm.icns"

  # Locate the Python framework that backs the target interpreter so we can embed
  # it and link the launcher against it.
  local PYFRAMEWORK PYFRAMEWORK_VER
  PYFRAMEWORK="$(python3 -c 'import sys; print(getattr(sys,"base_prefix",sys.prefix))' 2>/dev/null)"
  PYFRAMEWORK="$("$PY" -c 'import sys; print(getattr(sys,"base_prefix",sys.prefix))' 2>/dev/null)"
  PYFRAMEWORK_VER="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)"

  if command -v clang >/dev/null && [ -n "$PYFRAMEWORK" ] && [ -d "$PYFRAMEWORK/Frameworks/Python$PYFRAMEWORK_VER.framework" ] || [ -d "$PYFRAMEWORK/../Library/Frameworks/Python$PYFRAMEWORK_VER.framework" ]; then
    # Embed the Python framework into the bundle.
    local FWSRC
    FWSRC="$PYFRAMEWORK/Frameworks/Python$PYFRAMEWORK_VER.framework"
    [ -d "$FWSRC" ] || FWSRC="$PYFRAMEWORK/../Library/Frameworks/Python$PYFRAMEWORK_VER.framework"
    rm -rf "$APP/Contents/Frameworks/Python$PYFRAMEWORK_VER.framework"
    cp -R "$FWSRC" "$APP/Contents/Frameworks/Python$PYFRAMEWORK_VER.framework" 2>/dev/null || true

    # Compile the native launcher against the embedded framework.
    local FW="$APP/Contents/Frameworks/Python$PYFRAMEWORK_VER.framework/Versions/$PYFRAMEWORK_VER"
    if [ -f "$FW/lib/libpython$PYFRAMEWORK_VER.dylib" ] && [ -f "$FW/Headers/Python.h" ]; then
      clang -o "$APP/Contents/MacOS/ZDM" "$ROOT/zdm_launcher.m" \
        -I"$FW/Headers" "$FW/lib/libpython$PYFRAMEWORK_VER.dylib" \
        -DZDM_APP_DIR="\"$APP_DIR\"" \
        -Wl,-rpath,"@executable_path/../Frameworks/Python$PYFRAMEWORK_VER.framework/Versions/$PYFRAMEWORK_VER/lib" \
        -Wl,-rpath,"@executable_path/../Frameworks" >/dev/null 2>&1 \
        && echo "==> 已生成原生 App（内嵌 Python 运行时）" \
        || echo "==> 原生 launcher 编译失败，回退到脚本启动器"
    fi
  fi

  # Fallback: script launcher (still functional; Dock identity may be limited).
  if [ ! -f "$APP/Contents/MacOS/ZDM" ] || ! file "$APP/Contents/MacOS/ZDM" 2>/dev/null | grep -q "Mach-O"; then
    cat > "$APP/Contents/MacOS/ZDM" <<EOF
#!/bin/sh
PY="$PY"
APP_DIR="$APP_DIR"
if ! curl -s -m 2 http://127.0.0.1:9000/api/state >/dev/null 2>&1; then
  launchctl kickstart -k gui/\$(id -u)/com.zdm.service 2>/dev/null || true
  nohup "\$PY" "\$APP_DIR/zdm.py" serve >/dev/null 2>&1 &
  i=0
  while [ \$i -lt 30 ]; do
    curl -s -m 1 http://127.0.0.1:9000/api/state >/dev/null 2>&1 && break
    i=\$((i+1)); sleep 0.3
  done
fi
"\$PY" "\$APP_DIR/gui.py" 9000
EOF
    chmod +x "$APP/Contents/MacOS/ZDM"
  fi
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
  <key>CFBundleVersion</key><string>${VERSION}</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
EOF
  # clear the quarantine flag so a double click opens it right away
  xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true
  echo "==> 已生成 $APP"
  install_pyqt "$PY"
  EXT_DIR="$APP_DIR/extension"
}

# ---------------------------------------------------------------- main
if [ "$MODE" = "uninstall" ]; then
  if [ "$OS" = "Darwin" ]; then mac_uninstall; else linux_uninstall; fi
  exit 0
fi

PY="$(pick_python)"
if [ -z "$PY" ]; then
  echo "未找到 Python 3.9+"
  [ "$OS" = "Darwin" ] && echo "  brew install python@3.12  或  https://www.python.org/downloads/macos/"
  echo "  装好后重新运行本脚本"
  exit 1
fi
echo "==> python: $PY ($("$PY" -V 2>&1))"

if [ "$OS" = "Darwin" ]; then install_mac; else install_linux; fi

DESKTOP_HINT="应用菜单里的「ZDM 下载管理器」或 zdm gui"
[ "$OS" = "Darwin" ] && DESKTOP_HINT="双击 ~/Applications/ZDM.app 或 zdm gui"

cat <<EOF

安装完成 ✅

  桌面端     : $DESKTOP_HINT
  网页界面   : http://127.0.0.1:9000/
  命令行     : zdm add "https://example.com/file.iso" -c 16
               zdm list / zdm pause <id> / zdm resume <id> / zdm stop

浏览器扩展（Chrome / Edge，三个平台步骤相同）：
  1) chrome://extensions  → 打开右上角「开发者模式」
  2) 「加载已解压的扩展程序」→ 选择 $EXT_DIR
  3) 点扩展图标 → ⚙ → 测试连接 → 勾选「自动接管浏览器下载」

重装系统后：重新运行本脚本，再重新加载一次扩展即可；下载目录和设置会自动重建。
EOF
