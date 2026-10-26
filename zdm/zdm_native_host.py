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


def hide_own_console() -> None:
    """Hide the console window Windows gave this process.

    Chrome starts the native-messaging host with CreateProcess and **no**
    CREATE_NO_WINDOW, so a console-subsystem exe (which this host must be —
    it needs stdio for the length-prefixed protocol) brings up a black
    conhost window that stays on screen for as long as the process lives.
    Users see it as "点下载此视频弹出一个黑窗口", which looks like a crash.

    Hiding it is safe as long as the console was created *for us*: if the
    host was started from an interactive shell, GetConsoleProcessList reports
    more than one process and we leave the user's terminal alone.
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        hwnd = kernel32.GetConsoleWindow()
        if not hwnd:
            return
        buf = (ctypes.c_uint * 8)()
        count = kernel32.GetConsoleProcessList(buf, 8)
        if count == 1 and buf[0] == kernel32.GetCurrentProcessId():
            user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:
        pass


def config_port() -> int:
    """Port from the client's state.json, falling back to 9000.

    Windows keeps its config under %APPDATA%\\zdm; hardcoding ~/.config there
    silently produced the default port, which breaks anyone who changed it.
    """
    import sys

    candidates = []
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        candidates.append(os.path.join(base, "zdm", "state.json"))
    candidates.append(
        os.path.join(
            os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
            "zdm",
            "state.json",
        )
    )
    if sys.platform == "darwin":
        candidates.append(
            os.path.expanduser("~/Library/Application Support/zdm/state.json")
        )
    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                port = json.load(fh).get("config", {}).get("port")
            if port:
                return int(port)
        except Exception:
            continue
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


def auth_token() -> str:
    """Read the per-user API token created by the service.

    The host is a separate process launched by the browser; it shares the
    user's config directory with the desktop app and CLI.
    """
    try:
        from zdm.apitoken import get_token

        return get_token()
    except Exception:
        return ""


def api(port: int, path: str, body=None, timeout: float = 5.0):
    url = f"http://127.0.0.1:{port}/api/{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-ZDM-Extension", os.environ.get("ZDM_EXTENSION_ID", ""))
    token = auth_token()
    if token:
        req.add_header("X-ZDM-Token", token)
    # 127.0.0.1 绝不能走系统代理：Windows 上设了公司代理时 urlopen 会把它
    # 当普通 HTTP 请求发出去，等到超时 —— 表现就是「唤醒失败」而服务其实好好的。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def ping(port: int) -> bool:
    try:
        return bool(api(port, "state", None, timeout=1.0).get("ok"))
    except Exception:
        return False


def launch_command():
    """argv that brings the desktop client up (service + window).

    Frozen builds have a single exe: ``--gui`` selects the window, ``gui`` as a
    CLI subcommand first makes sure the local service is running. Bare
    ``ZDM.exe`` must not be used — that lands on ``start``, i.e. a foreground
    service plus a browser tab, which is how users ended up with "只有网页版".
    """
    if getattr(sys, "frozen", False):
        base = os.path.dirname(os.path.abspath(sys.executable))
        for name in ("ZDM.exe", "ZDM"):
            exe = os.path.join(base, name)
            if os.path.isfile(exe):
                return [exe, "gui"]
        return []
    entry = os.path.join(HERE, "zdm.py")
    if os.path.isfile(entry):
        return [sys.executable or "python3", entry, "gui"]
    return []


def ensure_service(port: int) -> bool:
    if ping(port):
        return True
    cmd = launch_command()
    if not cmd:
        return False
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008  # DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            **kwargs,
        )
    except Exception:
        return False
    for _ in range(60):  # up to 15 s: a cold Qt start is ~1-2 s
        time.sleep(0.25)
        if ping(port):
            return True
    return False


def main() -> int:
    hide_own_console()
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
    if msg.get("source"):
        body["source"] = msg["source"]
    if msg.get("kind"):
        body["kind"] = msg["kind"]
    # YouTube：给了页面地址 + 高度就让后端走 yt-dlp；视频轨还要配套音频轨合流。
    # 这些字段 background.js 在唤醒时已经算好并随消息传过来，这里原样转发，
    # 否则服务没在跑、由原生消息拉起的那一次下载会缺 cookie / 缺音频轨。
    if msg.get("audio_url"):
        body["audio_url"] = msg["audio_url"]
    if msg.get("yt_page"):
        body["yt_page"] = msg["yt_page"]
    if msg.get("yt_height"):
        body["yt_height"] = msg["yt_height"]
    headers = {}
    if msg.get("referer"):
        headers["Referer"] = msg["referer"]
    if msg.get("cookie"):
        # 已经是 Netscape 文本（background.js 在 service worker 里现取 HttpOnly）。
        headers["Cookie"] = msg["cookie"]
    if headers:
        body["headers"] = headers
    try:
        res = api(port, "tasks", body)
        send({"ok": bool(res.get("ok")), "id": res.get("id"), "error": res.get("error")})
    except Exception as e:  # noqa: BLE001
        send({"ok": False, "error": str(e)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
