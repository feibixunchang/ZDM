"""Where the app lives at runtime: plain source checkout vs. a frozen build.

The desktop client ships in three shapes:

* **source checkout** (``git clone`` / ``zdm-linux.tar.gz`` legacy layout):
  ``python3 zdm.py gui``, ``gui.py`` next to the ``zdm`` package;
* **macOS app bundle**: a native launcher embedding CPython, resources under
  ``ZDM.app/Contents/Resources``;
* **frozen Linux/Windows build** (PyInstaller): a real ELF/PE executable named
  ``ZDM`` with its support files in ``_internal/`` (``sys._MEIPASS``).

Anything that spawns a child process, or writes an autostart entry, has to know
which shape it is in - otherwise a frozen build would try to run
``sys.executable zdm.py`` and fail. Keep those decisions here so there is one
place to reason about them.
"""

from __future__ import annotations

import os
import sys


def is_frozen() -> bool:
    """True when running from a bundled executable (PyInstaller and friends)."""
    return bool(getattr(sys, "frozen", False))


def app_root() -> str:
    """Directory that holds the read-only resources (``web/``, ``extension/``).

    * frozen: the bundle's internal dir (``_MEIPASS`` for onedir builds);
    * source: the parent of the ``zdm`` package.
    """
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            return meipass
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def exe_path() -> str:
    """Abs path of the executable that represents this app."""
    return os.path.abspath(sys.executable or "python3")


def cli_cmd(*args: str) -> list:
    """argv that runs this app's CLI with the same runtime we are using."""
    if is_frozen():
        return [exe_path(), *args]
    return [sys.executable or "python3", os.path.join(app_root(), "zdm.py"), *args]


def gui_cmd(*args: str) -> list:
    """argv that opens the desktop window with the same runtime we are using.

    A frozen build has one executable whose *name* selects the role
    (``zdm-desktop`` -> GUI, anything else -> CLI). Re-execing the raw binary
    would land in the CLI branch, where argparse rejects the bare port number -
    so the frozen branch passes the explicit ``--gui`` switch instead of
    relying on argv[0].
    """
    if is_frozen():
        return [exe_path(), "--gui", *args]
    return [sys.executable or "python3", os.path.join(app_root(), "gui.py"), *args]


def autostart_exec(*args: str) -> str:
    """``Exec=`` line for an XDG autostart entry / launchd plist.

    Quotes the executable because a bundle may live under a path with spaces.
    """
    return " ".join(f'"{a}"' if " " in a else a for a in cli_cmd(*args))
