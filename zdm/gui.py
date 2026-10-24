"""ZDM desktop client (PyQt5). Talks to the local ZDM service over its REST API.

Run directly with any python3 that has PyQt5:
    /usr/bin/python3 ~/.local/share/zdm/zdm/gui.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from html import escape as _html_escape

# Make the bundled ``zdm`` package importable when run from its install dir.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from zdm import __version__ as VERSION  # noqa: E402
from zdm import autostart, desktop, runtime, update  # noqa: E402
from zdm.videopage import is_video_page, is_youtube  # noqa: E402
import theme as _theme  # noqa: E402

# ---- 自动检查更新的节奏 -----------------------------------------------------
# 桌面端是常驻托盘的，用户可能几天不重启一次；只在「启动后 2 秒」查一次，
# 等于发版之后永远没人知道 —— 用户的实际感受就是「得亲手点一下检查更新」。
# 改成：启动时查一次 + 后台每 4 小时复查；失败按 10min → 30min → 1h 退避
# （api.github.com 未登录限流 60 次/小时，不能失败就立刻重试）。
UPDATE_RECHECK_INTERVAL = 4 * 3600.0    # 成功问到版本后的下次检查间隔
UPDATE_STARTUP_GAP = 20 * 60.0          # 启动时距上次检查不足此值就先跳过
UPDATE_RETRY_MIN = 10 * 60.0            # 首次失败后的重试间隔
UPDATE_RETRY_MAX = 60 * 60.0            # 退避上限
UPDATE_TICK_MS = 5 * 60 * 1000          # 后台 tick（只判断「到期没有」）

# Global theme manager. Created lazily in main() (it needs the QApplication),
# but these helpers let widgets pull the active palette without depending on it.
_THEME_MANAGER = None


def theme_manager():
    """Return the process-wide ThemeManager (None before main() sets it up)."""
    return _THEME_MANAGER


def palette():
    """Active colour palette for the current light/dark mode."""
    # A live preview from the Settings dialog takes precedence until saved.
    app = _THEME_MANAGER.qapp if _THEME_MANAGER is not None else None
    win = getattr(app, "_main_window", None) if app is not None else None
    if win is not None:
        preview = getattr(win, "_theme_preview", None)
        if preview == "dark":
            return _theme.DARK
        if preview == "light":
            return _theme.LIGHT
    if _THEME_MANAGER is not None:
        return _THEME_MANAGER.palette()
    return _theme.LIGHT if not _theme.system_prefers_dark() else _theme.DARK


def build_style_sheet():
    return _theme.build_stylesheet(palette())


APP_NAME = "ZDM"
APP_TITLE = "ZDM 下载管理器"
ICON = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "extension", "icons", "icon128.png"
)
if not os.path.exists(ICON):
    ICON = os.path.join(
        os.path.expanduser("~/.local/share/icons/hicolor/128x128/apps"), "zdm.png"
    )


# ---------------------------------------------------------------------------
# Desktop-only preferences.
#
# The service owns everything download related, but a few window-behaviour
# choices have nowhere to live there, so they are stored client-side next to
# the dock log.
# ---------------------------------------------------------------------------
# 三端各有各的「用户配置」位置：Windows 必须落在 %APPDATA%，硬编码
# ~/.config 会写到 C:\Users\X\.config\zdm\（能用，但不是该放的地方，
# 备份/清理工具都扫不到）。macOS/Linux 保持 ~/.config 不变，避免已经在
# 那里存过的列宽设置「莫名丢失」。
if os.name == "nt":
    _GUI_CFG_PATH = os.path.join(
        os.environ.get("APPDATA") or os.path.expanduser("~"), "zdm", "gui.json"
    )
else:
    _GUI_CFG_PATH = os.path.join(os.path.expanduser("~/.config/zdm"), "gui.json")


def _gui_cfg_all() -> dict:
    try:
        with open(_GUI_CFG_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _gui_cfg(key: str, default=None):
    return _gui_cfg_all().get(key, default)


def _gui_cfg_set(**kwargs) -> None:
    data = _gui_cfg_all()
    data.update(kwargs)
    try:
        os.makedirs(os.path.dirname(_GUI_CFG_PATH), exist_ok=True)
        with open(_GUI_CFG_PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except Exception:
        pass

STATUS_TEXT = {
    "queued": "排队中",
    "downloading": "下载中",
    "paused": "已暂停",
    "completed": "已完成",
    "error": "失败",
    "canceled": "已取消",
    "merging": "合并中",
}

_DOCK_LOG = None  # resolved lazily by _dock_log_file()


def _dock_log_file() -> str:
    """Path of the diagnostic log (Dock / tray troubleshooting).

    Preferred location is the per-user config dir, so the log survives app
    upgrades and never pollutes the .app bundle itself (writing into
    ``Contents/Resources`` also invalidates the code signature). Only when that
    is unwritable do we fall back to the directory next to gui.py. Never raises:
    a broken log must not break the app.
    """
    global _DOCK_LOG
    if _DOCK_LOG:
        return _DOCK_LOG
    candidates = []
    try:
        from zdm.manager import config_dir

        candidates.append(os.path.join(config_dir(), "dock.log"))
    except Exception:
        pass
    try:
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "dock.log"))
    except Exception:
        pass
    for path in candidates:
        try:
            with open(path, "a", encoding="utf-8"):
                pass
            _DOCK_LOG = path
            return path
        except Exception:
            continue
    _DOCK_LOG = os.devnull
    return _DOCK_LOG


def _dlog(msg: str) -> None:
    """Append a diagnostic line (Dock/window troubleshooting)."""
    try:
        with open(_dock_log_file(), "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass


def _safe_print(msg: str) -> None:
    """print() that never raises, even with a non-UTF8 stdout or a closed pipe.

    When the app is launched from Finder (no TTY) the interpreter may still be
    configured with the ASCII codec; a bare print() of Chinese text would then
    raise UnicodeEncodeError and abort the caller.
    """
    try:
        print(msg)
    except Exception:
        try:
            sys.stdout.write(msg.encode("utf-8", "replace").decode("ascii", "replace") + "\n")
        except Exception:
            pass


def app_is_active() -> bool:
    """True when ZDM itself is the frontmost application.

    Qt's ``applicationState()`` on macOS can keep reporting
    ``Qt.ApplicationActive`` while another app is actually in front (verified by
    driving AppKit directly), so on macOS we ask Cocoa instead of trusting it.
    Getting this wrong makes ``toggle_window()`` hide a window the user wanted
    brought forward.
    """
    if sys.platform == "darwin":
        try:
            from AppKit import NSApplication

            return bool(NSApplication.sharedApplication().isActive())
        except Exception:
            pass
    app = QApplication.instance()
    return app is not None and app.applicationState() == Qt.ApplicationActive


def activate_app() -> None:
    """Bring the ZDM process to the front (macOS only, no-op elsewhere).

    Why this is needed: clicking our menu-bar (status item) icon does NOT make
    the app active. macOS renders the menu of an inactive app greyed out, and the
    menu is not key, so clicks do nothing - exactly the "文字全灰、点不了，先点
    一下主页面才能用" bug.

    Which API: ``-[NSApplication activate]`` (macOS 14+) only works when the app
    already has "user activation", which a status-item click does not grant -
    measured: it silently does nothing here. ``activateIgnoringOtherApps:`` is the
    deprecated-but-working variant and does flip NSApp.isActive() (verified from
    inside the bundle), so it is what we use.

    Ordering matters: activate BEFORE showing anything. Forcing the activation
    after the window is already up can leave it visible but non-interactive
    (a half-activated state), which is a different bug we hit before.
    """
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSApplication

        ns = NSApplication.sharedApplication()
        try:
            ns.activateIgnoringOtherApps_(True)
        except Exception:
            try:
                ns.activate()  # in case the old selector is ever removed
            except Exception:
                pass
    except Exception as e:  # noqa: BLE001
        _dlog(f"[activate] failed: {type(e).__name__}: {e}")


def default_port() -> int:
    home = os.path.expanduser("~")
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
        path = os.path.join(base, "ZDM", "state.json")
    elif sys.platform == "darwin":
        path = os.path.join(home, "Library", "Application Support", "ZDM", "state.json")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
        path = os.path.join(base, "zdm", "state.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return int(json.load(f).get("config", {}).get("port") or 9000)
    except Exception:
        return 9000


def _num(v, default: float = 0.0) -> float:
    """把任务字段转成数字。字段来自 REST 响应，理论上类型正确，
    但旧版客户端或写坏的状态文件可能塞了字符串，float() 会直接抛
    ValueError 把整个表格渲染带崩。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def fmt_size(n) -> str:
    n = _num(n)
    if n < 1024:
        return f"{int(n)} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024
        if n < 1024:
            return f"{n:.1f} {unit}"
    return f"{n:.1f} PB"


def fmt_pair(downloaded, total) -> str:
    """「已下载 / 总大小」，重复的单位只留一份。

    「100.8 / 161.6 MB」比「100.8 MB / 161.6 MB」省 30 多像素 —— 大小列窄了以后
    这点宽度正好够一行放下，不用折行。
    """
    got, ttl = fmt_size(downloaded), fmt_size(total)
    try:
        g_val, g_unit = got.split(" ", 1)
        _t_val, t_unit = ttl.split(" ", 1)
    except ValueError:
        return f"{got} / {ttl}"
    return f"{g_val} / {ttl}" if g_unit == t_unit else f"{got} / {ttl}"


def fmt_eta(s) -> str:
    s = _num(s)
    if not s or s <= 0:
        return ""
    s = int(s)
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    if h:
        return f"{h}时{m}分"
    if m:
        return f"{m}分{sec}秒"
    return f"{sec}秒"


def fmt_clock(ts) -> str:
    """Unix 时间戳 → 「几点」显示。今天的只给 时:分，更早的带上日期。"""
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    lt = time.localtime(ts)
    now = time.localtime()
    if (lt.tm_year, lt.tm_yday) == (now.tm_year, now.tm_yday):
        return time.strftime("%H:%M", lt)
    return time.strftime("%m-%d %H:%M", lt)


# 来源徽标：只给非「手动」的任务打，颜色取自主题调色板（不在代码里写死，
# 深浅色切换时才不会留下一块异色）。见 _render() 里的用法。
SOURCE_BADGES = {
    "browser": ("浏览器", "status_downloading"),
    "sniffer": ("嗅探", "status_merging"),
    # 播放界面右上角「下载此视频」推来的：和嗅探面板里点的「推送」是两条
    # 不同的入口，混成一个标签会让人以为是从嗅探推的。
    "player": ("播放器", "status_downloading"),
    "webui": ("网页版", "status_queued"),
    "protocol": ("协议", "status_queued"),
}


def _esc(s) -> str:
    """HTML 转义。文件名来自远端（可能是网页上的标题），必须转义后才能进
    QLabel 的富文本，否则尖括号会被当成标签解析。

    现在文件名走 :class:`ElidedLabel`（纯文本 + 省略号），不再需要富文本，
    但设置页等处仍在用这个转义，留着。
    """
    return _html_escape(str(s if s is not None else ""), quote=True)


class Api:
    """Tiny REST client for the local service (proxy bypassed on purpose)."""

    def __init__(self, port: int):
        self.port = port
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _req(self, method: str, path: str, body=None, timeout: float = 8.0):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/{path.lstrip('/')}", data=data, method=method
        )
        req.add_header("Content-Type", "application/json")
        try:
            with self._opener.open(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                return json.loads(raw)
            except Exception:
                return {"ok": False, "error": raw}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def state(self):
        return self._req("GET", "state")

    def extension(self):
        """「浏览器扩展」对话框用：扩展最近有没有心跳。"""
        return self._req("GET", "extension", timeout=3.0)

    def youtube(self, url: str, cookie: str = ""):
        """解析 YouTube 直链（服务端跑 yt-dlp，可能要几秒）。"""
        return self._req(
            "POST", "youtube/resolve", {"url": url, "cookie": cookie}, timeout=120.0
        )

    def add(self, payload=None, **kw):
        """Create a task.

        Accept either a ready-made payload dict (the call sites in this module
        pass ``{"url": ...}`` positionally) or keyword arguments.
        """
        body = dict(payload or {})
        body.update(kw)
        return self._req("POST", "tasks", body)

    def action(self, task_id: str, action: str):
        return self._req("POST", f"tasks/{task_id}/{action}", {}, timeout=30.0)

    def remove(self, task_id: str, keep_files: bool = False):
        return self._req("DELETE", f"tasks/{task_id}{'?files=0' if keep_files else ''}")

    def bulk(self, action: str):
        return self._req("POST", "action", {"action": action})

    def set_config(self, patch: dict):
        return self._req("POST", "config", patch)

    def open_path(self, path: str):
        return self._req("POST", "open", {"path": path})

    def open_file(self, path: str):
        """Ask the service to open the file with its default application."""
        return self._req("POST", "open", {"path": path, "mode": "file"})

    def reveal(self, path: str):
        """Ask the service to show the file inside its folder."""
        return self._req("POST", "open", {"path": path, "mode": "reveal"})


# --------------------------------------------------------------------------- Qt
from PyQt5.QtCore import (  # noqa: E402
    QEvent,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
    QThread,
    QTimer,
    QUrl,
    pyqtSignal,
)
from PyQt5.QtNetwork import QLocalServer, QLocalSocket  # noqa: E402
from PyQt5.QtGui import (  # noqa: E402
    QColor,
    QCursor,
    QDesktopServices,
    QFont,
    QIcon,
    QPainter,
    QPixmap,
    QPolygonF,
)
from PyQt5.QtWidgets import (  # noqa: E402
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStatusBar,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QVBoxLayout,
    QWidget,
)


class _ZDMReopenDelegate:
    """Placeholder retained for backwards compatibility.

    The real delegate is built by :func:`_build_reopen_delegate_class`. We DO
    replace the NSApplication delegate (Qt installs its own
    ``QCocoaApplicationDelegate`` - verified on Qt 5.15.19, and it responds to
    ``applicationShouldHandleReopen:``), but the replacement keeps a reference
    to Qt's delegate and forwards every selector it does not implement, so
    Qt's native entry points (openFiles:, openURLs:, termination, dock menu)
    keep working.
    """

    _zdm_target = None
    _zdm_orig = None


def _build_reopen_delegate_class():
    """Build the NSObject subclass that handles applicationShouldHandleReopen:.

    Qt installs ``QCocoaApplicationDelegate`` as soon as the QApplication
    exists, and that delegate does implement the reopen selector - but its
    implementation does not restore a window the user hid to the tray, which
    is why ZDM needs its own. Because we swap the delegate out, the class also
    implements ``forwardingTargetForSelector_:`` and hands every *other*
    selector to the delegate we replaced; without that, replacing the delegate
    silently drops Qt's native behaviour (file-open requests, URL schemes,
    termination confirmation, ...).
    """
    try:
        import objc  # noqa: F401  (PyObjC must be importable for NSObject)
        from AppKit import NSObject
    except Exception:
        return None

    class _Delegate(NSObject):
        def applicationShouldHandleReopen_hasVisibleWindows_(self, sender, flag):
            _dlog(f"[reopen] Dock click received (visibleWindows={flag})")
            target = self._zdm_target
            if target is not None:
                try:
                    # Restore on the next runloop turn so Cocoa finishes the reopen.
                    from PyQt5.QtCore import QTimer
                    QTimer.singleShot(0, lambda: target._maybe_restore("cocoaReopen"))
                except Exception:
                    try:
                        target._maybe_restore("cocoaReopen")
                    except Exception:
                        pass
            return True

        def forwardingTargetForSelector_(self, sel):
            """Route everything we do not implement to Qt's own delegate."""
            orig = self._zdm_orig
            if orig is not None and orig.respondsToSelector_(sel):
                return orig
            return None

    return _Delegate


_ZDMReopenDelegate = None
if sys.platform == "darwin":
    try:
        _ZDMReopenDelegate = _build_reopen_delegate_class()
    except Exception:
        _ZDMReopenDelegate = None


class ZDMApplication(QApplication):
    """QApplication that brings the main window back when the Dock icon is clicked.

    On macOS, clicking the Dock icon of an app whose only window was hidden (e.g.
    closed to the tray) is delivered by Cocoa as `applicationShouldHandleReopen:`
    and does NOT reliably surface as a Qt `applicationStateChanged` / `focusChanged`
    signal (especially when the app is already the frontmost app but has no visible
    window). Relying on those signals alone therefore misses the Dock click.

    A robust, event-agnostic approach: poll the combination
    "window not visible AND application is active". When a user clicks the Dock
    icon, macOS makes the app active; if our window is hidden we bring it back.
    We keep the signal handlers as a fast path, but the poller is the guarantee.
    """

    def __init__(self, argv):
        super().__init__(argv)
        self._main_window = None
        self._theme_hooks = []

    def add_theme_hook(self, fn):
        """Register a callback invoked after the light/dark palette changes."""
        self._theme_hooks.append(fn)

    def notify_theme_changed(self):
        """Let colour-aware widgets (progress bars, table text) re-read colours."""
        for fn in list(self._theme_hooks):
            try:
                fn()
            except Exception:
                pass
        win = self._main_window
        if win is not None:
            try:
                win.refresh_theme()
            except Exception:
                pass
        # NOTE: never touch self._reopen_delegate / self._active_observer here.
        # NSApplication keeps an *unretained* delegate reference, so dropping our
        # Python reference on a theme switch would leave Cocoa pointing at a
        # freed object (Dock click stops working, or the app crashes).

    def set_main_window(self, win):
        self._main_window = win
        # Fast-path: react to Qt activation signals if the platform emits them.
        self.applicationStateChanged.connect(self._on_state_changed)
        # Primary (macOS): observe NSApplicationDidBecomeActiveNotification. Clicking
        # the Dock icon always makes the app active, so this reliably catches the
        # "reopen with no visible window" case without touching Qt's own delegate.
        self._install_macos_reopen_hook(win)

    def _install_macos_reopen_hook(self, win):
        """Restore the window when the Dock icon is clicked (macOS).

        Two mechanisms, in order of reliability:

        1. our own ``applicationShouldHandleReopen:`` delegate. Qt installs
           ``QCocoaApplicationDelegate`` (verified on Qt 5.15.19) and that
           delegate also answers reopen - but it does not restore a window the
           user hid to the tray. So we swap the delegate out and *forward*
           every selector we do not implement back to Qt's delegate, which
           keeps its openFiles:/openURLs:/termination behaviour intact;
        2. a ``NSApplicationDidBecomeActiveNotification`` observer as a
           backstop (cmd-tab, foreground switches).
        """
        if sys.platform != "darwin":
            return
        # 1) Primary mechanism: install our own Cocoa delegate that implements
        #    applicationShouldHandleReopen:hasVisibleWindows: - the canonical
        #    "user clicked the Dock icon while the app has no visible windows"
        #    callback. Qt's delegate implements it too but does not restore a
        #    tray-hidden window, so we swap the delegate out - and keep Qt's
        #    one reachable through forwardingTargetForSelector_ so none of its
        #    other native entry points are lost.
        if _ZDMReopenDelegate is not None:
            try:
                from AppKit import NSApplication
                ns = NSApplication.sharedApplication()
                d = _ZDMReopenDelegate.alloc().init()
                d._zdm_target = self
                d._zdm_orig = ns.delegate()  # Qt's QCocoaApplicationDelegate
                ns.setDelegate_(d)
                self._reopen_delegate = d
                _dlog("[init] reopen delegate installed (forwarding to Qt's)")
            except Exception as e:
                _dlog(f"[init] delegate failed: {type(e).__name__}: {e}")

        # 2) Backstop: also observe DidBecomeActive (fires on cmd-tab and when the
        #    app transitions from background to foreground).
        try:
            # NOTE on naming (both of these were wrong before and silently broke
            # the observer because the AttributeError was swallowed):
            #   - the selector is...usingBlock_ (not ...handler_)
            #   - the notification name is the MODULE-LEVEL constant
            #     NSApplicationDidBecomeActiveNotification (not a method on
            #     NSApplication)
            from AppKit import NSApplicationDidBecomeActiveNotification
            from Foundation import NSNotificationCenter, NSOperationQueue

            def _on_active(note):
                _dlog("[didBecomeActive] fired")
                # Defer slightly so the activation state settles before showing.
                QTimer.singleShot(60, lambda: self._maybe_restore("didBecomeActive"))

            center = NSNotificationCenter.defaultCenter()
            # NOTE: the correct PyObjC selector is ...usingBlock_ (not ...handler_).
            # Getting this name wrong raises AttributeError, which we would otherwise
            # silently swallow -> the observer never gets installed and the Dock
            # click never restores the window.
            token = center.addObserverForName_object_queue_usingBlock_(
                NSApplicationDidBecomeActiveNotification,
                None,
                NSOperationQueue.mainQueue(),
                lambda n: _on_active(n),
            )
            self._active_observer = token
            _dlog("[init] observer installed")
        except Exception as e:
            _dlog(f"[init-FAILED] {type(e).__name__}: {e}")
            self._active_observer = None

    def _on_state_changed(self, state):
        # Qt.ApplicationActive == 2
        _dlog(f"[stateChanged] state={state}")
        if state != 2:
            return
        # On macOS a Dock click is delivered as an app activation, so the
        # window has to be brought back by hand. On Linux/Windows the window
        # manager re-maps it itself; reacting to every activation flip here
        # only steals focus from whatever the user is typing into.
        if sys.platform != "darwin":
            _dlog("[stateChanged] ignored (non-macOS: WM restores the window)")
            return
        self._maybe_restore("stateChanged")

    def _maybe_restore(self, _tag="?"):
        win = self._main_window
        if win is None:
            return
        # We activate the app on purpose right before popping the tray menu;
        # that activation must not also yank the window on screen.
        if getattr(win, "_suppress_restore_until", 0.0) > time.time():
            _dlog(f"[restore:{_tag}] suppressed (tray menu opening)")
            return
        # The user minimized the window from the title bar (or the WM). Honour
        # that: without this guard every ApplicationActive flip yanked the
        # window back on screen, so "minimize" looked like a no-op.
        if getattr(win, "_user_minimized", False):
            _dlog(f"[restore:{_tag}] skipped (minimized by user)")
            return
        # Same for a window the user closed to the tray on purpose - otherwise
        # the next activation flipped it straight back open.
        if getattr(win, "_user_hidden", False):
            _dlog(f"[restore:{_tag}] skipped (hidden by user)")
            return
        # NOTE: on macOS, QWidget.isVisible() only reflects "not explicitly hidden"
        # and flips to True as soon as show() is called -- even if the window has
        # not actually been mapped to the screen yet. Trusting isVisible() here
        # made us skip raising/activating a window that the user could see but
        # not interact with (greyed out). We therefore treat a visible-but-not-
        # active window as "needs restoring" too.
        visible = win.isVisible()
        minimized = win.isMinimized()
        is_active = app_is_active()
        is_key = win.isActiveWindow()
        _dlog(f"[restore:{_tag}] visible={visible} minimized={minimized} "
              f"active={is_active} key={is_key}")
        try:
            if minimized:
                win.setWindowState(
                    (win.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive
                )
            elif visible and is_active and is_key:
                # Already on screen, frontmost and focused - nothing to do.
                return
            # Guard: don't force the window up while a modal dialog (e.g. the
            # settings / new-download sheet) is the active window.
            if self.activeModalWidget() is not None:
                return
            win.show()
            win.setWindowState(
                (win.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive
            )
            win.raise_()
            win.activateWindow()
            # NOTE: deliberately NOT calling
            # NSApplication.activateIgnoringOtherApps_(True) here. Forcing the app
            # forward that way bypasses the normal macOS activation flow and can
            # leave the window visible but greyed out / non-interactive, which is
            # exactly the bug we hit. Qt's own activateWindow() is sufficient.
        except Exception:
            pass


class PollWorker(QThread):
    tick = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, api: Api, interval: float = 0.8):
        super().__init__()
        self.api = api
        self.interval = interval
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        while not self._stop:
            st = self.api.state()
            if st.get("ok"):
                self.tick.emit(st)
            else:
                self.failed.emit(st.get("error", "服务未响应"))
            for _ in range(int(self.interval / 0.1)):
                if self._stop:
                    return
                self.msleep(100)


class OpWorker(QThread):
    done = pyqtSignal(str, object)

    def __init__(self, fn, *args):
        super().__init__()
        self.fn = fn
        self.args = args

    def run(self):
        try:
            self.done.emit("ok", self.fn(*self.args))
        except Exception as e:  # noqa: BLE001
            self.done.emit("err", str(e))


# The stylesheet is no longer a fixed string: it is rendered from the active
# theme palette (see theme.py) so light and dark mode stay perfectly in sync.
# `build_style_sheet()` is the single source of truth; nothing here hardcodes a
# colour, which is what keeps the two themes visually consistent.
STYLE = _theme.build_stylesheet(_theme.LIGHT)


class ElidedLabel(QLabel):
    """单行文本，宽度不够时用「…」省略，而不是硬裁掉半个字。

    QLabel 默认是把超出部分直接裁掉（连省略号都没有），看着就像名字被切断了。
    这里自己画：``elidedText`` 会保证结尾有省略号，悬停还能看到全名。
    """

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._full = ""
        # Ignored 让标签可以被压得很窄 —— 否则 QLabel 的最小宽度等于文本宽度，
        # 表格列一窄，整列就顶住不让缩，文件名反而更看不全。
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(0)
        self.setText(text)

    def setText(self, text):  # noqa: N802 - Qt 命名
        self._full = text or ""
        super().setText(self._full)
        self.setToolTip(self._full)
        self.update()

    def fullText(self) -> str:
        return self._full

    def paintEvent(self, _e):
        p = QPainter(self)
        fm = p.fontMetrics()
        elided = fm.elidedText(self._full, Qt.ElideRight, max(0, self.width()))
        p.setPen(self.palette().color(self.foregroundRole()))
        p.drawText(self.rect(), Qt.AlignLeft | Qt.AlignVCenter, elided)


class ExtensionDialog(QDialog):
    """「浏览器扩展」引导：检测扩展是否已加载 + 一键打开扩展目录。

    扩展没法上架 Chrome 应用商店（它要访问 127.0.0.1 才能工作，审核基本
    过不了），所以每台新电脑上都要手动「加载已解压的扩展程序」——这是
    新用户最容易卡住的一步。这个对话框把三件事放到一个页面：

    * **装没装**：不读注册表、不猜浏览器配置目录，反过来听——扩展每次
      被唤醒都会向本机服务报一个到（``/api/extension/ping`` 心跳），
      服务记下最近一次时间，这里轮询 ``/api/extension`` 即可；
    * **目录在哪**：:func:`zdm.runtime.extension_dir` 给出真实磁盘路径，
      一键打开（选「加载已解压的扩展程序」时要的就是它）；
    * **怎么装**：三步说明。

    三端（macOS / Windows / Linux）跑的是同一份 PyQt5 代码，这里天然覆盖。
    """

    def __init__(self, api: Api, parent=None):
        super().__init__(parent)
        self.setWindowTitle("浏览器扩展")
        self.setMinimumWidth(600)
        self.api = api
        self._worker = None  # 防止轮询堆叠

        v = QVBoxLayout(self)
        v.setSpacing(10)

        self.status = QLabel("正在检测…")
        self.status.setWordWrap(True)
        v.addWidget(self.status)

        tips = QLabel(
            "尚未安装？按下面三步加载（Chrome / Edge 通用）：\n"
            "  1. 浏览器地址栏打开 chrome://extensions，右上角开启「开发者模式」\n"
            "  2. 点「加载已解压的扩展程序」，选择下面的扩展目录\n"
            "  3. 弹出「未列在 Chrome 网上应用商店」时点「继续」\n"
            "加载后自动生效；ZDM 升级后回到 chrome://extensions 点一下刷新即可。"
        )
        tips.setWordWrap(True)
        v.addWidget(tips)

        ext_dir = runtime.extension_dir()
        dir_title = QLabel("扩展目录：")
        v.addWidget(dir_title)
        dir_row = QWidget()
        dh = QHBoxLayout(dir_row)
        dh.setContentsMargins(0, 0, 0, 0)
        self.dir_label = ElidedLabel(ext_dir)
        self.dir_label.setToolTip(ext_dir)
        dh.addWidget(self.dir_label, 1)
        btn_open = QPushButton("打开目录")
        btn_open.clicked.connect(self._open_ext_dir)
        dh.addWidget(btn_open)
        btn_copy = QPushButton("复制路径")
        btn_copy.clicked.connect(self._copy_path)
        dh.addWidget(btn_copy)
        v.addWidget(dir_row)

        btns = QDialogButtonBox(QDialogButtonBox.Close)
        # 冻结包里 Qt 的翻译文件不一定能加载（CI 打包环境差异，Windows 实测
        # 出现过英文 Save/Cancel），标准按钮直接写中文，三端一致。
        btns.button(QDialogButtonBox.Close).setText("关闭")
        # Close 按钮在 Qt 里挂 RejectRole（部分版本是 AcceptRole），两边都连
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        v.addWidget(btns)

        self._timer = QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()
        self._refresh()

    # 对话框关掉后别再每 2 秒发一次请求（也少一条崩溃路径）：定时器只在
    # 可见期间运行，重新打开时 _extensions() 会再 start()。
    def hideEvent(self, e):  # noqa: N802 - Qt 命名
        self._timer.stop()
        super().hideEvent(e)

    def showEvent(self, e):  # noqa: N802 - Qt 命名
        super().showEvent(e)
        if not self._timer.isActive():
            self._timer.start()
        self._refresh()

    # ---- 状态轮询：网络请求走 OpWorker 线程，不卡 UI ----
    def _refresh(self):
        if self._worker is not None:
            return
        w = OpWorker(self.api.extension)
        self._worker = w
        w.done.connect(self._on_status)
        # **必须等线程真正结束再丢引用**。done 是在 run() 里发出的，那一刻
        # 线程还在跑；以前这里（_on_status 第一行）直接把 self._worker 置 None，
        # PyQt 随即析构 QThread → Qt 报 "QThread: Destroyed while thread is
        # still running" 并 abort。macOS 上实测就是这么「意外退出」的
        # （崩溃报告：QThread::~QThread ← sipQThread::~sipQThread）。
        w.finished.connect(lambda w=w: self._retire_worker(w))
        w.start()

    def _retire_worker(self, w):
        try:
            w.wait(3000)  # finished 只表示「即将结束」，wait 到完全退出才安全
        except Exception:  # noqa: BLE001
            pass
        if self._worker is w:
            self._worker = None

    def _on_status(self, tag: str, payload):
        if tag != "ok":
            self.status.setText(f"⚠️ 检测失败：{payload}")
            return
        r = payload if isinstance(payload, dict) else {}
        if not r.get("ok"):
            self.status.setText("⚠️ 本机服务未响应，无法检测扩展（服务可能没在运行）")
            return
        if r.get("seen_recently"):
            self.status.setText("✅ 已检测到浏览器扩展，工作正常。")
        elif r.get("last_seen"):
            self.status.setText(
                "✅ 浏览器扩展已连接过，最近一次活跃：{}。"
                "浏览器关闭后扩展休眠、不再心跳，属正常现象。".format(
                    fmt_clock(float(r["last_seen"]))
                )
            )
        else:
            self.status.setText(
                "❌ 未检测到浏览器扩展 —— 服务启动以来扩展从未连接过。"
                "如果还没装，按下面的步骤加载即可。"
            )

    def _open_ext_dir(self):
        d = runtime.extension_dir()
        if not os.path.isdir(d):
            QMessageBox.information(
                self, "扩展目录不存在",
                f"目录不存在：\n{d}\n\nZDM 安装可能不完整，请重新安装。",
            )
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(d))

    def _copy_path(self):
        d = runtime.extension_dir()
        QApplication.clipboard().setText(d)
        self.status.setText(f"已复制扩展路径：{d}")

    def closeEvent(self, e):  # noqa: N802 - Qt 命名
        self._timer.stop()
        super().closeEvent(e)


class NameCell(QWidget):
    """文件名 + 来源徽标。

    文件名用 :class:`ElidedLabel`（可省略），徽标单独一个 QLabel 固定在右侧 ——
    以前整串是富文本塞在一个标签里，列一窄就先被裁掉徽标、名字反而只剩几个字。
    """

    def __init__(self, task: dict, pal):
        super().__init__()
        self.setStyleSheet("background: transparent;")
        name = str(task.get("filename") or "")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 0, 6, 0)
        lay.setSpacing(6)

        self.name_label = ElidedLabel(name)
        f = QFont(self.name_label.font())
        f.setBold(True)
        self.name_label.setFont(f)
        lay.addWidget(self.name_label, 1)

        entry = SOURCE_BADGES.get(task.get("source") or "")
        if entry:
            label, attr = entry
            badge = QLabel(f"[{label}]")
            bf = QFont(badge.font())
            bf.setPointSizeF(max(8.0, bf.pointSizeF() - 1.0))
            badge.setFont(bf)
            badge.setStyleSheet(
                f"color: {getattr(pal, attr, pal.text_muted)}; background: transparent;"
            )
            lay.addWidget(badge, 0)

        url = task.get("url") or ""
        self.setToolTip(name + (("\n" + url) if url else ""))


class ProgressCell(QWidget):
    """Percent text above a slim rounded bar.

    Colours come from the active theme palette so the bar, the track and the
    label all follow light/dark mode automatically.
    """

    def __init__(self, pct: float, text: str, color: str = ""):
        super().__init__()
        self.pct = max(0.0, min(100.0, pct))
        self.text = text
        self.color = QColor(color or palette().status_downloading)
        self._palette = palette()
        self.setMinimumHeight(34)

    def apply_palette(self):
        """Re-read the palette after a theme switch and repaint."""
        self._palette = palette()
        self.setMinimumHeight(34)
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect().adjusted(8, 0, -8, 0)
        f = QFont(self.font())
        f.setPointSizeF(8.5)
        p.setFont(f)
        p.setPen(QColor(self._palette.text_muted))
        # 进度列现在窄了，「100.0% · 剩余 12分3秒」这种长文本要省略而不是硬裁。
        txt = p.fontMetrics().elidedText(self.text, Qt.ElideRight, r.width())
        p.drawText(QRect(r.left(), r.top() + 2, r.width(), 16), Qt.AlignLeft | Qt.AlignVCenter, txt)
        bar = QRect(r.left(), r.top() + 21, r.width(), 6)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(self._palette.track))
        p.drawRoundedRect(bar, 3, 3)
        w = int(bar.width() * self.pct / 100.0)
        if w > 0:
            p.setBrush(self.color)
            p.drawRoundedRect(QRect(bar.left(), bar.top(), max(4, w), bar.height()), 3, 3)


def glyph_icon(kind: str, size: int = 18, alpha: int = 255) -> QIcon:
    """Draw a pause / play glyph with the active palette.

    Qt's built-in media icons are black bitmaps that vanish on a dark toolbar,
    so we paint our own from ``palette().text`` - that keeps icon buttons legible
    in both themes, and ``refresh_theme()`` simply rebuilds them after a switch.

    ``alpha`` is used for the disabled state: QIcon has no Disabled pixmap here,
    so Qt would otherwise draw the normal glyph on a disabled button and it
    would look clickable.
    """
    pal = palette()
    ratio = 2  # crisp on retina displays
    pm = QPixmap(size * ratio, size * ratio)
    pm.setDevicePixelRatio(ratio)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    ink = QColor(pal.text)
    if alpha < 255:
        ink.setAlpha(alpha)
    p.setBrush(ink)
    s = float(size)
    if kind == "pause":
        w = s * 0.20
        p.drawRoundedRect(QRectF(s * 0.24, s * 0.17, w, s * 0.66), 1.6, 1.6)
        p.drawRoundedRect(QRectF(s * 0.56, s * 0.17, w, s * 0.66), 1.6, 1.6)
    elif kind == "play":
        p.drawPolygon(
            QPolygonF(
                [QPointF(s * 0.28, s * 0.16), QPointF(s * 0.80, s * 0.50),
                 QPointF(s * 0.28, s * 0.84)]
            )
        )
    p.end()
    return QIcon(pm)


GLASS_RADIUS = 12.0  # must match QMenu#glassMenu border-radius in theme.py


def _linux_composited() -> bool:
    """Whether the desktop composites ARGB windows (i.e. can do translucency).

    Without a compositing manager an ARGB popup shows black behind whatever we
    paint, so the fill has to become fully opaque. Probing ``_NET_WM_CM_S0``
    would need xcb bindings that PyQt5 does not expose portably; matching the
    session name is dependency-free and good enough - every mainstream desktop
    composites by default, XFCE/i3 without a compositor do not.
    """
    if sys.platform != "linux":
        return False
    if os.environ.get("WAYLAND_DISPLAY"):
        return True
    session = " ".join(
        filter(
            None,
            (
                os.environ.get("XDG_CURRENT_DESKTOP", ""),
                os.environ.get("DESKTOP_SESSION", ""),
            ),
        )
    ).lower()
    keys = ("gnome", "kde", "plasma", "unity", "cinnamon", "mate", "budgie",
            "pantheon", "deepin", "ukui", "pop")
    return any(k in session for k in keys)


class GlassMenu(QMenu):
    """Popup menu with a rounded, translucent "frosted" surface on Linux.

    macOS gets real vibrancy from ``NSVisualEffectView``; X11/Wayland expose no
    such API, so the closest honest thing is a translucent rounded panel that
    lets the desktop show through slightly. The hairline border, radius and item
    styling still come from ``QMenu#glassMenu`` in theme.py, so both platforms
    look like the same popup.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._translucent = _linux_composited()
        if self._translucent:
            self.setAttribute(Qt.WA_TranslucentBackground, True)

    def paintEvent(self, event):  # noqa: N802 - Qt override
        try:
            p = palette()
            fill = QColor(p.glass_fill)
            fill.setAlpha(int(p.glass_alpha) if self._translucent else 255)
            painter = QPainter(self)
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setPen(Qt.NoPen)
            painter.setBrush(fill)
            rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
            painter.drawRoundedRect(rect, GLASS_RADIUS, GLASS_RADIUS)
            painter.end()
        except Exception as e:  # noqa: BLE001 - never break the menu
            _dlog(f"[glass] paint failed: {type(e).__name__}: {e}")
        super().paintEvent(event)


def make_menu(parent=None) -> QMenu:
    """The popup type for this platform: vibrancy host vs. self-painted panel."""
    if sys.platform == "darwin":
        return QMenu(parent) if parent is not None else QMenu()
    return GlassMenu(parent)


def apply_menu_glass(menu: QMenu) -> None:
    """Give a popup menu the real macOS frosted-glass background.

    macOS menus are not just semi-transparent, they *blur* whatever is behind
    them (vibrancy). Qt has no API for that, so we drop a native
    ``NSVisualEffectView`` into the menu's own NSWindow and let the widget paint
    nothing but text on top of it.

    Two details decide whether it works at all:

    * the effect view must be a *sibling below* Qt's view, not a child - as a
      child it paints over the menu items and you get an empty grey slab;
    * the NSWindow must be non-opaque with a clear background, otherwise
      "behind window" blending samples the window's own (solid) fill and the
      result is flat grey instead of a blur.

    Failure is never fatal: without vibrancy the menu simply keeps its plain
    themed background.
    """
    if sys.platform != "darwin":
        return
    try:
        import objc
        from AppKit import (
            NSAppearance,
            NSColor,
            NSViewHeightSizable,
            NSViewWidthSizable,
            NSVisualEffectBlendingModeBehindWindow,
            NSVisualEffectMaterialPopover,
            NSVisualEffectStateActive,
            NSVisualEffectView,
            NSWindowBelow,
        )
    except Exception as e:  # pragma: no cover - only on a broken PyObjC install
        _dlog(f"[glass] PyObjC unavailable: {type(e).__name__}: {e}")
        return
    try:
        menu.setAttribute(Qt.WA_TranslucentBackground, True)
        wid = menu.winId()
        if not wid:
            return
        view = objc.objc_object(c_void_p=int(wid))
        win = view.window()
        if win is not None:
            win.setOpaque_(False)
            win.setBackgroundColor_(NSColor.clearColor())
        old = getattr(menu, "_glass_view", None)
        if old is not None:
            try:
                old.removeFromSuperview()
            except Exception:
                pass
        host = view.superview() or view
        eff = NSVisualEffectView.alloc().initWithFrame_(host.bounds())
        eff.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        eff.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
        eff.setMaterial_(NSVisualEffectMaterialPopover)
        eff.setState_(NSVisualEffectStateActive)
        # VibrantDark / VibrantLight is what makes the material (and therefore
        # the text contrast) match the app's light or dark theme.
        dark = _theme.DARK is palette()
        name = "NSAppearanceNameVibrantDark" if dark else "NSAppearanceNameVibrantLight"
        appearance = NSAppearance.appearanceNamed_(name)
        if appearance is not None:
            eff.setAppearance_(appearance)
        eff.setWantsLayer_(True)
        layer = eff.layer()
        if layer is not None:
            layer.setCornerRadius_(GLASS_RADIUS)
            layer.setMasksToBounds_(True)
        host.addSubview_positioned_relativeTo_(eff, NSWindowBelow, view)
        menu._glass_view = eff
    except Exception as e:  # noqa: BLE001 - cosmetic only, never break the menu
        _dlog(f"[glass] failed: {type(e).__name__}: {e}")


def style_glass_menu(menu: QMenu) -> None:
    """Mark a menu as a frosted popup and (re)apply the blur before each show.

    Both platforms share the ``QMenu#glassMenu`` styling: on macOS the blur is a
    native NSVisualEffectView behind a transparent window, on Linux GlassMenu
    paints the translucent fill itself.
    """
    menu.setObjectName("glassMenu")
    # The app-wide stylesheet already exists by now, so trigger a re-polish to
    # pick up the QMenu#glassMenu rule for this menu instance.
    try:
        menu.style().unpolish(menu)
        menu.style().polish(menu)
    except Exception:
        pass
    if sys.platform == "darwin":
        menu.setAttribute(Qt.WA_TranslucentBackground, True)
        menu.aboutToShow.connect(lambda: apply_menu_glass(menu))


class SingleApp:
    """Single-instance guard: a second launch just wakes the running window."""

    def __init__(self, key: str):
        self.already = False
        self.server = None
        self._sock = QLocalSocket()
        self._sock.connectToServer(key)
        if self._sock.waitForConnected(300):
            self.already = True
            return
        QLocalServer.removeServer(key)
        self.server = QLocalServer()
        self.server.listen(key)

    def on_message(self, fn):
        if self.server is not None:
            self.server.newConnection.connect(lambda: fn(self._read()))

    def _read(self) -> str:
        conn = self.server.nextPendingConnection() if self.server else None
        if conn is None:
            return ""
        try:
            conn.waitForReadyRead(800)
            data = bytes(conn.readAll()).decode("utf-8", "replace")
        finally:
            conn.close()
        return data

    def send(self, text: str) -> bool:
        if not self.already:
            return False
        self._sock.write(text.encode("utf-8"))
        ok = self._sock.waitForBytesWritten(800)
        self._sock.flush()
        return bool(ok)


class MainWindow(QMainWindow):
    def __init__(self, api: Api, port: int):
        super().__init__()
        self.api = api
        self.port = port
        self.tasks = []
        self.config = {}
        self._rows = {}
        self._workers = []
        self.autostart_action = None
        self.filter = "all"

        self.setWindowTitle(f"{APP_TITLE}  v{VERSION}")
        self.setWindowIcon(QIcon(ICON))
        self.resize(1080, 660)

        self._update_info = None
        self._update_checking = False
        # 下次允许自动检查的时间戳 / 失败退避当前值 / 后台 tick 定时器
        self._update_next_at = 0.0
        self._update_retry = UPDATE_RETRY_MIN
        self._update_timer = None
        self._update_notified = ""
        self._downloaded_update = None
        self._suppress_restore_until = 0.0

        self._build_ui()
        self._build_tray()
        self._start_polling()
        # Silent update check a moment after the window is up (never blocks).
        QTimer.singleShot(2200, self._auto_check_update)

    # ---------------- UI ----------------
    def _build_ui(self):
        tb = QToolBar()
        tb.setMovable(False)
        tb.setFloatable(False)
        self.addToolBar(tb)

        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("粘贴下载链接后回车，或把链接拖进窗口…")
        self.url_edit.setMinimumWidth(430)
        self.url_edit.returnPressed.connect(self._quick_add)
        _zh_standard_menu(self.url_edit)
        tb.addWidget(self.url_edit)

        btn_add = QPushButton("＋ 下载")
        btn_add.setObjectName("primary")
        # 工具栏输入框里有链接时直接走「粘贴即下载」：以前这里固定打开
        # 「新建下载」大对话框，视频页选完链接还要再弹一次清晰度——用户看到
        # 的就是「粘贴一个链接要关两个窗」。只有输入框和剪贴板都没有链接时
        # 才打开详细对话框。
        btn_add.clicked.connect(self._add_clicked)
        tb.addWidget(btn_add)

        # Per-task pause / resume live on each row (see _render); the global
        # "pause everything / resume everything" sit next to the filter chips in
        # plain Chinese, so the two levels never look alike.
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)

        for text, fn in (
            ("清除已完成", lambda: self._bulk("clear_finished")),
            ("打开网页版", self._open_web),
            ("浏览器扩展", self._extensions),
            ("打开目录", self._open_dir),
            ("设置", self._settings),
        ):
            b = QPushButton(text)
            b.clicked.connect(fn)
            tb.addWidget(b)

        tabs = QWidget()
        hl = QHBoxLayout(tabs)
        hl.setContentsMargins(2, 0, 2, 0)
        hl.setSpacing(4)
        self.filter_btns = {}
        for key, label in (("all", "全部"), ("downloading", "进行中"),
                           ("paused", "已暂停"), ("completed", "已完成"),
                           ("sniffer", "嗅探")):
            b = QPushButton(label)
            b.setCheckable(True)
            b.setFlat(True)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=key: self._set_filter(k))
            self.filter_btns[key] = b
            hl.addWidget(b)
        hl.addStretch(1)
        self.btn_pause_all = self._bulk_button(
            hl, "pause", "全部暂停", "暂停所有正在下载的任务", lambda: self._bulk("pause_all")
        )
        self.btn_resume_all = self._bulk_button(
            hl, "play", "全部继续", "继续所有已暂停的任务", lambda: self._bulk("resume_all")
        )
        self.filter_btns["all"].setChecked(True)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["文件", "大小", "进度", "速度", "状态", "时间", "操作"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(46)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(False)
        self.table.setShowGrid(False)
        self.table.setFocusPolicy(Qt.NoFocus)
        # 换行会让「100.8 MB / 161.6 MB」折成两行，把行撑高又更难读。
        # 放下不下的交给省略号。
        self.table.setWordWrap(False)
        hh = self.table.horizontalHeader()
        # 第 0 列（文件）吃满剩余空间；其余列 Interactive = 可以拖表头边界调整。
        # 之前 6 个固定列合计 962px，1080 的窗口里「文件」只剩一百来像素，
        # 长标题必然被截成「在黄河口，我」这样。现在固定列压到 ~790px，
        # 默认给文件列留出 ~270px，同时每一列都能手动拖宽/拖窄。
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        hh.setFixedHeight(38)
        hh.setHighlightSections(False)
        hh.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        # 不让任何一列被拖到看不见（文件列还有 Stretch 兜底）。
        hh.setMinimumSectionSize(56)
        # 先建好字典再设列宽：setColumnWidth 会同步发 sectionResized，
        # 回调里要往这个字典里写。
        self._col_widths = {}
        # 列宽按实测文本宽度定（QFontMetrics 量过）：
        #   大小 140 ← 「100.8 / 161.6 MB」
        #   进度 128 ← 「62.4% · 剩 40秒」+ 进度条
        #   速度 90  ← 「100.0 MB/s」
        #   状态 76  ← 「已完成」
        #   时间 172 ← 「14:03 → 预计 14:25」
        # 6 (the action column) must fit the pause glyph plus three text
        # buttons (34 + 50*3 + gaps + margins ~= 210).
        for col, w in ((1, 140), (2, 128), (3, 90), (4, 76), (5, 172), (6, 210)):
            hh.setSectionResizeMode(col, QHeaderView.Interactive)
            self.table.setColumnWidth(col, w)
        self._col_save_timer = QTimer(self)
        self._col_save_timer.setSingleShot(True)
        self._col_save_timer.timeout.connect(self._save_col_widths)
        hh.sectionResized.connect(self._on_section_resized)
        hh.setContextMenuPolicy(Qt.CustomContextMenu)
        hh.customContextMenuRequested.connect(self._col_menu)
        self._restore_col_widths()
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._menu)
        self.table.cellDoubleClicked.connect(lambda r, _c: self._open_selected_file(r))

        central = QWidget()
        v = QVBoxLayout(central)
        v.setContentsMargins(12, 10, 12, 6)
        v.setSpacing(8)
        v.addWidget(tabs)
        self.update_bar = self._build_update_bar()
        v.addWidget(self.update_bar)
        v.addWidget(self.table)
        self.setCentralWidget(central)

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("正在连接本地服务…")
        # Always-visible version number, bottom right.
        # 扩展连接状态：常驻主界面（以前只在「浏览器扩展」对话框里看得到）。
        # 点一下打开详情；后台每 5 秒问一次 /api/extension。
        self.ext_label = QLabel("扩展检测中…")
        self.ext_label.setObjectName("extStatus")
        self.ext_label.setCursor(Qt.PointingHandCursor)
        self.ext_label.setToolTip("浏览器扩展连接状态（点击查看详情）")
        self.ext_label.mousePressEvent = lambda _e: self._extensions()
        self.statusBar().addPermanentWidget(self.ext_label)
        self._ext_worker = None
        self._ext_timer = QTimer(self)
        self._ext_timer.setInterval(5000)
        self._ext_timer.timeout.connect(self._refresh_ext_status)
        self._ext_timer.start()
        QTimer.singleShot(2000, self._refresh_ext_status)

        self.version_label = QLabel(f"v{VERSION}")
        self.version_label.setObjectName("versionLabel")
        self.version_label.setToolTip(f"ZDM 桌面端 当前版本 v{VERSION}（点击检查更新）")
        self.version_label.setCursor(Qt.PointingHandCursor)
        self.version_label.mousePressEvent = lambda _e: self._check_update(silent=False)
        self.statusBar().addPermanentWidget(self.version_label)
        self.setAcceptDrops(True)

    # ---------- 列宽（可拖动 + 记住用户的选择） ----------
    #
    # COL_DEFAULTS 是出厂列宽。第 0 列（文件）是 Stretch，不在这里。
    COL_DEFAULTS = {1: 140, 2: 128, 3: 90, 4: 76, 5: 172, 6: 210}

    def _restore_col_widths(self):
        """把上次拖好的列宽还原回来（存在 ~/.config/zdm/gui.json）。"""
        saved = _gui_cfg("col_widths")
        if not isinstance(saved, dict):
            return
        for key, val in saved.items():
            try:
                col, w = int(key), int(val)
            except (TypeError, ValueError):
                continue
            if col in self.COL_DEFAULTS and w >= 56:
                self.table.setColumnWidth(col, w)
                self._col_widths[col] = w

    def _on_section_resized(self, col, _old, new):
        # 文件列是 Stretch，宽度由剩余空间算出来，记下来没意义还会互相打架。
        # _col_restoring 是「代码自己在设列宽」，不是用户拖的，不该被当成偏好存下来。
        if col == 0 or getattr(self, "_col_restoring", False):
            return
        self._col_widths[col] = new
        # 拖的时候每帧都会触发，停下来 0.7s 再落盘。
        self._col_save_timer.start(700)

    def _save_col_widths(self):
        if not getattr(self, "_col_widths", None):
            return
        _gui_cfg_set(col_widths={str(k): int(v) for k, v in self._col_widths.items()})

    def _reset_col_widths(self):
        """恢复出厂列宽，并把用户之前拖出来的偏好清掉。"""
        self._col_restoring = True
        self._col_widths = {}
        for col, w in self.COL_DEFAULTS.items():
            self.table.setColumnWidth(col, w)
        self._col_restoring = False
        self._col_save_timer.stop()
        _gui_cfg_set(col_widths={})

    def _col_menu(self, pos):
        hh = self.table.horizontalHeader()
        col = hh.logicalIndexAt(pos)
        menu = make_menu(self)
        style_glass_menu(menu)
        if col > 0:
            menu.addAction(
                "按内容自适应此列",
                lambda c=col: self.table.resizeColumnToContents(c),
            )
        menu.addAction("重置所有列宽", self._reset_col_widths)
        menu.exec_(hh.mapToGlobal(pos))

    def _build_update_bar(self) -> QFrame:
        """Banner shown when a newer release exists ("下载并安装" / "忽略")."""
        bar = QFrame()
        bar.setObjectName("updateBar")
        bar.setVisible(False)
        hl = QHBoxLayout(bar)
        hl.setContentsMargins(12, 8, 10, 8)
        hl.setSpacing(8)
        self.update_label = QLabel("")
        self.update_label.setObjectName("updateText")
        hl.addWidget(self.update_label, 1)
        self.update_download_btn = QPushButton("下载并安装")
        self.update_download_btn.setObjectName("updatePrimary")
        self.update_download_btn.setCursor(Qt.PointingHandCursor)
        self.update_download_btn.clicked.connect(self._download_update)
        hl.addWidget(self.update_download_btn)
        page_btn = QPushButton("打开发布页")
        page_btn.setCursor(Qt.PointingHandCursor)
        page_btn.setToolTip("在浏览器打开 GitHub Releases 页面，可用其它下载工具下载安装包")
        page_btn.clicked.connect(self._open_update_page)
        hl.addWidget(page_btn)
        notes_btn = QPushButton("更新说明")
        notes_btn.setCursor(Qt.PointingHandCursor)
        notes_btn.clicked.connect(self._show_release_notes)
        hl.addWidget(notes_btn)
        ignore_btn = QPushButton("忽略此版本")
        ignore_btn.setCursor(Qt.PointingHandCursor)
        ignore_btn.clicked.connect(self._ignore_update)
        hl.addWidget(ignore_btn)
        close_btn = QPushButton("✕")
        close_btn.setFixedWidth(34)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.clicked.connect(bar.hide)
        hl.addWidget(close_btn)
        return bar

    def _build_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = None
            return
        self.tray = QSystemTrayIcon(QIcon(ICON), self)
        menu = make_menu()
        style_glass_menu(menu)
        menu.addAction("显示/隐藏", self.toggle_window)
        menu.addAction("新建下载", self._new_download)
        menu.addSeparator()
        menu.addAction("全部暂停", lambda: self._bulk("pause_all"))
        menu.addAction("全部继续", lambda: self._bulk("resume_all"))
        menu.addSeparator()
        menu.addAction("打开网页版", self._open_web)
        menu.addAction("浏览器扩展", self._extensions)
        menu.addAction("打开下载目录", self._open_dir)
        menu.addSeparator()
        self.autostart_action = menu.addAction("开机自启")
        self.autostart_action.setCheckable(True)
        self.autostart_action.setChecked(autostart.is_enabled())
        self.autostart_action.triggered.connect(self._toggle_autostart)
        menu.addAction("检查更新", lambda: self._check_update(silent=False))
        menu.addAction("打开发布页", self._open_update_page)
        menu.addAction(f"关于 ZDM（v{VERSION}）", self._about)
        menu.addSeparator()
        menu.addAction("退出", self._quit)
        self.tray_menu = menu

        if sys.platform == "darwin":
            # On macOS a QSystemTrayIcon with a context menu has the menu popped
            # up by Qt *without activating the app*. A menu belonging to an
            # inactive app renders greyed out and ignores clicks - the bug where
            # "点菜单栏图标，文字全灰、选不了，得先点一下主页面". We therefore pop
            # the menu ourselves, right after activating ZDM.
            self.tray.setContextMenu(None)
            self.tray.activated.connect(self._on_tray_activated)
        else:
            self.tray.setContextMenu(menu)
            self.tray.activated.connect(self._on_tray_activated)
        self.tray.setToolTip(f"ZDM 下载管理器 v{VERSION}")
        self.tray.show()

        # Debug aid: `ZDM_TRAY_PREVIEW=x,y` pops the menu on startup so the
        # popup (and its vibrancy) can be inspected without clicking the icon.
        preview = os.environ.get("ZDM_TRAY_PREVIEW")
        if preview:
            try:
                px, py = (int(v) for v in preview.split(","))
            except ValueError:
                px, py = 200, 200
            QTimer.singleShot(2500, lambda: self.tray_menu.popup(QPoint(px, py)))

    def _on_tray_activated(self, reason):
        """Left click on the tray/menu-bar icon.

        macOS: activate the app, then show our menu (see _build_tray).
        Windows/Linux: plain show/hide toggle, menu on right click.
        """
        if sys.platform == "darwin":
            if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.Context,
                          QSystemTrayIcon.DoubleClick):
                self._show_tray_menu()
            return
        if reason == QSystemTrayIcon.Trigger:
            self.toggle_window()

    def _show_tray_menu(self):
        # Activating is what makes the popup menu interactive; suppress the
        # window restore that this activation would otherwise trigger.
        self._suppress_restore_until = time.time() + 4.0
        _dlog(f"[tray] click, active_before={app_is_active()}")
        activate_app()
        _dlog(f"[tray] active_after_activate={app_is_active()}")
        QTimer.singleShot(80, self._popup_tray_menu)

    def _popup_tray_menu(self):
        try:
            _dlog(f"[tray] popup, active={app_is_active()}")
            self.tray_menu.popup(QCursor.pos())
            QTimer.singleShot(400, lambda: _dlog(f"[tray] after_popup active={app_is_active()}"))
        except Exception as e:  # noqa: BLE001
            _dlog(f"[tray] popup failed: {type(e).__name__}: {e}")

    # ---------------- data ----------------
    def _start_polling(self):
        self.poll = PollWorker(self.api)
        self.poll.tick.connect(self._on_state)
        self.poll.failed.connect(self._on_offline)
        self.poll.start()

    def _on_offline(self, msg: str):
        # 服务没起来就自动拉起（升级后/开机后最常见的掉线场景）。
        # 每轮掉线只尝试一次（30s 节流），成败交给轮询自然验证——
        # 不再让用户去终端敲 zdm start --daemon（Linux 升级后实测）。
        if self._try_start_backend():
            self.statusBar().showMessage("服务未连接，正在自动启动服务…")
        else:
            self.statusBar().showMessage(f"服务未连接（{msg}）— 请运行：zdm start --daemon")

    def _try_start_backend(self) -> bool:
        """尝试把本地 serve 拉起来。返回 False 表示这轮已试过/失败。"""
        now = time.time()
        if now < getattr(self, "_next_backend_start", 0.0):
            return False
        self._next_backend_start = now + 30.0
        try:
            if runtime.is_frozen() and os.path.isfile(sys.executable):
                cmd = [sys.executable, "start", "--daemon", "--no-open"]
            else:
                cmd = runtime.cli_cmd("start", "--daemon", "--no-open")
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                **runtime.silent_proc_kwargs(),
            )
            return True
        except Exception:
            return False

    def _on_state(self, st: dict):
        self.config = st.get("config") or {}
        self.tasks = st.get("tasks") or []
        self._render()
        self._notify_failures()
        # Don't clobber a transient message ("正在检查更新…", "设置已保存", …)
        # that was just shown: the poll runs every 0.8s.
        if time.time() < getattr(self, "_status_hold_until", 0.0):
            return
        running = [t for t in self.tasks if t.get("status") == "downloading"]
        total_speed = sum(_num(t.get("speed")) for t in running)
        self.statusBar().showMessage(
            f"桌面端已连接本机服务 · v{VERSION} · 端口 {self.config.get('port', self.port)} · "
            f"进行中 {len(running)} · 总速度 {fmt_size(total_speed)}/s · 共 {len(self.tasks)} 项"
        )

    def _status(self, msg: str, hold: float = 3.0):
        """Show a message in the status bar and keep the poller off it briefly."""
        self.statusBar().showMessage(msg)
        self._status_hold_until = time.time() + hold

    def _notify_failures(self):
        """任务失败时弹系统通知（窗口可能缩在托盘里，用户看不到表格里的红字）。

        只对「这次轮询里新变成失败」的任务弹一次：用集合记下已经通知过的
        id，重下后再失败还能再弹（restart 会换新错误，值得再次提醒）。
        """
        if not getattr(self, "tray", None):
            return
        if not hasattr(self, "_failed_seen"):
            self._failed_seen = set()
        for t in self.tasks:
            tid = t.get("id")
            if t.get("status") != "error":
                # 离开失败态（重下/删除）就 forget，之后再次失败还能再提醒
                self._failed_seen.discard(tid)
                continue
            if tid in self._failed_seen:
                continue
            self._failed_seen.add(tid)
            reason = str(t.get("error") or "未知原因")[:120]
            self.tray.showMessage(
                "下载失败",
                f"{t.get('filename') or '任务'}：{reason}",
                QSystemTrayIcon.Warning,
                6000,
            )

    def _set_filter(self, key: str):
        self.filter = key
        for k, b in self.filter_btns.items():
            b.setChecked(k == key)
        self._render()

    def _visible(self):
        f = self.filter
        # Always hand back a fresh list: _render() sorts the result and must not
        # reorder self.tasks (that used to make the table order and the row ->
        # task lookup disagree).
        if f == "all":
            return list(self.tasks)
        if f == "downloading":
            return [t for t in self.tasks if t.get("status") in ("downloading", "queued", "merging")]
        if f == "sniffer":
            # Media the browser extension found on a page. This is the only
            # filter that keys off provenance rather than status.
            return [t for t in self.tasks if t.get("source") == "sniffer"]
        return [t for t in self.tasks if t.get("status") == f]

    def _task_at_row(self, row: int):
        """Map a visual table row to the task it currently shows.

        Looking the task up by filename (the old behaviour) picked the wrong
        row whenever the active filter reordered the list or two tasks shared a
        name.
        """
        try:
            tid = self._row_ids[row]
        except (AttributeError, IndexError, TypeError):
            return None
        return next((t for t in self.tasks if t["id"] == tid), None)

    def refresh_theme(self):
        """Re-read the palette after a light/dark switch and repaint everything."""
        # ProgressCell paints with QColor() so it must be told to reload.
        for cell in self.table.findChildren(ProgressCell):
            cell.apply_palette()
        # Row buttons draw their glyph pixmaps from the palette, and _render()
        # rebuilds them (plus the global pause/resume glyphs) with the new
        # colours.
        self._render()

    def _release_row_widgets(self):
        """Drop the cell widgets of the previous render.

        setCellWidget does NOT delete the widget it replaces: the old one is
        reparented to the viewport (measured: viewport children jump from 24
        to 48 on the render after a 4-row table, and stay there until the
        event loop gets around to dispatching the deferred deletes). hide()
        first so a replaced widget can never paint over its successor, then
        deleteLater() so the C++ object is reaped on the next loop iteration.
        """
        for row in range(self.table.rowCount()):
            # 第 0 列是文件名 + 来源徽标（NameCell），2 是进度条，6 是操作按钮。
            # 这三个每次渲染都会换成新控件。
            for col in (0, 2, 6):
                w = self.table.cellWidget(row, col)
                if w is not None:
                    self.table.setCellWidget(row, col, None)
                    w.hide()
                    w.deleteLater()

    def _render(self):
        items = self._visible()
        # created_at may be missing on a task restored from an old state file;
        # a KeyError in the sort key would abort the whole render.
        items.sort(key=lambda t: t.get("created_at") or 0, reverse=True)
        self._sync_bulk_buttons()
        self._release_row_widgets()
        self.table.setRowCount(len(items))
        pal = palette()
        colors = {
            "downloading": pal.status_downloading, "completed": pal.status_completed,
            "paused": pal.status_paused, "error": pal.status_error,
            "queued": pal.status_queued, "canceled": pal.status_canceled,
            "merging": pal.status_merging,
        }
        for row, t in enumerate(items):
            pct = min(100.0, _num(t.get("progress")))
            status = t.get("status") or "queued"
            color = colors.get(status, pal.status_downloading)

            # Where the task came from, as a coloured tag after the name. Only
            # the non-default sources get one: tagging every "manual" row would
            # be noise, and the point is to spot the extension/sniffer ones.
            #
            # The name cell holds a QLabel rather than a QTableWidgetItem
            # because only the label renders the <span> markup that colours
            # the tag; a table item would show the raw tags as text.
            # The name cell holds a widget rather than a QTableWidgetItem so the
            # filename can be elided with "…" while the source badge stays
            # visible, and the tooltip can still show the full name.
            self.table.setCellWidget(row, 0, NameCell(t, pal))

            # Every field is read with .get(): a task that came from an older
            # client (or a half-written state file) may be missing "speed" or
            # "downloaded", and a KeyError here would take down the whole
            # table render instead of just that one row.
            size_item = QTableWidgetItem(
                # 已完成就只报最终文件大小：合流后文件比视频轨大，
                # 「54.5 MB / 57.2 MB」这种写法会让人以为没下完。
                fmt_size(t.get("size"))
                if status == "completed" and t.get("size")
                else fmt_pair(t.get("downloaded"), t.get("size"))
                if t.get("size")
                else f"{fmt_size(t.get('downloaded'))} / 未知"
            )
            size_item.setForeground(QColor(pal.text_muted))
            size_item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            self.table.setItem(row, 1, size_item)

            label = f"{pct:.1f}%"
            if status == "downloading" and t.get("eta"):
                # 「剩余」缩成「剩」：进度列窄了，两个字的差别正好是一行放不放得下。
                label += f" · 剩 {fmt_eta(t['eta'])}"
            elif status == "error" and t.get("error"):
                label = str(t["error"])[:46]
            self.table.setCellWidget(row, 2, ProgressCell(pct, label, color))

            speed_text = (
                f"{fmt_size(t.get('speed'))}/s" if status == "downloading" else "—"
            )
            speed_item = QTableWidgetItem(speed_text)
            speed_item.setForeground(QColor(pal.text_strong))
            self.table.setItem(row, 3, speed_item)

            st_item = QTableWidgetItem(STATUS_TEXT.get(status, status))
            st_item.setForeground(QColor(color))
            st_item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            self.table.setItem(row, 4, st_item)

            # 时间列：几点开始下载、预计/实际几点完成。
            #   进行中  14:03 → 预计 14:25
            #   合并中  14:03 → 合并中…
            #   已完成  14:03 → 14:25
            #   失败/暂停/取消  14:03 开始
            # 用箭头而不是「开始 … · … 完成」：后者 218px 宽，这一列只有 158px。
            started = t.get("started_at") or t.get("created_at") or 0
            head = fmt_clock(started) if started else ""
            tail = ""
            if status == "downloading":
                if t.get("eta"):
                    tail = f"预计 {fmt_clock(time.time() + _num(t['eta']))}"
            elif status == "merging":
                tail = "合并中…"
            elif status == "completed" and t.get("completed_at"):
                tail = fmt_clock(t["completed_at"])
            if head and tail:
                time_text = f"{head} → {tail}"
            elif head:
                time_text = f"{head} 开始"
            else:
                time_text = "—"
            time_item = QTableWidgetItem(time_text)
            time_item.setForeground(QColor(pal.text_muted))
            time_item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            self.table.setItem(row, 5, time_item)

            w = QWidget()
            hl = QHBoxLayout(w)
            hl.setContentsMargins(2, 0, 2, 0)
            hl.setSpacing(6)
            # Per-task pause / resume: an icon on the row itself (the text would
            # not fit, and it makes "this pauses *this* file" unmistakable).
            # Every row keeps this slot -- on a finished task there is nothing to
            # pause, so the button is shown disabled instead of disappearing
            # (a control that comes and goes per status reads like a bug).
            if status in ("downloading", "queued"):
                self._glyph_btn(hl, "pause", "暂停这个任务", lambda _=False, i=t["id"]: self._act(i, "pause"))
            elif status in ("paused", "error", "canceled"):
                self._glyph_btn(hl, "play", "继续这个任务", lambda _=False, i=t["id"]: self._act(i, "resume"))
            else:
                self._glyph_btn(hl, "pause", "已完成，无需暂停", None, enabled=False)
            if status == "completed":
                # "打开" opens the downloaded file itself (a .dmg starts the
                # installer); "目录" reveals it in the file manager.
                self._btn(hl, "打开", lambda _=False, i=t["id"]: self._open_task(i, False))
                self._btn(hl, "目录", lambda _=False, i=t["id"]: self._open_task(i, True))
            else:
                self._btn(hl, "重下", lambda _=False, i=t["id"]: self._act(i, "restart"))
                # 失败行额外给一个「原因」按钮：点开是二级弹窗，看完整错误
                if status == "error" and t.get("error"):
                    self._btn(hl, "原因", lambda _=False, i=t["id"]: self._show_error_detail(i))
            self._btn(hl, "删除", lambda _=False, i=t["id"]: self._remove(i))
            hl.addStretch(1)
            self.table.setCellWidget(row, 6, w)
            self.table.setRowHeight(row, 46)
        self._rows = {t["id"]: r for r, t in enumerate(items)}
        self._row_ids = [t["id"] for t in items]

    def _btn(self, layout, text, fn):
        b = QPushButton(text)
        b.setFixedHeight(26)
        b.setMinimumWidth(50)
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(fn)
        layout.addWidget(b)

    def _glyph_btn(self, layout, kind, tip, fn, enabled: bool = True):
        """Compact icon-only action button for a task row."""
        b = QPushButton()
        # A disabled QIcon without a Disabled pixmap is drawn at full strength,
        # which makes the button look usable -- fade the glyph ourselves.
        b.setIcon(glyph_icon(kind, 17, alpha=255 if enabled else 70))
        b.setIconSize(QSize(17, 17))
        b.setFixedSize(34, 26)
        b.setToolTip(tip)
        b.setEnabled(enabled)
        if enabled:
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(fn)
        layout.addWidget(b)

    def _show_error_detail(self, tid: str):
        """失败行点「原因」弹出的二级弹窗：完整错误 + 可展开的调试信息。"""
        t = next((x for x in self.tasks if x.get("id") == tid), None)
        if not t:
            return
        reason = str(t.get("error") or "未知原因")
        box = QMessageBox(self)
        box.setWindowTitle("下载失败 — 原因")
        box.setIcon(QMessageBox.Warning)
        box.setText(f"文件：{t.get('filename') or '未知'}")
        box.setInformativeText(reason)
        box.setDetailedText(
            f"ID: {tid}\n状态: {t.get('status')}\nURL: {t.get('url') or ''}\n错误: {reason}"
        )
        box.setStandardButtons(QMessageBox.Ok)
        box.exec_()

    def _bulk_button(self, layout, kind, text, tip, fn):
        """Global action button: Chinese label plus a small theme-aware glyph."""
        b = QPushButton(text)
        b.setIcon(glyph_icon(kind, 13))
        b.setIconSize(QSize(13, 13))
        b.setToolTip(tip)
        b.setMinimumWidth(96)
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(fn)
        layout.addWidget(b)
        return b

    def _sync_bulk_buttons(self):
        """Grey out 全部暂停 / 全部继续 when the list has nothing for them.

        The glyph is a painted pixmap, so it has to be redrawn at reduced alpha
        as well -- otherwise a disabled button still looks clickable.
        """
        if not hasattr(self, "btn_pause_all"):
            return
        active = any(t.get("status") in ("downloading", "queued", "merging") for t in self.tasks)
        resumable = any(t.get("status") in ("paused", "error", "canceled") for t in self.tasks)
        for btn, kind, on in (
            (self.btn_pause_all, "pause", active),
            (self.btn_resume_all, "play", resumable),
        ):
            btn.setEnabled(on)
            btn.setIcon(glyph_icon(kind, 13, 255 if on else 70))

    # ---------------- actions ----------------
    def _run(self, fn, *args):
        w = OpWorker(fn, *args)
        self._workers.append(w)
        w.done.connect(lambda status, res: self._on_op_done(status, res, w))
        # 同 ExtensionDialog：done 发出时线程还没结束，必须等到 finished 再
        # 从列表里移除（移除就是丢掉最后一个引用 → QThread 被析构 → abort）。
        w.finished.connect(lambda w=w: self._retire_worker(w))
        w.start()

    def _retire_worker(self, worker):
        """线程真正退出后才丢引用，避免 QThread 在运行中被析构。"""
        try:
            worker.wait(5000)
        except Exception:  # noqa: BLE001
            pass
        if worker in self._workers:
            self._workers.remove(worker)

    def _on_op_done(self, status, res, worker):
        if status == "err" or (isinstance(res, dict) and not res.get("ok")):
            msg = str(res) if status == "err" else res.get("error", "操作失败")
            self._status(f"操作失败：{msg}")

    # ---------------- updates ----------------
    def _background(self, fn, on_done, *args, quiet: bool = False):
        """Run ``fn(*args)`` off the UI thread and call ``on_done(result)``.

        ``quiet`` suppresses the generic "操作失败" status for background
        housekeeping (silent update check), where a transient network error
        must not overwrite the "connected" status the user is watching.
        """
        w = OpWorker(fn, *args)
        self._workers.append(w)

        def _done(status, res, _w=w):
            if status == "ok":
                try:
                    on_done(res)
                except Exception as e:  # noqa: BLE001
                    _dlog(f"[bg] callback failed: {type(e).__name__}: {e}")
            if not quiet:
                self._on_op_done(status, res, _w)

        w.done.connect(_done)
        w.finished.connect(lambda w=w: self._retire_worker(w))
        w.start()

    def _auto_check_update(self):
        """启动时安排第一次静默检查，并启动「后台定期复查」定时器。

        以前这里只是一次 ``_check_update(silent=True)``：查完就再也不查了。
        常驻托盘的用户几周不重启一次 GUI，发版之后自然永远看不到提示。
        现在改成 tick 模型 —— 每 5 分钟判一次「到期没有」，真正的间隔由
        ``_update_next_at`` 决定（成功 4 小时、失败 10min→30min→1h 退避）。
        """
        if self._update_timer is None:
            self._update_timer = QTimer(self)
            self._update_timer.setInterval(UPDATE_TICK_MS)
            self._update_timer.timeout.connect(self._update_tick)
            self._update_timer.start()
        now = time.time()
        last = update.load_last_check()
        if last <= 0:
            # 从没查过（首次安装 / 1.15.23 之前的老客户端）：启动就查。
            self._update_next_at = now
        elif last + UPDATE_STARTUP_GAP > now:
            # 刚查过（刚重启过）：等到期再说，别拿 API 限额去撞墙。
            self._update_next_at = last + UPDATE_STARTUP_GAP
        else:
            # 隔了很久（常驻几天后重启）：留 20 秒给网络/配置就绪。
            self._update_next_at = now + 20.0
        _dlog("[update] timer armed: last_check=%s, next_in=%ds" % (
            "never" if last <= 0 else f"{int(now - last)}s ago",
            int(self._update_next_at - now),
        ))
        if self._update_next_at <= now + 60.0:
            # 距上次检查已经很久（或从没查过）：启动这次就查，不必再等。
            self._update_next_at = now
            self._update_tick()

    def _update_tick(self):
        """后台定时器：到点了才真正发起一次静默检查。"""
        if self._update_checking:
            return
        if not (self.config or {}).get("check_updates", True):
            return
        if time.time() < self._update_next_at:
            return
        self._check_update(silent=True)

    def _check_update(self, silent: bool = False):
        if self._update_checking:
            return
        self._update_checking = True
        if not silent:
            self._status("正在检查更新…")
        proxy = str((self.config or {}).get("proxy") or "")
        self._background(
            lambda: update.latest_release(proxy=proxy),
            lambda info: self._on_update_checked(info, silent),
            quiet=silent,
        )

    def _schedule_next_check(self, ok: bool):
        """排下一次自动检查：成功 4 小时后，失败按指数退避（上限 1 小时）。"""
        now = time.time()
        if ok:
            self._update_retry = UPDATE_RETRY_MIN
            self._update_next_at = now + UPDATE_RECHECK_INTERVAL
        else:
            self._update_next_at = now + self._update_retry
            self._update_retry = min(UPDATE_RETRY_MAX, self._update_retry * 3.0)

    def _on_update_checked(self, info, silent: bool):
        self._update_checking = False
        cur = VERSION
        if not info or not info.get("version"):
            # 网络/代理/API 限流。静默检查不能就这么放弃 —— 以前正是这里
            # return 掉，导致「启动那一次失败 = 这辈子不再提示」。
            self._schedule_next_check(ok=False)
            _dlog(f"[update] check failed (silent={silent}), retry in "
                  f"{int(self._update_next_at - time.time())}s")
            if not silent:
                self._message("检查更新",
                              "暂时连不上更新服务器（可能是网络或代理问题），请稍后再试。")
            return
        latest = info["version"]
        update.save_last_check()
        self._schedule_next_check(ok=True)
        _dlog(f"[update] latest={latest} current={cur} newer="
              f"{update.is_newer(latest, cur)} silent={silent}")
        if not update.is_newer(latest, cur):
            if not silent:
                self._message("检查更新", f"当前已是最新版本 v{cur}。")
            self.update_bar.setVisible(False)
            return
        if silent and update.load_ignored() == latest:
            return
        self._update_info = info
        self.update_label.setText(f"发现新版本 v{latest}（当前 v{cur}）")
        self.update_bar.setVisible(True)
        if self.tray is not None:
            try:
                self.tray.setToolTip(f"ZDM 下载管理器 v{cur} · 有新版 v{latest}")
            except Exception:
                pass
        # 「自动弹出提示」：同一版本只提示一次，避免每次复查都骚扰。
        if silent and self._update_notified != latest:
            self._update_notified = latest
            if self.tray is not None:
                try:
                    self.tray.showMessage(
                        "ZDM 有新版本",
                        f"v{latest} 已发布，点“下载并安装”即可更新。",
                        QSystemTrayIcon.Information, 6000,
                    )
                except Exception:
                    pass
            # 托盘气泡会被 Windows 通知中心/专注助手吞掉，任务栏闪一下更保险。
            try:
                QApplication.alert(self, 8000)
            except Exception:
                pass

    def _download_update(self):
        info = self._update_info or {}
        # 已经下过同一个版本的安装包就直接进安装流程：用户上次可能关掉了
        # 向导/装到一半失败，不该再让他等 120MB 重下一遍，更不该让他自己去
        # 下载目录里翻安装包（这是上一版最招骂的点）。
        cached = self._downloaded_update or {}
        if (
            cached.get("version")
            and cached.get("version") == info.get("version")
            and cached.get("path")
            and os.path.isfile(cached["path"])
        ):
            self._status("安装包已下载，正在打开安装向导…", hold=8)
            self._on_update_downloaded(cached["path"], info)
            return
        proxy = str((self.config or {}).get("proxy") or "")
        # 定位安装包可能要发网络请求（资产列表缺失时要再讨一次/探活），
        # 必须放后台线程 —— 以前这一步在主线程里同步做，弱网下界面会假死。
        self.update_download_btn.setEnabled(False)
        self.update_download_btn.setText("准备下载…")
        self._status("正在定位安装包…", hold=60)
        self._background(
            lambda: update.resolve_asset(info, proxy=proxy),
            lambda asset: self._begin_update_download(asset, info, proxy),
        )

    def _begin_update_download(self, asset, info, proxy):
        """拿到（或造出）下载地址后真正开始下载。asset 为 None 才是真没辙。"""
        if not asset:
            self.update_download_btn.setEnabled(True)
            self.update_download_btn.setText("下载并安装")
            ver = str((info or {}).get("version") or "")
            # 明确问一句，绝不静默把用户丢到网页上（1.15.24 的行为）。
            if self._message_yes_no(
                "拿不到安装包",
                f"没能确定 v{ver} 的安装包地址。\n"
                "可能是网络受限（GitHub 访问不通）或该版本资产还没上传完。\n\n"
                "要打开 Releases 页面手动下载吗？",
            ):
                self._open_url((info or {}).get("url") or update.PROJECT_URL)
            else:
                self._status("已取消", hold=6)
            return
        dest = os.path.join(update.downloads_dir(), asset["name"])
        # 下载目录里已经有这个版本的安装包（上次装失败/取消后关过程序）：
        # 直接进安装流程。GitHub 的 100MB+ 包在弱网下要几分钟，重下一遍纯属
        # 折磨；装包要么是 .part 被 rename 上来的（说明下完了），要么尺寸对得上。
        want = int(asset.get("size") or 0)
        if os.path.isfile(dest):
            got = os.path.getsize(dest)
            if (want and got == want) or (not want and got > 20 * 1048576):
                self._status("安装包已存在且完整，跳过下载…", hold=6)
                self._on_update_downloaded(dest, info)
                return
        self.update_download_btn.setText("测速中…")
        self._status("正在选择最快的下载线路…", hold=60)
        # 进度：update.download 在工作线程回调，这里只记数字；
        # 定时器在主线程轮询刷 UI（跨线程直接碰控件会崩）。
        self._dl_progress = {"done": 0, "total": asset.get("size") or 0}
        self._dl_timer = QTimer(self)
        self._dl_timer.setInterval(400)
        self._dl_timer.timeout.connect(self._tick_update_progress)
        self._dl_timer.start()
        self._background(
            lambda: update.download(
                asset["url"], dest, proxy=proxy,
                expected_size=asset.get("size") or 0,
                expected_sha256=asset.get("digest") or "",
                progress=self._on_dl_chunk,
            ),
            lambda path: self._on_update_downloaded(path, info),
        )

    def _on_dl_chunk(self, done: int, total: int):
        p = getattr(self, "_dl_progress", None)
        if p is not None:
            p["done"], p["total"] = done, total

    def _tick_update_progress(self):
        p = getattr(self, "_dl_progress", None) or {}
        done, total = p.get("done", 0), p.get("total", 0)
        if total > 0:
            pct = min(100, int(done * 100 / total))
            self.update_download_btn.setText(f"下载中… {pct}%")
            self._status(
                f"正在下载更新… {pct}%（{done // 1048576}/{total // 1048576} MB）",
                hold=60,
            )
        elif done > 0:
            # 资产列表是猜出来的（size 未知）或服务器没给 Content-Length：
            # 没有百分比也要让用户看见「在动」，否则看着像卡死。
            self.update_download_btn.setText(f"下载中… {done // 1048576} MB")
            self._status(f"正在下载更新… 已下载 {done // 1048576} MB", hold=60)

    def _on_update_downloaded(self, path, info):
        t = getattr(self, "_dl_timer", None)
        if t is not None:
            t.stop()
        self.update_download_btn.setEnabled(True)
        self.update_download_btn.setText("下载并安装")
        if not path:
            if self._message_yes_no(
                "下载失败",
                "安装包没能下载下来。\n\n"
                "已依次尝试 GitHub 直连和 3 个公共加速线路，全部失败 ——\n"
                "通常是网络/代理问题（或该版本资产还没上传完）。\n\n"
                "要打开 Releases 页面用浏览器手动下载吗？",
            ):
                self._open_url((info or {}).get("url") or update.PROJECT_URL)
            else:
                self._status("已取消，稍后可再点「下载并安装」重试", hold=8)
            return
        # 记住这份安装包：用户取消过向导/装到一半失败后，再点按钮直接进安装，
        # 不必重下 120MB，也不必自己去下载目录里翻 exe。
        ver = str((info or {}).get("version") or "")
        self._downloaded_update = {"version": ver, "path": path}
        self.update_download_btn.setText("开始安装")
        if ver:
            self.update_label.setText(f"v{ver} 安装包已就绪（已存到下载目录）")
        # 点「下载并安装」就是授权：下载完直接交给系统安装器，不再弹第二次确认。
        if sys.platform == "win32" and path.lower().endswith(".exe"):
            running = [t for t in self.tasks if t.get("status") == "downloading"]
            if running and not self._message_yes_no(
                "开始安装",
                f"安装期间 ZDM 会被关闭，正在进行的 {len(running)} 个下载会中断。\n"
                "装完新版会自动启动，未完成的任务会从断点继续。\n\n"
                f"安装包：{path}\n\n现在开始安装？",
            ):
                self._status(f"安装包已下载：{path}（随时可再点“下载并安装”）", hold=12)
                return
            if not update.install_windows(path):
                self._message(
                    "更新未能启动",
                    "没能启动安装程序，请手动运行：\n" + path,
                )
                return
            # 可见向导已经起来了，用户能看到「下一步」。
            # **不要**在这里 QApplication.quit()：主动退出会先留下一段空白，
            # 用户以为装崩了（这正是上一版 /SILENT 的观感）。向导走到 ssInstall
            # 时 Inno 自己会 taskkill 掉 ZDM.exe，完成页还能勾选「立即启动」。
            # 提示条保持可见：用户在向导里点了「取消」后，还能再点「开始安装」。
            self._status("安装向导已打开，按提示点「下一步」即可", hold=20)
            if self.tray is not None:
                try:
                    self.tray.showMessage(
                        "安装向导已打开",
                        "按提示点「下一步」；安装程序会自动关闭本程序，装完自动启动新版。",
                        QSystemTrayIcon.Information, 6000,
                    )
                except Exception:
                    pass
            return
        if sys.platform.startswith("linux") and path.lower().endswith(".deb"):
            # 用 GUI 自己的会话打开 .deb：QDesktopServices 走 Qt 的 xdg/DDE
            # 集成，实测能稳定弹出「软件包安装器」。从后台线程 Popen
            # deepin-deb-installer 在本机偶尔会直接退出，用户看到的就是
            # 「下载完了，安装器没出来」。
            self._status("正在打开系统安装器…", hold=10)
            if QDesktopServices.openUrl(QUrl.fromLocalFile(path)):
                self._status("已打开系统安装器，请按提示完成安装；ZDM 即将关闭", hold=15)
                if self.tray is not None:
                    try:
                        self.tray.showMessage(
                            "安装器已打开",
                            "按提示完成安装；ZDM 会自动退出，装完重新打开即可。",
                            QSystemTrayIcon.Information, 6000,
                        )
                    except Exception:
                        pass
                QTimer.singleShot(3500, QApplication.quit)
                return
            # 极端情况下没有 xdg handler：再退回 pkexec/dpkg。
            self._background(
                lambda: update.install_linux(path),
                lambda res: self._on_linux_installed(res, path),
            )
            return
        self._status(f"已下载 {os.path.basename(path)}，正在打开安装包…", hold=6)
        # macOS: opening the .dmg mounts it and shows the installer.
        self._run(self.api.open_file, path)
        self._message(
            "更新已就绪",
            f"安装包已保存到：\n{path}\n\n"
            "已为你打开这个安装包：把里面的 ZDM 拖进「应用程序」并确认「替换」。\n\n"
            "替换完成后请退出 ZDM 再重新打开（托盘/菜单栏图标 → 退出），"
            "新版本才会生效。",
        )

    def _on_linux_installed(self, res, path):
        ok, err = (res if isinstance(res, tuple) else (bool(res), ""))
        if ok:
            self._status("安装器已打开，请按提示完成安装；ZDM 即将关闭", hold=15)
            QTimer.singleShot(1500, QApplication.quit)
        else:
            self._message(
                "更新未能自动安装",
                f"没能打开系统安装器：{err or '未知原因'}\n\n"
                f"安装包已保存到：\n{path}\n"
                "可手动双击安装，或用“打开发布页”换其它下载工具。",
            )

    def _show_release_notes(self):
        info = self._update_info or {}
        notes = (info.get("notes") or "").strip() or "（该版本没有填写更新说明）"
        box = QMessageBox(self)
        box.setWindowTitle(f"更新说明 · v{info.get('version', '')}")
        box.setText(notes[:3000])
        box.setStandardButtons(QMessageBox.Ok)
        box.exec_()

    def _open_update_page(self):
        info = self._update_info or {}
        self._open_url(info.get("url") or update.PROJECT_URL)

    def _ignore_update(self):
        info = self._update_info or {}
        update.save_ignored(info.get("version", ""))
        self.update_bar.setVisible(False)
        self._status(
            f"已忽略 v{info.get('version', '')}，可在“设置 → 检查更新”里重新检查", hold=6
        )

    # ---------------- misc ----------------
    def _dialog_parent(self):
        """Prefer the open modal dialog (e.g. Settings) so messages stay on top."""
        return QApplication.activeModalWidget() or self

    def _message(self, title: str, text: str):
        box = QMessageBox(self._dialog_parent())
        box.setWindowTitle(title)
        box.setText(text)
        box.setStandardButtons(QMessageBox.Ok)
        box.exec_()

    def _message_yes_no(self, title: str, text: str) -> bool:
        box = QMessageBox(self._dialog_parent())
        box.setWindowTitle(title)
        box.setText(text)
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.Yes)
        return box.exec_() == QMessageBox.Yes

    def _open_url(self, url: str):
        try:
            import webbrowser

            webbrowser.open(url)
        except Exception as e:  # noqa: BLE001
            self._status(f"打开链接失败：{e}")

    def _about(self):
        box = QMessageBox(self)
        box.setWindowTitle("关于 ZDM")
        box.setIcon(QMessageBox.Information)
        box.setText(f"<b>ZDM 下载管理器</b> &nbsp; v{VERSION}")
        box.setInformativeText(
            "多线程下载器：桌面端 + 网页版 + 浏览器扩展\n"
            f"本地服务 http://127.0.0.1:{self.port}/\n"
            f"更新来源 github.com/{update.REPO}"
        )
        check = box.addButton("检查更新", QMessageBox.ActionRole)
        home = box.addButton("打开项目主页", QMessageBox.ActionRole)
        box.addButton(QMessageBox.Close)
        box.exec_()
        clicked = box.clickedButton()
        if clicked is check:
            self._check_update(silent=False)
        elif clicked is home:
            self._open_url(update.PROJECT_URL)

    def _open_task(self, task_id, reveal=False):
        """「打开 / 目录」用 GUI 自己的会话调用 xdg-open。

        后台服务是独立进程，启动时没有继承 DISPLAY/DBUS_SESSION_BUS_ADDRESS；
        它调用 xdg-open 只会静默失败（Linux 上「打开」「目录」点了没反应就是
        这个原因）。GUI 进程本身在图形会话里，QDesktopServices 能正确唤起
        文件管理器/默认播放器。
        """
        t = next((x for x in self.tasks if x["id"] == task_id), None)
        if not t:
            return
        directory = t.get("directory") or ""
        path = os.path.join(directory, t.get("filename") or "")
        if reveal or not os.path.exists(path):
            target = directory if os.path.isdir(directory) else os.path.expanduser("~")
        else:
            target = path
        if not os.path.exists(target):
            self._status(f"路径不存在：{target}", hold=6)
            return
        if QDesktopServices.openUrl(QUrl.fromLocalFile(target)):
            self._status(f"已打开：{target}", hold=4)
        else:
            self._status(f"无法打开：{target}（系统缺少文件管理器/默认程序）", hold=8)

    def _act(self, task_id, action):
        self._run(self.api.action, task_id, action)

    def _remove(self, task_id):
        """Delete a task, asking first whether its file should go too.

        The service deletes the downloaded file by default, and this button
        used to fire immediately - one stray click permanently removed a
        finished download. Keep the file unless the user explicitly opts in.
        """
        t = next((x for x in self.tasks if x["id"] == task_id), None)
        name = (t or {}).get("filename") or ""
        box = QMessageBox(self)
        box.setWindowTitle("删除任务")
        box.setIcon(QMessageBox.Question)
        box.setText(f"确定要删除任务“{name}”吗？")
        box.setInformativeText("默认只从列表中移除，已下载的文件会保留。")
        cb = QCheckBox("同时删除已下载的文件（不可恢复）")
        box.setCheckBox(cb)
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.No)
        yes = box.button(QMessageBox.Yes)
        no = box.button(QMessageBox.No)
        if yes is not None:
            yes.setText("删除")
        if no is not None:
            no.setText("取消")
        if box.exec_() != QMessageBox.Yes:
            return
        self._run(self.api.remove, task_id, not cb.isChecked())

    def _bulk(self, action):
        self._run(self.api.bulk, action)

    def _add_clicked(self):
        """工具栏「＋ 下载」：优先用输入框/剪贴板里的链接，一步进清晰度选择。"""
        # 某些 Qt 样式/工具栏会把同一次点击派发两次（实测一次点击进来两条），
        # 结果就是连开两个清晰度框。这里做 900ms 去抖，一次点击只处理一次。
        if getattr(self, "_add_guard", False):
            return
        self._add_guard = True
        QTimer.singleShot(900, lambda: setattr(self, "_add_guard", False))
        url = self.url_edit.text().strip()
        if not url:
            clip = QApplication.clipboard().text().strip()
            if clip.lower().startswith(("http://", "https://")):
                url = clip
                self.url_edit.setText(url)
        if url:
            self._quick_add()
            return
        self._new_download()

    def _quick_add(self):
        url = self.url_edit.text().strip()
        if not url:
            return
        if not url.lower().startswith(("http://", "https://")):
            self._status("只支持 http/https 链接，例如 https://example.com/file.zip")
            self.url_edit.selectAll()
            return
        self.url_edit.clear()
        self._add_url(url)

    # ---------------- 统一添加入口 ----------------
    def _add_url(self, url: str, extra: dict = None) -> None:
        """把链接变成任务：视频**页面**链接交给 yt-dlp，其它走多线程引擎。

        以前「粘贴链接 → 下载」对 YouTube/B站 的页面链接是当普通文件下的，
        存下来的就是一个名叫 ``watch`` 的 HTML 页面 —— 用户看到的就是
        「下载的都是 Watch，不是真正的视频」。这里按链接形态分流：
        YouTube 先解析清晰度让用户选；其它视频站直接让 yt-dlp 挑最佳。
        """
        url = (url or "").strip()
        extra = dict(extra or {})
        if not url:
            return
        if not url.lower().startswith(("http://", "https://")):
            self._status("只支持 http/https 链接，例如 https://example.com/file.zip")
            return
        if is_youtube(url):
            self._add_youtube(url, extra)
            return
        if is_video_page(url):
            self._status("这是视频页面链接，交给 yt-dlp 解析并下载最佳画质…")
            payload = {"url": url, "yt_page": url}
            payload.update(extra)
            self._run(self.api.add, payload)
            return
        payload = {"url": url}
        payload.update(extra)
        self._run(self.api.add, payload)

    def _add_youtube(self, url: str, extra: dict) -> None:
        self._status("正在解析 YouTube 清晰度（首次可能几秒）…")
        self._background(
            self.api.youtube,
            lambda res, _u=url, _e=extra: self._on_youtube_resolved(_u, _e, res),
            url,
        )

    def _on_youtube_resolved(self, url: str, extra: dict, res) -> None:
        formats = []
        if isinstance(res, dict) and res.get("ok"):
            formats = [
                f for f in (res.get("formats") or [])
                if f.get("kind") in ("video", "video+muxed") and f.get("url")
            ]
        if not formats:
            err = (res or {}).get("error") if isinstance(res, dict) else ""
            self._status(
                "解析失败{}，改为让 yt-dlp 直接下载…".format(f"（{err}）" if err else "")
            )
            payload = {"url": url, "yt_page": url}
            payload.update(extra)
            self._run(self.api.add, payload)
            return

        title = (res.get("title") or "YouTube 视频").strip()
        labels = []
        for f in formats:
            try:
                h = int(f.get("height") or 0)
            except (TypeError, ValueError):
                h = 0
            label = f"{h}P" if h else (f.get("label") or "默认")
            codec = str(f.get("codec") or "").lower()
            if codec.startswith(("avc1", "avc3")):
                label += " · H.264"
            elif codec.startswith(("vp9", "vp09")):
                label += " · VP9"
            elif codec.startswith("av01"):
                label += " · AV1"
            labels.append(label)

        # 默认 1080P：最高档经常是 4K，文件大、很多机器解码也吃力。
        # 选不高于 1080 的最高档；全都高于 1080 时再退回最高档。
        default = 0
        best_h = -1
        for i, f in enumerate(formats):
            try:
                h = int(f.get("height") or 0)
            except (TypeError, ValueError):
                h = 0
            if 0 < h <= 1080 and h > best_h:
                best_h, default = h, i

        pick, ok = QInputDialog.getItem(
            self, "选择清晰度", f"{title}\n\n下载哪个清晰度？", labels, default, False
        )
        if not ok or not pick:
            return
        f = formats[labels.index(pick)]
        height = int(f.get("height") or 0)
        payload = {
            "url": url,
            "yt_page": url,
            "yt_height": height,
            "filename": (f"{title} ({height}P).mp4" if height else title + ".mp4"),
        }
        payload.update(extra)  # 用户手填的文件名/目录优先
        self._run(self.api.add, payload)
        self._status(f"已加入下载：{title}（{pick}）")

    def _new_download(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("新建下载")
        form = QFormLayout(dlg)
        url = QLineEdit()
        url.setPlaceholderText("https://…（支持 YouTube/B站 视频页面链接）")
        url.setToolTip(
            "普通文件直接走多线程下载；粘贴 YouTube / B站 的**视频页面**链接时，\n"
            "会自动解析清晰度并交给 yt-dlp 下载真正的视频（而不是网页）。"
        )
        clip = QApplication.clipboard().text()
        if clip and clip.lower().startswith(("http://", "https://")):
            url.setText(clip.strip())
        name = QLineEdit()
        name.setPlaceholderText("留空自动识别")
        conns = QSpinBox()
        conns.setRange(1, 32)
        conns.setValue(int(self.config.get("connections") or 8))
        conns.setToolTip("分块并发连接数。YouTube/B站 视频任务由 yt-dlp 下载，此项不生效。")
        directory = QLineEdit(str(self.config.get("download_dir") or ""))
        btn_dir = QPushButton("选择")
        btn_dir.clicked.connect(lambda: directory.setText(
            QFileDialog.getExistingDirectory(dlg, "选择下载目录", directory.text()) or directory.text()))
        row = QWidget()
        rh = QHBoxLayout(row)
        rh.setContentsMargins(0, 0, 0, 0)
        rh.addWidget(directory)
        rh.addWidget(btn_dir)
        referer = QLineEdit()
        referer.setPlaceholderText("留空即可；只有防盗链站点才需要")
        referer.setToolTip(
            "Referer = 「你是从哪个页面点到这个文件的」来源地址。\n"
            "少数站点（如 B站 视频 CDN）会检查它做防盗链，缺失就直接 403；\n"
            "普通文件下载、YouTube 视频都不需要填。"
        )
        for w in (url, name, directory, referer):
            _zh_standard_menu(w)
        form.addRow("链接 URL", url)
        form.addRow("保存为", name)
        form.addRow("连接数", conns)
        form.addRow("保存到", row)
        form.addRow("Referer（来源页，可选）", referer)
        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.button(QDialogButtonBox.Ok).setText("确定")
        box.button(QDialogButtonBox.Cancel).setText("取消")
        box.accepted.connect(dlg.accept)
        box.rejected.connect(dlg.reject)
        form.addRow(box)
        if dlg.exec_() != QDialog.Accepted:
            return
        u = url.text().strip()
        if not u:
            return
        extra = {"connections": conns.value()}
        if name.text().strip():
            extra["filename"] = name.text().strip()
        if directory.text().strip():
            extra["dir"] = directory.text().strip()
        if referer.text().strip():
            extra["headers"] = {"Referer": referer.text().strip()}
        self._add_url(u, extra)

    def _settings(self):
        cfg = self.config or {}
        dlg = QDialog(self)
        dlg.setWindowTitle("设置")
        form = QFormLayout(dlg)
        directory = QLineEdit(str(cfg.get("download_dir") or ""))
        _zh_standard_menu(directory)
        btn_dir = QPushButton("选择")
        btn_dir.clicked.connect(lambda: directory.setText(
            QFileDialog.getExistingDirectory(dlg, "选择下载目录", directory.text()) or directory.text()))
        row = QWidget()
        rh = QHBoxLayout(row)
        rh.setContentsMargins(0, 0, 0, 0)
        rh.addWidget(directory)
        rh.addWidget(btn_dir)
        conns = QSpinBox()
        conns.setRange(1, 32)
        conns.setValue(int(cfg.get("connections") or 8))
        active = QSpinBox()
        active.setRange(1, 16)
        active.setValue(int(cfg.get("max_active") or 3))
        limit = QSpinBox()
        limit.setRange(0, 1000000)
        limit.setSuffix(" KB/s")
        limit.setValue(int(int(cfg.get("speed_limit") or 0) / 1024))
        limit.setSpecialValueText("不限速")
        proxy = QLineEdit(str(cfg.get("proxy") or ""))
        proxy.setPlaceholderText("留空则使用系统环境变量代理")
        _zh_standard_menu(proxy)
        port = QSpinBox()
        port.setRange(1024, 65535)
        port.setValue(int(cfg.get("port") or 9000))
        auto = QCheckBox("重启后自动继续未完成任务")
        auto.setChecked(bool(cfg.get("auto_resume", True)))
        autostart_cb = QCheckBox("开机自动启动 ZDM 服务")
        autostart_cb.setChecked(autostart.is_enabled())
        auto_gui_cb = QCheckBox("下载时自动打开桌面端（未运行时）")
        auto_gui_cb.setChecked(bool(cfg.get("auto_show_gui", True)))
        update_cb = QCheckBox("自动检查更新（启动时 + 后台每 4 小时）")
        update_cb.setChecked(bool(cfg.get("check_updates", True)))
        update_cb.setToolTip(
            "开启后：ZDM 启动时查一次，运行期间每 4 小时自动复查；\n"
            "发现新版本会在窗口顶部显示提示条，并弹系统通知。"
        )
        close_tray_cb = QCheckBox("关闭窗口时最小化到托盘（不退出程序）")
        close_tray_cb.setChecked(bool(_gui_cfg("close_to_tray", False)))
        # ---- appearance: follow the OS appearance by default ----
        appearance = QComboBox()
        for key, label in (("auto", "跟随系统"), ("light", "浅色"), ("dark", "深色")):
            appearance.addItem(label, key)
        cur_mode = theme_manager().mode if theme_manager() else "auto"
        idx = appearance.findData(cur_mode)
        appearance.setCurrentIndex(idx if idx >= 0 else 0)
        # Live preview so the user sees the effect before saving.
        appearance.currentIndexChanged.connect(
            lambda _i: self._preview_theme(appearance.currentData())
        )
        form.addRow("下载目录", row)
        form.addRow("单任务连接数", conns)
        form.addRow("同时下载任务数", active)
        form.addRow("全局限速", limit)
        form.addRow("代理", proxy)
        form.addRow("外观（深浅色）", appearance)
        form.addRow("服务端口（重启服务后生效）", port)
        form.addRow("", auto)
        form.addRow("", autostart_cb)
        form.addRow("", auto_gui_cb)
        form.addRow("", close_tray_cb)
        # ---- site cookies (YouTube / Bilibili need a logged-in session) ----
        cookies_box = QGroupBox("站点 Cookie（用于需要登录的视频站）")
        cookies_box.setToolTip(
            "浏览器扩展推送任务时会自动带上当前页面的 Cookie，一般不用填。\n"
            "只有从桌面端手动添加链接、或服务器要求登录态时才需要。"
        )
        cf = QFormLayout(cookies_box)
        site_cookies = cfg.get("site_cookies") or {}
        yt_cookie = QPlainTextEdit(str(site_cookies.get("youtube") or ""))
        yt_cookie.setPlaceholderText("从浏览器 F12 → Application → Cookies 复制，或用扩展自动带")
        yt_cookie.setMaximumHeight(72)
        yt_cookie.setTabChangesFocus(True)
        _zh_standard_menu(yt_cookie)
        bili_cookie = QPlainTextEdit(str(site_cookies.get("bilibili") or ""))
        bili_cookie.setPlaceholderText("B站的 SESSDATA（登录后可下 1080P+ / 会员内容）")
        bili_cookie.setMaximumHeight(72)
        bili_cookie.setTabChangesFocus(True)
        _zh_standard_menu(bili_cookie)
        cf.addRow("YouTube", yt_cookie)
        cf.addRow("B站", bili_cookie)
        clear_btn = QPushButton("清空两个站点的 Cookie")
        clear_btn.clicked.connect(lambda: (yt_cookie.setPlainText(""), bili_cookie.setPlainText("")))
        cf.addRow("", clear_btn)
        form.addRow(cookies_box)
        # ---- version + updates ----
        ver_row = QWidget()
        vh = QHBoxLayout(ver_row)
        vh.setContentsMargins(0, 0, 0, 0)
        vh.addWidget(QLabel(f"当前版本 v{VERSION}"))
        check_now = QPushButton("立即检查更新")
        check_now.clicked.connect(lambda: self._check_update(silent=False))
        vh.addWidget(check_now)
        vh.addStretch(1)
        form.addRow("版本", ver_row)
        form.addRow("", update_cb)
        box = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        box.button(QDialogButtonBox.Save).setText("保存")
        box.button(QDialogButtonBox.Cancel).setText("取消")
        box.accepted.connect(dlg.accept)
        box.rejected.connect(dlg.reject)
        quit_btn = QPushButton("退出")
        quit_btn.clicked.connect(self._quit)
        btn_row = QWidget()
        bl = QHBoxLayout(btn_row)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.addWidget(quit_btn)
        bl.addStretch(1)
        bl.addWidget(box)
        form.addRow(btn_row)
        if dlg.exec_() != QDialog.Accepted:
            # Discard the live preview and restore the persisted appearance.
            self._theme_preview = None
            tm = theme_manager()
            if tm is not None:
                tm.apply()
                # The preview overwrote _last_dark; re-sync it or the next
                # system appearance flip is mistaken for "no change".
                tm.prime()
            return
        # Persist the chosen appearance (and apply it if it changed).
        self._theme_preview = None
        tm = theme_manager()
        if tm is not None:
            chosen = appearance.currentData()
            if chosen and chosen != tm.mode:
                tm.set_mode(chosen)
            tm.prime()
        try:
            autostart.set_enabled(autostart_cb.isChecked())
        except Exception as e:
            self._status(f"开机自启设置失败：{e}")
        self._run(self.api.set_config, {
            "download_dir": directory.text().strip() or cfg.get("download_dir"),
            "connections": conns.value(),
            "max_active": active.value(),
            "speed_limit": limit.value() * 1024,
            "proxy": proxy.text().strip(),
            "port": port.value(),
            "auto_resume": auto.isChecked(),
            "auto_show_gui": auto_gui_cb.isChecked(),
            "check_updates": update_cb.isChecked(),
            "site_cookies": {
                "youtube": yt_cookie.toPlainText().strip(),
                "bilibili": bili_cookie.toPlainText().strip(),
            },
        })
        _gui_cfg_set(close_to_tray=close_tray_cb.isChecked())
        if getattr(self, "autostart_action", None) is not None:
            self.autostart_action.setChecked(autostart.is_enabled())
        self._status("设置已保存")

    def _preview_theme(self, mode: str):
        """Live-preview an appearance without persisting it (Settings dialog)."""
        tm = theme_manager()
        if tm is None or mode not in ("auto", "light", "dark"):
            return
        # Temporarily override the resolved mode; the choice is only written to
        # disk when the user hits Save, so Cancel really reverts.
        self._theme_preview = mode
        tm._last_dark = (mode == "dark") if mode != "auto" else tm._last_dark
        app = QApplication.instance()
        if app is None:
            return
        if mode == "auto":
            p = tm.palette()
        else:
            p = _theme.DARK if mode == "dark" else _theme.LIGHT
        app.setStyleSheet(_theme.build_stylesheet(p))
        app.setPalette(_theme._qt_palette(p))
        notify = getattr(app, "notify_theme_changed", None)
        if callable(notify):
            notify()

    def _open_dir(self):
        path = str((self.config or {}).get("download_dir") or os.path.expanduser("~/Downloads"))
        self._run(self.api.open_path, path)

    def _open_web(self):
        """Open the local web UI (http://127.0.0.1:<port>/) in the default browser."""
        port = int((self.config or {}).get("port") or self.port or 9000)
        url = f"http://127.0.0.1:{port}/"
        ok = False
        try:
            import webbrowser

            ok = bool(webbrowser.open(url))
        except Exception:
            ok = False
        if not ok and os.name == "posix":
            # webbrowser 模块在部分 Linux 发行版上找不到浏览器（没有
            # $BROWSER、注册表为空）会静默返回 False —— 兜底直接调
            # xdg-open，再不行就把原因告诉用户，而不是「点了没反应」。
            try:
                subprocess.Popen(
                    ["xdg-open", url],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    **runtime.silent_proc_kwargs(),
                )
                ok = True
            except Exception:
                ok = False
        if ok:
            self._status(f"已在浏览器打开网页版：{url}")
        else:
            self._status("打开网页版失败：系统里没有可用的浏览器（需要 xdg-open 或已安装的浏览器）")

    # ---- 主界面扩展状态（常驻） ----
    def _refresh_ext_status(self):
        if self._ext_worker is not None:
            return
        w = OpWorker(self.api.extension)
        self._ext_worker = w
        w.done.connect(self._on_ext_status_main)
        w.finished.connect(lambda w=w: self._retire_ext_worker(w))
        w.start()

    def _retire_ext_worker(self, w):
        try:
            w.wait(2000)
        except Exception:  # noqa: BLE001
            pass
        if self._ext_worker is w:
            self._ext_worker = None

    def _on_ext_status_main(self, tag: str, payload):
        r = payload if (tag == "ok" and isinstance(payload, dict)) else {}
        if not r.get("ok"):
            self.ext_label.setText("扩展状态未知")
            self.ext_label.setStyleSheet("color: #9ca3af;")
            return
        if r.get("seen_recently"):
            self.ext_label.setText("● 扩展已连接")
            self.ext_label.setStyleSheet("color: #22c55e; font-weight: 600;")
            self.ext_label.setToolTip("浏览器扩展工作正常；点击查看详情")
        elif r.get("last_seen"):
            self.ext_label.setText("● 扩展已休眠")
            self.ext_label.setStyleSheet("color: #f59e0b;")
            self.ext_label.setToolTip(
                "扩展最近活跃过，但当前没有心跳（浏览器关闭后会休眠）。点击查看详情"
            )
        else:
            self.ext_label.setText("● 扩展未连接")
            self.ext_label.setStyleSheet("color: #ef4444;")
            self.ext_label.setToolTip("尚未检测到浏览器扩展；点击查看安装方法")

    def _extensions(self):
        """「浏览器扩展」引导对话框（单例：避免重复弹出堆叠）。"""
        dlg = getattr(self, "_ext_dlg", None)
        if dlg is None:
            dlg = ExtensionDialog(self.api, self)
            self._ext_dlg = dlg
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        dlg._refresh()

    def _open_selected_file(self, row):
        """Double-click a row: open the finished file, or its folder otherwise."""
        t = self._task_at_row(row)
        if not t:
            return
        if t.get("status") == "completed":
            self._act(t["id"], "open")   # opens the file itself (e.g. the .dmg)
        else:
            self._run(self.api.open_path, t["directory"])

    def _menu(self, pos):
        row = self.table.rowAt(pos.y())
        if row < 0:
            return
        t = self._task_at_row(row)
        if not t:
            return
        menu = make_menu(self)
        style_glass_menu(menu)
        menu.addAction("复制链接", lambda: QApplication.clipboard().setText(t.get("url") or ""))
        if t.get("status") == "completed":
            menu.addAction("打开文件", lambda: self._open_task(t["id"], False))
            menu.addAction("在文件夹中显示", lambda: self._open_task(t["id"], True))
        menu.addSeparator()
        menu.addAction("暂停", lambda: self._act(t["id"], "pause"))
        menu.addAction("继续", lambda: self._act(t["id"], "resume"))
        menu.addAction("重新下载", lambda: self._act(t["id"], "restart"))
        menu.addSeparator()
        menu.addAction("删除任务", lambda: self._remove(t["id"]))
        menu.exec_(self.table.viewport().mapToGlobal(pos))

    def wake(self, payload: str = ""):
        """Raise the existing window (used when a second instance starts)."""
        self._show_front()
        for token in (payload or "").split():
            if token.lower().startswith(("http://", "https://")):
                self._add_url(token)

    def _show_front(self):
        """Show the window and make it the key window of an *active* app."""
        # On macOS an inactive app's window is drawn greyed out and eats the
        # first click, so activate the app before showing anything.
        activate_app()
        self._user_minimized = False
        self._user_hidden = False
        self.show()
        self.setWindowState((self.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive)
        self.raise_()
        self.activateWindow()

    def toggle_window(self):
        # isVisible() can be True while the window is not really on screen / not
        # focused (macOS show() is async). Treat "visible but not active" as
        # visible and just re-raise it instead of toggling.
        if self.isVisible() and not self.isMinimized() and app_is_active():
            self._user_hidden = True
            self.hide()
        else:
            self._show_front()

    # ---------------- events ----------------
    def changeEvent(self, e):
        super().changeEvent(e)
        if e.type() == QEvent.WindowStateChange:
            # Track whether *the user* put the window away. The app-level
            # restore hook (ZDMApplication._maybe_restore) fires on every
            # ApplicationActive flip and used to fight this, popping a
            # minimized window straight back up.
            self._user_minimized = bool(self.isMinimized())

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() or e.mimeData().hasText():
            e.acceptProposedAction()

    def dropEvent(self, e):
        urls = []
        if e.mimeData().hasUrls():
            urls = [u.toString() for u in e.mimeData().urls()]
        elif e.mimeData().hasText():
            urls = [e.mimeData().text()]
        for u in urls:
            if u.lower().startswith(("http://", "https://")):
                self._add_url(u.strip())

    def closeEvent(self, e):
        # 列宽可能在防抖计时器落盘前就被关窗，这里补一次。
        try:
            self._col_save_timer.stop()
            self._save_col_widths()
        except Exception:
            pass
        # Closing quits by default. Only fall back to the tray when the user
        # explicitly asked for it in Settings - hiding the window on every
        # close made the title-bar button look broken.
        if (self.tray is not None and self.tray.isVisible()
                and _gui_cfg("close_to_tray", False)):
            self._user_minimized = False
            self._user_hidden = True
            self.hide()
            self.tray.showMessage("ZDM", "已最小化到托盘，右键托盘图标可退出",
                                  QSystemTrayIcon.Information, 2500)
            e.ignore()
            return
        self._quit()

    def _quit(self):
        # 托盘菜单「退出」不走 closeEvent，这里也存一次列宽。
        try:
            self._col_save_timer.stop()
            self._save_col_widths()
        except Exception:
            pass
        try:
            self.poll.stop()
            self.poll.wait(2000)
        except Exception:
            pass
        # 扩展引导对话框的 2 秒轮询也要停（它一旦开过就活得和主窗口一样久）。
        ext_dlg = getattr(self, "_ext_dlg", None)
        if ext_dlg is not None:
            try:
                ext_dlg._timer.stop()
                if ext_dlg._worker is not None and ext_dlg._worker.isRunning():
                    ext_dlg._worker.wait(2000)
            except Exception:
                pass
        # In-flight API workers (e.g. a 30s-timeout pause request) must not
        # still be running when the interpreter tears down, or Qt aborts with
        # "QThread: Destroyed while thread is still running".
        for w in list(getattr(self, "_workers", [])):
            try:
                if w.isRunning():
                    w.wait(3000)
                    if w.isRunning():
                        w.terminate()
                        w.wait(500)
            except Exception:
                pass
        if self.tray:
            self.tray.hide()
        QApplication.quit()

    def _toggle_autostart(self, checked: bool):
        try:
            autostart.set_enabled(checked)
            ok = autostart.is_enabled() == checked
            self._status(
                "开机自启：" + ("已开启" if checked else "已关闭")
                + ("" if ok else "（无法写入，可能需要管理员/登录权限）")
            )
        except Exception as e:
            self._status(f"开机自启设置失败：{e}")


def _zh_standard_menu(edit) -> None:
    """把文本框的 Qt 标准右键菜单（撤销/剪切/复制/粘贴/删除/全选）换成中文。

    冻结包里 Qt 的翻译文件不可靠（Windows 实测 Save/Cancel 全英文），
    这些标准菜单项同样会出英文。做法：事件过滤器拦下 ContextMenu，
    取 Qt 原生菜单（保留自带的置灰/可用逻辑），再把六个固定文案改成
    中文——不自己拼菜单，避免各平台行为差异。
    """
    from PyQt5.QtCore import QEvent, QObject

    # 键是小写、去 & 的基础词（"Cu&t"→"cut"，"Select &All"→"select all"）。
    # 菜单项文本可能带 "\t快捷键" 后缀（如 "&Undo\tCtrl+Z"），改名时要保留。
    zh = {
        "undo": "撤销", "redo": "重做", "cut": "剪切", "copy": "复制",
        "paste": "粘贴", "delete": "删除", "select all": "全选",
    }

    class _Filter(QObject):
        def eventFilter(self, obj, ev):
            if ev.type() == QEvent.ContextMenu:
                menu = edit.createStandardContextMenu()
                for a in menu.actions():
                    t = a.text()
                    if not t:
                        continue
                    base, _, hotkey = t.partition("\t")
                    renamed = zh.get(base.replace("&", "").strip().lower())
                    if renamed:
                        a.setText(renamed + (("\t" + hotkey) if hotkey else ""))
                # 必须用 exec_（阻塞）：popup() 是非阻塞的，函数一返回 menu
                # 就没有 Python 引用了，PyQt 立刻把 QMenu 析构掉 —— 菜单
                # 一闪而过/根本不出现。Windows 上实测就是「右键没有任何反应」，
                # 于是设置里的 Cookie 框没法粘贴。exec_ 自己跑嵌套事件循环，
                # 菜单关闭后才返回，生命周期没问题。
                menu.exec_(ev.globalPos())
                return True
            return False

    f = _Filter(edit)
    edit.installEventFilter(f)
    # QPlainTextEdit/QTextEdit 是 QAbstractScrollArea：右键事件发给内部
    # viewport，只装在控件本体上拦不到（Windows 实测菜单仍是英文）。
    try:
        edit.viewport().installEventFilter(f)
    except Exception:
        pass


def _install_translations(app) -> None:
    """Translate Qt's own strings (Save/Cancel/Yes/No, file dialog, ...).

    Without a translator the Chinese UI still showed English "Save"/"Cancel"
    buttons in every QDialogButtonBox and QMessageBox, because those texts come
    from Qt itself. The Qt translation is shipped with both the system PyQt5
    and the frozen bundle.

    冻结包（Windows/Linux）里 QLibraryInfo 指向的翻译目录不一定带着
    qtbase_zh_CN.qm（zdm.spec 已把中文 *.qm 收进 _internal/i18n），
    所以除了 Qt 自己的路径外，再探一遍 app_root()/i18n。
    """
    try:
        from PyQt5.QtCore import QLibraryInfo, QLocale, QTranslator

        dirs = []
        try:
            p = QLibraryInfo.location(QLibraryInfo.TranslationsPath)
            if p:
                dirs.append(p)
        except Exception:  # noqa: BLE001
            pass
        try:
            dirs.append(os.path.join(runtime.app_root(), "i18n"))
        except Exception:  # noqa: BLE001
            pass
        names = ["qtbase_zh_CN", "qt_zh_CN"]
        try:
            locale = QLocale.system().name()  # e.g. zh_CN
            if locale:
                names.insert(0, f"qtbase_{locale}")
        except Exception:  # noqa: BLE001
            pass
        for d in dirs:
            for name in names:
                tr = QTranslator(app)
                if tr.load(name, d):
                    app.installTranslator(tr)
                    return
    except Exception as e:  # noqa: BLE001 - translations are cosmetic
        _dlog(f"[i18n] failed: {type(e).__name__}: {e}")


def _restart_stale_service(api: "Api") -> None:
    """让后台 serve 进程和界面跑同一版代码。

    升级（尤其 macOS 的 dmg 覆盖安装、Linux 免 root 包覆盖）只替换磁盘上的
    文件；后台 serve 进程是升级前启动的，内存里跑的还是旧代码。界面显示新版
    本号、下载引擎却还是旧的 —— 用户看到的就是「更新到最新了，下载还是老
    毛病」，实测 macOS 上更新到 1.15.15 后服务还是 1.15.14 的进程。

    这里发现版本不一致就请旧服务退出、再拉起当前版本的服务（launchd/systemd
    的 KeepAlive 也可能先把它拉起来，两种情况都能收敛到正确版本）。
    """
    try:
        info = api._req("GET", "", timeout=4.0)
    except Exception:  # noqa: BLE001
        return
    if not isinstance(info, dict) or not info.get("ok"):
        return
    running = str(info.get("version") or "")
    if not running or running == VERSION:
        return
    _dlog(f"[service] running v{running} != GUI v{VERSION} -> restart")
    api._req("POST", "action", {"action": "shutdown"}, timeout=3.0)
    for _ in range(60):
        res = api._req("GET", "", timeout=0.5)
        if not (isinstance(res, dict) and res.get("ok")):
            break
        time.sleep(0.1)
    try:
        if runtime.is_frozen() and os.path.isfile(sys.executable):
            cmd = [sys.executable, "start", "--daemon", "--no-open"]
        else:
            cmd = runtime.cli_cmd("start", "--daemon", "--no-open")
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            **runtime.silent_proc_kwargs(),
        )
    except Exception:  # noqa: BLE001
        return
    for _ in range(60):
        time.sleep(0.25)
        res = api._req("GET", "", timeout=1.0)
        if isinstance(res, dict) and res.get("ok") and str(res.get("version")) == VERSION:
            _dlog("[service] restarted with the new version")
            return


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    smoke = "--smoke" in argv
    if smoke:
        argv.remove("--smoke")
    port = default_port()
    rest = []
    for a in argv:
        if a.isdigit() and len(rest) == 0:
            port = int(a)
        else:
            rest.append(a)

    # Honor the desktop scale factor. The frozen build ships its own Qt, which
    # carries no Deepin dxcb plugin, so Qt falls back to 96 dpi and renders the
    # window *and* the fonts 1.5x too small on a 150% (Xft.dpi 144) screen.
    try:
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
        if sys.platform == "win32":
            # 分数缩放（125%）会让 Qt 用 1.25 倍绘制字体，Windows 上就是发虚；
            # Round 到 100%/150% 反而清晰。必须在 QApplication 创建前设置。
            try:
                QApplication.setHighDpiScaleFactorRoundingPolicy(
                    Qt.HighDpiScaleFactorRoundingPolicy.Round
                )
            except Exception:
                pass
    except Exception:
        pass

    app = ZDMApplication(sys.argv)
    if sys.platform == "win32":
        # 与 QSS 一致：雅黑 10pt + 全 hinting，避免正文比按钮“糊”。
        try:
            f = QFont("Microsoft YaHei UI", 10)
            f.setHintingPreference(QFont.PreferFullHinting)
            f.setStyleStrategy(QFont.PreferAntialias)
            app.setFont(f)
        except Exception:
            pass
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    app.setStyle("Fusion")

    # App 升级后把随包新版扩展同步到数据目录（Chrome 钉死在旧路径上，
    # 不同步的话用户永远跑着旧扩展 —— 详见 runtime.sync_extension_to_data_dir）
    try:
        runtime.sync_extension_to_data_dir()
    except Exception:
        pass
    _install_translations(app)
    _dlog(f"[init] ZDM v{VERSION} starting (log={_dock_log_file()}, exe={sys.executable})")

    # Light/dark theme: follows the OS appearance by default, and the user can
    # override it in Settings. Applying the palette before any widget is created
    # avoids a visible light -> dark flash on startup.
    global _THEME_MANAGER
    _THEME_MANAGER = _theme.ThemeManager(app)
    _THEME_MANAGER.prime()
    app.setStyleSheet(build_style_sheet())
    app.setPalette(_theme._qt_palette(palette()))
    _THEME_MANAGER.watch_system()

    guard = SingleApp(f"zdm-gui-{port}")
    if guard.already:
        guard.send(" ".join(rest) or "wake")
        _safe_print("ZDM 桌面端已在运行，已激活现有窗口")
        return 0

    api = Api(port)
    # 升级后旧服务进程可能还跑着上一版代码：先对齐版本，再开窗口。
    _restart_stale_service(api)
    win = MainWindow(api, port)
    app.set_main_window(win)
    guard.on_message(lambda payload: win.wake(payload))
    for token in rest:
        if token.lower().startswith(("http://", "https://")):
            win._add_url(token)
    win.show()
    if smoke:
        QTimer.singleShot(3500, win._quit)
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
