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

block_cipher = None

a = Analysis(
    [os.path.join(SRC, "zdm_app.py")],
    pathex=[SRC],
    binaries=[],
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
