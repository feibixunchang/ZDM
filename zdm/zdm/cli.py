"""Command line interface for ZDM."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Optional

from . import desktop, runtime, server
from .manager import Manager, config_dir, load_config

PID_FILE = os.path.join(config_dir(), "zdm.pid")


def _detached() -> dict:
    """Popen kwargs that detach a background process on POSIX and Windows."""
    if os.name == "nt":
        return {"creationflags": 0x00000008}  # DETACHED_PROCESS
    return {"start_new_session": True}


def _api(method: str, path: str, payload: Optional[dict] = None, port: int = 0) -> dict:
    import urllib.error
    import urllib.request

    port = port or 0
    url = f"http://127.0.0.1:{port}/api/{path.lstrip('/')}"
    data = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return json.loads(body)
        except Exception:
            return {"ok": False, "error": body}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"service not reachable: {e}"}


def _resolve_port(args) -> int:
    port = getattr(args, "port", 0) or 0
    if port:
        return int(args.port)
    m = Manager()
    return int(m.config.get("port") or 9000)


def _fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024
    return f"{n:.1f}TB"


def cmd_start(args) -> int:
    port = int(getattr(args, "port", 0) or 0) or int(load_config().get("port") or 9000)
    state = server.ping(port=port)
    if state and not args.force:
        print(f"service already running on port {port}")
        return 0
    if args.daemon:
        # Re-run ourselves, not "python3 zdm.py": in a frozen build there is no
        # script on disk and sys.executable is the bundle's own executable.
        cmd = runtime.cli_cmd("serve", "--port", str(port))
        log = os.path.join(config_dir(), "zdm.log")
        with open(log, "a", encoding="utf-8") as lf:
            p = subprocess.Popen(
                cmd,
                stdout=lf,
                stderr=lf,
                stdin=subprocess.DEVNULL,
                **_detached(),
            )
        with open(PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(p.pid))
        for _ in range(40):
            time.sleep(0.15)
            if server.ping(port=port):
                print(f"ZDM started in background (pid {p.pid}) -> http://127.0.0.1:{port}/")
                return 0
        print(f"started (pid {p.pid}), waiting for service... check {log}")
        return 0
    m = Manager()
    server.serve(m, port=port, open_ui=not args.no_open)
    return 0


def cmd_serve(args) -> int:
    m = Manager()
    port = int(getattr(args, "port", 0) or 0) or int(m.config.get("port") or 9000)
    server.serve(m, port=port, open_ui=False)
    return 0


def cmd_stop(args) -> int:
    port = _resolve_port(args)
    r = _api("POST", "action", {"action": "shutdown"}, port=port)
    try:
        if os.path.exists(PID_FILE):
            with open(PID_FILE, encoding="utf-8") as f:
                pid = int(f.read().strip())
            os.kill(pid, 15)
            os.remove(PID_FILE)
    except Exception:
        pass
    print("stopped" if r.get("ok") else f"no running service ({r.get('error')})")
    return 0


def cmd_status(args) -> int:
    port = _resolve_port(args)
    st = server.ping(port=port)
    if not st:
        print("service: stopped")
        return 1
    print(f"service: running on http://127.0.0.1:{port}/")
    r = _api("GET", "tasks", port=port)
    for t in r.get("tasks", []):
        print(
            f"  {t['id']}  {t['status']:<11} {t['progress']:>6.1f}%  "
            f"{_fmt_size(t['downloaded'])}/{_fmt_size(t['size'])}  {t['filename']}"
        )
    return 0


def cmd_add(args) -> int:
    port = _resolve_port(args)
    if not server.ping(port=port):
        print("service not running. Start it first:  zdm start --daemon")
        return 1
    payload = {"url": args.url}
    if args.dir:
        payload["dir"] = os.path.abspath(os.path.expanduser(args.dir))
    if args.name:
        payload["filename"] = args.name
    if args.connections:
        payload["connections"] = int(args.connections)
    if args.header:
        headers = {}
        for h in args.header:
            k, _, v = h.partition(":")
            headers[k.strip()] = v.strip()
        payload["headers"] = headers
    r = _api("POST", "tasks", payload, port=port)
    if not r.get("ok"):
        print(f"error: {r.get('error')}")
        return 1
    print(f"queued {r['id']}: {args.url}")
    # Make sure the user actually sees the download: open the desktop client if
    # it is not running (the service already does this for extension requests;
    # the CLI talks to the same API but with auto_show_gui it would be a no-op
    # only when ZDM is already visible).
    if load_config().get("auto_show_gui", True):
        # No url here: the API call above already queued it. Passing it again
        # makes the GUI (or the already-running instance it wakes) add a
        # duplicate task.
        desktop.ensure_desktop([], port)
    return 0


def cmd_list(args) -> int:
    port = _resolve_port(args)
    r = _api("GET", "tasks", port=port)
    if not r.get("ok"):
        print(f"error: {r.get('error')}")
        return 1
    tasks = r["tasks"]
    if args.json:
        print(json.dumps(tasks, ensure_ascii=False, indent=2))
        return 0
    if not tasks:
        print("(empty)")
        return 0
    print(f"{'ID':<14}{'STATUS':<12}{'%':>7}{'SIZE':>18}  NAME")
    for t in tasks:
        print(
            f"{t['id']:<14}{t['status']:<12}{t['progress']:>6.1f}%"
            f"{_fmt_size(t['downloaded']) + '/' + _fmt_size(t['size']):>18}  {t['filename']}"
        )
    return 0


def _task_action(args, action: str) -> int:
    port = _resolve_port(args)
    r = _api("POST", f"tasks/{args.id}/{action}", {}, port=port)
    print("ok" if r.get("ok") else f"error: {r.get('error')}")
    return 0 if r.get("ok") else 1


def cmd_remove(args) -> int:
    port = _resolve_port(args)
    suffix = "?files=0" if args.keep_files else ""
    r = _api("DELETE", f"tasks/{args.id}{suffix}", port=port)
    print("ok" if r.get("ok") else f"error: {r.get('error')}")
    return 0 if r.get("ok") else 1


def cmd_config(args) -> int:
    port = _resolve_port(args)
    if args.key and args.value is not None:
        raw = args.value
        if raw.lower() in ("true", "false"):
            val = raw.lower() == "true"
        else:
            try:
                val = int(raw)
            except ValueError:
                try:
                    val = float(raw)
                except ValueError:
                    val = raw
        r = _api("POST", "config", {args.key: val}, port=port)
        print(json.dumps(r.get("config", {}), ensure_ascii=False, indent=2))
        return 0
    print(json.dumps(load_config(), ensure_ascii=False, indent=2))
    return 0


def _python_with_qt() -> Optional[str]:
    """Find an interpreter that can import PyQt5 (desktop UI)."""
    import shutil

    candidates = [sys.executable, shutil.which("python3"), "/usr/bin/python3"]
    seen = set()
    for py in candidates:
        if not py or py in seen:
            continue
        seen.add(py)
        try:
            r = subprocess.run(
                [py, "-c", "import PyQt5.QtWidgets"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
            )
            if r.returncode == 0:
                return py
        except Exception:
            continue
    return None


def cmd_gui(args) -> int:
    """Launch the native desktop window (falls back to the web UI)."""
    import webbrowser

    port = _resolve_port(args)
    if not server.ping(port=port):
        subprocess.run(
            runtime.cli_cmd("start", "--daemon", "--port", str(port)),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        for _ in range(30):
            time.sleep(0.2)
            if server.ping(port=port):
                break

    # desktop.ensure_desktop knows about the macOS app bundle / Windows exe and
    # wakes an already-running instance instead of starting a second one.
    desktop._last_launch = 0.0  # explicit request: bypass the throttle
    if desktop.ensure_desktop([], port):
        print(f"ZDM 桌面端已启动（端口 {port}）；若已在运行则已激活现有窗口")
        return 0
    print(f"未找到可用的桌面端（{desktop.last_error() or '缺少 PyQt5'}），改用浏览器界面")
    webbrowser.open(f"http://127.0.0.1:{port}/")
    return 0


def cmd_open(args) -> int:
    port = _resolve_port(args)
    import webbrowser

    webbrowser.open(f"http://127.0.0.1:{port}/")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="zdm", description="ZDM download manager")
    p.add_argument("--port", type=int, default=0, help="service port (default 9000)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start", help="start the service")
    s.add_argument("--port", type=int, default=argparse.SUPPRESS)
    s.add_argument("-d", "--daemon", action="store_true", help="run in background")
    s.add_argument("--no-open", action="store_true", help="do not open the web UI")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_start)

    s = sub.add_parser("serve", help="run service in foreground (internal)")
    s.add_argument("--port", type=int, default=argparse.SUPPRESS)
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("stop", help="stop the service")
    s.add_argument("--port", type=int, default=argparse.SUPPRESS)
    s.set_defaults(func=cmd_stop)
    s = sub.add_parser("status", help="show service and tasks")
    s.add_argument("--port", type=int, default=argparse.SUPPRESS)
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("add", help="add a download")
    s.add_argument("url")
    s.add_argument("-o", "--dir", help="output directory")
    s.add_argument("-n", "--name", help="file name")
    s.add_argument("-c", "--connections", type=int)
    s.add_argument("-H", "--header", action="append", help="extra header, e.g. 'Referer: https://x'")
    s.add_argument("--port", type=int, default=argparse.SUPPRESS)
    s.set_defaults(func=cmd_add)

    s = sub.add_parser("list", help="list downloads")
    s.add_argument("--json", action="store_true")
    s.add_argument("--port", type=int, default=argparse.SUPPRESS)
    s.set_defaults(func=cmd_list)

    for name, action in (("pause", "pause"), ("resume", "resume"), ("cancel", "cancel"), ("restart", "restart")):
        s = sub.add_parser(name, help=f"{action} a download")
        s.add_argument("id")
        s.add_argument("--port", type=int, default=argparse.SUPPRESS)
        s.set_defaults(func=lambda a, act=action: _task_action(a, act))

    s = sub.add_parser("remove", help="remove a download from the list")
    s.add_argument("id")
    s.add_argument("--keep-files", action="store_true")
    s.add_argument("--port", type=int, default=argparse.SUPPRESS)
    s.set_defaults(func=cmd_remove)

    s = sub.add_parser("config", help="show or set config")
    s.add_argument("key", nargs="?")
    s.add_argument("value", nargs="?")
    s.add_argument("--port", type=int, default=argparse.SUPPRESS)
    s.set_defaults(func=cmd_config)

    s = sub.add_parser("gui", help="open the native desktop window")
    s.add_argument("--port", type=int, default=argparse.SUPPRESS)
    s.set_defaults(func=cmd_gui)

    s = sub.add_parser("open", help="open the web UI")
    s.add_argument("--port", type=int, default=argparse.SUPPRESS)
    s.set_defaults(func=cmd_open)
    return p


def main(argv=None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        argv = ["start"]
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
