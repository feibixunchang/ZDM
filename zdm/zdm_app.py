"""Entry point for the frozen desktop build (PyInstaller).

Two names point at the same executable, which is what makes one binary serve
both roles - exactly like the macOS bundle's launcher:

* ``ZDM`` / ``zdm``       -> the CLI (``zdm start``, ``zdm add URL``, ...)
* ``zdm-desktop``         -> the Qt window (what the .desktop file launches)

``zdm gui`` still works: the CLI hands over to :func:`zdm.desktop.ensure_desktop`,
which looks up ``zdm-desktop`` on PATH and falls back to re-execing this same
binary.

``--selfcheck`` is for packaging/CI: it reports what the bundle can actually do
(interpreter, PyQt5, resource paths) without needing a display.
"""

from __future__ import annotations

import os
import sys


def _print_version() -> int:
    from zdm import __version__

    print(f"ZDM {__version__}")
    return 0


def _selfcheck() -> int:
    """Machine-readable smoke test for the packaged app (no GUI needed)."""
    import json

    from zdm import __version__, runtime

    root = runtime.app_root()
    report = {
        "version": __version__,
        "frozen": runtime.is_frozen(),
        "executable": runtime.exe_path(),
        "app_root": root,
        "python": sys.version.split()[0],
        "web_index": os.path.exists(os.path.join(root, "web", "index.html")),
        "extension_manifest": os.path.exists(
            os.path.join(root, "extension", "manifest.json")
        ),
        # The media sniffer is a separate content script; a package that
        # ships the manifest but forgets sniffer.js would look fine here and
        # still fail the moment the user opens a video page.
        "extension_sniffer": os.path.exists(
            os.path.join(root, "extension", "sniffer.js")
        ),
        "theme_module": False,
        "pyqt5": "",
        "qt": "",
    }
    try:
        import theme  # noqa: F401  (sits next to gui.py, not in the package)

        report["theme_module"] = True
    except Exception as e:  # noqa: BLE001
        report["theme_module_error"] = f"{type(e).__name__}: {e}"
    try:
        from PyQt5.QtCore import QT_VERSION_STR, PYQT_VERSION_STR
        import PyQt5

        report["pyqt5"] = PYQT_VERSION_STR
        report["qt"] = QT_VERSION_STR
        report["pyqt5_path"] = os.path.dirname(PyQt5.__file__)
    except Exception as e:  # noqa: BLE001
        report["pyqt5_error"] = f"{type(e).__name__}: {e}"
    print(json.dumps(report, ensure_ascii=False, indent=2))
    ok = bool(
        report["web_index"]
        and report["extension_manifest"]
        and report["extension_sniffer"]
        and report["theme_module"]
        and report["pyqt5"]
    )
    return 0 if ok else 1


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if argv and argv[0] in ("--version", "-V", "version"):
        return _print_version()
    if argv and argv[0] == "--selfcheck":
        return _selfcheck()

    # Explicit role override: desktop.py re-execs the frozen binary with this
    # flag because argv[0] there is "ZDM" (CLI name), not "zdm-desktop".
    if argv and argv[0] == "--gui":
        import gui

        return gui.main(argv[1:])

    name = os.path.basename(sys.argv[0] or "").lower()
    if name.startswith(("zdm-desktop", "zdm-gui")):
        import gui

        return gui.main(argv)

    from zdm.cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    sys.exit(main())
