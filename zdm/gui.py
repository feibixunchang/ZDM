"""ZDM desktop client (PyQt5). Talks to the local ZDM service over its REST API.

Run directly with any python3 that has PyQt5:
    /usr/bin/python3 ~/.local/share/zdm/zdm/gui.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

# Make the bundled ``zdm`` package importable when run from its install dir.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from zdm import autostart  # noqa: E402
import theme as _theme  # noqa: E402

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
ICON = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "extension", "icons", "icon128.png"
)
if not os.path.exists(ICON):
    ICON = os.path.join(
        os.path.expanduser("~/.local/share/icons/hicolor/128x128/apps"), "zdm.png"
    )

STATUS_TEXT = {
    "queued": "排队中",
    "downloading": "下载中",
    "paused": "已暂停",
    "completed": "已完成",
    "error": "失败",
    "canceled": "已取消",
    "merging": "合并中",
}

_DOCK_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dock.log")


def _dlog(msg: str) -> None:
    """Append a diagnostic line (Dock/window troubleshooting)."""
    try:
        with open(_DOCK_LOG, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
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


def fmt_size(n) -> str:
    n = float(n or 0)
    if n < 1024:
        return f"{int(n)} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024
        if n < 1024:
            return f"{n:.1f} {unit}"
    return f"{n:.1f} PB"


def fmt_eta(s) -> str:
    if not s or s <= 0:
        return ""
    s = int(s)
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    if h:
        return f"{h}时{m}分"
    if m:
        return f"{m}分{sec}秒"
    return f"{sec}秒"


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

    def add(self, **kw):
        return self._req("POST", "tasks", kw)

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


# --------------------------------------------------------------------------- Qt
from PyQt5.QtCore import QRect, Qt, QThread, QTimer, pyqtSignal  # noqa: E402
from PyQt5.QtNetwork import QLocalServer, QLocalSocket  # noqa: E402
from PyQt5.QtGui import QColor, QFont, QIcon, QPainter  # noqa: E402
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
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
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

    We no longer replace the NSApplication delegate: Qt's own Cocoa integration
    already implements applicationShouldHandleReopen:, and swapping it out (with an
    imperfect forwarding shim) breaks Qt's native window-restoration. Instead we
    observe NSApplicationDidBecomeActiveNotification, which fires reliably whenever
    the Dock icon is clicked, and restore the window from there.
    """

    _zdm_target = None
    _zdm_orig = None


def _build_reopen_delegate_class():
    """Build the NSObject subclass that handles applicationShouldHandleReopen:.

    Qt's QApplication on macOS does NOT install an NSApplication delegate for the
    reopen callback (verified: NSApplication.delegate() is None), so installing our
    own delegate is safe and is the canonical way to receive the Dock-icon click.

    The delegate must implement exactly one selector; we deliberately do NOT try to
    forward unknown selectors, because forwarding through a plain NSObject subclass
    is error-prone (Objective-C selectors cannot be proxied with Python super()).
    """
    try:
        import objc
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
        self._dock_watch = None
        self._reopen_delegate = None
        self._active_observer = None

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

        Clicking the Dock icon of an app that has no visible windows makes Cocoa
        post NSApplicationDidBecomeActiveNotification (and Qt also implements
        applicationShouldHandleReopen: internally). We observe the notification and
        re-show our window. This does NOT replace the NSApplication delegate, so
        Qt's own macOS behaviour stays intact.
        """
        if sys.platform != "darwin":
            return
        # 1) Primary mechanism: install our own Cocoa delegate that implements
        #    applicationShouldHandleReopen:hasVisibleWindows: - the canonical
        #    "user clicked the Dock icon while the app has no visible windows"
        #    callback. Qt's QApplication does NOT install an NSApplication
        #    delegate for this (verified: NSApplication.delegate() is None), so
        #    this fills a gap rather than fighting Qt.
        if _ZDMReopenDelegate is not None:
            try:
                from AppKit import NSApplication
                ns = NSApplication.sharedApplication()
                d = _ZDMReopenDelegate.alloc().init()
                d._zdm_target = self
                ns.setDelegate_(d)
                self._reopen_delegate = d
                _dlog("[init] reopen delegate installed")
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
        if state == 2:
            self._maybe_restore("stateChanged")

    def _maybe_restore(self, _tag="?"):
        win = self._main_window
        if win is None:
            return
        # NOTE: on macOS, QWidget.isVisible() only reflects "not explicitly hidden"
        # and flips to True as soon as show() is called -- even if the window has
        # not actually been mapped to the screen yet. Trusting isVisible() here
        # made us skip raising/activating a window that the user could see but
        # not interact with (greyed out). We therefore treat a visible-but-not-
        # active window as "needs restoring" too.
        visible = win.isVisible()
        minimized = win.isMinimized()
        is_active = (self.applicationState() == Qt.ApplicationActive)
        _dlog(f"[restore:{_tag}] visible={visible} minimized={minimized} active={is_active}")
        try:
            if minimized:
                win.setWindowState(
                    (win.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive
                )
            elif visible and is_active:
                # Already on screen and focused - nothing to do.
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
        p.drawText(QRect(r.left(), r.top() + 2, r.width(), 16), Qt.AlignLeft | Qt.AlignVCenter, self.text)
        bar = QRect(r.left(), r.top() + 21, r.width(), 6)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(self._palette.track))
        p.drawRoundedRect(bar, 3, 3)
        w = int(bar.width() * self.pct / 100.0)
        if w > 0:
            p.setBrush(self.color)
            p.drawRoundedRect(QRect(bar.left(), bar.top(), max(4, w), bar.height()), 3, 3)


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

        self.setWindowTitle("ZDM 下载管理器")
        self.setWindowIcon(QIcon(ICON))
        self.resize(1080, 640)

        self._build_ui()
        self._build_tray()
        self._start_polling()

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
        tb.addWidget(self.url_edit)

        btn_add = QPushButton("＋ 下载")
        btn_add.setObjectName("primary")
        btn_add.clicked.connect(self._new_download)
        tb.addWidget(btn_add)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)

        for text, fn in (
            ("全部暂停", lambda: self._bulk("pause_all")),
            ("全部继续", lambda: self._bulk("resume_all")),
            ("清除已完成", lambda: self._bulk("clear_finished")),
            ("打开网页版", self._open_web),
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
                           ("paused", "已暂停"), ("completed", "已完成")):
            b = QPushButton(label)
            b.setCheckable(True)
            b.setFlat(True)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=key: self._set_filter(k))
            self.filter_btns[key] = b
            hl.addWidget(b)
        hl.addStretch(1)
        self.filter_btns["all"].setChecked(True)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["文件", "大小", "进度", "速度", "状态", "操作"])
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(46)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(False)
        self.table.setShowGrid(False)
        self.table.setFocusPolicy(Qt.NoFocus)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        hh.setFixedHeight(38)
        hh.setHighlightSections(False)
        hh.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        for col, w in ((1, 150), (2, 240), (3, 110), (4, 90), (5, 190)):
            self.table.setColumnWidth(col, w)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._menu)
        self.table.cellDoubleClicked.connect(lambda r, _c: self._open_selected_file(r))

        central = QWidget()
        v = QVBoxLayout(central)
        v.setContentsMargins(12, 10, 12, 6)
        v.setSpacing(8)
        v.addWidget(tabs)
        v.addWidget(self.table)
        self.setCentralWidget(central)

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("正在连接本地服务…")
        self.setAcceptDrops(True)

    def _build_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = None
            return
        self.tray = QSystemTrayIcon(QIcon(ICON), self)
        menu = QMenu()
        menu.addAction("显示/隐藏", self.toggle_window)
        menu.addAction("新建下载", self._new_download)
        menu.addAction("打开网页版", self._open_web)
        menu.addAction("打开下载目录", self._open_dir)
        menu.addSeparator()
        self.autostart_action = menu.addAction("开机自启")
        self.autostart_action.setCheckable(True)
        self.autostart_action.setChecked(autostart.is_enabled())
        self.autostart_action.triggered.connect(self._toggle_autostart)
        menu.addSeparator()
        menu.addAction("退出", self._quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.toggle_window()
                                    if reason == QSystemTrayIcon.Trigger else None)
        self.tray.setToolTip("ZDM 下载管理器")
        self.tray.show()

    # ---------------- data ----------------
    def _start_polling(self):
        self.poll = PollWorker(self.api)
        self.poll.tick.connect(self._on_state)
        self.poll.failed.connect(self._on_offline)
        self.poll.start()

    def _on_offline(self, msg: str):
        self.statusBar().showMessage(f"服务未连接（{msg}）— 请运行：zdm start --daemon")

    def _on_state(self, st: dict):
        self.config = st.get("config") or {}
        self.tasks = st.get("tasks") or []
        self._render()
        running = [t for t in self.tasks if t["status"] == "downloading"]
        total_speed = sum(t.get("speed") or 0 for t in running)
        self.statusBar().showMessage(
            f"已连接 · 端口 {self.config.get('port', self.port)} · 进行中 {len(running)} · "
            f"总速度 {fmt_size(total_speed)}/s · 共 {len(self.tasks)} 项"
        )

    def _set_filter(self, key: str):
        self.filter = key
        for k, b in self.filter_btns.items():
            b.setChecked(k == key)
        self._render()

    def _visible(self):
        f = self.filter
        if f == "all":
            return self.tasks
        if f == "downloading":
            return [t for t in self.tasks if t["status"] in ("downloading", "queued", "merging")]
        return [t for t in self.tasks if t["status"] == f]

    def refresh_theme(self):
        """Re-read the palette after a light/dark switch and repaint everything."""
        # ProgressCell paints with QColor() so it must be told to reload.
        for cell in self.table.findChildren(ProgressCell):
            cell.apply_palette()
        # Table text colours (status / size / speed) are set in _render.
        self._render()

    def _render(self):
        items = self._visible()
        items.sort(key=lambda t: t["created_at"], reverse=True)
        self.table.setRowCount(len(items))
        pal = palette()
        colors = {
            "downloading": pal.status_downloading, "completed": pal.status_completed,
            "paused": pal.status_paused, "error": pal.status_error,
            "queued": pal.status_queued, "canceled": pal.status_canceled,
            "merging": pal.status_merging,
        }
        for row, t in enumerate(items):
            pct = min(100.0, t.get("progress") or 0)
            status = t["status"]
            color = colors.get(status, pal.status_downloading)

            name_item = QTableWidgetItem(t["filename"])
            name_item.setToolTip(t["url"])
            f = QFont()
            f.setBold(True)
            name_item.setFont(f)
            self.table.setItem(row, 0, name_item)

            size_item = QTableWidgetItem(
                f"{fmt_size(t['downloaded'])} / {fmt_size(t['size']) if t['size'] else '未知'}"
            )
            size_item.setForeground(QColor(pal.text_muted))
            size_item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            self.table.setItem(row, 1, size_item)

            label = f"{pct:.1f}%"
            if status == "downloading" and t.get("eta"):
                label += f"  ·  剩余 {fmt_eta(t['eta'])}"
            elif status == "error" and t.get("error"):
                label = str(t["error"])[:46]
            self.table.setCellWidget(row, 2, ProgressCell(pct, label, color))

            speed_text = f"{fmt_size(t['speed'])}/s" if status == "downloading" else "—"
            speed_item = QTableWidgetItem(speed_text)
            speed_item.setForeground(QColor(pal.text_strong))
            self.table.setItem(row, 3, speed_item)

            st_item = QTableWidgetItem(STATUS_TEXT.get(status, status))
            st_item.setForeground(QColor(color))
            st_item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            self.table.setItem(row, 4, st_item)

            w = QWidget()
            hl = QHBoxLayout(w)
            hl.setContentsMargins(2, 0, 2, 0)
            hl.setSpacing(6)
            if status in ("downloading", "queued"):
                self._btn(hl, "暂停", lambda _=False, i=t["id"]: self._act(i, "pause"))
            if status in ("paused", "error", "canceled"):
                self._btn(hl, "继续", lambda _=False, i=t["id"]: self._act(i, "resume"))
            if status == "completed":
                self._btn(hl, "打开", lambda _=False, i=t["id"]: self._act(i, "open"))
            else:
                self._btn(hl, "重下", lambda _=False, i=t["id"]: self._act(i, "restart"))
            self._btn(hl, "删除", lambda _=False, i=t["id"]: self._remove(i))
            hl.addStretch(1)
            self.table.setCellWidget(row, 5, w)
            self.table.setRowHeight(row, 46)
        self._rows = {t["id"]: r for r, t in enumerate(items)}

    def _btn(self, layout, text, fn):
        b = QPushButton(text)
        b.setFixedHeight(26)
        b.setMinimumWidth(50)
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(fn)
        layout.addWidget(b)

    # ---------------- actions ----------------
    def _run(self, fn, *args):
        w = OpWorker(fn, *args)
        self._workers.append(w)
        w.done.connect(lambda status, res: self._on_op_done(status, res, w))
        w.start()

    def _on_op_done(self, status, res, worker):
        if status == "err" or (isinstance(res, dict) and not res.get("ok")):
            msg = str(res) if status == "err" else res.get("error", "操作失败")
            self.statusBar().showMessage(f"操作失败：{msg}")
        if worker in self._workers:
            self._workers.remove(worker)

    def _act(self, task_id, action):
        self._run(self.api.action, task_id, action)

    def _remove(self, task_id):
        self._run(self.api.remove, task_id)

    def _bulk(self, action):
        self._run(self.api.bulk, action)

    def _quick_add(self):
        url = self.url_edit.text().strip()
        if not url:
            return
        self.url_edit.clear()
        self._run(self.api.add, {"url": url})

    def _new_download(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("新建下载")
        form = QFormLayout(dlg)
        url = QLineEdit()
        url.setPlaceholderText("https://…")
        clip = QApplication.clipboard().text()
        if clip and clip.lower().startswith(("http://", "https://")):
            url.setText(clip.strip())
        name = QLineEdit()
        name.setPlaceholderText("留空自动识别")
        conns = QSpinBox()
        conns.setRange(1, 32)
        conns.setValue(int(self.config.get("connections") or 8))
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
        referer.setPlaceholderText("可选，部分站点需要")
        form.addRow("链接 URL", url)
        form.addRow("保存为", name)
        form.addRow("连接数", conns)
        form.addRow("保存到", row)
        form.addRow("Referer", referer)
        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.accepted.connect(dlg.accept)
        box.rejected.connect(dlg.reject)
        form.addRow(box)
        if dlg.exec_() != QDialog.Accepted:
            return
        u = url.text().strip()
        if not u:
            return
        payload = {"url": u, "connections": conns.value()}
        if name.text().strip():
            payload["filename"] = name.text().strip()
        if directory.text().strip():
            payload["dir"] = directory.text().strip()
        if referer.text().strip():
            payload["headers"] = {"Referer": referer.text().strip()}
        self._run(self.api.add, payload)

    def _settings(self):
        cfg = self.config or {}
        dlg = QDialog(self)
        dlg.setWindowTitle("设置")
        form = QFormLayout(dlg)
        directory = QLineEdit(str(cfg.get("download_dir") or ""))
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
        port = QSpinBox()
        port.setRange(1024, 65535)
        port.setValue(int(cfg.get("port") or 9000))
        auto = QCheckBox("重启后自动继续未完成任务")
        auto.setChecked(bool(cfg.get("auto_resume", True)))
        autostart_cb = QCheckBox("开机自动启动 ZDM 服务")
        autostart_cb.setChecked(autostart.is_enabled())
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
        box = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
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
            return
        # Persist the chosen appearance (and apply it if it changed).
        self._theme_preview = None
        tm = theme_manager()
        if tm is not None:
            chosen = appearance.currentData()
            if chosen and chosen != tm.mode:
                tm.set_mode(chosen)
        try:
            autostart.set_enabled(autostart_cb.isChecked())
        except Exception as e:
            self.statusBar().showMessage(f"开机自启设置失败：{e}")
        self._run(self.api.set_config, {
            "download_dir": directory.text().strip() or cfg.get("download_dir"),
            "connections": conns.value(),
            "max_active": active.value(),
            "speed_limit": limit.value() * 1024,
            "proxy": proxy.text().strip(),
            "port": port.value(),
            "auto_resume": auto.isChecked(),
        })
        if getattr(self, "autostart_action", None) is not None:
            self.autostart_action.setChecked(autostart.is_enabled())
        self.statusBar().showMessage("设置已保存")

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
        try:
            import webbrowser
            webbrowser.open(f"http://127.0.0.1:{port}/")
            self.statusBar().showMessage(f"已在浏览器打开网页版：http://127.0.0.1:{port}/")
        except Exception as e:
            self.statusBar().showMessage(f"打开网页版失败：{e}")

    def _open_selected_file(self, row):
        item = self.table.item(row, 0)
        if not item:
            return
        t = next((x for x in self._visible() if x["filename"] == item.text()), None)
        if t:
            if t["status"] == "completed":
                self._run(self.api.open_path, os.path.join(t["directory"], t["filename"]))
            else:
                self._run(self.api.open_path, t["directory"])

    def _menu(self, pos):
        row = self.table.rowAt(pos.y())
        if row < 0:
            return
        item = self.table.item(row, 0)
        t = next((x for x in self._visible() if x["filename"] == item.text()), None)
        if not t:
            return
        menu = QMenu(self)
        menu.addAction("复制链接", lambda: QApplication.clipboard().setText(t["url"]))
        menu.addSeparator()
        menu.addAction("暂停", lambda: self._act(t["id"], "pause"))
        menu.addAction("继续", lambda: self._act(t["id"], "resume"))
        menu.addAction("重新下载", lambda: self._act(t["id"], "restart"))
        menu.addSeparator()
        menu.addAction("删除任务", lambda: self._remove(t["id"]))
        menu.exec_(self.table.viewport().mapToGlobal(pos))

    def wake(self, payload: str = ""):
        """Raise the existing window (used when a second instance starts)."""
        self.show()
        self.setWindowState((self.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive)
        self.raise_()
        self.activateWindow()
        for token in (payload or "").split():
            if token.lower().startswith(("http://", "https://")):
                self._run(self.api.add, {"url": token})

    def toggle_window(self):
        # isVisible() can be True while the window is not really on screen / not
        # focused (macOS show() is async). Treat "visible but not active" as
        # visible and just re-raise it instead of toggling.
        app = QApplication.instance()
        active = (app is not None and app.applicationState() == Qt.ApplicationActive)
        if self.isVisible() and not self.isMinimized() and active:
            self.hide()
        else:
            self.show()
            self.setWindowState((self.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive)
            self.raise_()
            self.activateWindow()

    # ---------------- events ----------------
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
                self._run(self.api.add, {"url": u.strip()})

    def closeEvent(self, e):
        if self.tray is not None and self.tray.isVisible():
            self.hide()
            self.tray.showMessage("ZDM", "已最小化到托盘，右键托盘图标可退出",
                                  QSystemTrayIcon.Information, 2500)
            e.ignore()
            return
        self._quit()

    def _quit(self):
        try:
            self.poll.stop()
            self.poll.wait(2000)
        except Exception:
            pass
        if self.tray:
            self.tray.hide()
        QApplication.quit()

    def _toggle_autostart(self, checked: bool):
        try:
            autostart.set_enabled(checked)
            ok = autostart.is_enabled() == checked
            self.statusBar().showMessage(
                "开机自启：" + ("已开启" if checked else "已关闭")
                + ("" if ok else "（无法写入，可能需要管理员/登录权限）")
            )
        except Exception as e:
            self.statusBar().showMessage(f"开机自启设置失败：{e}")


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

    app = ZDMApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    app.setStyle("Fusion")

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
    win = MainWindow(api, port)
    app.set_main_window(win)
    guard.on_message(lambda payload: win.wake(payload))
    for token in rest:
        if token.lower().startswith(("http://", "https://")):
            win._run(api.add, {"url": token})
    win.show()
    if smoke:
        QTimer.singleShot(3500, win._quit)
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
