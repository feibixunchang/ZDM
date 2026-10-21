"""Theme handling for the ZDM desktop client (light / dark, all platforms).

Design goals
------------
1. **Unified**: every colour used by the desktop client comes from a single
   palette object. No widget is allowed to hardcode a hex colour, otherwise a
   dark theme ends up with light-coloured fragments on a dark background.
2. **Follows the system by default**: on macOS we observe
   ``AppleInterfaceThemeChangedNotification``; on Windows we poll the registry
   ``AppsUseLightTheme``; on Linux we watch the ``gtk-theme``/``color-scheme``
   gsettings key when available. The user can override with an explicit
   choice (auto / light / dark) that persists across restarts.
3. **Same behaviour on Linux / macOS / Windows**: one code path, platform
   differences only in how the system preference is read.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

APP_NAME = "ZDM"


# --------------------------------------------------------------------------- paths
def _conf_dir() -> str:
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    elif os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, APP_NAME.lower())


def _theme_conf_path() -> str:
    return os.path.join(_conf_dir(), "theme.json")


# --------------------------------------------------------------------------- palettes
class Palette:
    """A flat set of semantic colours. Attribute names are shared by both themes."""

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)

    def as_dict(self) -> dict:
        return {k: v for k, v in vars(self).items() if not k.startswith("_")}


# Light theme tuned for a white/very light neutral surface.
LIGHT = Palette(
    window="#f5f7fa",
    surface="#ffffff",
    surface_alt="#fafbfc",
    hover="#f3f4f6",
    pressed="#e8ecf1",
    border="#e3e8ef",
    border_soft="#f1f3f7",
    text="#1f2937",
    text_muted="#6b7280",
    text_strong="#374151",
    accent="#2563eb",
    accent_hover="#1d4fd8",
    accent_pressed="#1a45bf",
    accent_soft="#eff4ff",
    accent_soft_border="#c7d7fe",
    accent_text="#ffffff",
    track="#e8ecf1",
    # per-status colours (progress bar + status text)
    status_downloading="#2563eb",
    status_completed="#16a34a",
    status_paused="#d97706",
    status_error="#dc2626",
    status_queued="#94a3b8",
    status_canceled="#94a3b8",
    status_merging="#7c3aed",
    # QR / dialog surface
    selection="#eff4ff",
    selection_text="#1f2937",
    # hairline around a vibrancy (frosted glass) popup: the menu is transparent
    # on macOS so the NSVisualEffectView behind it shows through.
    glass_border="rgba(0, 0, 0, 0.14)",
    # On Linux there is no system blur API, so the popup paints this colour
    # itself at `glass_alpha` opacity (255 = fully solid, for desktops without a
    # compositing manager).
    glass_fill="#ffffff",
    glass_alpha=232,
)

# Dark theme: neutral slate surfaces, same hue family for accents so the UI keeps
# its identity rather than turning into a different-looking app.
DARK = Palette(
    window="#16181d",
    surface="#1e2128",
    surface_alt="#23262e",
    hover="#2a2e37",
    pressed="#333843",
    border="#333944",
    border_soft="#282c34",
    text="#e6e8ec",
    text_muted="#9aa3b2",
    text_strong="#cfd4dc",
    accent="#3b82f6",
    accent_hover="#2f6fe0",
    accent_pressed="#2559bd",
    accent_soft="#1e2c4d",
    accent_soft_border="#2f4a7a",
    accent_text="#ffffff",
    track="#31353e",
    status_downloading="#60a5fa",
    status_completed="#4ade80",
    status_paused="#fbbf24",
    status_error="#f87171",
    status_queued="#94a3b8",
    status_canceled="#94a3b8",
    status_merging="#a78bfa",
    selection="#24334f",
    selection_text="#e6e8ec",
    glass_border="rgba(255, 255, 255, 0.18)",
    glass_fill="#1e2128",
    glass_alpha=238,
)

PALETTES = {"light": LIGHT, "dark": DARK}


# --------------------------------------------------------------------------- stylesheet
def build_stylesheet(p: Palette) -> str:
    """Render the QSS for a palette. Every colour comes from `p` - no literals."""
    # Windows 上 px 字号在 125%/150% 缩放下会被 Qt 舍入成难看的位图感；
    # 统一用 pt + 雅黑，和按钮（font-weight:600）保持同一套字体度量。
    base_font = (
        'font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI"; '
        "font-size: 10pt;"
        if sys.platform == "win32"
        else "font-size: 13px;"
    )
    return f"""
QMainWindow, QDialog {{ background: {p.window}; }}
QWidget {{ {base_font} color: {p.text}; }}
QToolBar {{ background: {p.surface}; border: none; border-bottom: 1px solid {p.border};
    padding: 8px 10px; spacing: 6px; }}
QToolBar QLabel {{ color: {p.text_muted}; }}
QLineEdit, QSpinBox {{ background: {p.surface}; border: 1px solid {p.border};
    border-radius: 7px; padding: 6px 9px; color: {p.text}; }}
QLineEdit:focus, QSpinBox:focus {{ border-color: {p.accent}; }}
QPushButton {{ background: {p.surface}; border: 1px solid {p.border};
    border-radius: 7px; padding: 5px 12px; color: {p.text}; }}
QPushButton:hover {{ background: {p.hover}; }}
QPushButton:pressed {{ background: {p.pressed}; }}
QPushButton:disabled {{ background: {p.surface_alt}; border-color: {p.border_soft};
    color: {p.text_muted}; }}
QPushButton:checked {{ background: {p.accent_soft}; border-color: {p.accent_soft_border};
    color: {p.accent}; font-weight: 600; }}
QTableWidget {{ background: {p.surface}; border: 1px solid {p.border}; border-radius: 10px;
    gridline-color: {p.border_soft}; color: {p.text}; }}
QTableWidget::item {{ border-bottom: 1px solid {p.border_soft}; padding: 6px 8px; }}
QTableWidget::item:selected {{ background: {p.selection}; color: {p.selection_text}; }}
QHeaderView::section {{ background: {p.surface_alt}; border: none;
    border-bottom: 1px solid {p.border}; padding: 9px 8px; color: {p.text_muted};
    font-weight: 600; }}
QHeaderView::section:hover {{ background: {p.hover}; }}
QStatusBar {{ background: {p.surface}; border-top: 1px solid {p.border}; color: {p.text_muted}; }}
QStatusBar QLabel {{ color: {p.text_muted}; }}
QProgressBar {{ border: none; background: transparent; }}
QPushButton#primary {{ background: {p.accent}; border: 1px solid {p.accent};
    color: {p.accent_text}; font-weight: 600; padding: 6px 16px; }}
QPushButton#primary:hover {{ background: {p.accent_hover}; border-color: {p.accent_hover}; }}
QPushButton#primary:pressed {{ background: {p.accent_pressed}; }}
QProgressBar::chunk {{ background: {p.accent}; border-radius: 3px; }}
QScrollBar:vertical {{ background: {p.window}; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {p.border}; border-radius: 5px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {p.text_muted}; }}
QScrollBar:horizontal {{ background: {p.window}; height: 10px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: {p.border}; border-radius: 5px; min-width: 30px; }}
QScrollBar::handle:horizontal:hover {{ background: {p.text_muted}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QMenu {{ background: {p.surface}; border: 1px solid {p.border}; border-radius: 8px; padding: 4px; }}
QMenu::item {{ padding: 6px 22px 6px 14px; border-radius: 6px; color: {p.text}; }}
QMenu::item:selected {{ background: {p.accent_soft}; color: {p.accent}; }}
QMenu::separator {{ height: 1px; background: {p.border}; margin: 4px 8px; }}
/* Frosted-glass popup: the widget paints nothing but text, a hairline and the
   rounded shape. macOS blurs the desktop behind it with an NSVisualEffectView;
   on Linux GlassMenu paints {p.glass_fill} at glass_alpha inside this border.
   Radius must match GLASS_RADIUS in gui.py. */
QMenu#glassMenu {{ background: transparent; border: 1px solid {p.glass_border};
    border-radius: 12px; padding: 5px; }}
QMenu#glassMenu::item {{ padding: 7px 24px 7px 14px; border-radius: 7px; color: {p.text}; }}
QMenu#glassMenu::item:selected {{ background: {p.accent_soft}; color: {p.accent}; }}
QMenu#glassMenu::item:disabled {{ color: {p.text_muted}; }}
QMenu#glassMenu::separator {{ height: 1px; background: {p.glass_border}; margin: 4px 10px; }}
QCheckBox, QRadioButton {{ color: {p.text}; spacing: 6px; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 15px; height: 15px; }}
QComboBox {{ background: {p.surface}; border: 1px solid {p.border}; border-radius: 7px;
    padding: 5px 10px; color: {p.text}; }}
QComboBox:hover {{ background: {p.hover}; }}
QComboBox QAbstractItemView {{ background: {p.surface}; border: 1px solid {p.border};
    selection-background-color: {p.accent_soft}; selection-color: {p.accent};
    outline: none; padding: 4px; }}
QToolTip {{ background: {p.surface_alt}; color: {p.text}; border: 1px solid {p.border};
    padding: 4px 7px; border-radius: 6px; }}
QGroupBox {{ border: 1px solid {p.border}; border-radius: 9px; margin-top: 10px;
    padding-top: 8px; color: {p.text_muted}; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px;
    color: {p.text_muted}; }}
QSpinBox::up-button, QSpinBox::down-button {{ background: {p.surface_alt};
    border: none; width: 16px; }}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {{ background: {p.hover}; }}
/* update banner (new release available) */
QFrame#updateBar {{ background: {p.accent_soft}; border: 1px solid {p.accent_soft_border};
    border-radius: 9px; }}
QLabel#updateText {{ color: {p.text}; font-weight: 600; background: transparent; }}
QLabel#versionLabel {{ color: {p.text_muted}; padding: 0 8px; }}
QPushButton#updatePrimary {{ background: {p.accent}; border: 1px solid {p.accent};
    color: {p.accent_text}; font-weight: 600; }}
QPushButton#updatePrimary:hover {{ background: {p.accent_hover}; border-color: {p.accent_hover}; }}
QPushButton#updatePrimary:pressed {{ background: {p.accent_pressed}; }}
"""


# --------------------------------------------------------------------------- system mode
def _macos_is_dark() -> bool:
    """Read the macOS system appearance."""
    try:
        out = subprocess.check_output(
            ["defaults", "read", "-g", "AppleInterfaceStyle"],
            stderr=subprocess.DEVNULL, timeout=3,
        )
        return out.strip() == b"Dark"
    except Exception:
        # Key absent == Light appearance
        return False


def _windows_is_dark() -> bool:
    """Read the Windows AppsUseLightTheme registry value."""
    if sys.platform != "win32":
        return False
    try:
        import winreg  # type: ignore
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )
        value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return int(value) == 0
    except Exception:
        return False


def _linux_is_dark() -> bool:
    """Best-effort read of the GNOME/GTK colour preference."""
    if sys.platform != "linux":
        return False
    # 1) modern gsettings colour-scheme: "prefer-dark" / "default"
    for getter in (
        ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
        ["gsettings", "get", "org.gnome.desktop.interface", "gtk-theme"],
    ):
        try:
            out = subprocess.check_output(getter, stderr=subprocess.DEVNULL, timeout=3)
            val = out.decode().strip().strip('"').lower()
            if "dark" in val:
                return True
            if getter[-1] == "color-scheme" and val == "default":
                break
        except Exception:
            continue
    # 2) XDG portal / environment hints used by some desktops
    env = (os.environ.get("GTK_THEME") or "").lower()
    if "dark" in env:
        return True
    return False


def system_prefers_dark() -> bool:
    if sys.platform == "darwin":
        return _macos_is_dark()
    if sys.platform == "win32":
        return _windows_is_dark()
    if sys.platform.startswith("linux"):
        return _linux_is_dark()
    return False


# --------------------------------------------------------------------------- manager
class ThemeManager:
    """Resolves the effective mode and applies the matching stylesheet.

    ``mode`` is one of "auto" / "light" / "dark" and is persisted so the user's
    explicit choice survives restarts.
    """

    def __init__(self, qapp=None):
        self.qapp = qapp
        self.mode = self._load_mode()
        self._watchers = []

    # -- persistence -----------------------------------------------------------
    @staticmethod
    def _load_mode() -> str:
        try:
            with open(_theme_conf_path(), "r", encoding="utf-8") as f:
                mode = json.load(f).get("mode", "auto")
            return mode if mode in ("auto", "light", "dark") else "auto"
        except Exception:
            return "auto"

    def _save_mode(self) -> None:
        try:
            os.makedirs(_conf_dir(), exist_ok=True)
            with open(_theme_conf_path(), "w", encoding="utf-8") as f:
                json.dump({"mode": self.mode}, f)
        except Exception:
            pass

    # -- resolution ------------------------------------------------------------
    def is_dark(self) -> bool:
        if self.mode == "dark":
            return True
        if self.mode == "light":
            return False
        return system_prefers_dark()

    def palette(self) -> Palette:
        return DARK if self.is_dark() else LIGHT

    # -- application -----------------------------------------------------------
    def apply(self) -> None:
        """Apply the effective palette to the running application."""
        if self.qapp is None:
            return
        p = self.palette()
        self.qapp.setStyleSheet(build_stylesheet(p))
        self.qapp.setPalette(_qt_palette(p))
        # Let widgets that paint with QColor() (progress bars, table text)
        # repaint against the new background.
        try:
            for w in self.qapp.topLevelWidgets():
                w.style().unpolish(w)
                w.style().polish(w)
                w.update()
        except Exception:
            pass
        # Give the app a chance to refresh colour-aware widgets (our
        # ZDMApplication implements notify_theme_changed).
        try:
            if hasattr(self.qapp, "notify_theme_changed"):
                self.qapp.notify_theme_changed()
        except Exception:
            pass

    def set_mode(self, mode: str) -> None:
        if mode not in ("auto", "light", "dark"):
            return
        self.mode = mode
        self._save_mode()
        self.apply()
        # Switching back to "follow the system" has to (re)install the watcher,
        # otherwise the appearance stops tracking the OS until a restart.
        if mode == "auto":
            self.prime()
            self.watch_system()

    # -- system watching -------------------------------------------------------
    def _stop_watchers(self) -> None:
        for w in self._watchers:
            try:
                if hasattr(w, "stop"):  # QTimer
                    w.stop()
                    w.deleteLater()
                else:  # NSObject observer token from addObserver...usingBlock_
                    from Foundation import NSNotificationCenter

                    NSNotificationCenter.defaultCenter().removeObserver_(w)
            except Exception:
                pass
        self._watchers = []

    def watch_system(self) -> None:
        """Re-apply the stylesheet when the OS appearance changes.

        Only meaningful while ``mode == "auto"``; the explicit modes are stable.
        Safe to call repeatedly: previous watchers are torn down first.
        """
        if self.mode != "auto":
            return
        self._stop_watchers()
        if sys.platform == "darwin":
            try:
                from Foundation import (
                    NSDistributedNotificationCenter,
                    NSOperationQueue,
                )

                # The constant is NOT exported by AppKit (importing
                # NSWorkspaceDidChangeNotification raises and used to be
                # swallowed, silently downgrading us to the 2s poll). It lives
                # on the *distributed* center, not NSWorkspace's.
                token = NSDistributedNotificationCenter.defaultCenter() \
                    .addObserverForName_object_queue_usingBlock_(
                        "AppleInterfaceThemeChangedNotification",
                        None,
                        NSOperationQueue.mainQueue(),
                        lambda _n: self._on_system_change(),
                    )
                self._watchers.append(token)
                return
            except Exception:
                pass
        # Portable fallback for Linux/Windows (and if the observer failed):
        # a light poll is cheap and robust.
        try:
            from PyQt5.QtCore import QTimer
            t = QTimer()
            t.setInterval(2000)
            t.timeout.connect(self._on_system_change)
            t.start()
            self._watchers.append(t)
        except Exception:
            pass

    def _on_system_change(self) -> None:
        if self.mode != "auto":
            return
        # Only re-apply when the effective palette actually flipped, otherwise we
        # would rebuild the stylesheet every tick for no reason.
        try:
            want_dark = system_prefers_dark()
            have_dark = self._last_dark
            if want_dark != have_dark:
                self._last_dark = want_dark
                self.apply()
        except Exception:
            pass

    # track last known state so _on_system_change can detect flips
    _last_dark: bool = False

    def prime(self) -> None:
        """Record the current effective mode so polling can detect flips."""
        self._last_dark = self.is_dark()


def _qt_palette(p: Palette):
    """Build a QPalette so non-stylesheet widgets (tooltips, disabled text, …)
    also follow the theme."""
    from PyQt5.QtGui import QPalette
    from PyQt5.QtCore import Qt

    qp = QPalette()
    qp.setColor(QPalette.Window, _q(p.window))
    qp.setColor(QPalette.WindowText, _q(p.text))
    qp.setColor(QPalette.Base, _q(p.surface))
    qp.setColor(QPalette.AlternateBase, _q(p.surface_alt))
    qp.setColor(QPalette.ToolTipBase, _q(p.surface_alt))
    qp.setColor(QPalette.ToolTipText, _q(p.text))
    qp.setColor(QPalette.Text, _q(p.text))
    qp.setColor(QPalette.Button, _q(p.surface))
    qp.setColor(QPalette.ButtonText, _q(p.text))
    qp.setColor(QPalette.BrightText, _q("#ff6b6b"))
    qp.setColor(QPalette.Link, _q(p.accent))
    qp.setColor(QPalette.Highlight, _q(p.accent))
    qp.setColor(QPalette.HighlightedText, _q(p.accent_text))
    qp.setColor(QPalette.Mid, _q(p.border))
    qp.setColor(QPalette.Dark, _q(p.border))
    qp.setColor(QPalette.Shadow, _q(p.track))
    qp.setColor(QPalette.PlaceholderText, _q(p.text_muted))
    qp.setColor(QPalette.Disabled, QPalette.Text, _q(p.text_muted))
    qp.setColor(QPalette.Disabled, QPalette.ButtonText, _q(p.text_muted))
    qp.setColor(QPalette.Disabled, QPalette.WindowText, _q(p.text_muted))
    return qp


def _q(hex_str: str):
    from PyQt5.QtGui import QColor
    return QColor(hex_str)