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


def extension_dir() -> str:
    """Best-effort absolute path of the shipped browser-extension folder.

    The layout differs per shape, and the difference is *user facing*: this is
    the folder the user must pick in Chrome's "Load unpacked" dialog, so it
    has to be the real one on disk, not a guess.

    * Windows frozen: the installer deliberately puts a copy at
      ``{app}\\extension`` (a sibling of ``_internal``) because Chrome pins the
      unpacked extension to that path — updating must not move it;
    * macOS bundle / Linux frozen / source checkout: ``<app_root>/extension``
      (Resources, ``_internal``, or the repo root respectively);
    * the macOS CLI flavor (``install.sh``) also keeps a copy in the data dir.

    Returns the first candidate that actually contains a ``manifest.json``;
    when none does (broken install) it returns the path the *installer* would
    have used, so the dialog still points the user at the right place.
    """
    root = app_root()
    cands = [os.path.join(root, "extension")]
    if is_frozen() and os.name == "nt":
        # PyInstaller onedir: root is <app>\_internal; the user-facing copy
        # sits next to it.
        cands.append(os.path.join(os.path.dirname(root), "extension"))
    if sys.platform == "darwin":
        cands.append(
            os.path.join(
                os.path.expanduser("~"), "Library", "Application Support",
                "ZDM", "extension",
            )
        )
    for c in cands:
        if os.path.isfile(os.path.join(c, "manifest.json")):
            return c
    return cands[-1] if (is_frozen() and os.name == "nt") else cands[0]


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
