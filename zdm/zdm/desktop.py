"""Find and launch the ZDM desktop client.

Why this module exists
----------------------
The browser extension (and ``zdm add``) talks to the local REST service. If the
user has no ZDM window open, that download would silently run in the background
and the user would never see it. :func:`ensure_desktop` fixes that: after a task
is created we bring the desktop client up (or, if it is already running but
hidden, to the front).

Design notes
------------
* **macOS**: ``open -a /path/ZDM.app`` is perfect for both cases. LaunchServices
  starts the bundle when it is not running and merely *activates* the existing
  instance when it is - no second process, and activation is what makes our
  reopen hook restore the hidden window.
* **Windows / Linux**: we spawn the GUI executable (or ``python gui.py``). The
  client's single-instance guard turns a second launch into "wake the existing
  window and exit", so this works whether or not ZDM is already running.
* A short throttle keeps a burst of downloads (the extension can post several in
  a row) from spawning several GUI processes.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

from . import runtime

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

#: Minimum seconds between two launch attempts.
THROTTLE = 5.0
_last_launch = 0.0
_last_error = ""


def _detached() -> dict:
    """Popen kwargs that detach the child from our process group."""
    if IS_WIN:
        return {"creationflags": 0x00000008}  # DETACHED_PROCESS
    return {"start_new_session": True}


def last_error() -> str:
    """Reason of the last failed launch attempt (empty when fine)."""
    return _last_error


def code_dir() -> str:
    """Directory holding ``gui.py`` / ``zdm.py`` (the runtime root)."""
    if runtime.is_frozen():
        return runtime.app_root()
    pkg = os.path.dirname(os.path.abspath(__file__))       # .../zdm
    root = os.path.dirname(pkg)                            # parent of the package
    if os.path.exists(os.path.join(root, "gui.py")):
        return root
    try:
        from .manager import config_dir

        d = config_dir()
        if os.path.exists(os.path.join(d, "gui.py")):
            return d
    except Exception:
        pass
    return root


def _mac_bundle() -> str:
    """Path of the installed ZDM.app bundle, or "" when not found."""
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, "Applications", "ZDM.app"),
        "/Applications/ZDM.app",
        os.path.join(code_dir(), "ZDM.app"),
    ]
    for c in candidates:
        if os.path.isdir(c) and os.path.exists(os.path.join(c, "Contents", "MacOS", "ZDM")):
            return c
    return ""


def _win_exe() -> str:
    """Path of an installed ZDM desktop executable, or "" when not found."""
    home = os.path.expanduser("~")
    local = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
    candidates = [
        os.path.join(local, "Programs", "ZDM", "ZDM.exe"),
        os.path.join(code_dir(), "ZDM.exe"),
        os.path.join(os.path.dirname(sys.executable), "ZDM.exe"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return ""


def _python_with_qt(code: str) -> str:
    """An interpreter that can import PyQt5 (the GUI's only hard requirement)."""
    candidates = []
    if IS_WIN:
        candidates.append(os.path.join(code, "venv", "Scripts", "pythonw.exe"))
        candidates.append(os.path.join(code, "venv", "Scripts", "python.exe"))
    else:
        candidates.append(os.path.join(code, "venv", "bin", "python"))
    candidates.append(sys.executable)
    candidates.append(shutil.which("python3") or "")
    seen = set()
    for py in candidates:
        if not py or py in seen or not os.path.exists(py):
            continue
        seen.add(py)
        # A bundled venv is trusted (the installer created it with PyQt5); for
        # system interpreters spend the extra 0.3s on a real import check.
        if os.path.sep + "venv" + os.path.sep in py:
            return py
        try:
            r = subprocess.run(
                [py, "-c", "import PyQt5.QtWidgets"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=25,
            )
            if r.returncode == 0:
                return py
        except Exception:
            continue
    return ""


def _spawn_python_gui(urls, port: int) -> bool:
    """Fallback: re-run the desktop client with a PyQt5-capable runtime.

    A frozen build ships its own interpreter and has no ``gui.py`` on disk, so
    we simply re-exec the bundle; ``zdm_app`` sees the ``gui`` subcommand and
    opens the window. Everything else uses the script + an interpreter that
    really can import PyQt5.
    """
    code = code_dir()
    args = [str(port)] + list(urls or [])
    if runtime.is_frozen():
        cmd = runtime.gui_cmd(*args)
        # The bundle lives in /opt (root-owned, read-only for the user): write
        # the child's log next to the config, never inside the bundle.
        try:
            from .manager import config_dir

            log_path = os.path.join(config_dir(), "gui.log")
            cwd = os.path.expanduser("~")
        except Exception:
            log_path = os.devnull
            cwd = os.path.expanduser("~")
    else:
        gui = os.path.join(code, "gui.py")
        if not os.path.exists(gui):
            return False
        py = _python_with_qt(code)
        if not py:
            return False
        cmd = [py, gui] + args
        log_path = os.path.join(code, "gui.log")
        cwd = code
    try:
        with open(log_path, "a", encoding="utf-8") as log:
            subprocess.Popen(
                cmd,
                stdout=log,
                stderr=log,
                stdin=subprocess.DEVNULL,
                cwd=cwd,
                **_detached(),
            )
        return True
    except Exception as e:  # noqa: BLE001
        global _last_error
        _last_error = f"{type(e).__name__}: {e}"
        return False


def ensure_desktop(urls=None, port: int = 9000) -> bool:
    """Start the desktop client, or bring the running one to the front.

    Returns True when a launch/activation was issued. Never raises: a download
    must not fail just because the GUI could not be opened.
    """
    global _last_launch, _last_error
    now = time.time()
    if now - _last_launch < THROTTLE:
        return False
    _last_launch = now
    _last_error = ""
    urls = [u for u in (urls or []) if u]
    try:
        if IS_MAC:
            bundle = _mac_bundle()
            if bundle:
                cmd = ["open", bundle]
                if urls:
                    cmd += ["--args"] + urls
                subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                return True
        elif IS_WIN:
            exe = _win_exe()
            if exe:
                subprocess.Popen([exe] + urls, **_detached())
                return True
        else:
            launcher = shutil.which("zdm-gui") or shutil.which("zdm-desktop")
            if launcher:
                subprocess.Popen([launcher] + urls, **_detached())
                return True
        return _spawn_python_gui(urls, port)
    except Exception as e:  # noqa: BLE001
        _last_error = f"{type(e).__name__}: {e}"
        return False
