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

# 随包的 yt-dlp 二进制。**必须走 binaries 而不是 datas**：datas 有时会丢掉
# 可执行位，装到用户机器上就变成「文件在、但执行不了」。落点在 _internal/bin/，
# 正好被 ytdlp_bin.binary_path() 的 dirname(here)/bin 探到。
binaries = []
_ytdlp = os.path.join(SRC, "bin", "yt-dlp")
if os.path.isfile(_ytdlp):
    binaries.append((_ytdlp, "bin"))
else:
    print("WARNING: zdm/bin/yt-dlp missing — 打包版将无法解析/下载 YouTube")

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
    console=True,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="ZDM",
)
