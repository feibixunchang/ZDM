"""Local HTTP service: REST API, SSE stream and static Web UI."""

from __future__ import annotations

import hmac
import json
import os
import queue
import threading
import time
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Tuple
from urllib.parse import parse_qs, urlparse

from . import __version__ as VERSION
from . import apitoken
from . import desktop
from . import presence
from . import runtime
from .manager import Manager
from .videopage import video_page_filename, video_page_url

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")

# Pinned by the ``key`` field in extension/manifest.json, so it survives
# reloads and updates. Only this extension may fetch the API token.
EXTENSION_ID = "mpodbnemeeghjpllgphpemhoogddfhjg"

# Which configured cookie belongs to which host. The media itself often lives
# on a CDN (googlevideo.com, bilivideo.com) while the session cookie belongs
# to the site (youtube.com, bilibili.com) — so we match on the *site* hosts
# and only fall back to the configured cookie when the caller's URL is one we
# know. CDN hosts are listed too because some players hand out CDN URLs that
# still need the first-party session.
SITE_COOKIE_HOSTS = {
    "youtube": (
        "youtube.com",
        "youtu.be",
        "googlevideo.com",
        "ytimg.com",
        "ggpht.com",
    ),
    "bilibili": (
        "bilibili.com",
        "bilivideo.com",
        "bilivideo.cn",
        "hdslb.com",
        "biliapi.net",
    ),
}

# Referer that matches the site, so a first-party session is accepted.
SITE_REFERRERS = {
    "youtube": "https://www.youtube.com/",
    "bilibili": "https://www.bilibili.com/",
}


def _site_referer(url: str) -> str:
    """Best-guess Referer for a URL, based on the known video sites."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return ""
    for site, patterns in SITE_COOKIE_HOSTS.items():
        if any(host == p or host.endswith("." + p) for p in patterns):
            return SITE_REFERRERS.get(site, "")
    return ""


# ---------------------------------------------------------------- 大小探测
#
# 为什么这件事只能放在桌面端做：
#   googlevideo 的 CORS 只放行 https://www.youtube.com 这个源。
#   - 浏览器扩展的 service worker 里 fetch，Origin 是 chrome-extension://…，
#     googlevideo 不回 Access-Control-Allow-Origin → 响应头一个都读不到；
#   - 页面里 fetch 又常常命中 SABR（POST 流式），既没有 Content-Length
#     也没有 Content-Range。
#   桌面端没有 CORS 这回事，带上同一份 Referer/cookie 发一个 2 字节的
#   Range 请求，`Content-Range: bytes 0-1/84426489` 里的总数就是文件大小，
#   而且只读 2 字节，不会把视频真的拖下来。

_PROBE_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
_PROBE_CACHE: dict = {}  # url -> (bytes, 时间戳)。直链会过期，缓存 10 分钟
_PROBE_TTL = 600.0


def probe_one_size(
    url: str,
    *,
    cookie: str = "",
    referer: str = "",
    proxy: str = "",
    timeout: float = 8.0,
) -> int:
    """问出「这个资源总共多大」，失败返回 0。绝不下载响应体。"""
    if not url or not url.lower().startswith(("http://", "https://")):
        return 0
    hit = _PROBE_CACHE.get(url)
    if hit and time.time() - hit[1] < _PROBE_TTL:
        return hit[0]
    import urllib.request

    req = urllib.request.Request(url, method="GET")
    req.add_header("Range", "bytes=0-1")
    req.add_header("User-Agent", _PROBE_UA)
    req.add_header("Accept", "*/*")
    if referer:
        req.add_header("Referer", referer)
    if cookie:
        # 扩展导出的完整 cookie 是 Netscape 文件格式（含 tab 和换行），
        # 原样 add_header 会被 urllib 抛 "Invalid header value"，探测直接
        # 失败（返回 0 → 界面显示「大小未知」）。压成 header 形式。
        from .core import _cookie_header
        try:
            host = (urlparse(url).hostname or "").lower()
        except Exception:  # noqa: BLE001
            host = ""
        req.add_header("Cookie", _cookie_header(cookie, host))
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    if handlers:
        opener = urllib.request.build_opener(*handlers).open
    else:
        opener = urllib.request.urlopen
    total = 0
    try:
        resp = opener(req, timeout=timeout)
    except Exception:
        return 0
    try:
        headers = resp.headers
        ctype = (headers.get("Content-Type") or "").lower()
        # 有些站点会回一张很小的 HTML 错误页（200 + text/html），
        # 那不是媒体，别把它的长度当成视频大小。
        if ctype.startswith("text/html"):
            return 0
        cr = headers.get("Content-Range") or ""
        if "/" in cr:
            try:
                total = int(cr.rsplit("/", 1)[1].strip())
            except Exception:
                total = 0
        # 200 说明服务端忽略了 Range，Content-Length 就是整个文件；
        # 206 却拿不到 Content-Range 时，Content-Length 只是那一小段，不能用。
        if not total and resp.status == 200:
            try:
                total = int(headers.get("Content-Length") or 0)
            except Exception:
                total = 0
    except Exception:
        total = 0
    finally:
        try:
            resp.close()
        except Exception:
            pass
    if total > 0:
        _PROBE_CACHE[url] = (total, time.time())
    return total


def probe_sizes(
    urls,
    *,
    cookie: str = "",
    referer: str = "",
    proxy: str = "",
    timeout: float = 8.0,
    limit: int = 12,
) -> dict:
    """并发问一批网址的大小。返回 {url: bytes}（问不到的就不出现在结果里）。"""
    out: dict = {}
    todo = [u for u in (urls or []) if isinstance(u, str) and u][:limit]
    if not todo:
        return out
    lock = threading.Lock()

    def work(u):
        n = probe_one_size(
            u, cookie=cookie, referer=referer, proxy=proxy, timeout=timeout
        )
        if n > 0:
            with lock:
                out[u] = n

    threads = [threading.Thread(target=work, args=(u,), daemon=True) for u in todo]
    for t in threads:
        t.start()
    # 等全部结束（每个都有自己的 timeout），最多再多等 2 秒收尾。
    deadline = time.time() + timeout + 2.0
    for t in threads:
        t.join(max(0.05, deadline - time.time()))
    return out

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

    def _port(self) -> int:
        try:
            return int(self.server.server_address[1])  # type: ignore[attr-defined]
        except Exception:
            return 0

    def _allowed_origin(self) -> str:
        """Return the request origin when it is one we explicitly trust."""
        origin = self.headers.get("Origin") or ""
        if not origin:
            return ""
        port = self._port()
        allowed = {
            f"chrome-extension://{EXTENSION_ID}",
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
        }
        return origin if origin in allowed else ""

    def _cors(self) -> None:
        # 本地 API 不需要 keep-alive：开着它，服务重启（升级后 postinst 会
        # 杀掉旧 serve）后浏览器连接池里全是死连接，扩展要重启浏览器才
        # 恢复（实测）。每响应一条就关，客户端下次新建连接，彻底规避。
        self.close_connection = True
        self.send_header("Connection", "close")
        # Never answer ``*``: any web page the user visits could otherwise read
        # the response (site cookies, task headers) from 127.0.0.1.
        origin = self._allowed_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers", "Content-Type,X-ZDM-Token,X-ZDM-Extension"
        )
        self.send_header("Access-Control-Max-Age", "86400")
        # 顺手做扩展存在性检测：_cors 在每个响应（含 OPTIONS 预检）上都会走，
        # 而扩展的每个请求要么带 X-ZDM-Extension、要么 Origin 是
        # chrome-extension://。GUI 的「浏览器扩展」对话框就靠这个回答
        # 「扩展装上了没有」——比读注册表/翻浏览器配置目录可靠得多。
        if self.headers.get("X-ZDM-Extension") == EXTENSION_ID or (
            self.headers.get("Origin") or ""
        ) == f"chrome-extension://{EXTENSION_ID}":
            presence.note_seen()

    def _supplied_token(self) -> str:
        header = self.headers.get("X-ZDM-Token") or ""
        if header:
            return header
        raw = self.headers.get("Cookie") or ""
        if raw:
            try:
                jar = SimpleCookie()
                jar.load(raw)
                morsel = jar.get("zdm_token")
                if morsel is not None:
                    return morsel.value
            except Exception:
                pass
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        if query:
            try:
                values = parse_qs(query).get("token") or [""]
                return values[0]
            except Exception:
                pass
        return ""

    def _auth_ok(self) -> bool:
        if not self.token:
            # Fail closed: an empty token means setup went wrong, not "open to
            # the world". The browser UI still works because the service always
            # creates a token before listening.
            return False
        supplied = self._supplied_token()
        return bool(supplied) and hmac.compare_digest(supplied, self.token)

    def _extension_ok(self) -> bool:
        """Only our pinned extension may ask for the token."""
        origin = self.headers.get("Origin") or ""
        if origin:
            return origin == f"chrome-extension://{EXTENSION_ID}"
        return (self.headers.get("X-ZDM-Extension") or "") == EXTENSION_ID

    def _host_ok(self) -> bool:
        """Reject DNS-rebinding requests that kept a non-local Host header."""
        raw = self.headers.get("Host") or ""
        if not raw:
            return True
        try:
            host = (urlparse("//" + raw).hostname or "").lower()
        except Exception:
            return False
        return host in ("127.0.0.1", "localhost", "::1")

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
        if not self._host_ok():
            return self._error(403, "forbidden host")
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if not self._host_ok():
            return self._error(403, "forbidden host")
        parts = self._path_parts()
        if not parts:
            return self._serve_static("index.html")
        if parts[0] == "api":
            if parts == ["api", "extension", "token"]:
                if not self._extension_ok():
                    return self._error(403, "not the ZDM extension")
                return self._json({"ok": True, "token": self.token})
            if parts == ["api", "extension", "ping"]:
                # 扩展心跳：故意不鉴权（它只记录「扩展来过」，不泄露任何
                # 东西），否则 token 校验会把最需要这条信号的新装机用户挡在
                # 门外。返回体里也不带任何路径信息。
                presence.note_seen()
                # 扩展靠版本号判断磁盘上有没有更新的扩展文件：不一致就自动
                # reload 自己（从磁盘加载新 extension/），不用用户重开浏览器。
                # 这里报**随包扩展**的 manifest version（读不到才退回桌面端
                # 版本号）——扩展更新跟着安装包走，不必为两行 JS 逼桌面端
                # 整体跳版本；老版扩展读 version 字段也能触发同一机制。
                ext_v = runtime.bundled_extension_version() or VERSION
                return self._json(
                    {
                        "ok": True,
                        "version": ext_v,
                        "ext_version": ext_v,
                        "app_version": VERSION,
                    }
                )
            if parts == ["api"]:
                # 存活/版本探测，故意不鉴权：安装器、CI 和启动器只需要知道
                # 「服务起来了」，这里不暴露任何任务或配置。
                return self._json({"ok": True, "version": VERSION})
            if not self._auth_ok():
                if parts == ["api", "state"]:
                    # 安装器与 CI（build-windows）直接 GET /api/state 且不带
                    # token；以前这里回 401，健康检查就永远等不到 200，Windows
                    # 安装包因此一直没能挂上 Release。只回最小存活信息，
                    # 真正的任务列表仍然必须鉴权。
                    return self._json(
                        {"ok": True, "version": VERSION, "auth_required": True}
                    )
                return self._error(401, "invalid token")
            return self._api_get(parts[1:])
        if parts[0] == "events":
            # EventSource cannot set request headers, so the web UI presents
            # the SameSite cookie set when index.html was served. This used to
            # be reachable without any credential and streamed site cookies.
            if not self._auth_ok():
                return self._error(401, "invalid token")
            return self._sse()
        return self._serve_static("/".join(parts))

    def do_POST(self) -> None:  # noqa: N802
        if not self._host_ok():
            return self._error(403, "forbidden host")
        parts = self._path_parts()
        if not parts or parts[0] != "api":
            return self._error(404, "not found")
        if not self._auth_ok():
            return self._error(401, "invalid token")
        return self._api_post(parts[1:])

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._host_ok():
            return self._error(403, "forbidden host")
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
        if parts == ["extension"]:
            # GUI「浏览器扩展」对话框轮询这个：扩展装没装、最近什么时候
            # 活跃过。扩展目录的定位在 GUI 侧用 runtime.extension_dir() 做
            # （GUI 可能是独立进程，路径要自己算），这里只给信号不给路径。
            return self._json(
                {
                    "ok": True,
                    "version": VERSION,
                    "last_seen": presence.last_seen(),
                    "seen_recently": presence.recent(),
                }
            )
        if parts == ["events"]:
            return self._sse()
        return self._error(404, "not found")

    def _site_cookie_for(self, url: str) -> str:
        """Look up a configured site cookie for this URL's host.

        The browser extension normally sends the page's own cookie with each
        task, so this is only the fallback: a task started from the desktop
        app, or a media URL on a host we recognise but the extension could not
        authenticate against.
        """
        try:
            host = (urlparse(url).hostname or "").lower()
        except Exception:
            return ""
        if not host:
            return ""
        cookies = self.manager.config.get("site_cookies") or {}
        if not isinstance(cookies, dict):
            return ""
        for site, patterns in SITE_COOKIE_HOSTS.items():
            if not any(host == p or host.endswith("." + p) for p in patterns):
                continue
            value = cookies.get(site)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def _api_post(self, parts):
        body = self._body()

        if parts == ["tasks"]:
            url = (body.get("url") or "").strip()
            if not url:
                return self._error(400, "缺少下载链接")
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
            # Fall back to the cookie configured in Settings when the caller
            # did not send one. Never override an explicit cookie.
            if not headers.get("Cookie"):
                auto_cookie = self._site_cookie_for(url)
                if auto_cookie:
                    headers["Cookie"] = auto_cookie
                    # A first-party session is only accepted with the matching
                    # Referer; without it the server answers 403.
                    if not headers.get("Referer"):
                        headers["Referer"] = _site_referer(url)
            # 扩展传过来的可能是 Netscape 文件格式的完整 cookie 导出
            # （YouTube 就是这样）。原样塞进请求头会被 urllib 以
            # "Invalid header value" 拒掉 —— 表现为任务「probe failed」，
            # 一个字节都下不来。统一压成 header 形式。
            if headers.get("Cookie"):
                from .core import _cookie_header
                try:
                    _host = (urlparse(url).hostname or "").lower()
                except Exception:  # noqa: BLE001
                    _host = ""
                headers["Cookie"] = _cookie_header(headers["Cookie"], _host)
            # 视频**页面**链接（YouTube/B站…）不能当普通文件下：那样存下来的
            # 是一个叫 watch 的 HTML。桌面端、网页版、CLI 都在这里汇合，所以
            # 在服务端统一转成页面任务交给 yt-dlp；扩展推的媒体直链不会命中。
            if (
                not body.get("yt_page")
                and not body.get("page_url")
                and not body.get("kind")
            ):
                page = video_page_url(url)
                if page:
                    body["yt_page"] = page
                    # These tasks now go through yt-dlp. Mark them as video
                    # (the list shows the right kind/icon) and give them a
                    # fallback filename: the GUI sends the resolved title, but
                    # the web UI and `zdm add` do not, and the Download default
                    # "download" would leave an extension-less file.
                    body["kind"] = "video"
                    if not (body.get("filename") or body.get("name")):
                        body["filename"] = video_page_filename(url)
            # 扩展推的是**媒体直链**（googlevideo…）且带 kind，上面的页面
            # 链接推导（要求 not kind）不会命中 —— 但 Referer 里就有
            # YouTube 页面地址。直链只能顺序读，部分网络（TUN/Fake-IP）下
            # mid-file Range 被 302 打断，吞吐只有几 KB/s；带上页面地址让
            # 任务走 yt-dlp 通道（自己解挑战、按段重取、下完合流），被拦时
            # 还会自动退回这条直链。这是入口侧兜底：不管扩展新旧、标签页
            # 是否重载过，推送都能走对通道。
            if (
                not body.get("yt_page")
                and not body.get("page_url")
                and "googlevideo.com/" in (url or "")
            ):
                from .core import yt_page_from_headers

                page = yt_page_from_headers(headers)
                if page:
                    body["yt_page"] = page
            # A caller that resolved the page itself (desktop GUI, browser
            # extension) sends yt_page but often no kind; mark it here so the
            # list renders it as a video and not as an ordinary file.
            if (body.get("yt_page") or body.get("page_url")) and not body.get("kind"):
                body["kind"] = "video"
            directory = body.get("dir") or body.get("directory")
            if directory is not None:
                if not isinstance(directory, str):
                    return self._error(400, "directory must be a string")
                if "\x00" in directory:
                    return self._error(400, "invalid directory")
                directory = os.path.abspath(os.path.expanduser(directory.strip()))
                body["dir"] = directory
            try:
                d = self.manager.add(
                    url=url,
                    directory=directory,
                    filename=body.get("filename") or body.get("name"),
                    connections=body.get("connections"),
                    headers=headers,
                    proxy=body.get("proxy"),
                    speed_limit=body.get("speed_limit"),
                    start_now=body.get("paused") is not True,
                    source=body.get("source"),
                    kind=body.get("kind"),
                    audio_url=body.get("audio_url"),
                    # YouTube：给了页面地址就走 yt-dlp 下载通道
                    yt_page=body.get("yt_page") or body.get("page_url"),
                    yt_height=body.get("yt_height") or body.get("height"),
                )
            except (ValueError, TypeError) as e:
                # TypeError as well as ValueError: the body is untrusted JSON,
                # and int() on a list/dict raises TypeError, which used to
                # escape this handler and drop the connection without a reply.
                return self._error(400, str(e))
            # If the desktop client is not open (typical when the download comes
            # from the browser extension), bring it up so the user sees the task.
            # Pass NO urls: the task is already queued above, and handing the
            # url to the GUI makes it add a second copy.
            if self.manager.config.get("auto_show_gui", True):
                threading.Thread(
                    target=desktop.ensure_desktop,
                    args=([], int(self.manager.config.get("port") or 9000)),
                    daemon=True,
                ).start()
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
                # "打开" means open the downloaded file itself (a .dmg brings up
                # the installer), not its folder. Unfinished tasks have no file
                # yet, so those reveal the download directory instead.
                if d.status == "completed":
                    self.manager.open_file(d.target_path)
                else:
                    self.manager.open_path(d.directory)
            elif action == "reveal":
                self.manager.reveal_path(
                    d.target_path if d.status == "completed" else d.directory
                )
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
                path = body.get("path") or str(self.manager.config["download_dir"])
                if not self.manager.can_open_path(path):
                    return self._error(403, "path outside download directories")
                self.manager.open_path(path)
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
            if not self.manager.can_open_path(path):
                return self._error(403, "path outside download directories")
            mode = (body.get("mode") or "dir").lower()
            if mode == "file":
                ok = self.manager.open_file(path)
            elif mode == "reveal":
                ok = self.manager.reveal_path(path)
            else:
                ok = self.manager.open_path(path)
            return self._json({"ok": ok})

        # 嗅探面板的「大小未知」补全：扩展把拿不到长度的直链发过来，
        # 桌面端用 2 字节 Range 问出真实总大小（浏览器侧被 CORS 卡死，
        # 详见 probe_one_size 的注释）。
        if parts == ["probe-size"]:
            urls = body.get("urls") or []
            if not isinstance(urls, list):
                urls = []
            referer = (body.get("referer") or "").strip() or (
                _site_referer(urls[0]) if urls else ""
            )
            cookie = body.get("cookie") or (
                self._site_cookie_for(urls[0]) if urls else ""
            )
            sizes = probe_sizes(
                urls,
                cookie=cookie or "",
                referer=referer or "",
                proxy=self.manager.config.get("proxy") or "",
            )
            return self._json({"ok": True, "sizes": sizes})

        # YouTube resolution via yt-dlp (desktop-side, robust). The extension
        # forwards a watch/shorts URL; we return plain, downloadable streams so
        # it never has to fight YouTube's signatureCipher in the browser.
        if len(parts) == 2 and parts[0] == "youtube" and parts[1] == "resolve":
            from .yt_resolve import resolve as _yt_resolve

            url = (body.get("url") or "").strip()
            if not url:
                return self._error(400, "缺少视频链接")
            # Prefer an explicit cookie from the caller, then the cookie
            # configured in Settings (first-party session) as a fallback.
            cookie = body.get("cookie") or self._site_cookie_for(url) or None
            proxy = self.manager.config.get("proxy") or None
            try:
                result = _yt_resolve(url, cookie=cookie, proxy=proxy)
            except Exception as e:  # resolver should self-report, but stay safe
                result = {"ok": False, "formats": [], "title": "", "error": str(e)}
            return self._json(result)

        return self._error(404, "not found")

    # ---------- static ----------
    def _serve_static(self, rel: str) -> None:
        rel = rel.lstrip("/") or "index.html"
        path = os.path.normpath(os.path.join(WEB_DIR, rel))
        try:
            inside = os.path.commonpath(
                [os.path.realpath(WEB_DIR), os.path.realpath(path)]
            ) == os.path.realpath(WEB_DIR)
        except ValueError:  # different drives on Windows
            inside = False
        if not inside or not os.path.isfile(path):
            return self._error(404, "not found")
        ext = os.path.splitext(path)[1]
        with open(path, "rb") as f:
            data = f.read()
        extra = None
        if rel in ("index.html", "/index.html") and self.token:
            # Same-origin fetch/EventSource send this automatically. HttpOnly
            # keeps page scripts from copying the secret; SameSite=Strict stops
            # other sites from riding on it.
            extra = {
                "Set-Cookie": (
                    f"zdm_token={self.token}; Path=/; HttpOnly; SameSite=Strict"
                )
            }
        self._send(200, data, MIME.get(ext, "application/octet-stream"), extra)

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


def make_server(
    manager: Manager,
    host: str = "127.0.0.1",
    port: int = 9000,
    token: Optional[str] = None,
):
    token = apitoken.get_token() if token is None else token
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
    token: Optional[str] = None,
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
    """Check whether a ZDM service is listening.

    A bare TCP connect is not enough: another program may own the port, and
    ``zdm start`` used to report "service already running" for it (the failed
    HTTP probe still returned a truthy dict). The response must carry the API's
    own signature before we call it ours.
    """
    import socket
    import urllib.request

    with socket.socket() as s:
        s.settimeout(0.6)
        if s.connect_ex((host, port)) != 0:
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        req = urllib.request.Request(
            f"http://{host}:{port}/api/state?_={time.time()}",
            headers=apitoken.header(),
        )
        with opener.open(req, timeout=1.5) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    # 未鉴权时会拿到最小存活响应；它证明「有个 ZDM 在跑」，但不能证明是
    # **我们这个用户的** ZDM（token 不一致），所以不能当成自己。
    if data.get("auth_required") is True:
        return None
    # /api/state answers {"ok": true, "config": ..., "tasks": [...]}; /api
    # answers {"ok": true, "version": ...}. Anything else is a foreign service.
    if data.get("ok") is True and ("tasks" in data or "config" in data or "version" in data):
        return data
    return None
