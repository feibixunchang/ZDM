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
import urllib.parse
from typing import Tuple

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


def _exited_badly(p, what: str) -> bool:
    """True when the child process clearly died right away (non-zero exit).

    "Popen did not throw" is not success: a broken argv (e.g. the frozen binary
    re-execing itself under the CLI name) or a damaged bundle makes the child
    exit within milliseconds, and ensure_desktop used to report it as launched
    anyway. A single-instance wake-up also exits fast - but with code 0 - so only
    a non-zero exit counts as failure. Still running after the grace period means
    it handed off to a real window.
    """
    global _last_error
    deadline = time.time() + 2.5
    while time.time() < deadline:
        rc = p.poll()
        if rc is not None:
            if rc == 0:
                return False
            _last_error = f"{what} exited with code {rc}"
            return True
        time.sleep(0.1)
    return False


def _spawn_python_gui(urls, port: int) -> bool:
    """Fallback: re-run the desktop client with a PyQt5-capable runtime.

    A frozen build ships its own interpreter and has no ``gui.py`` on disk, so
    we simply re-exec the bundle; ``zdm_app`` sees the ``--gui`` switch and
    opens the window. Everything else uses the script + an interpreter that
    really can import PyQt5.
    """
    code = code_dir()
    args = [str(port)] + list(urls or [])
    # The bundle lives in /opt (root-owned, read-only for the user) and a .app
    # may run from a read-only volume: write the child's log next to the
    # config, never inside the code directory.
    try:
        from .manager import config_dir

        log_path = os.path.join(config_dir(), "gui.log")
    except Exception:
        log_path = os.path.join(os.path.expanduser("~"), ".zdm-gui.log")
    cwd = os.path.expanduser("~")
    if runtime.is_frozen():
        cmd = runtime.gui_cmd(*args)
    else:
        gui = os.path.join(code, "gui.py")
        if not os.path.exists(gui):
            return False
        py = _python_with_qt(code)
        if not py:
            return False
        cmd = [py, gui] + args
    try:
        with open(log_path, "a", encoding="utf-8") as log:
            p = subprocess.Popen(
                cmd,
                stdout=log,
                stderr=log,
                stdin=subprocess.DEVNULL,
                cwd=cwd,
                **_detached(),
            )
    except Exception as e:  # noqa: BLE001
        global _last_error
        _last_error = f"{type(e).__name__}: {e}"
        return False
    if _exited_badly(p, f"desktop client (see {log_path})"):
        return False
    return True


# --------------------------------------------------------------------------- open
def _spawn(argv, env) -> Tuple[bool, str]:
    """Launch one handler and tell the caller whether it clearly failed.

    ``xdg-open`` and friends return immediately; a non-zero exit code means
    "no handler / could not launch" and is what used to be swallowed (the
    button looked dead). If the process is still running after a short grace
    period it has handed off to a browser/file manager, so treat it as success.
    """
    try:
        p = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
    except Exception as e:  # noqa: BLE001
        return False, f"{os.path.basename(argv[0])}: {e}"
    try:
        rc = p.wait(timeout=1.5)
    except subprocess.TimeoutExpired:
        return True, ""
    if rc == 0:
        return True, ""
    return False, f"{os.path.basename(argv[0])} 退出码 {rc}"


def _linux_open_path(target: str, reveal: bool) -> Tuple[bool, str]:
    env = runtime.child_env()
    path_env = env.get("PATH") or ""

    if reveal and os.path.isfile(target):
        # FileManager1 是标准接口，能直接在文件管理器里选中文件。
        dbus = shutil.which("dbus-send", path=path_env)
        if dbus:
            uri = "file://" + urllib.parse.quote(os.path.abspath(target))
            try:
                r = subprocess.run(
                    [
                        dbus, "--session",
                        "--dest=org.freedesktop.FileManager1",
                        "--type=method_call",
                        "/org/freedesktop/FileManager1",
                        "org.freedesktop.FileManager1.ShowItems",
                        "array:string:" + uri, "string:",
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=3,
                    env=env,
                )
                if r.returncode == 0:
                    return True, ""
            except Exception:  # noqa: BLE001
                pass
        for name, argv in (
            ("nautilus", ["nautilus", "--select", target]),
            ("dolphin", ["dolphin", "--select", target]),
            ("nemo", ["nemo", target]),
            ("thunar", ["thunar", target]),
        ):
            if shutil.which(name, path=path_env):
                ok, err = _spawn(argv, env)
                if ok:
                    return True, ""
                last = err
        # 选不中文件时退一步：至少把所在目录打开。
        target = os.path.dirname(target) or "."

    # xdg-open is the canonical entry point; gio/kde-open5/... cover minimal
    # installs where xdg-utils is missing. Browser/file-manager names are the
    # last resort so a desktop without those helpers still does something.
    cmds = [
        ["xdg-open", target],
        ["gio", "open", target],
        ["kde-open5", target],
        ["kioclient5", "exec", target],
        ["exo-open", target],
        ["gnome-open", target],
        ["deepin-open", target],
    ]
    if os.path.isdir(target):
        for name in ("nautilus", "dolphin", "thunar", "nemo", "pcmanfm", "caja"):
            cmds.append([name, target])
    last = ""
    for argv in cmds:
        if shutil.which(argv[0], path=path_env) is None:
            continue
        ok, err = _spawn(argv, env)
        if ok:
            return True, ""
        last = err
    return False, last or "系统里没有可用的文件管理器/默认程序"


def _linux_open_url(url: str) -> Tuple[bool, str]:
    env = runtime.child_env()
    path_env = env.get("PATH") or ""
    cmds = [
        ["xdg-open", url],
        ["gio", "open", url],
        ["sensible-browser", url],
    ]
    for name in (
        "google-chrome", "chromium", "chromium-browser", "firefox",
        "microsoft-edge", "brave-browser", "vivaldi",
    ):
        cmds.append([name, url])
    last = ""
    for argv in cmds:
        if shutil.which(argv[0], path=path_env) is None:
            continue
        ok, err = _spawn(argv, env)
        if ok:
            return True, ""
        last = err
    return False, last or "系统里没有可用的浏览器（需要 xdg-open 或已安装的浏览器）"


def open_external(target: str, reveal: bool = False) -> Tuple[bool, str]:
    """Open a path or URL with the desktop's default handler.

    Returns ``(ok, reason)`` instead of a bare bool so the GUI can tell the
    user *why* nothing happened. Must run in the GUI process: the background
    service often has no DISPLAY/DBUS env, and a frozen bundle's
    ``LD_LIBRARY_PATH`` breaks system helpers (see :func:`runtime.child_env`).
    """
    target = str(target or "").strip()
    if not target:
        return False, "路径为空"
    is_url = target.lower().startswith(("http://", "https://"))
    if is_url:
        if IS_WIN:
            try:
                os.startfile(target)  # type: ignore[attr-defined]
                return True, ""
            except Exception as e:  # noqa: BLE001
                return False, str(e)
        if IS_MAC:
            return _spawn(["open", target], runtime.child_env())
        return _linux_open_url(target)

    target = os.path.expanduser(target)
    if not os.path.exists(target):
        return False, f"路径不存在：{target}"
    if IS_WIN:
        try:
            if reveal and os.path.isfile(target):
                subprocess.Popen(
                    ["explorer", "/select,", os.path.normpath(target)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                os.startfile(target)  # type: ignore[attr-defined]
            return True, ""
        except Exception as e:  # noqa: BLE001
            return False, str(e)
    if IS_MAC:
        argv = ["open", "-R", target] if (reveal and os.path.isfile(target)) else ["open", target]
        return _spawn(argv, runtime.child_env())
    return _linux_open_path(target, reveal)


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
                p = subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                # `open` exits on its own: a damaged bundle (or a launcher that
                # re-execs into the CLI) exits non-zero in milliseconds, and
                # returning True here used to print "桌面端已启动" for nothing.
                if not _exited_badly(p, f"open {bundle}"):
                    return True
        elif IS_WIN:
            exe = _win_exe()
            if exe:
                # 冻结版只有一个 exe，角色由参数决定（见 zdm_app.py）：必须带
                # --gui 才会进窗口分支。以前这里裸拉 [exe]+urls：
                #   urls 为空 → zdm_app 兜成 ["gui"] 子命令 → cmd_gui 又调
                #   ensure_desktop（且 cmd_gui 每次都清零节流）→ 无限拉起链，
                #   十几秒堆出几十个进程 → 资源耗尽 → ucrtbase BEX64 崩溃，
                #   表层就是「Failed to load Python DLL / 内存资源不足」；
                #   urls 非空 → 落进 CLI 分支被 argparse 拒掉，窗口照样不出。
                # macOS 走 open bundle、Linux 走 zdm-gui 启动器，都没这条死路。
                p = subprocess.Popen(
                    [exe, "--gui", str(port)] + list(urls), **_detached()
                )
                if not _exited_badly(p, exe):
                    return True
        else:
            # Prefer the source-install launcher / our own gui.py. Falling back
            # to a "zdm-desktop" found on PATH used to pick an old system-wide
            # (frozen) build when the service was started with a login PATH that
            # lacks ~/.local/bin - the window then ran the previous version.
            launcher = shutil.which("zdm-gui")
            if launcher:
                p = subprocess.Popen([launcher] + urls, **_detached())
                if not _exited_badly(p, launcher):
                    return True
        return _spawn_python_gui(urls, port)
    except Exception as e:  # noqa: BLE001
        _last_error = f"{type(e).__name__}: {e}"
        return False
