"""Local HTTP service: REST API, SSE stream and static Web UI."""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Tuple

from .manager import Manager

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
VERSION = "1.0.0"

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    manager: Manager
    token: str = ""

    # ---------- helpers ----------
    def log_message(self, fmt: str, *args) -> None:  # silence default stderr logging
        pass

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,X-ZDM-Token")
        self.send_header("Access-Control-Max-Age", "86400")

    def _auth_ok(self) -> bool:
        if not self.token:
            return True
        if self.headers.get("X-ZDM-Token") == self.token:
            return True
        q = self.path.split("?", 1)[1] if "?" in self.path else ""
        return f"token={self.token}" in q

    def _send(self, code: int, body: bytes, ctype: str, extra=None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), MIME[".json"])

    def _error(self, code: int, msg: str) -> None:
        self._json({"ok": False, "error": msg}, code)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _path_parts(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        return [p for p in path.split("/") if p]

    # ---------- routes ----------
    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parts = self._path_parts()
        if not parts:
            return self._serve_static("index.html")
        if parts[0] == "api":
            if not self._auth_ok():
                return self._error(401, "invalid token")
            return self._api_get(parts[1:])
        if parts[0] == "events" or (parts[0] == "api" and len(parts) > 1 and parts[1] == "events"):
            return self._sse()
        return self._serve_static("/".join(parts))

    def do_POST(self) -> None:  # noqa: N802
        parts = self._path_parts()
        if not parts or parts[0] != "api":
            return self._error(404, "not found")
        if not self._auth_ok():
            return self._error(401, "invalid token")
        return self._api_post(parts[1:])

    def do_DELETE(self) -> None:  # noqa: N802
        parts = self._path_parts()
        if len(parts) >= 3 and parts[0] == "api" and parts[1] == "tasks":
            if not self._auth_ok():
                return self._error(401, "invalid token")
            keep = "files=0" not in self.path
            ok = self.manager.remove(parts[2], remove_files=keep)
            return self._json({"ok": ok})
        return self._error(404, "not found")

    def _api_get(self, parts):
        if not parts:
            return self._json({"ok": True, "version": VERSION})
        if parts == ["tasks"]:
            return self._json({"ok": True, "tasks": self.manager.list()})
        if parts == ["state"]:
            return self._json(
                {"ok": True, "config": self.manager.config, "tasks": self.manager.list()}
            )
        if parts == ["config"]:
            return self._json({"ok": True, "config": self.manager.config})
        if parts == ["events"]:
            return self._sse()
        return self._error(404, "not found")

    def _api_post(self, parts):
        body = self._body()

        if parts == ["tasks"]:
            url = (body.get("url") or "").strip()
            if not url:
                return self._error(400, "url required")
            headers = body.get("headers") or {}
            for src, dst in (
                ("referer", "Referer"),
                ("referrer", "Referer"),
                ("cookie", "Cookie"),
                ("useragent", "User-Agent"),
                ("user_agent", "User-Agent"),
            ):
                if body.get(src):
                    headers[dst] = body[src]
            try:
                d = self.manager.add(
                    url=url,
                    directory=body.get("dir") or body.get("directory"),
                    filename=body.get("filename") or body.get("name"),
                    connections=body.get("connections"),
                    headers=headers,
                    proxy=body.get("proxy"),
                    speed_limit=body.get("speed_limit"),
                    start_now=body.get("paused") is not True,
                )
            except ValueError as e:
                return self._error(400, str(e))
            return self._json({"ok": True, "id": d.id, "task": d.to_dict()})

        if len(parts) == 3 and parts[0] == "tasks":
            tid, action = parts[1], parts[2]
            d = self.manager.get(tid)
            if not d:
                return self._error(404, "task not found")
            if action == "pause":
                self.manager.pause(tid)
            elif action == "resume":
                self.manager.resume(tid)
            elif action == "restart":
                self.manager.restart(tid)
            elif action == "cancel":
                self.manager.cancel(tid, remove_files=body.get("files", True))
            elif action == "open":
                target = d.target_path if d.status == "completed" else d.directory
                self.manager.open_path(target)
            else:
                return self._error(400, f"unknown action {action}")
            return self._json({"ok": True, "task": d.to_dict()})

        if parts == ["action"]:
            act = body.get("action")
            if act == "pause_all":
                self.manager.pause_all()
            elif act == "resume_all":
                self.manager.resume_all()
            elif act == "clear_finished":
                self.manager.clear_finished()
            elif act == "open_folder":
                self.manager.open_path(body.get("path") or str(self.manager.config["download_dir"]))
            elif act == "shutdown":
                threading.Timer(0.6, self.server.shutdown).start()
            else:
                return self._error(400, f"unknown action {act}")
            return self._json({"ok": True})

        if parts == ["config"]:
            self.manager.set_config(body)
            return self._json({"ok": True, "config": self.manager.config})

        if parts == ["open"]:
            path = body.get("path") or str(self.manager.config["download_dir"])
            return self._json({"ok": self.manager.open_path(path)})

        return self._error(404, "not found")

    # ---------- static ----------
    def _serve_static(self, rel: str) -> None:
        rel = rel.lstrip("/") or "index.html"
        path = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not path.startswith(WEB_DIR) or not os.path.isfile(path):
            return self._error(404, "not found")
        ext = os.path.splitext(path)[1]
        with open(path, "rb") as f:
            data = f.read()
        self._send(200, data, MIME.get(ext, "application/octet-stream"))

    # ---------- SSE ----------
    def _sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self._cors()
        self.end_headers()

        q = self.manager.subscribe()
        last_ping = 0.0
        try:
            while True:
                try:
                    ev = q.get(timeout=0.5)
                    payload = json.dumps(ev, ensure_ascii=False)
                    self.wfile.write(f"event: task\ndata: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    last_ping = time.time()
                except queue.Empty:
                    now = time.time()
                    if now - last_ping >= 1.0:
                        snap = json.dumps(
                            {
                                "event": "snapshot",
                                "tasks": self.manager.list(),
                                "config": self.manager.config,
                            },
                            ensure_ascii=False,
                        )
                        self.wfile.write(f"event: snapshot\ndata: {snap}\n\n".encode("utf-8"))
                        self.wfile.flush()
                        last_ping = now
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.manager.unsubscribe(q)


def make_server(manager: Manager, host: str = "127.0.0.1", port: int = 9000, token: str = ""):
    cls = type(
        "BoundHandler",
        (Handler,),
        {"manager": manager, "token": token},
    )
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer.allow_reuse_address = True
    httpd = ThreadingHTTPServer((host, port), cls)
    httpd.manager = manager  # type: ignore[attr-defined]
    return httpd


def serve(
    manager: Manager,
    host: str = "127.0.0.1",
    port: int = 9000,
    token: str = "",
    open_ui: bool = True,
) -> None:
    try:
        httpd = make_server(manager, host, port, token)
    except OSError as e:
        print(f"端口 {port} 已被占用，服务可能已在运行（{e}）")
        return
    url = f"http://{host}:{port}/"
    print(f"ZDM service running at {url}  (Ctrl+C to stop)")
    if open_ui:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        manager.shutdown()
        httpd.server_close()


def ping(host: str = "127.0.0.1", port: int = 9000) -> Optional[dict]:
    """Check whether a service is already listening."""
    import socket
    import urllib.request

    with socket.socket() as s:
        s.settimeout(0.6)
        if s.connect_ex((host, port)) != 0:
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://{host}:{port}/api/state?_={time.time()}", timeout=1.5) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return {"ok": True, "unknown": True}
