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


def silent_proc_kwargs() -> dict:
    """Extra ``subprocess`` kwargs that suppress the console window on Windows.

    GUI 进程（无控制台）拉起 ffmpeg / yt-dlp / curl / powershell 这类控制台
    程序时，Windows 会给子进程分配一个**新的**控制台窗口 —— 用户看到的就是
    「点一下下载弹个黑窗」。加 ``CREATE_NO_WINDOW`` 后子进程不继承也不新建
    控制台。POSIX 上是空参数，调用处直接 ``**silent_proc_kwargs()``。
    """
    if os.name == "nt":
        import subprocess

        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}


def child_env() -> dict:
    """Environment for launching *system* helpers from this app.

    PyInstaller puts the bundle's ``_internal`` directory on
    ``LD_LIBRARY_PATH``. That is required by the bundled interpreter, but every
    child inherits it: system programs such as ``xdg-open``, ``gio``,
    ``dbus-send`` or the distro's .deb installer then load our ``libssl`` /
    ``libstdc++`` and exit without doing anything. The user sees a button that
    simply does nothing (Linux 上「打开网页版 / 打开目录」就是这个原因).
    Remove only our own runtime directories, plus the Python overrides a frozen
    bundle must not leak into system scripts.
    """
    env = os.environ.copy()
    if sys.platform.startswith("linux"):
        drop = set()
        if is_frozen():
            try:
                drop.add(os.path.realpath(app_root()))
            except OSError:
                pass
        libpath = env.get("LD_LIBRARY_PATH")
        if libpath:
            keep = []
            for part in libpath.split(":"):
                if not part:
                    continue
                try:
                    if os.path.realpath(part) in drop:
                        continue
                except OSError:
                    pass
                keep.append(part)
            if keep:
                env["LD_LIBRARY_PATH"] = ":".join(keep)
            else:
                env.pop("LD_LIBRARY_PATH", None)
        for key in ("PYTHONHOME", "PYTHONPATH"):
            env.pop(key, None)
    return env


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


def _manifest_version(path: str) -> str:
    """manifest.json 的 version 字段；读不到就返回空串。"""
    import json

    try:
        with open(os.path.join(path, "manifest.json"), encoding="utf-8") as f:
            return str(json.load(f).get("version") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def bundled_extension_version() -> str:
    """磁盘上随包扩展的 manifest version；读不到返回空串。

    给 /api/extension/ping 用：扩展拿它和自己运行中的 manifest.version
    对比，不一致就 reload 自己。这是「扩展跟着安装包走」链路的最后一环
    ——安装包把新 extension/ 落盘，服务把新版本号报出去，扩展自动重载。
    """
    return _manifest_version(extension_dir())


def sync_extension_to_data_dir() -> str:
    """把随包扩展同步到数据目录（仅 macOS 有此问题）。

    Chrome 的「加载已解压的扩展程序」会把扩展**钉死在第一次选择的路径**上——
    install.sh 时代的指引选的是数据目录（``~/Library/Application Support/ZDM/
    extension``）。App 升级只更新 bundle 里的副本，数据目录那份永远停在旧版，
    用户看到的就是「升级了 App，扩展行为还是老的」（v1.15.3 实锤：数据目录
    停在 1.5.10，没有 cookie 导出，YouTube 全部报人机验证）。

    所以启动时对比两边 manifest 的 version，不一致就把随包版整体覆盖过去。
    Windows/Linux 的用户可见目录就是随包目录本身，无需同步；数据目录还没有
    扩展的全新安装也不动它 —— 指引本来就指向随包目录，不凭空造一份。
    """
    if sys.platform != "darwin":
        return ""
    shipped = os.path.join(app_root(), "extension")
    data = os.path.join(
        os.path.expanduser("~"), "Library", "Application Support", "ZDM", "extension"
    )
    if not os.path.isfile(os.path.join(shipped, "manifest.json")):
        return ""
    if not os.path.isdir(data):
        return ""
    v_ship, v_data = _manifest_version(shipped), _manifest_version(data)
    if not v_ship or v_ship == v_data:
        return ""

    import shutil

    try:
        tmp = data + ".syncing"
        if os.path.exists(tmp):
            shutil.rmtree(tmp)
        shutil.copytree(shipped, tmp)
        shutil.rmtree(data)
        try:
            os.replace(tmp, data)
        except OSError:
            # 同目录改名被拒（云同步/权限加锁）时退回复制
            shutil.copytree(shipped, data, dirs_exist_ok=True)
            shutil.rmtree(tmp, ignore_errors=True)
        _log_extension_sync(f"synced extension {v_data} -> {v_ship}")
        return data
    except Exception as e:  # noqa: BLE001
        _log_extension_sync(f"sync failed: {e}")
        return ""


def _log_extension_sync(msg: str) -> None:
    """同步结果落到数据目录的小日志里 —— 关键路径不允许静默失败。"""
    try:
        p = os.path.join(
            os.path.expanduser("~"), "Library", "Application Support", "ZDM",
            "extension_sync.log",
        )
        import time

        with open(p, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:  # noqa: BLE001
        pass


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
