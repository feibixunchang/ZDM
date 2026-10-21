#!/usr/bin/env python3
"""Build distributable packages for ZDM.

Outputs (into ./dist):
  - zdm_1.14.7_amd64.deb       Linux  (self-contained: bundled CPython + PyQt5)
  - zdm-linux-amd64.tar.gz     Linux  (same bundle, user-level install, no root)
  - zdm-macos.zip              macOS  (ZDM.app + extension + build_macos_dmg.sh)

Windows 由 zdm/build_windows_app.ps1 构建（同一份 PyQt5 界面 + 同一个 PyInstaller
spec，自带 CPython 运行时），产出 ZDM-Setup-*.exe。

2026-10 起 Windows 也是 PyQt5 那一套了。之前它是独立的 C#/WPF 客户端，自带下载
引擎和 HTTP API，导致「同一功能两份实现」：mac 上修好的 YouTube 解析、探大小、
DASH 音频合流，Windows 一行都没生效。现在三端一份代码，修一次三端都生效。
windows-native/ 仅作参考与回退保留，不再参与构建。

The macOS .dmg itself must be produced on a Mac (hdiutil is macOS-only);
build_macos_dmg.sh does that in one command.
"""
import os
import shutil
import subprocess
import sys

ROOT = os.path.abspath(os.path.dirname(__file__))
ZDM = os.path.join(ROOT, "zdm")
DIST = os.path.join(ROOT, "dist")


def _read_version() -> str:
    """版本号只认 zdm/zdm/__init__.py 一个真源。

    这里以前写死过 1.13.0，而后端 /api 报的是 1.14.0 —— 于是打出过「标题栏
    与 GitHub 最新版对不上」的包，CI 的版本自检也是因为这个挂的。
    """
    p = os.path.join(ZDM, "zdm", "__init__.py")
    try:
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("__version__"):
                    return line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return "0.0.0"


VERSION = _read_version()


def which(cmd):
    from shutil import which as w
    return w(cmd)


def run(*args, cwd=None):
    print("+", " ".join(args))
    subprocess.run(args, cwd=cwd, check=True)


def clean_pycache():
    for dirpath, dirs, _ in os.walk(ZDM):
        for d in list(dirs):
            if d == "__pycache__":
                shutil.rmtree(os.path.join(dirpath, d), ignore_errors=True)


def stage_files(dst):
    """Copy the runtime tree (source + installers) into dst."""
    os.makedirs(dst, exist_ok=True)
    for item in ("zdm", "web", "extension"):
        shutil.copytree(os.path.join(ZDM, item), os.path.join(dst, item),
                        dirs_exist_ok=True,
                        # yt_dlp_pkg 是旧的 pip 包目录（25MB，且要求 Python 3.10+），
                        # 现在用 bin/yt-dlp 二进制，别把它再拖进安装包。
                        ignore=shutil.ignore_patterns("__pycache__", "yt_dlp_pkg"))
    # 随包的 yt-dlp 单文件二进制（YouTube 解析/下载靠它，pip 包在 Python 3.9 上
    # 直接抛 "unsupported version of Python"）。它跟 zdm/ 平级，不在包目录里，
    # 上面那个循环带不上来 —— 漏了的话「打包版 YouTube 不能用」而源码跑得好好的。
    bin_src = os.path.join(ZDM, "bin")
    if os.path.isdir(bin_src):
        shutil.copytree(bin_src, os.path.join(dst, "bin"), dirs_exist_ok=True)
    for f in ("zdm.py", "gui.py", "theme.py", "README.md", "install.sh",
              "zdm_native_host.py",
              "install.ps1", "install.bat", "setup.cmd", "build_macos_dmg.sh",
              "zdm_launcher.m", "build_macos_app.sh", "LICENSE.txt"):
        src = os.path.join(ZDM, f) if os.path.exists(os.path.join(ZDM, f)) \
            else os.path.join(ROOT, f)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(dst, f))


# ----------------------------------------------------------------- Linux
def build_linux_native():
    """Build the self-contained Linux app (ZDM onedir + .deb + portable tar.gz).

    The heavy lifting lives in zdm/build_linux_app.sh so that CI, a developer
    machine and this script all use one code path. Linux packaging used to ship
    a ~350 KB source-only .deb whose launcher was a 4-line shell script calling
    the system python3 - it contained no interpreter and no PyQt5, so the window
    never opened on a clean machine. The bundle produced here embeds CPython,
    PyQt5/Qt and the resources, exactly like the macOS .dmg.
    """
    import glob

    if not which("dpkg-deb"):
        # The .deb needs Debian tooling; a macOS/Windows host can still build
        # the other packages instead of aborting the whole run.
        print("==> Linux (native)  [跳过：本机缺少 dpkg-deb，请在 Debian/Ubuntu 上构建]")
        return
    out = os.path.join(DIST, "linux-build")
    run("bash", os.path.join(ZDM, "build_linux_app.sh"), out, VERSION)
    for pat in ("zdm_*_*.deb", "zdm-linux-*.tar.gz"):
        for f in glob.glob(os.path.join(out, pat)):
            shutil.copy2(f, os.path.join(DIST, os.path.basename(f)))
            print(f"    -> dist/{os.path.basename(f)}")


# ----------------------------------------------------------------- Windows
def build_windows_native():
    """Build the Windows client (PyQt5 onedir + Inno installer).

    The heavy lifting lives in zdm/build_windows_app.ps1 so that CI and a
    developer machine run exactly the same steps. Only meaningful on Windows:
    PyInstaller cannot cross-compile, and iscc.exe is Windows-only.
    """
    if sys.platform != "win32":
        print("==> Windows  [跳过：PyInstaller 不能交叉编译，请在 Windows 上构建]")
        return
    script = os.path.join(ZDM, "build_windows_app.ps1")
    run(
        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", script, "-OutDir", DIST, "-Version", VERSION,
    )


# ----------------------------------------------------------------- macOS zip
def build_macos_app(dst_app):
    os.makedirs(os.path.join(dst_app, "Contents", "MacOS"), exist_ok=True)
    os.makedirs(os.path.join(dst_app, "Contents", "Resources"), exist_ok=True)
    launcher = os.path.join(dst_app, "Contents", "MacOS", "ZDM")
    with open(launcher, "w", encoding="utf-8") as fh:
        fh.write(
            "#!/bin/sh\n"
            'PY="$(command -v python3 || command -v python)"\n'
            'SUPPORT="$HOME/Library/Application Support/ZDM"\n'
            'if [ ! -x "$SUPPORT/zdm.py" ]; then\n'
            '  osascript -e \'display dialog "请先运行 zdm/install.sh 完成安装"\' '
            "2>/dev/null\n"
            '  open "$(dirname "$0")/../../.."\n'
            "  exit 1\n"
            "fi\n"
            'if ! curl -s -m 2 http://127.0.0.1:9000/api/state >/dev/null 2>&1; then\n'
            '  launchctl kickstart -k gui/$(id -u)/com.zdm.service 2>/dev/null || \\\n'
            '    nohup "$PY" "$SUPPORT/zdm.py" serve >/dev/null 2>&1 &\n'
            "fi\n"
            'exec "$PY" "$SUPPORT/gui.py" 9000\n'
        )
    os.chmod(launcher, 0o755)
    info = os.path.join(dst_app, "Contents", "Info.plist")
    with open(info, "w", encoding="utf-8") as fh:
        fh.write(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            "<plist version=\"1.0\">\n<dict>\n"
            "  <key>CFBundleName</key><string>ZDM</string>\n"
            "  <key>CFBundleDisplayName</key><string>ZDM 下载管理器</string>\n"
            "  <key>CFBundleIdentifier</key><string>com.zdm.desktop</string>\n"
            "  <key>CFBundleExecutable</key><string>ZDM</string>\n"
            "  <key>CFBundleIconFile</key><string>zdm</string>\n"
            f"  <key>CFBundleVersion</key><string>{VERSION}</string>\n"
            "  <key>CFBundlePackageType</key><string>APPL</string>\n"
            "  <key>NSHighResolutionCapable</key><true/>\n"
            "</dict>\n</plist>\n"
        )


def build_macos_zip():
    d = os.path.join(DIST, "zdm-macos")
    if os.path.exists(d):
        shutil.rmtree(d)
    os.makedirs(d, exist_ok=True)
    build_macos_app(os.path.join(d, "ZDM.app"))
    shutil.copytree(os.path.join(ZDM, "extension"), os.path.join(d, "extension"),
                    dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__"))
    for f in ("install.sh", "zdm_native_host.py", "install.ps1", "install.bat",
              "build_macos_dmg.sh",
              "zdm_launcher.m", "build_macos_app.sh", "README.md", "LICENSE.txt"):
        src = os.path.join(ZDM, f) if os.path.exists(os.path.join(ZDM, f)) \
            else os.path.join(ROOT, f)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(d, f))

    zf = os.path.join(DIST, "zdm-macos.zip")
    if os.path.exists(zf):
        os.remove(zf)
    run("zip", "-r", "-q", zf, "zdm-macos", cwd=DIST)


def main():
    os.makedirs(DIST, exist_ok=True)
    clean_pycache()
    # Remove previously built artifacts FIRST. Otherwise a stale zdm_1.0.0_all.deb
    # sitting next to a freshly built zdm_1.13.0_all.deb makes any downstream
    # glob ("dist/zdm_*_all.deb") ambiguous and breaks packaging/verification.
    import glob
    for pat in ("dist/zdm_*.deb", "dist/zdm-linux*.tar.gz", "dist/zdm-macos.zip",
                "dist/ZDM-Setup-*.exe", "dist/ZDM-*[0-9].dmg"):
        for stale in glob.glob(os.path.join(ROOT, pat)):
            if os.path.isfile(stale):
                os.remove(stale)
                print(f"    (removed stale {os.path.basename(stale)})")
    print("==> Linux native bundle + .deb + portable tarball")
    build_linux_native()
    print("==> Windows bundle + installer")
    build_windows_native()
    print("==> macOS .zip (ZDM.app + dmg script)")
    build_macos_zip()
    # tidy intermediates
    for junk in ("zdm-payload.7z", "sfx_config.txt", "zdm-windows.zip",
                 "zdm-windows", "zdm-macos", "deb_staging", "linux-build"):
        jp = os.path.join(DIST, junk)
        if os.path.isdir(jp):
            shutil.rmtree(jp, ignore_errors=True)
        elif os.path.exists(jp):
            os.remove(jp)
    # dist-level README
    with open(os.path.join(DIST, "README.txt"), "w", encoding="utf-8") as fh:
        fh.write(
            f"ZDM 三端安装包 — v{VERSION}\n"
            "====================\n\n"
            "1) Linux\n"
            f"   - zdm_{VERSION}_amd64.deb  （推荐，需 root：sudo apt install ./zdm_{VERSION}_amd64.deb）\n"
            "       自带 Python 运行时 + PyQt5/Qt，装到 /opt/zdm，不需要系统 python3、pip 或联网装依赖。\n"
            "       ※ .deb 需在 Debian/Ubuntu 上用 dpkg-deb 构建（CI 在 ubuntu-22.04 上产出）。\n"
            f"   - zdm-linux-amd64.tar.gz   （同一份自带运行时；免 root：解压后 ./install.sh，装到 ~/.local）\n\n"
            "2) macOS\n"
            "   - zdm-macos.zip\n"
            "       解压后运行:  bash install.sh   （自动生成 ~/Applications/ZDM.app + 登录自启）\n"
            f"       若要 .dmg：在 Mac 上执行包内 build_macos_dmg.sh 一键生成 ZDM-{VERSION}.dmg\n"
            "       （.dmg 需 macOS 的 hdiutil 生成，无法在 Linux 上构建）\n\n"
            "3) Windows\n"
            f"   - ZDM-Setup-{VERSION}.exe   （PyQt5 + 自带 Python 运行时，与 mac/Linux 同一份界面与引擎）\n"
            "       双击安装，用户级、无需管理员、不依赖本机 Python；软件内可直接一键更新。\n"
            f"       （需在 Windows 上用 zdm/build_windows_app.ps1 构建，CI 在 windows-latest 上产出）\n\n"
            f"本版新增（v{VERSION}）：\n"
            "  · Linux 安装包改为真正的自包含原生版：内嵌 CPython + PyQt5/Qt，"
            "不再依赖系统 python3 / pip（旧包只有 87KB，桌面窗口其实起不来）\n"
            "  · Linux 托盘 / 右键菜单改为圆角半透明磨砂面板（与 macOS 同一套配色与圆角）\n"
            "  · 每个任务行前面有 ⏸ / ▶（单文件暂停 / 继续）；筛选标签右侧是「全部暂停 / 全部继续」\n"
            "  · macOS 菜单栏图标弹出毛玻璃菜单（vibrancy），深浅色自动匹配\n"
            "  · 「打开」打开下载的文件本身（.dmg 直接进安装界面），另设「目录」定位文件\n"
            "  · 桌面端未运行时，浏览器扩展 / zdm add 发起下载会自动拉起窗口\n"
            "  · 窗口标题、状态栏、托盘、关于框显示版本号\n"
            "  · 有新版本时主界面弹横幅，可一键下载并打开安装包（也可忽略该版本）\n"
            "  · 修复点菜单栏图标时菜单文字发灰、点不了的问题\n"
            "  · 安装包不再依赖本机的 Xcode / Python（内嵌运行时，移机可运行）\n\n"
            "安装后在桌面端「设置」或托盘图标右键菜单里，可随时开关「开机自启」与「退出」。\n"
            "浏览器扩展：加载各包内 extension/ 目录即可接管下载。\n"
        )
    print("\n=== dist/ ===")
    for f in sorted(os.listdir(DIST)):
        p = os.path.join(DIST, f)
        if os.path.isfile(p):
            print(f"  {f:28s} {os.path.getsize(p):>10,d} bytes")


if __name__ == "__main__":
    main()
