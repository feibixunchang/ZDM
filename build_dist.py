#!/usr/bin/env python3
"""Build distributable packages for ZDM on all three platforms.

Outputs (into ./dist):
  - zdm_1.2.0_all.deb          Linux  (apt/dpkg installable, real .deb)
  - zdm-linux.tar.gz           Linux  (fallback, user-level, no root)
  - ZDM-Setup-1.2.0.exe        Windows (.exe self-extracting installer, no admin)
  - zdm-macos.zip              macOS  (ZDM.app + extension + build_macos_dmg.sh)

The Windows .exe is assembled on Linux with 7-Zip's SFX module (7zS.sfx):
    [ 7zS.sfx stub ] + [ installer config ] + [ archive.7z ]
7zS.sfx extracts the payload to a temp dir and runs setup.cmd, which invokes
install.ps1. The macOS .dmg itself must be produced on a Mac (hdiutil is
macOS-only); build_macos_dmg.sh does that in one command.
"""
import os
import shutil
import subprocess
import sys

ROOT = os.path.abspath(os.path.dirname(__file__))
ZDM = os.path.join(ROOT, "zdm")
DIST = os.path.join(ROOT, "dist")
SFX = os.path.join(ROOT, "7zS.sfx")
VERSION = "1.2.0"


def which(cmd):
    from shutil import which as w
    return w(cmd)


EXE7Z = None
for c in ("7z", "7za", "7zr"):
    if which(c):
        EXE7Z = c
        break
if not EXE7Z:
    sys.exit("ERROR: 7z not found on PATH")


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
                        ignore=shutil.ignore_patterns("__pycache__"))
    for f in ("zdm.py", "gui.py", "README.md", "install.sh",
              "install.ps1", "install.bat", "setup.cmd", "build_macos_dmg.sh",
              "zdm_launcher.m", "LICENSE.txt"):
        src = os.path.join(ZDM, f) if os.path.exists(os.path.join(ZDM, f)) \
            else os.path.join(ROOT, f)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(dst, f))


# ----------------------------------------------------------------- Linux deb
def build_deb():
    staging = os.path.join(DIST, "deb_staging")
    if os.path.exists(staging):
        shutil.rmtree(staging)
    app = os.path.join(staging, "usr", "local", "share", "zdm")
    bindir = os.path.join(staging, "usr", "local", "bin")
    appdir = os.path.join(staging, "usr", "local", "share", "applications")
    icondir = os.path.join(staging, "usr", "local", "share", "icons", "hicolor",
                          "128x128", "apps")
    os.makedirs(app, exist_ok=True)
    os.makedirs(bindir, exist_ok=True)
    os.makedirs(appdir, exist_ok=True)
    os.makedirs(icondir, exist_ok=True)

    for item in ("zdm", "web", "extension"):
        shutil.copytree(os.path.join(ZDM, item), os.path.join(app, item),
                        dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__"))
    for f in ("zdm.py", "gui.py", "README.md"):
        shutil.copy2(os.path.join(ZDM, f), os.path.join(app, f))

    with open(os.path.join(bindir, "zdm"), "w", encoding="utf-8") as fh:
        fh.write('#!/bin/sh\nPY="$(command -v python3 || command -v python)"\n'
                 'exec "$PY" "/usr/local/share/zdm/zdm.py" "$@"\n')
    os.chmod(os.path.join(bindir, "zdm"), 0o755)

    shutil.copy2(os.path.join(ZDM, "extension", "icons", "icon128.png"),
                 os.path.join(icondir, "zdm.png"))
    with open(os.path.join(appdir, "zdm.desktop"), "w", encoding="utf-8") as fh:
        fh.write(
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=ZDM 下载管理器\n"
            "Comment=多线程下载管理器（浏览器扩展可接管下载）\n"
            "Exec=/usr/local/bin/zdm gui\n"
            "Icon=zdm\n"
            "Terminal=false\n"
            "Categories=Network;FileTransfer;\n"
            "Keywords=download;zdm;下载;\n"
            "StartupNotify=true\n"
        )

    debian = os.path.join(staging, "DEBIAN")
    os.makedirs(debian, exist_ok=True)
    size_mb = int(subprocess.check_output(["du", "-sm", staging]).split()[0]) + 1
    with open(os.path.join(debian, "control"), "w", encoding="utf-8") as fh:
        fh.write(
            "Package: zdm\n"
            f"Version: {VERSION}\n"
            "Section: net\n"
            "Priority: optional\n"
            "Architecture: all\n"
            f"Installed-Size: {size_mb}\n"
            "Depends: python3 (>= 3.9)\n"
            "Maintainer: ZDM <noreply@example.com>\n"
            "Description: ZDM 跨平台多线程下载管理器\n"
            " 纯标准库引擎 + PyQt5 桌面端；多线程分块、断点续传、暂停/继续、队列、限速；\n"
            " 自带浏览器扩展接管下载；桌面端单实例 + 系统托盘（开机自启/退出）。\n"
        )
    with open(os.path.join(debian, "postinst"), "w", encoding="utf-8") as fh:
        fh.write(
            "#!/bin/sh\n"
            "set -e\n"
            "if command -v update-desktop-database >/dev/null 2>&1; then "
            "update-desktop-database /usr/local/share/applications || true; fi\n"
            "if command -v gtk-update-icon-cache >/dev/null 2>&1; then "
            "gtk-update-icon-cache -f -t /usr/local/share/icons/hicolor || true; fi\n"
            'PY="$(command -v python3 || command -v python)"\n'
            'if [ -n "$PY" ] && ! "$PY" -c "import PyQt5.QtWidgets" >/dev/null 2>&1; then\n'
            '  "$PY" -m pip install --user PyQt5 >/dev/null 2>&1 || true\n'
            "fi\n"
            "exit 0\n"
        )
    os.chmod(os.path.join(debian, "postinst"), 0o755)

    out = os.path.join(DIST, f"zdm_{VERSION}_all.deb")
    run("dpkg-deb", "--build", "--root-owner-group", staging, out)


def build_linux_tarball():
    name = "zdm-linux"
    d = os.path.join(DIST, name)
    if os.path.exists(d):
        shutil.rmtree(d)
    stage_files(d)
    run("chmod", "+x", os.path.join(d, "install.sh"))
    tar = os.path.join(DIST, f"{name}.tar.gz")
    if os.path.exists(tar):
        os.remove(tar)
    run("tar", "-czf", tar, "-C", DIST, name)


# ----------------------------------------------------------------- Windows exe
def build_windows_exe():
    d = os.path.join(DIST, "zdm-windows")
    if os.path.exists(d):
        shutil.rmtree(d)
    stage_files(d)
    # Build the 7z payload (relative paths, from inside the staged dir).
    archive = os.path.join(DIST, "zdm-payload.7z")
    if os.path.exists(archive):
        os.remove(archive)
    run(EXE7Z, "a", "-t7z", "-mx=5", "-y", archive, ".", cwd=d)

    config = (
        ";!@Install@!UTF-8!\n"
        'Title="ZDM 下载管理器"\n'
        'BeginPrompt="安装 ZDM 下载管理器（多线程下载 + 浏览器扩展接管）？"\n'
        'RunProgram="setup.cmd"\n'
        ";!@InstallEnd@!\n"
    )
    cfg = os.path.join(DIST, "sfx_config.txt")
    with open(cfg, "w", encoding="utf-8") as fh:
        fh.write(config)

    out = os.path.join(DIST, f"ZDM-Setup-{VERSION}.exe")
    with open(out, "wb") as out_fh:
        for part in (SFX, cfg, archive):
            with open(part, "rb") as pf:
                shutil.copyfileobj(pf, out_fh)
    os.chmod(out, 0o755)
    print(f"built {out}")


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
    for f in ("install.sh", "install.ps1", "install.bat", "build_macos_dmg.sh",
              "zdm_launcher.m", "README.md", "LICENSE.txt"):
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
    # sitting next to a freshly built zdm_1.2.0_all.deb makes any downstream
    # glob ("dist/zdm_*_all.deb") ambiguous and breaks packaging/verification.
    import glob
    for pat in ("dist/zdm_*_all.deb", "dist/ZDM-Setup-*.exe",
                "dist/zdm-linux.tar.gz", "dist/zdm-macos.zip"):
        for stale in glob.glob(os.path.join(ROOT, pat)):
            if os.path.isfile(stale):
                os.remove(stale)
                print(f"    (removed stale {os.path.basename(stale)})")
    # The .deb is a Linux artifact and needs dpkg-deb (not available on macOS).
    # Skip it gracefully so the other packages still build, instead of aborting
    # the whole run on a missing Linux-only tool.
    if which("dpkg-deb"):
        print("==> Linux .deb")
        build_deb()
    else:
        print("==> Linux .deb  [跳过：本机缺少 dpkg-deb，请在 Linux 上构建]")
    print("==> Linux tarball")
    build_linux_tarball()
    print("==> Windows .exe (7z SFX)")
    build_windows_exe()
    print("==> macOS .zip (ZDM.app + dmg script)")
    build_macos_zip()
    # tidy intermediates
    for junk in ("zdm-payload.7z", "sfx_config.txt", "zdm-windows.zip",
                 "zdm-windows", "zdm-macos", "deb_staging"):
        jp = os.path.join(DIST, junk)
        if os.path.isdir(jp):
            shutil.rmtree(jp, ignore_errors=True)
        elif os.path.exists(jp):
            os.remove(jp)
    # dist-level README
    with open(os.path.join(DIST, "README.txt"), "w", encoding="utf-8") as fh:
        fh.write(
            "ZDM 三端安装包\n"
            "=============\n\n"
            "1) Linux\n"
            f"   - zdm_{VERSION}_all.deb   （推荐，需 root：sudo dpkg -i zdm_{VERSION}_all.deb）\n"
            "       ※ .deb 需在 Linux 上用 dpkg-deb 构建；若当前生成的包里没有它，\n"
            "         请到 Linux 环境执行 python3 build_dist.py 补齐。\n"
            "   - zdm-linux.tar.gz    （免 root：解压后 ./install.sh）\n\n"
            "2) macOS\n"
            "   - zdm-macos.zip\n"
            "       解压后运行:  bash install.sh   （自动生成 ~/Applications/ZDM.app + 登录自启）\n"
            f"       若要 .dmg：在 Mac 上执行包内 build_macos_dmg.sh 一键生成 ZDM-{VERSION}.dmg\n"
            "       （.dmg 需 macOS 的 hdiutil 生成，无法在 Linux 上构建）\n\n"
            "3) Windows\n"
            f"   - ZDM-Setup-{VERSION}.exe\n"
            "       双击运行，按提示安装（用户级、无需管理员；自动检测/安装 Python 与 PyQt5）\n\n"
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
