# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the self-contained Linux build (ZDM onedir bundle).

Produces ``dist/ZDM`` - a real ELF executable - plus ``dist/ZDM/_internal`` with
CPython, PyQt5/Qt and the app's resources. Nothing is loaded from the system
Python, so the .deb can be installed on a machine that has no pip and no
``python3-pyqt5``.

Build it through ``build_linux_app.sh``; that script creates the venv, runs
PyInstaller, verifies the result and packs the .deb.
"""

import os
import sys

from PyInstaller.utils.hooks import collect_submodules

SRC = SPECPATH  # .../zdm (the dir holding gui.py, theme.py, zdm/)
# collect_submodules() imports the package while the spec is being executed, so
# the source dir has to be importable at spec time too (pathex only affects the
# analysis phase).
if SRC not in sys.path:
    sys.path.insert(0, SRC)

# Resources the client reads at runtime (server.py serves web/, gui.py loads the
# toolbar icon from extension/icons). They land in _internal/, which is exactly
# where runtime.app_root() points in a frozen build.
datas = [
    (os.path.join(SRC, "web"), "web"),
    (os.path.join(SRC, "extension"), "extension"),
]
# Deno 与 N_m3u8DL-RE 都是 MIT 许可；许可声明随二进制一起放进每个安装包。
for _lic in ("DENO-LICENSE.md", "RE-LICENSE.txt"):
    _deno_lic = os.path.join(SRC, "bin", _lic)
    if os.path.isfile(_deno_lic):
        datas.append((_deno_lic, "bin"))

# Qt 内置文案（QDialogButtonBox 的 Save/Cancel/OK、QMessageBox 的 Yes/No、
# QFileDialog 的按钮）都来自 Qt 自己的翻译文件。不收集的话，冻结包里
# _install_translations() 无翻译可加载，中文界面上全是英文按钮
# （Windows 实测：设置页底部是 "Save"）。只收中文，控制体积。
try:
    from PyQt5.QtCore import QLibraryInfo as _QLI

    _qtdir = _QLI.location(_QLI.TranslationsPath)
    if os.path.isdir(_qtdir):
        for _f in sorted(os.listdir(_qtdir)):
            if _f.endswith(".qm") and ("zh_CN" in _f or "zh_TW" in _f):
                datas.append((os.path.join(_qtdir, _f), "i18n"))
except Exception as _e:  # pragma: no cover
    print(f"WARNING: cannot collect Qt zh_CN translations: {_e} - frozen buttons will be English")

hiddenimports = [
    "PyQt5.QtNetwork",   # single-instance guard (QLocalServer)
    "PyQt5.QtSvg",       # icon rendering fallbacks
    "zdm.runtime",
]
# The theme/watchdog code imports platform helpers lazily; make sure the whole
# package travels with the binary.
hiddenimports += collect_submodules("zdm")
# YouTube 的解析/下载已经改走「自带解释器的单文件 yt-dlp」二进制（见 binaries），
# 不再依赖 pip 的 yt_dlp 包 —— 2026.x 的包要求 Python 3.10+，而容器/内嵌解释器
# 可能是 3.9，import 会直接抛 "unsupported version of Python"。
# 这里仍保留可选的收集：仓库里如果还带着旧包就不会被漏掉，没带也不让构建失败。
try:
    hiddenimports += collect_submodules("yt_dlp")
except Exception:
    pass

# sftp:// 通道（zdm/sftp.py 里是延迟 import，Analysis 顺着字节码看不到它），
# 所以显式点名。paramiko 背后的 nacl/bcrypt/cryptography 都带 C 扩展，交给
# PyInstaller 自己的钩子收。没装 paramiko 的本地临时构建不因此失败 —— 发布
# 构建脚本会检查 --selfcheck 里的 sftp 字段，缺了就挡住。
try:
    import paramiko  # noqa: F401

    hiddenimports += collect_submodules("paramiko")
except Exception as _e:  # pragma: no cover
    print(f"WARNING: paramiko not available: {_e} - sftp:// will be refused at add-time")

# 随包的 yt-dlp 二进制。**必须走 binaries 而不是 datas**：datas 有时会丢掉
# 可执行位，装到用户机器上就变成「文件在、但执行不了」。落点在 _internal/bin/，
# 正好被 ytdlp_bin.binary_path() 的 dirname(here)/bin 探到。
#
# Windows 上文件名带 .exe（CI 下载的是 yt-dlp.exe / ffmpeg.exe），Linux/macOS
# 不带 —— 两边都探一遍，免得某一端构建时静默少一个文件。
WIN = sys.platform.startswith("win")

binaries = []
for _name in ("yt-dlp", "yt-dlp.exe", "ffmpeg", "ffmpeg.exe", "deno", "deno.exe",
              "N_m3u8DL-RE", "N_m3u8DL-RE.exe"):
    # 一个 Linux 构建目录里可能同时放着给 Windows 安装包准备的 .exe；不要把
    # 别的平台的二进制塞进当前 bundle（会白胖几十 MB，而且跑不起来）。
    if WIN and not _name.endswith(".exe"):
        continue
    if (not WIN) and _name.endswith(".exe"):
        continue
    _p = os.path.join(SRC, "bin", _name)
    if os.path.isfile(_p):
        binaries.append((_p, "bin"))

# 按「有没有那一类文件」判断，不要按具体文件名 —— macOS/Linux 是 yt-dlp，
# Windows 是 yt-dlp.exe，写死哪个都会在另一条流水线上误报。
_found = {os.path.basename(_p) for _p, _ in binaries}
if not (_found & {"yt-dlp", "yt-dlp.exe"}):
    print("WARNING: zdm/bin/yt-dlp missing - YouTube resolve/download unavailable")
if not (_found & {"ffmpeg", "ffmpeg.exe"}):
    print("WARNING: zdm/bin/ffmpeg missing - DASH muxing will fall back to system ffmpeg")
if not (_found & {"deno", "deno.exe"}):
    print("WARNING: zdm/bin/deno missing - YouTube JS challenges cannot be solved")
# 流媒体引擎缺失时 m3u8/mpd 会退回 ffmpeg：能下完，但没有分片并发、单片重试
# 和「按分片续传」。发布用的构建脚本（build_*.sh / .ps1）把这条当硬错误处理，
# 只有本地临时构建才允许带着 WARNING 过去。
if not (_found & {"N_m3u8DL-RE", "N_m3u8DL-RE.exe"}):
    print("WARNING: zdm/bin/N_m3u8DL-RE missing - HLS/DASH falls back to ffmpeg (no segment resume)")

block_cipher = None

a = Analysis(
    [os.path.join(SRC, "zdm_app.py")],
    pathex=[SRC],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # tkinter/unittest are pulled in by stray imports but never used by ZDM.
    excludes=["tkinter", "unittest", "pydoc_data", "test"],
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

_icon = os.path.join(SRC, "extension", "icons", "icon.ico")

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ZDM",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # Windows 上是 GUI 程序：console=True 会额外挂一个黑色控制台窗口。
    # 代价是 --version / --selfcheck 的输出也一并被吞掉，所以 CI 校验只能
    # 用 exit code（见 build_windows_app.ps1）。
    console=not WIN,
    icon=_icon if (WIN and os.path.isfile(_icon)) else None,
    disable_windowed_traceback=False,
)

extra_exes = []
if WIN:
    # Chrome/Edge 的原生消息主机必须是可执行文件，且只能由 manifest 的
    # "path" 启动（不能带命令行参数），所以单独出一个 console 版小 exe。
    # 窗口版的 ZDM.exe 没有 stdio，拿它当主机会被 Chrome 判定为「主机退出」。
    host_a = Analysis(
        [os.path.join(SRC, "zdm_native_host.py")],
        pathex=[SRC],
        binaries=[],
        datas=[],
        hiddenimports=[],
        hookspath=[],
        hooksconfig={},
        runtime_hooks=[],
        excludes=["tkinter", "unittest", "pydoc_data", "test", "PyQt5"],
        noarchive=False,
        cipher=block_cipher,
    )
    host_pyz = PYZ(host_a.pure, host_a.zipped_data, cipher=block_cipher)
    extra_exes.append(
        EXE(
            host_pyz,
            host_a.scripts,
            [],
            exclude_binaries=True,
            name="ZDM-NativeHost",
            debug=False,
            bootloader_ignore_signals=False,
            strip=False,
            upx=False,
            console=True,
        )
    )

coll = COLLECT(
    exe,
    *extra_exes,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="ZDM",
)
