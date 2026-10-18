#!/usr/bin/env python3
"""Build distributable install packages for NDM on Linux, macOS and Windows.

Output (into ./dist):
  ndm-linux.tar.gz      Linux (run ./install.sh, no root needed)
  ndm_1.0.0_all.deb    Linux .deb (sudo dpkg -i)  [bonus formal package]
  ndm-macos.zip        macOS (run ./install.sh -> builds NDM.app + LaunchAgent)
  ndm-windows.zip      Windows (run install.ps1 or install.bat)
"""
import os
import shutil
import tarfile
import zipfile

SRC = "/home/ooo/WorkBuddy/2026-10-19-00-23-14/ndm"
OUT = "/home/ooo/WorkBuddy/2026-10-19-00-23-14/dist"

# What goes into every package (the whole app, copy of installers included).
COMMON = ["ndm", "web", "extension", "ndm.py", "gui.py", "README.md"]

WIN_ONLY = ["install.ps1", "install.bat"]
UNIX_ONLY = ["install.sh"]


def copytree_clean(src, dst):
    if os.path.exists(dst):
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def stage(name, extra):
    d = os.path.join(OUT, name)
    if os.path.exists(d):
        shutil.rmtree(d)
    os.makedirs(d, exist_ok=True)
    for item in COMMON:
        s = os.path.join(SRC, item)
        if os.path.isdir(s):
            copytree_clean(s, os.path.join(d, item))
        else:
            shutil.copy2(s, os.path.join(d, item))
    for item in extra:
        s = os.path.join(SRC, item)
        if os.path.exists(s):
            shutil.copy2(s, os.path.join(d, item))
    return d


def make_targz(folder, arcname):
    path = os.path.join(OUT, arcname)
    with tarfile.open(path, "w:gz") as tf:
        tf.add(os.path.join(OUT, folder), arcname=folder, recursive=True)
    return path


def make_zip(folder, arcname, exec_files=()):
    path = os.path.join(OUT, arcname)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        root = os.path.join(OUT, folder)
        for base, _dirs, files in os.walk(root):
            for fn in files:
                full = os.path.join(base, fn)
                rel = os.path.relpath(full, OUT)
                if rel in exec_files:
                    zi = zipfile.ZipInfo(rel)
                    zi.external_attr = 0o755 << 16
                    with open(full, "rb") as fh:
                        zf.writestr(zi, fh.read())
                else:
                    zf.write(full, rel)
    return path


def build_deb():
    """Build a user-friendly .deb (installs to /opt/ndm, /usr/local/bin/ndm)."""
    stage_dir = os.path.join(OUT, "deb_staging")
    if os.path.exists(stage_dir):
        shutil.rmtree(stage_dir)
    app = os.path.join(stage_dir, "opt", "ndm")
    os.makedirs(app, exist_ok=True)
    for item in COMMON:
        s = os.path.join(SRC, item)
        if os.path.isdir(s):
            copytree_clean(s, os.path.join(app, item))
        else:
            shutil.copy2(s, os.path.join(app, item))
    # launcher wrapper
    bin_dir = os.path.join(stage_dir, "usr", "local", "bin")
    os.makedirs(bin_dir, exist_ok=True)
    with open(os.path.join(bin_dir, "ndm"), "w", encoding="utf-8") as f:
        f.write("#!/bin/sh\n# NDM launcher (system install)\n"
                'exec python3 /opt/ndm/ndm.py "$@"\n')
    os.chmod(os.path.join(bin_dir, "ndm"), 0o755)
    # .desktop for the GUI
    app_dir = os.path.join(stage_dir, "usr", "share", "applications")
    os.makedirs(app_dir, exist_ok=True)
    with open(os.path.join(app_dir, "ndm.desktop"), "w", encoding="utf-8") as f:
        f.write(
            "[Desktop Entry]\nType=Application\nName=NDM 下载管理器\n"
            "Comment=多线程下载管理器（浏览器扩展可接管下载）\n"
            "Exec=/usr/local/bin/ndm gui\nTerminal=false\n"
            "Categories=Network;FileTransfer;\nKeywords=download;ndm;下载;\n"
            "StartupNotify=true\n")
    debian = os.path.join(stage_dir, "DEBIAN")
    os.makedirs(debian, exist_ok=True)
    with open(os.path.join(debian, "control"), "w", encoding="utf-8") as f:
        f.write(
            "Package: ndm\nVersion: 1.0.0\nSection: net\nPriority: optional\n"
            "Architecture: all\nMaintainer: NDM <ndm@example.com>\n"
            "Description: NDM 多线程下载管理器\n"
            " 本地多线程下载管理器：断点续传、浏览器扩展接管下载、桌面端、\n"
            " 开机自启与托盘菜单。GUI 需要 PyQt5（pip install PyQt5）。\n")
    out = os.path.join(OUT, "ndm_1.0.0_all.deb")
    r = shutil.run if hasattr(shutil, "run") else None
    import subprocess
    subprocess.run(["dpkg-deb", "--build", "--root-owner-group", stage_dir, out],
                   check=True)
    return out


def main():
    os.makedirs(OUT, exist_ok=True)

    # Linux tarball
    stage("ndm-linux", UNIX_ONLY)
    p1 = make_targz("ndm-linux", "ndm-linux.tar.gz")

    # macOS zip (install.sh builds the .app on the target mac)
    stage("ndm-macos", UNIX_ONLY)
    p2 = make_zip("ndm-macos", "ndm-macos.zip", exec_files={"ndm-macos/install.sh"})

    # Windows zip
    stage("ndm-windows", WIN_ONLY)
    p3 = make_zip("ndm-windows", "ndm-windows.zip")

    # Bonus: Linux .deb
    p4 = build_deb()

    for p in (p1, p2, p3, p4):
        size = os.path.getsize(p)
        print(f"{os.path.basename(p):24s} {size:>10,d} bytes")

    # Dist-level README
    with open(os.path.join(OUT, "README.txt"), "w", encoding="utf-8") as f:
        f.write(
            "NDM 三端安装包\n"
            "=============\n\n"
            "1) Linux\n"
            "   - ndm-linux.tar.gz  （推荐，无需 root）\n"
            "       解压后运行:  ./install.sh\n"
            "   - ndm_1.0.0_all.deb（需要 root）\n"
            "       sudo dpkg -i ndm_1.0.0_all.deb\n\n"
            "2) macOS\n"
            "   - ndm-macos.zip\n"
            "       解压后运行:  bash install.sh\n"
            "       脚本会自动生成 ~/Applications/NDM.app 并注册登录自启\n\n"
            "3) Windows\n"
            "   - ndm-windows.zip\n"
            "       解压后右键 install.ps1 选「使用 PowerShell 运行」\n"
            "       或在解压目录双击 install.bat\n\n"
            "安装后在「设置」或托盘图标右键菜单里可随时开关「开机自启」与「退出」。\n"
            "浏览器扩展：加载各包内 extension/ 目录即可接管下载。\n"
        )
    print("dist README.txt written")


if __name__ == "__main__":
    main()
