#!/usr/bin/env python3
"""Chrome/Chromium native-messaging host for ZDM.

The browser extension (ZDM Connector) uses ``chrome.runtime.connectNative()``
to wake ZDM when the local service is not running. Chrome launches this script
with a length-prefixed JSON message on stdin and expects a length-prefixed JSON
reply on stdout.

Message (from background.js):
    {"url": ..., "filename": ..., "referer": ..., "connections": ..., "dir": ...}

Reply:
    {"ok": true, "id": "<task id>"}          task handed over
    {"ok": true, "started": true}            only asked to start the service
    {"ok": false, "error": "..."}            anything went wrong

Nothing else may be written to stdout: a stray print() corrupts the protocol.
"""

import json
import os
import struct
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PORT = 9000
MAX_MESSAGE = 4 * 1024 * 1024


def config_port() -> int:
    """Port from ~/.config/zdm/state.json, falling back to 9000."""
    try:
        path = os.path.join(
            os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
            "zdm",
            "state.json",
        )
        with open(path, "r", encoding="utf-8") as fh:
            port = json.load(fh).get("config", {}).get("port")
        return int(port or DEFAULT_PORT)
    except Exception:
        return DEFAULT_PORT


def read_message():
    header = sys.stdin.buffer.read(4)
    if len(header) < 4:
        return None
    length = struct.unpack("=I", header)[0]
    if length <= 0 or length > MAX_MESSAGE:
        return None
    payload = sys.stdin.buffer.read(length)
    try:
        return json.loads(payload.decode("utf-8", "replace"))
    except Exception:
        return None


def send(obj) -> None:
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("=I", len(data)))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def api(port: int, path: str, body=None, timeout: float = 5.0):
    url = f"http://127.0.0.1:{port}/api/{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-ZDM-Extension", os.environ.get("ZDM_EXTENSION_ID", ""))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def ping(port: int) -> bool:
    try:
        return bool(api(port, "state", None, timeout=1.0).get("ok"))
    except Exception:
        return False


def ensure_service(port: int) -> bool:
    if ping(port):
        return True
    python = sys.executable or "/usr/bin/python3"
    entry = os.path.join(HERE, "zdm.py")
    try:
        subprocess.Popen(
            [python, entry, "start", "--daemon", "--no-open"],
            cwd=HERE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass
    for _ in range(80):  # up to 20 s
        time.sleep(0.25)
        if ping(port):
            return True
    return False


def main() -> int:
    msg = read_message()
    if msg is None:
        return 1
    port = config_port()
    if not ensure_service(port):
        send({"ok": False, "error": "ZDM 服务未能启动"})
        return 0

    url = str(msg.get("url") or "").strip()
    if not url:
        send({"ok": True, "started": True})
        return 0

    body = {"url": url}
    if msg.get("filename"):
        body["filename"] = msg["filename"]
    if msg.get("connections"):
        body["connections"] = msg["connections"]
    if msg.get("dir"):
        body["dir"] = msg["dir"]
    referer = msg.get("referer")
    if referer:
        body["headers"] = {"Referer": referer}
    try:
        res = api(port, "tasks", body)
        send({"ok": bool(res.get("ok")), "id": res.get("id"), "error": res.get("error")})
    except Exception as e:  # noqa: BLE001
        send({"ok": False, "error": str(e)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
