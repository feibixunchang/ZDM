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
from .aria2 import Aria2Rpc
from .core import normalize_section
from .manager import Manager, to_bool
from .videopage import is_playlist_page, video_page_filename, video_page_url

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")

# aria2 兼容层的挂载路径（POST）；aria2 自己默认叫 /jsonrpc，客户端一般能改。
RPC_PATHS = ("jsonrpc", "rpc")
# 一条 RPC 请求（含 system.multicall 批量）的上限：本地服务，没必要吃下无界请求体。
RPC_MAX_BODY = 4 * 1024 * 1024
# 被拒的请求体在多大范围内还值得「读干净再回错」：超过这个数就当作有人在乱来，
# 直接断开，不给它慢慢灌。
RPC_DRAIN_LIMIT = RPC_MAX_BODY * 4

# 预览一次最多回多少条给界面。上限只管**响应体大小**，不影响能选的范围：
# count 仍是展开到的总数，加任务用的是序号，不依赖界面收到过那一条。
PLAYLIST_PREVIEW_MAX = 200


def _playlist_indices(body: dict, count: int):
    """播放列表 add 要建哪几条：range 文本（"1-20"）或显式 indices，二选一。"""
    from .yt_playlist import parse_range

    raw = body.get("indices")
    if isinstance(raw, list) and raw:
        out = []
        for v in raw:
            try:
                n = int(v)
            except (TypeError, ValueError):
                raise ValueError(f"indices 里需要整数（收到 {v!r}）") from None
            if n < 1 or n > count:
                raise ValueError(f"序号超出清单条数（共 {count} 条，收到 {n}）")
            out.append(n)
        if not out:
            raise ValueError("没有选中任何一条")
        return sorted(set(out))
    return parse_range(body.get("range"), count)


_RPC: Optional[Aria2Rpc] = None
_RPC_LOCK = threading.Lock()


def _is_rpc_path(parts) -> bool:
    return bool(parts) and parts[0] in RPC_PATHS


def _parse_start_at(value) -> float:
    """Accept epoch seconds or ISO-8601 (local time); "" / None means ASAP."""
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return max(0.0, float(value))
    text = str(value).strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    from datetime import datetime

    try:
        return max(0.0, datetime.fromisoformat(text).timestamp())
    except ValueError as e:
        raise ValueError("start_at 需要 Unix 时间戳或 ISO 时间") from e


def _parse_section(value) -> str:
    """Normalise a yt-dlp section range ("00:01:00-00:05:00" → "*…")."""
    return normalize_section(value)


# 轨道选择：扩展/网页/CLI 传进来的键名不统一，认这几个别名（第一个是正式名）。
_TRACK_KEYS = {
    "stream_video": ("stream_video", "video_track", "select_video"),
    "stream_audio": ("stream_audio", "audio_track", "select_audio"),
    "stream_subtitle": ("stream_subtitle", "subtitle_track", "select_subtitle"),
    "stream_sub_format": ("stream_sub_format", "subtitle_format", "sub_format"),
}


def _track_value(body: dict, key: str):
    """取一项轨道选择。键都没出现时返回 None —— 编辑接口要靠它区分
    「这次没改」和「改成空（清掉选择）」，两者行为不同。
    """
    for alias in _TRACK_KEYS[key]:
        if alias in body:
            value = body[alias]
            if value is None:
                return ""
            if not isinstance(value, str):
                raise ValueError("%s 需要字符串（收到 %r）" % (key, value))
            return value
    return None


_LIVE_LIMIT_KEYS = ("stream_live_limit", "live_limit", "record_limit")


def _live_limit_value(body: dict):
    """直播录制的时长上限（秒），键都没出现时返回 None。

    编辑接口要靠 None 区分「这次没改上限」和「改成不限时长（0）」；数值本身
    的换算和区间校验交给 manager.normalize_live_limit，四端共用一条口径。
    """
    for alias in _LIVE_LIMIT_KEYS:
        if alias in body:
            value = body[alias]
            if value is None or value == "":
                return 0
            return value
    return None


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


def _probe_nonhttp_size(url: str, timeout: float) -> int:
    """ftp/ftps/sftp 的大小预探：走下载引擎同一套通道，不用 urllib。

    urllib 的 ftp 处理器不认 Range，硬套上面的逻辑会把整个文件当成「探一下」
    读进来 —— 预探一个 4GB 的镜像包不该变成一次满速下载。
    """
    from .transport import has_supported_scheme, probe_with_fallback

    if not has_supported_scheme(url):
        return 0
    hit = _PROBE_CACHE.get(url)
    if hit and time.time() - hit[1] < _PROBE_TTL:
        return hit[0]
    try:
        _transport, info = probe_with_fallback(url, {}, timeout, "")
    except Exception:  # noqa: BLE001  预探失败只是「大小未知」，不是错误
        return 0
    total = int(info.get("size") or 0)
    if total:
        _PROBE_CACHE[url] = (total, time.time())
    return total


def probe_one_size(
    url: str,
    *,
    cookie: str = "",
    referer: str = "",
    proxy: str = "",
    timeout: float = 8.0,
) -> int:
    """问出「这个资源总共多大」，失败返回 0。绝不下载响应体。"""
    if not url:
        return 0
    if not url.lower().startswith(("http://", "https://")):
        return _probe_nonhttp_size(url, timeout)
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

    def _cors(self, any_origin: bool = False) -> None:
        # 本地 API 不需要 keep-alive：开着它，服务重启（升级后 postinst 会
        # 杀掉旧 serve）后浏览器连接池里全是死连接，扩展要重启浏览器才
        # 恢复（实测）。每响应一条就关，客户端下次新建连接，彻底规避。
        self.close_connection = True
        self.send_header("Connection", "close")
        # Never answer ``*``: any web page the user visits could otherwise read
        # the response (site cookies, task headers) from 127.0.0.1.
        # aria2 兼容层是唯一例外（any_origin=True）：AriaNg 这类现成客户端多数
        # 跑在别的端口或直接开本地文件，同源策略会让它连不上，而它连不上时用户
        # 只看到一句「RPC 连接失败」，查不到是 ZDM 没回 CORS。放开的前提是这里
        # 只放行 /jsonrpc 那条路，而且服务只绑 127.0.0.1、每条调用都要带令牌 ——
        # 网页拿不到令牌就仍然只能读到一句错误说明。
        origin = self.headers.get("Origin") or ""
        allowed = origin if any_origin else self._allowed_origin()
        if allowed:
            self.send_header("Access-Control-Allow-Origin", allowed)
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
        # 版本号是扩展自己报的（X-ZDM-Ext-Version）：状态栏要说「扩展是几点几」，
        # 随包 manifest 只能说明磁盘上放着哪一份，装进浏览器的可能还没换过来。
        if self.headers.get("X-ZDM-Extension") == EXTENSION_ID or (
            self.headers.get("Origin") or ""
        ) == f"chrome-extension://{EXTENSION_ID}":
            presence.note_seen(version=self.headers.get("X-ZDM-Ext-Version") or "")

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

    def _send(self, code: int, body: bytes, ctype: str, extra=None, any_origin: bool = False) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors(any_origin)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), MIME[".json"])

    def _json_rpc(self, code: int, payload) -> None:
        """aria2 兼容层的响应：跨域也放行（见 _cors 的 any_origin 说明）。"""
        if payload is None:
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self._cors(any_origin=True)
            self.end_headers()
            return
        self._send(
            code,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            MIME[".json"],
            any_origin=True,
        )

    def _error(self, code: int, msg: str) -> None:
        self._json({"ok": False, "error": msg}, code)

    def _open_why(self, path) -> str:
        """打开位置失败的解释：把「路径根本不存在」和「系统没响应」分清。"""
        text = str(path or "")
        if text and not os.path.exists(os.path.expanduser(text)):
            return f"没能打开 {text}：路径不存在（文件还没下完？）"
        return f"没能打开 {text or '这个位置'}：系统没有响应（桌面端是否在运行？）"

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            # 以前坏 JSON 回 {}：POST /api/config 带一个残缺请求体就等于「改了
            # 一个空补丁」，服务端回 ok:true，用户以为设置已经保存了。
            raise ValueError(f"请求体不是合法的 JSON（{type(e).__name__}）") from e
        if not isinstance(data, dict):
            raise ValueError("请求体需要是 JSON 对象")
        return data

    def _path_parts(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        return [p for p in path.split("/") if p]

    # ---------- routes ----------
    def _rpc(self) -> Aria2Rpc:
        """aria2 兼容层的常驻实例（会话 id 要稳定，客户端靠它判断服务重启动过没有）。"""
        global _RPC
        with _RPC_LOCK:
            if _RPC is None or _RPC.manager is not self.manager or _RPC.token != self.token:
                _RPC = Aria2Rpc(self.manager, self.token, self._create_task)
            else:
                # create_task 绑在本次连接的 Handler 上：每次换绑，不让常驻实例
                # 一直抓住一个早已结束的旧连接对象。
                _RPC.token = self.token
                _RPC.create_task = self._create_task
            return _RPC

    def do_OPTIONS(self) -> None:  # noqa: N802
        if not self._host_ok():
            return self._error(403, "forbidden host")
        self.send_response(204)
        self._cors(any_origin=_is_rpc_path(self._path_parts()))
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
                presence.note_seen(version=self.headers.get("X-ZDM-Ext-Version") or "")
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
        if _is_rpc_path(parts):
            return self._rpc_post()
        if not parts or parts[0] != "api":
            return self._error(404, "not found")
        if not self._auth_ok():
            return self._error(401, "invalid token")
        return self._api_post(parts[1:])

    def _rpc_post(self) -> None:
        """aria2 兼容的 JSON-RPC 入口：令牌走 aria2 的 token: 约定（也可用平时的
        X-ZDM-Token 头 / ?token=）。关掉开关时明确回一句「未启用」，而不是让客户端
        对着一个 404 猜是哪一步错了。"""
        if not self.manager.config.get("rpc_enabled", True):
            return self._json_rpc(
                404,
                Aria2Rpc.err(None, "aria2 JSON-RPC 未启用：在设置里打开「兼容 aria2 JSON-RPC」", -1),
            )
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0:
            return self._json_rpc(400, Aria2Rpc.err(None, "Content-Length 无效", -32600))
        if length > RPC_MAX_BODY:
            # 先把体读干净再回 413。不读就写响应，对端会在还剩几 MB 没发完时
            # 被 RST，浏览器/curl 只报「连接被重置」，用户看不出真正原因是太大。
            self._drain_body(length)
            return self._json_rpc(413, Aria2Rpc.err(None, f"请求体过大（上限 {RPC_MAX_BODY} 字节）", -32600))
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return self._json_rpc(
                200,
                Aria2Rpc.err(None, "需要 JSON-RPC 请求体", -32600),
            )
        code, payload = self._rpc().handle(raw, self._supplied_token())
        self._json_rpc(code, payload)

    def _drain_body(self, length: int) -> None:
        if length > RPC_DRAIN_LIMIT:
            # 声明得离谱的请求不陪它耗时间，让连接直接结束。
            self.close_connection = True
            return
        remaining = length
        while remaining > 0:
            try:
                chunk = self.rfile.read(min(64 * 1024, remaining))
            except OSError:
                break
            if not chunk:
                break
            remaining -= len(chunk)

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._host_ok():
            return self._error(403, "forbidden host")
        parts = self._path_parts()
        if len(parts) >= 3 and parts[0] == "api" and parts[1] == "tasks":
            if not self._auth_ok():
                return self._error(401, "invalid token")
            keep = "files=0" not in self.path
            tid = parts[2]
            if not self.manager.remove(tid, remove_files=keep):
                # 只回 {"ok": false} 的话，命令行打印出来是「error: None」，
                # 像程序坏了；其实十有八九是这条任务已经不在列表里了。
                return self._error(404, f"没有 ID 为 {tid} 的任务（可能已经删掉了）")
            return self._json({"ok": True})
        return self._error(404, "not found")

    def _api_get(self, parts):
        if not parts:
            return self._json({"ok": True, "version": VERSION})
        if parts == ["token"]:
            # 设置界面要显示这枚令牌（AriaNg 一类客户端得填它）。到这里为止请求
            # 已经过了 _auth_ok —— 拿不到令牌的人问不到令牌，网页版靠的就是
            # 服务端发的 Cookie，所以它自己已经带着。
            return self._json(
                {
                    "ok": True,
                    "token": self.token,
                    "rpc_enabled": bool(self.manager.config.get("rpc_enabled", True)),
                    "rpc_paths": ["/" + p for p in RPC_PATHS],
                }
            )
        if parts == ["tasks"]:
            return self._json({"ok": True, "tasks": self.manager.list()})
        if parts == ["state"]:
            reply = {"ok": True, "config": self.manager.config, "tasks": self.manager.list()}
            # 历史文件读坏了、或者最近一次设置没写进磁盘：只写在 download-errors.log
            # 里没人会去看，界面必须第一时间说清楚，否则用户以为记录还在。
            if self.manager.state_error:
                reply["warning"] = self.manager.state_error
            return self._json(reply)
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
                    # 装进浏览器的那一份扩展自己报的版本号。老扩展不报，退回
                    # 随包 manifest 的版本 —— 那至少说明磁盘上是哪一份，界面
                    # 不会显示成一个凭空的数。
                    "ext_version": presence.ext_version()
                    or runtime.bundled_extension_version()
                    or "",
                    "last_seen": presence.last_seen(),
                    "seen_recently": presence.recent(),
                }
            )
        if parts == ["extension", "firefox.xpi"]:
            return self._firefox_xpi()
        if parts == ["events"]:
            return self._sse()
        return self._error(404, "not found")

    def _firefox_xpi(self) -> None:
        """把磁盘上那份扩展现场打成 Firefox 的 .xpi。

        不预先存第二个文件：装到用户机器上的 extension/ 才是唯一真相，仓库里再造
        一份迟早和它对不上（改 JS 的人不会记得同步打一遍）。Firefox 只认 .xpi，
        没有 Chrome 那句「加载已解压的扩展程序」，所以这一步得让用户点一下就有。
        """
        from . import firefox_ext

        ext_dir = runtime.extension_dir()
        if not ext_dir or not os.path.isdir(ext_dir):
            return self._error(404, "找不到随包的扩展目录，打不了 Firefox 包")
        try:
            manifest = firefox_ext.gecko_manifest(ext_dir)
            data = firefox_ext.build_zip(ext_dir)
        except Exception as e:  # noqa: BLE001
            return self._error(500, f"打 Firefox 包失败：{type(e).__name__}: {e}")
        name = "zdm-connector-%s-firefox.xpi" % (manifest.get("version") or VERSION)
        self._send(
            200,
            data,
            "application/x-xpinstall",
            {"Content-Disposition": 'attachment; filename="%s"' % name},
        )

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

    def _normalize_headers(self, body: dict, url: str, auto_cookie: bool = True) -> dict:
        """Build request headers from the loose shapes callers send.

        The extension may hand over a Netscape cookie file (YouTube does) and
        older callers use lowercase short names. Anything not compressed here
        reaches urllib as an invalid header value and the task fails at probe.
        """
        headers = body.get("headers") or {}
        if not isinstance(headers, dict):
            headers = {}
        headers = dict(headers)
        for src, dst in (
            ("referer", "Referer"),
            ("referrer", "Referer"),
            ("cookie", "Cookie"),
            ("useragent", "User-Agent"),
            ("user_agent", "User-Agent"),
        ):
            if body.get(src):
                headers[dst] = body[src]
        # 新增任务时用设置里的站点 Cookie 兜底；编辑任务时不能用 ——
        # 用户手动清空 Cookie 的意思就是不要它了。
        if auto_cookie and not headers.get("Cookie"):
            auto_cookie_value = self._site_cookie_for(url)
            if auto_cookie_value:
                headers["Cookie"] = auto_cookie_value
                if not headers.get("Referer"):
                    headers["Referer"] = _site_referer(url)
        if headers.get("Cookie"):
            from .core import _cookie_header

            try:
                host = (urlparse(url).hostname or "").lower()
            except Exception:  # noqa: BLE001
                host = ""
            headers["Cookie"] = _cookie_header(headers["Cookie"], host)
        return headers

    def _api_post(self, parts):
        try:
            body = self._body()
        except ValueError as e:
            return self._error(400, str(e))

        if parts == ["tasks"]:
            try:
                return self._json(self._create_task(body))
            except ValueError as e:
                return self._error(400, str(e))

        if len(parts) == 3 and parts[0] == "tasks":
            tid, action = parts[1], parts[2]
            d = self.manager.get(tid)
            if not d:
                return self._error(404, "task not found")
            if action == "edit":
                try:
                    eu = (body.get("url") or "").strip()
                    header_keys = (
                        "headers", "referer", "referrer", "cookie",
                        "useragent", "user_agent",
                    )
                    hdrs = None
                    if any(k in body for k in header_keys):
                        hdrs = self._normalize_headers(
                            body, eu or d.url, auto_cookie=False
                        )
                    proxy = body.get("proxy") if "proxy" in body else None
                    start_at = (
                        _parse_start_at(body.get("start_at"))
                        if "start_at" in body
                        else None
                    )
                    # 数字字段原样传下去，由 manager 换算：裸 int() 抛的是
                    # 「int() with base 10: 'abc'」，看不出是哪个字段写错了。
                    priority = (
                        body.get("priority")
                        if body.get("priority") not in (None, "")
                        else None
                    )
                    section = (
                        _parse_section(body.get("section"))
                        if "section" in body
                        else None
                    )
                    conns = body.get("connections")
                    info = self.manager.edit(
                        tid,
                        url=eu or None,
                        headers=hdrs,
                        proxy=proxy,
                        start_at=start_at,
                        priority=priority,
                        section=section,
                        stream_video=_track_value(body, "stream_video"),
                        stream_audio=_track_value(body, "stream_audio"),
                        stream_subtitle=_track_value(body, "stream_subtitle"),
                        stream_sub_format=_track_value(body, "stream_sub_format"),
                        stream_live_limit=_live_limit_value(body),
                        connections=(
                            conns if conns not in (None, "") else None
                        ),
                    )
                except (ValueError, TypeError) as e:
                    return self._error(400, str(e))
                return self._json({"ok": True, **info, "task": d.to_dict()})
            if action == "pause":
                if not self.manager.pause(tid):
                    return self._error(409, f"当前状态（{d.status}）不能暂停")
            elif action == "resume":
                if not self.manager.resume(tid):
                    return self._error(409, f"当前状态（{d.status}）不能继续下载")
            elif action == "restart":
                if not self.manager.restart(tid):
                    return self._error(409, "重下没有执行（任务已不在列表里）")
            elif action == "cancel":
                if not self.manager.cancel(tid, remove_files=body.get("files", True)):
                    return self._error(409, "取消没有执行（任务已不在列表里）")
            elif action == "open":
                # "打开" means open the downloaded file itself (a .dmg brings up
                # the installer), not its folder. Unfinished tasks have no file
                # yet, so those reveal the download directory instead.
                opened = (
                    self.manager.open_file(d.target_path)
                    if d.status == "completed"
                    else self.manager.open_path(d.directory)
                )
                if not opened:
                    return self._error(
                        500,
                        self._open_why(
                            d.target_path if d.status == "completed" else d.directory
                        ),
                    )
            elif action == "reveal":
                reveal_target = (
                    d.target_path if d.status == "completed" else d.directory
                )
                shown = self.manager.reveal_path(reveal_target)
                if not shown:
                    return self._error(500, self._open_why(reveal_target))
            else:
                return self._error(400, f"unknown action {action}")
            return self._json({"ok": True, "task": d.to_dict()})

        if parts == ["action"]:
            act = body.get("action")
            if act == "pause_all":
                changed = self.manager.pause_all()
            elif act == "resume_all":
                changed = self.manager.resume_all()
            elif act == "clear_finished":
                changed = self.manager.clear_finished()
            elif act == "open_folder":
                path = body.get("path") or str(self.manager.config["download_dir"])
                if not self.manager.can_open_path(path):
                    return self._error(403, "path outside download directories")
                if not self.manager.open_path(path):
                    return self._error(500, self._open_why(path))
                changed = 1
            elif act == "shutdown":
                threading.Timer(0.6, self.server.shutdown).start()
                changed = 1
            else:
                return self._error(400, f"unknown action {act}")
            # 0 也要如实回：以前「全部暂停」点了没反应（没有可暂停的任务）和
            # 「已经暂停好了」在界面上长得一模一样。
            return self._json({"ok": True, "changed": int(changed)})

        if parts == ["config"]:
            try:
                self.manager.set_config(body)
            except (ValueError, TypeError) as e:
                # 端口/连接数写了字母、开关写了 "maybe"：说清楚比默默什么都不改好。
                return self._error(400, f"配置项无效：{e}")
            reply = {"ok": True, "config": self.manager.config}
            # 设置当场生效了，但可能没写进磁盘（目录只读、磁盘满）：不说的话
            # 用户要到下次重启才发现全丢了。
            if self.manager.state_error:
                reply["warning"] = "设置已生效，但没能写入磁盘：" + self.manager.state_error
            return self._json(reply)

        if parts == ["archive", "sweep"]:
            # 把归档规则补用到**已经下好**的文件上。默认只出清单不动手：移动用户
            # 的文件不是「忘了传参数」就该发生的事，得有人明确说 apply。
            from . import archive as archive_mod

            apply_it = to_bool(body.get("apply", False), "apply")
            directory = str(body.get("directory") or body.get("dir") or "").strip()
            if directory and not self.manager.can_open_path(directory):
                return self._error(403, "只能整理下载目录（或任务目录）里的文件")
            report = self.manager.archive_sweep(directory or None, apply_files=apply_it)
            if not report.get("ok") and not report.get("moves"):
                # 目录根本读不了：这已经是失败，不该让界面拿着一份空清单说「整理好了」。
                return self._error(400, report.get("error") or "整理失败")
            report["rules"] = archive_mod.describe(self.manager.config.get("archive_rules"))
            # 界面把「真正生效的那份规则」和输入框里的内容对一下：改了规则没点保存
            # 就来整理，看到的是空清单，很容易误判成「规则没管用」。
            report["archive_rules"] = archive_mod.prune_rules(
                self.manager.config.get("archive_rules")
            )
            report["applied"] = bool(apply_it)
            return self._json(dict(report, ok=True))

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
            # 桌面端没在运行时 xdg-open/Start-Process 会静静失败；回 200 +
            # ok:false 又不给原因，界面只能显示「点了没反应」。
            if not ok:
                return self._error(500, self._open_why(path))
            return self._json({"ok": True})

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

        # 播放列表：列出名单（preview）和按范围建任务（add）。
        # 两步都放在服务端，网页版 / Qt / CLI / 扩展才可能有同一份语义 ——
        # 尤其是「跳过了几条」「清单被上限截断」这种说漏了就会让用户以为
        # 下全了的细节，不该由四个客户端各写一遍。
        if len(parts) == 2 and parts[0] == "playlist" and parts[1] in ("preview", "add"):
            return self._playlist(parts[1], body)

        return self._error(404, "not found")

    def _playlist(self, action: str, body: dict):
        """POST /api/playlist/preview 与 /api/playlist/add 的共同实现。"""
        from .yt_playlist import (
            DEFAULT_ITEMS,
            MAX_ITEMS,
            item_filename,
            list_items,
            safe_dirname,
        )

        url = str(body.get("url") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            return self._error(400, "需要 http(s) 的播放列表链接")
        cookie = body.get("cookie") or self._site_cookie_for(url) or None
        proxy = self.manager.config.get("proxy") or None
        try:
            wanted_max = int(body.get("max_items") or DEFAULT_ITEMS)
        except (TypeError, ValueError):
            return self._error(400, "max_items 需要是整数")
        cap = min(MAX_ITEMS, max(1, wanted_max))

        try:
            res = list_items(url, cookie=cookie, proxy=proxy, max_items=cap)
        except Exception as e:  # noqa: BLE001
            res = {"ok": False, "is_playlist": False, "title": "", "items": [],
                   "count": 0, "skipped": 0, "truncated": False, "error": str(e)}
        items = res.get("items") or []
        base = {
            "ok": bool(res.get("ok")),
            "is_playlist": bool(res.get("is_playlist")),
            "title": res.get("title") or "",
            "count": len(items),
            "skipped": int(res.get("skipped") or 0),
            "truncated": bool(res.get("truncated")),
            # 界面据此决定「还有更多」的话术与 max_items 默认值
            "limit": int(res.get("limit") or cap),
            "error": res.get("error") or "",
            "playlist_url": url,
        }
        if not items:
            # 展开失败（要登录 / 私密清单 / 站点没有清单）也要带 is_playlist，
            # 界面才知道该说「这不是播放列表」还是「展开失败」。
            base["items"] = []
            return self._json(base)
        if action == "preview":
            base["items"] = items[:PLAYLIST_PREVIEW_MAX]
            base["preview_truncated"] = len(items) > PLAYLIST_PREVIEW_MAX
            return self._json(base)

        # ---- add ----
        if res.get("is_playlist"):
            try:
                picked = _playlist_indices(body, len(items))
            except ValueError as e:
                return self._error(400, str(e))
        else:
            picked = [1]
        by_index = {int(it["index"]): it for it in items}

        directory = body.get("dir") or body.get("directory")
        directory = str(directory).strip() if directory else str(
            self.manager.config.get("download_dir") or ""
        )
        folder = ""
        # 整份清单默认收进一个以清单标题命名的子目录：几十集散在「下载」里
        # 会和别的文件混成一团，而跨清单的同名分集（第 1 集、第 2 集）会互相
        # 顶成 "…(1)"，谁都认不出谁。给 false 就按用户点的目录原样落。
        if res.get("is_playlist") and body.get("subdir", True) is not False:
            given = body.get("subdir")
            folder = safe_dirname(
                given if isinstance(given, str) and given.strip() else res.get("title"),
                fallback="",
            )
        dest = os.path.join(directory, folder) if folder else directory
        if folder:
            # 子目录当场建出来：任务可能是「暂停/定时」状态，引擎要到真正开始
            # 下载才 makedirs；那之前界面里已经出现一份指向不存在目录的任务，
            # 用户点「打开目录」会落空。建不出来（只读盘、路径被文件占了）就
            # 直接报错，不能悄悄改用上级目录 —— 那等于把整份清单下到别处。
            try:
                os.makedirs(dest, exist_ok=True)
            except OSError as e:
                return self._error(400, f"没法创建清单子目录 {dest}：{e}")

        added: List[str] = []
        failed: List[Dict] = []
        for idx in picked:
            it = by_index.get(idx)
            if not it:
                failed.append({"index": idx, "error": "清单里没有这一条"})
                continue
            task_body = {
                "url": it["url"],
                "yt_page": it["url"],
                "kind": "video",
                "filename": item_filename(it, url),
                "directory": dest,
                "source": body.get("source") or "manual",
                "yt_height": body.get("yt_height") or body.get("height") or 0,
                "priority": body.get("priority"),
                "start_at": body.get("start_at"),
                "paused": body.get("paused"),
            }
            if cookie:
                # 展开用的是调用方给的 cookie（扩展会把页面 cookie 一起送过来），
                # 建出来的每条任务也该带着它，否则设置里没有这个站点时就变成匿名下载。
                task_body["cookie"] = cookie
            try:
                reply = self._create_task(task_body, show_desktop=False)
                if reply.get("ok"):
                    added.append(reply.get("id") or "")
                else:
                    failed.append({"index": idx, "error": reply.get("error") or "建任务失败"})
            except ValueError as e:
                failed.append({"index": idx, "url": it["url"], "error": str(e)[:300]})
        if added and self.manager.config.get("auto_show_gui", True):
            threading.Thread(
                target=desktop.ensure_desktop,
                args=([], int(self.manager.config.get("port") or 9000)),
                daemon=True,
            ).start()
        base.update(
            {
                "ok": bool(added),
                "added": len(added),
                "ids": added,
                "directory": dest,
                "failed": failed,
                "error": "" if added else (failed[0]["error"] if failed else "没有建成任何任务"),
            }
        )
        return self._json(base)

    def _create_task(self, body: dict, show_desktop: bool = True) -> dict:
        """建一条任务：POST /api/tasks 与 aria2 兼容层的 addUri 共用这一段。

        这样 RPC 建出来的任务和桌面端 / 网页版 / 扩展建的完全同路 —— 视频页面
        链接一样会转成 yt-dlp 任务，站点 Cookie 一样兜底，一样受重复下载约束。
        失败一律抛 ValueError（REST 侧回 400，RPC 侧回 JSON-RPC 错误），
        成功返回响应体。

        show_desktop=False 供播放列表批量建任务用：那一路几十条任务只该唤起
        一次桌面窗口，不该每条都起一个线程去问一遍。
        """
        url = (body.get("url") or "").strip()
        if not url:
            raise ValueError("缺少下载链接")
        if (
            not body.get("yt_page")
            and not body.get("page_url")
            and is_playlist_page(url)
        ):
            # 实测（随包 yt-dlp 2026.08.19）：--no-playlist 只对「既是视频又是清单」
            # 的地址生效，纯清单地址照样整份下完；而下载通道的输出模板是固定的
            # zdm-<任务id>.%(ext)s，于是每一集覆盖上一集，最后只剩一个文件、
            # 状态还是「已完成」。这种假成功必须当场拦下来，并说清该走哪条路。
            raise ValueError(
                "这是播放列表/合集地址，一条任务装不下整份（会一集覆盖一集，只留最后一个文件）。"
                "请展开后按条下载：网页版「播放列表…」/ 命令行 zdm add <链接> --playlist"
                " / POST /api/playlist/add"
            )
        headers = self._normalize_headers(body, url)
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
        # GUI/CLI 让 yt-dlp 自己挑画质时不会先解析标题；没有文件名时
        # 下载结束只能叫「download」或 zdm-<任务id>。给一个按链接推导的
        # 可读名字（音乐站是「歌手 - 曲名」，视频站是 Video-<id>）。
        if (
            (body.get("yt_page") or body.get("page_url"))
            and not (body.get("filename") or body.get("name"))
        ):
            body["filename"] = video_page_filename(url)
        section = _parse_section(body.get("section"))
        if section and not (body.get("yt_page") or body.get("page_url")):
            # 普通文件任务没有 yt-dlp 通道，选段只会下整包。静默忽略
            # 比报错更伤人：用户以为限制生效，结果拉了个全片。
            raise ValueError("选段只适用于视频页面链接（YouTube/B站等）")
        directory = body.get("dir") or body.get("directory")
        if directory is not None:
            if not isinstance(directory, str):
                raise ValueError("directory must be a string")
            if "\x00" in directory:
                raise ValueError("invalid directory")
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
                audio_only=str(body.get("audio_only") or "").lower()
                in ("1", "true", "yes", "on"),
                # YouTube：给了页面地址就走 yt-dlp 下载通道
                yt_page=body.get("yt_page") or body.get("page_url"),
                yt_height=body.get("yt_height") or body.get("height"),
                section=section,
                stream_video=_track_value(body, "stream_video"),
                stream_audio=_track_value(body, "stream_audio"),
                stream_subtitle=_track_value(body, "stream_subtitle"),
                stream_sub_format=_track_value(body, "stream_sub_format"),
                stream_live_limit=_live_limit_value(body),
                priority=body.get("priority"),
                start_at=_parse_start_at(body.get("start_at")),
            )
        except (ValueError, TypeError) as e:
            # TypeError as well as ValueError: the body is untrusted JSON,
            # and int() on a list/dict raises TypeError, which used to
            # escape this handler and drop the connection without a reply.
            raise ValueError(str(e)) from e
        # If the desktop client is not open (typical when the download comes
        # from the browser extension), bring it up so the user sees the task.
        # Pass NO urls: the task is already queued above, and handing the
        # url to the GUI makes it add a second copy.
        if show_desktop and self.manager.config.get("auto_show_gui", True):
            threading.Thread(
                target=desktop.ensure_desktop,
                args=([], int(self.manager.config.get("port") or 9000)),
                daemon=True,
            ).start()
        reply = {"ok": True, "id": d.id, "task": d.to_dict()}
        if self.manager.state_error:
            # 任务已经在跑，但这条记录没能写进 state.json：重启后它会消失。
            reply["warning"] = "任务已开始，但记录没能写入磁盘：" + self.manager.state_error
        return reply

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
) -> int:
    """Run the service. Returns the process exit status.

    端口被占时不能一律回 0。占着的是另一个健康的 ZDM，那本就该安静退出（桌面端
    和 LaunchAgent 会同时想当那个「唯一实例」，谁先绑上谁服务）；占着的不是 ZDM，
    那是真故障，必须非 0 退出 —— launchd/systemd 只在失败时重启，回 0 就等于让
    服务永远起不来还没人知道。
    """
    try:
        httpd = make_server(manager, host, port, token)
    except OSError as e:
        if ping(host, port):
            print(f"端口 {port} 上已有 ZDM 在服务，本进程退出（{e}）")
            return 0
        print(f"端口 {port} 被其他程序占用，ZDM 起不来（{e}）")
        return 1
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
    return 0


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
