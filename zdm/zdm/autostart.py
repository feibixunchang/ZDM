"""Cross-platform login autostart toggle for ZDM.

The desktop GUI calls these functions so the user can turn "launch on login"
on/off at runtime, on Linux, macOS and Windows, without re-running the
installer. The toggle manages the *same* artifacts the installers create
(so it stays coherent with a fresh install):

  - Linux   : ~/.config/autostart/zdm.desktop              (XDG autostart)
  - macOS   : ~/Library/LaunchAgents/com.zdm.service.plist (launchd)
  - Windows : HKCU\\...\\Run "ZDM"  +  Startup "ZDM Service.lnk"

The running GUI always knows where it lives (``gui.py`` sits in APP_DIR), so
we point the autostart command straight at the installed ``zdm.py``.
"""

from __future__ import annotations

import os
import subprocess
import sys

from . import runtime

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"


# ----------------------------------------------------------------- helpers
def _app_dir() -> str:
    """Install root that contains zdm.py / gui.py (parent of the zdm package)."""
    return runtime.app_root()


def _python() -> str:
    return sys.executable or "python3"


# ----------------------------------------------------------------- Linux
def _linux_path() -> str:
    home = os.path.expanduser("~")
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    return os.path.join(base, "autostart", "zdm.desktop")


def _linux_enable(app_dir: str, py: str) -> None:
    p = _linux_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    # runtime.autostart_exec() knows whether we are a frozen bundle (one
    # executable) or a source tree (interpreter + zdm.py).
    content = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=ZDM Service\n"
        "Comment=ZDM 下载服务后台常驻\n"
        f"Exec={runtime.autostart_exec('start', '--daemon', '--no-open')}\n"
        "Icon=zdm\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
        "NoDisplay=true\n"
    )
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)


def _linux_disable() -> None:
    try:
        os.remove(_linux_path())
    except OSError:
        pass


# ----------------------------------------------------------------- macOS
def _mac_plist_path() -> str:
    return os.path.join(
        os.path.expanduser("~"), "Library", "LaunchAgents", "com.zdm.service.plist"
    )


_MAC_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.zdm.service</string>
  <key>ProgramArguments</key>
  <array>
{program_args}
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>{app_dir}/zdm.log</string>
  <key>StandardErrorPath</key><string>{app_dir}/zdm.log</string>
</dict>
</plist>
"""


def _mac_enable(app_dir: str, py: str) -> None:
    p = _mac_plist_path()
    # The bundle has no zdm.py on disk; runtime.cli_cmd() already collapses the
    # interpreter + script pair into the bundle executable when frozen.
    program_args = "\n".join(
        f"    <string>{a}</string>" for a in runtime.cli_cmd("serve")
    )
    with open(p, "w", encoding="utf-8") as f:
        f.write(_MAC_PLIST.format(program_args=program_args, app_dir=app_dir))
    subprocess.run(["launchctl", "load", "-w", p], capture_output=True)


def _mac_disable() -> None:
    p = _mac_plist_path()
    subprocess.run(["launchctl", "unload", "-w", p], capture_output=True)
    try:
        os.remove(p)
    except OSError:
        pass


# ----------------------------------------------------------------- Windows
def _win_reg_key() -> str:
    return r"Software\Microsoft\Windows\CurrentVersion\Run"


def _win_startup_lnk() -> str:
    sp = os.environ.get("APPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Roaming"
    )
    return os.path.join(
        sp, "Microsoft", "Windows", "Start Menu", "Programs", "Startup", "ZDM Service.lnk"
    )


def _win_enabled() -> bool:
    try:
        import winreg

        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _win_reg_key())
        try:
            winreg.QueryValueEx(key, "ZDM")
            winreg.CloseKey(key)
            return True
        except OSError:
            winreg.CloseKey(key)
    except Exception:
        pass
    return os.path.exists(_win_startup_lnk())


def _win_cmd_list() -> list:
    """``serve`` invocation, using pythonw.exe for a source install (no console)."""
    cmd = runtime.cli_cmd("serve")
    if not runtime.is_frozen():
        pyw = os.path.join(os.path.dirname(cmd[0]), "pythonw.exe")
        if os.path.exists(pyw):
            cmd[0] = pyw
    return cmd


def _win_enable(app_dir: str, py: str) -> None:
    try:
        import winreg

        cmd = subprocess.list2cmdline(_win_cmd_list())
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _win_reg_key(), 0, winreg.KEY_SET_VALUE
        )
        winreg.SetValueEx(key, "ZDM", 0, winreg.REG_SZ, cmd)
        winreg.CloseKey(key)
    except Exception:
        # Fall back to a Startup shortcut via PowerShell if winreg is unavailable.
        try:
            lnk = _win_startup_lnk()
            os.makedirs(os.path.dirname(lnk), exist_ok=True)
            target = _win_cmd_list()
            ps = (
                "$ws=New-Object -ComObject WScript.Shell;"
                f"$l=$ws.CreateShortcut('{lnk}');"
                f"$l.TargetPath='{target[0]}';$l.Arguments='{subprocess.list2cmdline(target[1:])}';"
                f"$l.WorkingDirectory='{app_dir}';$l.WindowStyle=7;$l.Save()"
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps], capture_output=True
            )
        except Exception:
            pass


def _win_disable() -> None:
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _win_reg_key(), 0, winreg.KEY_SET_VALUE
        )
        try:
            winreg.DeleteValue(key, "ZDM")
        except OSError:
            pass
        winreg.CloseKey(key)
    except Exception:
        pass
    try:
        os.remove(_win_startup_lnk())
    except OSError:
        pass


# ----------------------------------------------------------------- public
def is_enabled() -> bool:
    if IS_WIN:
        return _win_enabled()
    if IS_MAC:
        return os.path.exists(_mac_plist_path())
    return os.path.exists(_linux_path())


def enable() -> None:
    app_dir, py = _app_dir(), _python()
    if IS_WIN:
        _win_enable(app_dir, py)
    elif IS_MAC:
        _mac_enable(app_dir, py)
    else:
        _linux_enable(app_dir, py)


def disable() -> None:
    if IS_WIN:
        _win_disable()
    elif IS_MAC:
        _mac_disable()
    else:
        _linux_disable()


def set_enabled(on: bool) -> None:
    if on:
        enable()
    else:
        disable()
