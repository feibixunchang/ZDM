"""Multi-connection segmented download engine (dependency free)."""

from __future__ import annotations

import errno
import json
import os
import re as _re
import shutil
import sys
import threading
import time
import urllib.parse
import urllib.request
from typing import Callable, Dict, List, Optional, Tuple

from . import transport as transport_mod
from .http import HttpError, filename_from_headers
from .runtime import silent_proc_kwargs


def _no_window() -> dict:
    """Windows 上 ffmpeg 等控制台子进程不弹黑窗（见 runtime.silent_proc_kwargs）。"""
    return silent_proc_kwargs()


# ffmpeg 的 stderr 头里有 "Duration: 00:01:20.00" —— 流媒体的总时长，
# 事先拿不到总大小，只能靠它算百分比。
_RE_DURATION = _re.compile(r"Duration:\s*(\d+:\d+:\d+(?:\.\d+)?)")
# 音轨本身也是条播放列表（X/Twitter 的音频轨是 /pl/mp4a/128000/xxx.m3u8）
_PLAYLIST_URL_RE = _re.compile(r"\.(m3u8|mpd)(?:$|[?#])", _re.IGNORECASE)


def _parse_hms(text: str) -> float:
    """'01:20.00' / '00:01:20.00' → 秒。"""
    try:
        total = 0.0
        for part in str(text).split(":"):
            total = total * 60.0 + float(part)
        return total
    except Exception:  # noqa: BLE001
        return 0.0


def _yt_should_swap_client(err: str) -> bool:
    """下载失败的报错是否值得换 player_client 再试一次。

    只认实测特征：SABR 拦截（"page needs to be reloaded"）、人机验证
    （"Sign in to confirm" / "not a bot"），以及 SABR 只回分段不给格式时
    的 "Requested format is not available"（此时默认客户端的格式列表是
    空的，任何 -f 表达式都落空，换客户端才有完整阶梯）。其它错误（磁盘满、
    视频被删等）换客户端没用，不重试。"""
    if not err:
        return False
    e = err.lower()
    return (
        "page needs to be reloaded" in e
        or "sign in to confirm" in e
        or "not a bot" in e
        or "requested format is not available" in e
    )


def _yt_login_required(err: str) -> bool:
    """YouTube 明确要求登录/人机验证（而不是随机限流）。"""
    e = (err or "").lower()
    return (
        "not a bot" in e
        or "sign in to confirm" in e
        or "login_required" in e
        or "please sign in" in e
    )


def _yt_permanent_error(err: str) -> bool:
    """确定性失败：换客户端/换时间都没用，重试链应当立刻收工。

    和 ``_yt_should_swap_client`` 是互补关系：那个决定「值不值得再试」，
    这个决定「试都不用试了」。缺了它，一个已被删除的视频会把 4 次尝试
    （含两次 3 秒冷却）全部跑满，用户看着进度条空转半分钟。
    """
    if not err:
        return False
    e = err.lower()
    return (
        "video unavailable" in e
        or "this video is unavailable" in e
        or "private video" in e
        or "members-only" in e
        or "has been removed" in e
        or "unsupported url" in e
        or "is not a valid url" in e
        or "no space left" in e
    )


def _yt_runtime_hint(binp: str, err: str) -> str:
    """YouTube 失败时判断是不是「缺 JS 运行时」，并给出能动手的提示。

    2026 起 yt-dlp 要跑 YouTube 的播放器 JS 来解签名 / n 参数；没有 deno
    或 node 时，YouTube 只回故事板，或者干脆报 "The page needs to be
    reloaded"。照抄 yt-dlp 原文，用户会以为是 Cookie / 登录问题（实测），
    所以在错误后面补一句真正的解决办法。
    """
    try:
        from . import ytdlp_bin

        if ytdlp_bin.js_runtime_args(binp):
            return ""
    except Exception:  # noqa: BLE001
        return ""
    e = (err or "").lower()
    looks_like_js = (
        not e
        or "page needs to be reloaded" in e
        or "requested format is not available" in e
        or "signature solving failed" in e
        or "n challenge" in e
        or "javascript runtime" in e
    )
    if not looks_like_js:
        return ""
    return (
        "\n\n原因很可能是缺少 JS 运行时：YouTube 的签名/防爬挑战需要 deno（或 node）。"
        "装一个 deno（https://deno.com，装到 ~/.deno/bin 或 ~/.local/bin 即可，"
        "ZDM 会自动找到），然后重启 ZDM 再试。验证：终端执行 deno --version。"
    )


MIN_SPLIT = 1 << 20  # 1 MiB: minimum remaining size to justify splitting
HAS_PWRITE = hasattr(os, "pwrite")
O_BINARY = getattr(os, "O_BINARY", 0)  # Windows: without it files open in text mode

# 有些 CDN（尤其 B站音轨）没有 UA/Referer 就直接 403。
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _find_ffmpeg() -> Optional[str]:
    """Locate ffmpeg on this machine, tolerating a sandbox/app PATH.

    The ZDM.app process does not inherit the user's interactive-shell PATH, so a
    bare ``shutil.which("ffmpeg")`` misses Homebrew/MacPorts installs (e.g.
    /opt/homebrew/bin). Windows has no such convention at all — there ffmpeg is
    wherever the user unzipped it — so probe the common install roots there too.

    Keep this list in sync across the three platforms: a path missing here means
    「B站下载没声音」only on that OS, which is impossible to notice when you only
    test on one machine.
    """
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    # 随包分发的 ffmpeg 排最前：那是打包时验证过的那一份，用户机器上有没有
    # 装 brew/winget 都不该影响「B站下完没声音」这件事。
    # app_root() 在三种形态下分别指向 PyInstaller 的 _internal/、macOS 的
    # Contents/Resources/、源码树的 zdm/ —— 三者下面都有 bin/。
    bundled = []
    try:
        from . import runtime

        bundled = [os.path.join(runtime.app_root(), "bin", exe)]
    except Exception:  # noqa: BLE001 - 取不到就用系统装的那份
        pass
    candidates = bundled + [
        # --- macOS ---
        "/opt/homebrew/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        "/opt/local/bin/ffmpeg",
        # --- Linux ---
        "/usr/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        "/snap/bin/ffmpeg",
        "/var/lib/flatpak/exports/bin/ffmpeg",
        # --- 随 app 一起分发（三端同一思路：放在可执行文件旁边） ---
        os.path.join(os.path.dirname(sys.executable or ""), exe),
        "/Applications/ZDM.app/Contents/Resources/ffmpeg",
    ]
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "")
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        user = os.environ.get("USERPROFILE", "")
        candidates += [
            r"C:\ffmpeg\bin\ffmpeg.exe",
            os.path.join(pf, "ffmpeg", "bin", "ffmpeg.exe"),
            os.path.join(pf86, "ffmpeg", "bin", "ffmpeg.exe"),
            os.path.join(local, "Microsoft", "WinGet", "Links", "ffmpeg.exe"),
            os.path.join(local, "Programs", "ffmpeg", "bin", "ffmpeg.exe"),
            os.path.join(user, "scoop", "shims", "ffmpeg.exe"),
            os.path.join(user, "chocolatey", "bin", "ffmpeg.exe"),
        ]
    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    if shutil.which("ffmpeg"):
        return shutil.which("ffmpeg")
    # 兜底：把常见 bin 目录拼进 PATH 再找一次。
    # 分隔符必须用 os.pathsep —— 在 Windows 上硬写 ":" 会把整条 PATH 拼坏，
    # 于是「装了 ffmpeg 也永远找不到」。
    extra = [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/opt/local/bin",
        "/snap/bin",
    ]
    if os.name == "nt":
        extra = [
            r"C:\ffmpeg\bin",
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Links"),
        ]
    aug = os.pathsep.join(
        [p for p in [os.environ.get("PATH", "")] + extra if p]
    )
    return shutil.which("ffmpeg", path=aug)


def _netscape_pairs(text: str, host: str = "") -> List[Tuple[str, str]]:
    """从 Netscape cookie 文本里取出 (name, value)。

    只保留与 ``host`` 相关的域；一条都匹配不上时返回空列表，调用方
    决定是否退回「全带」（CDN 域与页面域不同是常态，硬过滤会把
    登录态全丢掉）。
    """
    pairs: List[Tuple[str, str]] = []
    seen = set()
    h = (host or "").lower().lstrip(".")
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        if line.startswith("#") and not line.startswith("#HttpOnly_"):
            continue
        body = line[len("#HttpOnly_") :] if line.startswith("#HttpOnly_") else line
        parts = body.split("\t")
        if len(parts) < 7:
            continue
        domain = parts[0].lower().lstrip(".")
        if h and domain and not (h == domain or h.endswith("." + domain)):
            continue
        name, value = parts[5].strip(), parts[6].strip()
        if name and name not in seen:
            seen.add(name)
            pairs.append((name, value))
    return pairs


def _json_pairs(text: str, host: str = "") -> List[Tuple[str, str]]:
    """从 JSON 形态的 cookie 数组里取出 (name, value)。"""
    import json as _json

    try:
        data = _json.loads(text)
    except Exception:  # noqa: BLE001
        return []
    if isinstance(data, dict):
        data = data.get("cookies") or []
    if not isinstance(data, list):
        return []
    pairs: List[Tuple[str, str]] = []
    seen = set()
    h = (host or "").lower().lstrip(".")
    for c in data:
        if not isinstance(c, dict):
            continue
        name = (c.get("name") or "").strip()
        if not name or name in seen:
            continue
        dom = (c.get("domain") or "").lower().lstrip(".")
        if h and dom and not (h == dom or h.endswith("." + dom)):
            continue
        seen.add(name)
        pairs.append((name, str(c.get("value") or "")))
    return pairs


def _cookie_header(cookie: str, host: str = "") -> str:
    """把各种形态的 cookie 压成能直接放进 HTTP 头的一行。

    扩展导出的完整 cookie 是 **Netscape 文件格式**（含 tab 和换行），
    原样塞进 Cookie 头会被 urllib 挡下来：
        Invalid header value b'# Netscape HTTP Cookie File\\n...'
    —— probe 阶段就抛错，任务报「probe failed」，一个字节都下不来。

    Netscape / JSON 形态压成 `name=value; name2=value2`；按域名过滤后
    一条都不剩时退回「全带」（媒体常在 CDN 域上，硬过滤会把登录态丢掉）。
    普通形态（`a=b; c=d`）原样透传。
    """
    text = (cookie or "").strip()
    if not text:
        return ""
    if "\t" in text or "Netscape" in text:
        pairs = _netscape_pairs(text, host)
        if not pairs:
            pairs = _netscape_pairs(text, "")
    elif text.startswith("[") or text.startswith("{"):
        pairs = _json_pairs(text, host)
        if not pairs:
            pairs = _json_pairs(text, "")
    else:
        return text
    return "; ".join("%s=%s" % (k, v) for k, v in pairs)


def _probe_has_audio(path: str) -> Optional[bool]:
    """文件里到底有没有音轨。ffmpeg 不可用/探测失败时返回 None（不下结论）。

    只用来「说实话」：DASH 视频轨天生无声，如果调用方没配 audio_url，
    以前的实现会静默当成功保存，用户只会觉得「下载坏了」。
    """
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        return None
    import subprocess

    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", path],
            capture_output=True,
            timeout=30,
            **_no_window(),
        )
    except Exception:
        return None
    text = (proc.stderr or b"").decode("utf-8", "ignore")
    if "Input #0" not in text:
        return None
    return "Audio:" in text


def _write_at(fd, tls, path, data, offset):
    """Write at an offset without moving the shared file position."""
    if HAS_PWRITE:
        return os.pwrite(fd, data, offset)
    # Windows: os.pwrite is not guaranteed - use a per-thread handle instead.
    h = getattr(tls, "fd", None)
    if h is None:
        h = os.open(path, os.O_RDWR | os.O_CREAT | O_BINARY, 0o644)
        tls.fd = h
    os.lseek(h, offset, os.SEEK_SET)
    return os.write(h, data)


class RateLimiter:
    """Global token bucket speed limiter (bytes per second, 0 = unlimited)."""

    def __init__(self, rate: int = 0):
        self.rate = rate
        self._lock = threading.Lock()
        self._tokens = float(rate or 0)
        self._last = time.monotonic()
        self._capacity = max(rate, 1)

    def set_rate(self, rate: int) -> None:
        with self._lock:
            self.rate = rate
            self._capacity = max(rate, 1)
            self._tokens = min(self._tokens, float(self._capacity))

    def consume(self, n: int) -> None:
        if self.rate <= 0:
            return
        while n > 0:
            with self._lock:
                now = time.monotonic()
                self._tokens += (now - self._last) * self.rate
                self._last = now
                if self._tokens > self._capacity:
                    self._tokens = float(self._capacity)
                take = min(n, int(self._tokens))
                self._tokens -= take
            if take <= 0:
                time.sleep(0.05)
                continue
            n -= take


class Chunk:
    """A byte range [start, end) with its own write cursor."""

    __slots__ = ("start", "end", "pos", "owner", "attempts")

    def __init__(self, start: int, end: int, pos: Optional[int] = None):
        self.start = start
        self.end = end
        self.pos = start if pos is None else pos
        self.owner: Optional[int] = None
        self.attempts = 0

    @property
    def remaining(self) -> int:
        return max(0, self.end - self.pos)

    def to_list(self) -> List[int]:
        return [self.start, self.end, self.pos]

    @classmethod
    def from_list(cls, data) -> "Chunk":
        c = cls(int(data[0]), int(data[1]), int(data[2]))
        return c


class Segmenter:
    """Splits a file into segments and hands work to idle connections."""

    def __init__(self, size: int, chunks: Optional[List[Chunk]] = None):
        self.size = size
        self._lock = threading.Lock()
        if chunks:
            self.chunks = chunks
        else:
            self.chunks = [Chunk(0, size)]

    def get_work(self, owner: int) -> Optional[Chunk]:
        with self._lock:
            free = None
            biggest = None
            for c in self.chunks:
                if c.pos >= c.end:
                    continue
                if c.owner is None:
                    free = c
                    break
                if biggest is None or c.remaining > biggest.remaining:
                    biggest = c
            if free is not None:
                free.owner = owner
                return free

            if biggest is not None and biggest.remaining >= MIN_SPLIT * 2:
                mid = biggest.pos + biggest.remaining // 2
                tail = Chunk(mid, biggest.end, mid)
                biggest.end = mid
                idx = self.chunks.index(biggest)
                self.chunks.insert(idx + 1, tail)
                tail.owner = owner
                return tail
            return None

    def release(self, chunk: Chunk) -> None:
        with self._lock:
            chunk.owner = None

    def done(self, chunk: Chunk) -> None:
        with self._lock:
            chunk.pos = chunk.end
            chunk.owner = None

    def downloaded(self) -> int:
        with self._lock:
            return sum(min(c.pos, c.end) - c.start for c in self.chunks)

    def is_complete(self) -> bool:
        with self._lock:
            if self.size <= 0:
                return False
            return all(c.pos >= c.end for c in self.chunks)

    def snapshot(self) -> List[List[int]]:
        with self._lock:
            return [c.to_list() for c in self.chunks]

    def balance(self, connections: int) -> None:
        """Pre-split into `connections` segments so each connection starts with work."""
        if self.size <= 0 or connections <= 1:
            return
        with self._lock:
            if len(self.chunks) > 1:
                return
            step = self.size // connections
            if step < MIN_SPLIT:
                return
            self.chunks = [
                Chunk(i * step, self.size if i == connections - 1 else (i + 1) * step)
                for i in range(connections)
            ]


class SpeedMeter:
    """Rolling speed estimation over a 5s window."""

    def __init__(self, window: float = 5.0):
        self.window = window
        self._lock = threading.Lock()
        self._samples: List[Tuple[float, int]] = []
        self.total = 0

    def add(self, n: int) -> None:
        with self._lock:
            self.total += n
            now = time.time()
            self._samples.append((now, n))
            cutoff = now - self.window
            while self._samples and self._samples[0][0] < cutoff:
                self._samples.pop(0)

    def speed(self) -> float:
        with self._lock:
            if not self._samples:
                return 0.0
            span = time.time() - self._samples[0][0]
            if span <= 0.1:
                return 0.0
            return sum(n for _, n in self._samples) / span

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()


STATUS = (
    "queued",
    "downloading",
    "paused",
    "completed",
    "error",
    "canceled",
    "merging",
)

# Where a task came from. The web UI, the browser extension and the desktop
# clients all render this as a small badge, and the extension's sniffer uses it
# to filter "media found on this page" from ordinary downloads. Anything not in
# this tuple is normalised to "manual" on the way in (see Manager.add), so a
# stale or hostile payload cannot invent new labels.
SOURCES = ("manual", "browser", "sniffer", "player", "webui", "protocol")
# Broad content kind. Empty string means "not a media file" (an ordinary
# download), which is what every pre-1.12.0 task effectively was.
KINDS = ("", "video", "audio", "hls", "dash")


def normalize_source(value, default: str = "manual") -> str:
    """Coerce an untrusted value into one of <see cref="SOURCES"/>.

    The value arrives as JSON from the browser extension (or anything else that
    can reach the loopback API), so it can be a number, a list or null. Calling
    ``.strip()`` on those used to raise AttributeError inside the request
    handler - the only ValueError catch in the server let it escape and the
    connection was dropped mid-request. Type-check first, then whitelist.
    """
    if not isinstance(value, str):
        return default
    v = value.strip().lower()
    return v if v in SOURCES else default


def normalize_kind(value) -> str:
    if not isinstance(value, str):
        return ""
    v = value.strip().lower()
    return v if v in KINDS else ""


def needs_sequential(url: str) -> bool:
    """YouTube 的 googlevideo 直链只能**从头顺序**读。

    实测（2026-10，android_vr 解出的 1080p 直链）：
      Range: bytes=0-1023        → 206
      Range: bytes=1000000-...   → 403 Forbidden
    在 URL 上追加 range=/rn= 参数也没用，同样 403 —— 签名（lsig/sig +
    sparams）把「从 0 开始」写死在里面了。

    所以这类地址必须单连接下载：8 线程分片会让 7 个线程从中间开始取，
    每个都拿到 403，任务直接失败（用户看到的就是「YouTube 一直下载失败」）。
    """
    if not url:
        return False
    try:
        p = urllib.parse.urlparse(url)
    except ValueError:
        return False
    host = (p.hostname or "").lower()
    if host != "googlevideo.com" and not host.endswith(".googlevideo.com"):
        return False
    return "/videoplayback" in (p.path or "")


def _effective_connections(url: str, connections: int) -> int:
    """按地址特性收敛连接数（目前只有 YouTube 直链需要单连接）。"""
    if needs_sequential(url):
        return 1
    return max(1, int(connections))


# googlevideo 直链任务的速度看门狗。推送时没带页面地址的任务（旧版扩展、
# 重载前打开的标签页、嗅探面板路径）会掉进内置顺序引擎：googlevideo 的
# mid-file Range 在部分网络（TUN/Fake-IP）下被 302 打断，吞吐只有几 KB/s
# （实测 720P 百十 KB、144P 3 KB/s）。Referer 里就有 YouTube 页面地址，
# 持续低速就地把任务切去 yt-dlp 通道 —— 引擎侧的「自动寻找最快下载渠道」。
_YT_SLOW_BPS = 96 * 1024      # 5 秒滚动速度低于此值算「慢」
_YT_SLOW_WINDOW = 40.0        # 持续慢这么久才切（避开起速/短暂抖动）
_YT_PAGE_RE = _re.compile(
    r"^https?://(www\.|m\.)?youtube(-nocookie)?\.com/(watch\?|shorts/)", _re.I
)


def yt_page_from_headers(headers: Optional[Dict[str, str]]) -> str:
    """从请求头里找 YouTube 页面地址（Referer）。

    扩展推送时总会带 `referer: location.href`，YouTube 页面上它就是
    watch 地址 —— 服务端推导、看门狗切换都靠它拿到页面链接。
    """
    for k, v in (headers or {}).items():
        if str(k).lower() == "referer":
            s = str(v or "").strip()
            if s and _YT_PAGE_RE.match(s):
                return s
    return ""


class _YTAbort(Exception):
    """内部信号：进度回调发现用户暂停/取消，用来立刻中断 yt-dlp。

    不用 KeyboardInterrupt（会连整个进程一起打），也不用 yt-dlp 自己的
    DownloadCancelled —— 那个只在特定阶段生效。这里抛什么都会被 yt-dlp
    包成 DownloadError，调用方再看 self._stop 判断真实意图。
    """


class Download:
    """One download task: metadata + running state."""
    def __init__(
        self,
        id: str,
        url: str,
        directory: str,
        filename: Optional[str] = None,
        connections: int = 8,
        headers: Optional[Dict[str, str]] = None,
        proxy: Optional[str] = None,
        max_retries: int = 5,
        timeout: float = 30.0,
        speed_limit: int = 0,
        source: str = "manual",
        kind: str = "",
        audio_url: str = "",
        yt_page: str = "",
        yt_height: int = 0,
    ):
        self.id = id
        self.url = url
        self.final_url = url
        self.directory = directory
        self.filename = filename or "download"
        self.connections = _effective_connections(url, connections)
        self.headers: Dict[str, str] = dict(headers or {})
        self.proxy = proxy
        self.max_retries = max(0, int(max_retries))
        self.timeout = timeout
        self.speed_limit = speed_limit
        # Provenance: who asked for this task, and what kind of payload it is.
        self.source = normalize_source(source)
        self.kind = normalize_kind(kind)
        # DASH streams (Bilibili / YouTube adaptive) ship video and audio as
        # separate files. When the caller supplies the matching audio track we
        # download it too and mux the two with ffmpeg — otherwise the video
        # would be silent.
        self.audio_url = audio_url or ""
        # YouTube 专用通道：页面地址 + 目标高度。有值时整个任务交给 yt-dlp
        # 下载（原因见 _run_ytdlp）。普通下载这两个字段都是空。
        self.yt_page = yt_page or ""
        self.yt_height = int(yt_height or 0)
        self._yt_bytes = 0        # 当前这一路流已下载的字节
        self._yt_completed = 0    # 之前几路流（视频轨）已完成的字节
        self._yt_speed = 0.0
        self._yt_proc = None      # 正在跑的 yt-dlp 子进程（暂停/取消要 terminate）
        # 音视频合并失败的具体原因（哪一步断了）。None 表示没试过/成功。
        self.mux_error: Optional[str] = None
        # HLS / DASH 通道的状态（ffmpeg 子进程 + 进度）
        self._stream_proc = None
        self._stream_bytes = 0
        self._stream_time = 0.0
        self._stream_duration = 0.0
        # 多阶段拉流（视频轨+音频轨两条播放列表）时的**直接**百分比。
        # -1 表示未启用，progress 走「时长占比」的老路。
        self._stream_progress = -1.0

        self.size = 0
        self.status = "queued"
        self.error: Optional[str] = None
        self.supports_range = False
        self.created_at = time.time()
        # 首次真正开始下载的时刻（resume 不覆盖）。GUI 用它显示
        # 「几点开始下载、预计/实际几点完成」。
        self.started_at: Optional[float] = None
        self.completed_at: Optional[float] = None
        self.meter = SpeedMeter()
        self.segmenter: Optional[Segmenter] = None
        self.limiter = RateLimiter(speed_limit)
        self.retries = 0
        self.transport_name = "native"
        self._transport = transport_mod.NativeTransport()
        self.global_limiter: Optional[RateLimiter] = None
        self._tls = threading.local()

        self._stop = threading.Event()
        # 用户按下暂停的意图。和 _stop 分开：_stop 是「让工作线程现在退出」，
        # 它会被 start() 清掉；暂停意图必须活到 resume()，否则排队中的任务
        # 被暂停后仍会被调度器拉起（_stop 在 start() 里被清空）。
        self._pause_requested = threading.Event()
        # 看门狗正在把任务切去 yt-dlp 通道（见 _upgrade_to_ytdlp）。
        # 置位期间 start() 的主循环必须安静退出，否则会和看门狗抢任务。
        self._yt_switching = threading.Event()
        self._threads: List[threading.Thread] = []
        self._fd: Optional[int] = None
        self._on_event: Optional[Callable[[str, "Download"], None]] = None
        self._lock = threading.Lock()

    def _http_headers(self) -> Dict[str, str]:
        """发普通 HTTP 请求用的请求头（Cookie 压成一行 header 形式）。

        ``self.headers`` 保持原样不动 —— yt-dlp 通道要的是 Netscape 原文
        （见 _run_ytdlp / _cookiefile）。只有真正发 HTTP 请求时才规范化，
        否则 Netscape 文本塞进 Cookie 头会被 urllib 以
        "Invalid header value" 直接拒掉，probe 阶段就失败。
        """
        h = dict(self.headers or {})
        for k in list(h.keys()):
            if k.lower() != "cookie":
                continue
            try:
                host = (urllib.parse.urlparse(self.url or "").hostname or "").lower()
            except Exception:  # noqa: BLE001
                host = ""
            h[k] = _cookie_header(h[k] or "", host)
        return h

    # ---------- paths ----------
    @property
    def part_path(self) -> str:
        return os.path.join(self.directory, self.filename + ".part")

    @property
    def meta_path(self) -> str:
        return self.part_path + ".zdm"

    @property
    def target_path(self) -> str:
        return os.path.join(self.directory, self.filename)

    # ---------- serialization ----------
    def to_dict(self) -> dict:
        seg = self.segmenter.snapshot() if self.segmenter else []
        return {
            "id": self.id,
            "url": self.url,
            "final_url": self.final_url,
            "filename": self.filename,
            "directory": self.directory,
            "size": self.size,
            "status": self.status,
            "error": self.error,
            # 音轨合并失败的具体一步（下载 / ffmpeg / 复查），出错弹窗用
            "mux_error": self.mux_error,
            "downloaded": self.downloaded,
            "speed": round(
                self._yt_speed
                if (self.yt_page or self._stream_bytes)
                else self.meter.speed()
            ),
            "connections": self.connections,
            "supports_range": self.supports_range,
            "transport": self.transport_name,
            "headers": self.headers,
            "proxy": self.proxy,
            "speed_limit": self.speed_limit,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "eta": self.eta,
            "progress": self.progress,
            "source": self.source,
            "kind": self.kind,
            "audio_url": self.audio_url,
            "yt_page": self.yt_page,
            "yt_height": self.yt_height,
            "chunks": seg,
        }

    def to_meta(self) -> dict:
        return {
            "url": self.url,
            "final_url": self.final_url,
            "filename": self.filename,
            "size": self.size,
            "connections": self.connections,
            "supports_range": self.supports_range,
            "transport": self.transport_name,
            "headers": self.headers,
            "proxy": self.proxy,
            "speed_limit": self.speed_limit,
            "source": self.source,
            "kind": self.kind,
            "audio_url": self.audio_url,
            "yt_page": self.yt_page,
            "yt_height": self.yt_height,
            "chunks": self.segmenter.snapshot() if self.segmenter else [],
        }

    def load_meta(self, data: dict) -> None:
        self.url = data.get("url", self.url)
        self.final_url = data.get("final_url", self.url)
        self.filename = data.get("filename", self.filename)
        self.size = int(data.get("size", 0) or 0)
        # 恢复时也要按地址特性收敛：老 meta 里可能存着 8 连接。
        self.connections = _effective_connections(
            self.final_url or self.url, int(data.get("connections", self.connections) or 1)
        )
        self.supports_range = bool(data.get("supports_range"))
        self.transport_name = data.get("transport") or "native"
        self._transport = transport_mod.make(self.transport_name)
        self.headers = data.get("headers") or {}
        self.proxy = data.get("proxy")
        self.speed_limit = int(data.get("speed_limit", 0) or 0)
        self.audio_url = str(data.get("audio_url") or "")
        self.yt_page = str(data.get("yt_page") or self.yt_page or "")
        self.yt_height = int(data.get("yt_height") or self.yt_height or 0)
        self.limiter.set_rate(self.speed_limit)
        # Tasks persisted before 1.12.0 have no source/kind; keep "manual".
        self.source = normalize_source(data.get("source"), self.source)
        self.kind = normalize_kind(data.get("kind") or self.kind)
        chunks = data.get("chunks") or []
        if chunks and self.size:
            self.segmenter = Segmenter(
                self.size, [Chunk.from_list(c) for c in chunks]
            )

    # ---------- progress ----------
    @property
    def downloaded(self) -> int:
        # yt-dlp 通道自己记账（视频轨 + 音频轨是两个文件，meter 一个都统计不到）
        if self.yt_page:
            if self.status == "completed" and self.size:
                return self.size
            return max(0, self._yt_bytes)
        # 流媒体通道：ffmpeg 自己写文件，meter 统计不到
        if self._stream_bytes:
            if self.status == "completed" and self.size:
                return self.size
            return self._stream_bytes
        # 合流后文件变大（视频轨 + 音频轨），而 meter 只累计了视频轨的字节。
        # 已完成的项再拿 meter 算，就会显示成「54.5 MB / 57.2 MB、95.3%」——
        # 明明下完了却像没下完。完成态直接按最终文件大小报。
        if self.status == "completed" and self.size:
            return self.size
        if self.segmenter:
            return self.segmenter.downloaded()
        return self.meter.total

    @property
    def progress(self) -> float:
        if self.yt_page:
            # 视频轨下完还有音频轨，那会儿远没结束，不能像分块下载那样直接报 100
            if self.status == "completed":
                return 100.0
            if not self.size:
                return 0.0
            return min(99.5, round(self.downloaded / self.size * 100, 2))
        # 流媒体：总大小事先不知道，用「已播时长 / 总时长」算百分比。
        # 多阶段拉流（视频轨+音频轨）时 _stream_progress 已经是映射好的
        # 整体百分比（视频 0-85%、音频 85-95%、合流 95-100），优先用它。
        if self._stream_progress >= 0:
            if self.status == "completed":
                return 100.0
            return min(99.5, round(self._stream_progress, 2))
        if self._stream_duration:
            if self.status == "completed":
                return 100.0
            return min(
                99.5, round(self._stream_time / self._stream_duration * 100, 2)
            )
        # 合流/完成阶段：字节已经全部到手，百分比只能是 100。
        if self.status in ("merging", "completed"):
            return 100.0
        if not self.size:
            return 0.0
        return min(100.0, round(self.downloaded / self.size * 100, 2))

    @property
    def eta(self) -> Optional[int]:
        # 流媒体：总大小事先不知道，用剩余时长除以速度
        if self._stream_duration and self._yt_speed:
            left = self._stream_duration - self._stream_time
            if left <= 0:
                return 0
            return int(left / max(0.01, self._yt_speed))
        sp = self._yt_speed if self.yt_page else self.meter.speed()
        if not sp or not self.size:
            return None
        left = self.size - self.downloaded
        if left <= 0:
            return 0
        return int(left / sp)

    # ---------- control ----------
    def set_event_callback(self, cb: Optional[Callable[[str, "Download"], None]]) -> None:
        self._on_event = cb

    def _emit(self, event: str) -> None:
        if event == "error":
            _log_download_error(self)
        if self._on_event:
            try:
                self._on_event(event, self)
            except Exception:
                pass

    def _prepare(self) -> None:
        os.makedirs(self.directory, exist_ok=True)
        meta_file = self.meta_path
        if os.path.exists(meta_file):
            try:
                with open(meta_file, "r", encoding="utf-8") as f:
                    self.load_meta(json.load(f))
            except Exception:
                pass

        if self.segmenter is None:
            url = self.final_url or self.url
            try:
                t, info = transport_mod.probe_with_fallback(
                    url, self._http_headers(), self.timeout, self.proxy
                )
            except Exception as e:
                raise ConnectionError(f"probe failed: {e}") from e

            self.transport_name = t.name
            self._transport = t
            self.final_url = info["final_url"]
            self.supports_range = bool(info["supports_range"])
            total = int(info["size"] or 0)
            if not total:
                self.supports_range = False
            self.size = total
            if not self.filename or self.filename == "download":
                self.filename = filename_from_headers(info["headers"], self.final_url)
            self.filename = _unique_name(self.directory, self.filename)  # never clobber
            # 只有支持 Range 才用分块器。单流任务用它的话，工作线程从不更新
            # 分块位置，downloaded 永远是 0 —— 进度条就会一直卡在 0%。
            self.segmenter = Segmenter(total) if (total and self.supports_range) else None

        elif self.size and not os.path.exists(self.part_path):
            # metadata survived but the partial file is gone
            self.segmenter = Segmenter(self.size) if self.supports_range else None

        if not self.supports_range:
            # 旧 meta 里的 chunks 对单流没有意义（必须从头重下），留着还会
            # 让恢复时误以为已完成。
            self.segmenter = None
            try:
                os.remove(self.meta_path)
            except OSError:
                pass

        if self.size and self.supports_range and self.segmenter is None:
            # meta 里只剩 size（chunks 丢失/为空）时的保底
            self.segmenter = Segmenter(self.size)
        if self.size and self.supports_range and self.segmenter:
            self.segmenter.balance(self.connections)

        fd = os.open(self.part_path, os.O_RDWR | os.O_CREAT | O_BINARY, 0o644)
        self._fd = fd
        # 单流会以 "wb" 重新打开 .part，这里预分配只会白写一遍磁盘。
        if self.size and self.supports_range:
            try:
                os.posix_fallocate(fd, 0, self.size)
            except (AttributeError, OSError):
                os.ftruncate(fd, self.size)

    def _save_meta(self) -> None:
        if not self.segmenter:
            return
        tmp = self.meta_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.to_meta(), f)
            os.replace(tmp, self.meta_path)
        except Exception:
            pass

    def start(self) -> None:
        """Run the download to completion in the calling thread."""
        # 调度器可能刚把 status 改成 downloading 就撞上用户暂停：暂停意图
        # 还在，就不要再清 _stop 开跑，否则「暂停排队任务」会变成空操作。
        if self._pause_requested.is_set():
            self.status = "paused"
            self._emit("paused")
            return
        if self.yt_page:
            self._start_ytdlp()
            return
        if self.kind in ("hls", "dash"):
            # 流媒体（m3u8 / mpd）：地址指向的是**播放列表**，不是媒体文件。
            # 嗅探到的几百字节条目是它的一个分片，直接推给下载引擎只能下到
            # 一个碎片。整条交给 ffmpeg 拉：它会跟着 playlist 取分片、
            # 解 AES-128，最后封装成一个能播放的文件。
            self._start_stream()
            return
        try:
            self._prepare()
        except Exception as e:
            self.status = "error"
            self.error = str(e)
            self._emit("error")
            return

        # probe / 读 meta 可能要几秒，暂停可能在这期间落到队列上。
        if self._pause_requested.is_set():
            self._close_fd()
            self.status = "paused"
            self._emit("paused")
            return

        self.status = "downloading"
        if self.started_at is None:
            self.started_at = time.time()
        self._stop.clear()
        # 与 pause() 的交错窗口：清完 _stop 再看一眼暂停意图。
        if self._pause_requested.is_set():
            self._close_fd()
            self.status = "paused"
            self._emit("paused")
            return
        self.meter.reset()
        self._emit("started")

        # googlevideo 直链 + Referer 是 YouTube 页面 → 挂速度看门狗。
        # 持续低速（内置顺序引擎在部分网络下被 302 反复打断）就地把任务
        # 切去 yt-dlp 通道。yt_page 已在头的任务本来就走的 yt-dlp，不挂。
        if not self.yt_page and needs_sequential(self.url):
            if yt_page_from_headers(self.headers):
                self._arm_yt_slow_guard()

        if self.size and self.supports_range:
            self._run_segmented()
        else:
            self._run_single()

        while True:
            if self._yt_switching.is_set():
                # 看门狗正在切去 yt-dlp 通道：它已接管收尾（关 fd、清文件、
                # 重跑下载），主循环这里安静退出，别去走暂停/重试分支。
                return
            if self._stop.is_set():
                if self._yt_switching.is_set():
                    return
                # a pause that landed after the last byte still has to finalize
                if self.segmenter and self.segmenter.is_complete() and self.size:
                    self._finish()
                    return
                self._close_fd()
                self._save_meta()
                if self.status == "downloading":
                    self.status = "paused"
                self._emit("paused")
                return
            if (
                self.segmenter
                and self.segmenter.is_complete()
                and not any(t.is_alive() for t in self._threads)
            ):
                self._finish()
                return
            if self.status == "error":
                self._close_fd()
                self._save_meta()
                self._emit("error")
                return
            if self._threads and all(not t.is_alive() for t in self._threads):
                if self._yt_switching.is_set():
                    return
                # every connection exited without finishing
                if self.segmenter and self.segmenter.downloaded() < self.size:
                    self.retries += 1
                    if self.retries <= self.max_retries:
                        time.sleep(min(2 ** self.retries, 8))
                        for c in self.segmenter.chunks:
                            c.attempts = 0
                        self._threads = []
                        (
                            self._run_segmented()
                            if self.supports_range and self.size
                            else self._run_single()
                        )
                        continue
                    self.status = "error"
                    self.error = self.error or "download stalled"
                    self._close_fd()
                    self._save_meta()
                    self._emit("error")
                    return
            time.sleep(0.2)

    # ---------- YouTube：交给 yt-dlp（见 _run_ytdlp 的说明） ----------
    def _yt_fallback_direct(self) -> bool:
        """SABR 把 yt-dlp 下载通道拦死时的最后兜底。返回是否接管了任务。

        yt-dlp 按请求随机被拦（格式列表为空/page needs to be reloaded），
        重试链全灭后，解析通道（yt_resolve，带 5 分钟缓存）往往还拿得到
        可用直链 —— 把任务原地切换成内置引擎的**顺序**直链下载
        （googlevideo 只认从头的连续段，并发分片必 403，见 needs_sequential），
        音轨用 format 自带的 audio_url 在 _finish 里 ffmpeg 合流。
        """
        try:
            from . import yt_resolve

            cookie = (self.headers or {}).get("Cookie") or ""
            best = None
            # 第一优先：任务自带直链。播放器按钮推送时扩展已经把解析的
            # 直链放进 body.url（googlevideo），这条链在推送当时刚验证
            # 可用 —— 不必再赌一遍解析通道（它同样会被随机拦）。
            if self.url and "googlevideo.com/" in self.url:
                best = {"url": self.url, "audio_url": self.audio_url or ""}
            # 第二优先：重新解析（带 5 分钟缓存），解析通道还活着就能拿到。
            if best is None:
                res = yt_resolve.resolve(
                    self.yt_page, cookie=cookie, proxy=self.proxy or ""
                )
                if not (res and res.get("ok")):
                    return False
                want = int(self.yt_height or 0)
                for f in res.get("formats") or []:
                    if not f or f.get("kind") != "video" or not f.get("url"):
                        continue
                    h = int(f.get("height") or 0)
                    if want and h > want:
                        continue
                    if best is None or h >= int(best.get("height") or 0):
                        best = f
            if best is None:
                return False
        except Exception:
            return False

        # 原地切换成普通下载通道。meta 里存着旧的 yt_page，先删掉，
        # 否则 _prepare 的 load_meta 会把 yt_page 还原回来。
        try:
            if os.path.exists(self.meta_path):
                os.remove(self.meta_path)
        except OSError:
            pass
        self.yt_page = ""
        self.url = best["url"]
        self.final_url = best["url"]
        if best.get("audio_url"):
            self.audio_url = best["audio_url"]
        if needs_sequential(self.url):
            self.connections = 1
        try:
            self._prepare()
        except Exception:
            return False
        # 兜底切换发生在 yt-dlp 失败之后，这期间用户可能已经按了暂停。
        if self._pause_requested.is_set():
            self._close_fd()
            self.status = "paused"
            self._emit("paused")
            return True
        self.status = "downloading"
        self._stop.clear()
        self.meter.reset()
        self._emit("started")
        if self.size and self.supports_range:
            self._run_segmented()
        else:
            self._run_single()
        # 与 start() 的普通通道相同的收尾循环：跟进分块线程并回报终态。
        while True:
            if self._stop.is_set():
                self._close_fd()
                self._save_meta()
                if self.status == "downloading":
                    self.status = "paused"
                self._emit("paused")
                return True
            if (
                self.segmenter
                and self.segmenter.is_complete()
                and not any(t.is_alive() for t in self._threads)
            ):
                self._finish()
                return True
            if self.status == "error":
                self._close_fd()
                self._save_meta()
                self._emit("error")
                return True
            if self._threads and all(not t.is_alive() for t in self._threads):
                self.status = "error"
                self.error = self.error or "download stalled"
                self._close_fd()
                self._save_meta()
                self._emit("error")
                return True
            time.sleep(0.2)

    # ---------- YouTube：慢速直链任务自动切去 yt-dlp 通道 ----------
    def _arm_yt_slow_guard(self) -> None:
        """启动速度看门狗（daemon 线程，任务结束/暂停时一起退）。"""
        t = threading.Thread(target=self._watch_yt_slow, daemon=True)
        t.start()

    def _watch_yt_slow(self) -> None:
        """盯着 googlevideo 直链任务的速度，持续低速就升级下载通道。

        判定用 SpeedMeter 的 5 秒滚动速度连续 _YT_SLOW_WINDOW 秒低于
        _YT_SLOW_BPS —— 40 秒的窗口足以越过起速抖动和一次 302 重试，
        又不会让用户干等太久。等待用 `self._stop.wait(5)`：暂停/取消时
        看门狗和工作线程一起退，不会变成野线程。
        """
        slow_since: Optional[float] = None
        while not self._stop.wait(5.0):
            if self._yt_switching.is_set():
                return
            if self.status not in ("downloading", "queued"):
                return
            if self.segmenter and self.segmenter.is_complete():
                return
            now = time.time()
            if self.meter.speed() < _YT_SLOW_BPS:
                if slow_since is None:
                    slow_since = now
                elif now - slow_since >= _YT_SLOW_WINDOW:
                    self._upgrade_to_ytdlp()
                    return
            else:
                slow_since = None

    def _upgrade_to_ytdlp(self) -> bool:
        """把慢速的 googlevideo 直链任务**原地**切去 yt-dlp 通道。

        和 `_yt_fallback_direct` 是反方向：那边是 yt-dlp 被 SABR 拦死时
        退回直链，这边是直链慢到没法用（几 KB/s）时改走 yt-dlp。页面地址
        从 Referer 拿（扩展推送必带）。yt-dlp 自己下载+合流；万一它也被拦，
        它内部还会再退回这条直链，最坏情况等于升级前的行为。
        """
        page = yt_page_from_headers(self.headers)
        if not page:
            return False
        # 随包 yt-dlp 不在（破损安装）时不升级：慢总比直接报错好。
        try:
            from . import ytdlp_bin

            if not ytdlp_bin.binary_path():
                return False
        except Exception:  # noqa: BLE001
            return False
        self._yt_switching.set()
        try:
            self._stop.set()
            # 35s > 传输层最大读超时（默认 30s），保证工作线程一定退干净了
            # 再动 fd 和文件 —— 否则残留线程会把删掉的 .part 重新创建出来。
            if not self._join_workers(timeout=35.0):
                # 极端情况：线程卡死。放弃升级，退回暂停态让用户手动重来
                #（resume 会重新挂上看门狗）。
                self._yt_switching.clear()
                self._close_fd()
                self._save_meta()
                self.status = "paused"
                self._emit("paused")
                return False
            self._close_fd()
            # meta 里是旧的 chunks/直链状态，留着会让后续 _prepare 恢复错乱
            for p in (self.meta_path, self.part_path):
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except OSError:
                    pass
            # yt-dlp 从头下自己的文件，旧进度统计作废（几 KB/s 爬出来的
            # 那几个 MB 不要也罢，yt-dlp 通道几十秒就下完了）。
            self.yt_page = page
            self.segmenter = None
            self._yt_bytes = 0
            self._yt_completed = 0
            self._yt_speed = 0.0
            self.retries = 0
            self.meter.reset()
            self._stop.clear()
            if self._pause_requested.is_set():
                self.status = "paused"
                self._emit("paused")
                return True
            self.status = "downloading"
            self.error = None
            self.mux_error = None
            self._emit("progress")
            self._run_ytdlp()
        finally:
            self._yt_switching.clear()
        return True

    def _start_ytdlp(self) -> None:
        self._stop.clear()
        if self._pause_requested.is_set():
            self.status = "paused"
            self._emit("paused")
            return
        self.status = "downloading"
        if self.started_at is None:
            self.started_at = time.time()
        self._emit("started")
        self._run_ytdlp()

    # ---------- HLS / DASH（流媒体播放列表）----------

    def _start_stream(self) -> None:
        self._stop.clear()
        if self._pause_requested.is_set():
            self.status = "paused"
            self._emit("paused")
            return
        self.status = "downloading"
        if self.started_at is None:
            self.started_at = time.time()
        self._stream_bytes = 0
        self._stream_time = 0.0
        self._stream_duration = 0.0
        self._stream_progress = -1.0
        self._emit("started")
        self._run_stream()

    def _run_stream(self) -> None:
        """把 m3u8 / mpd 整条拉下来，封装成一个文件。

        为什么必须走 ffmpeg 而不是普通下载通道：m3u8 是**播放列表**，
        里面列的是一串分片（.ts / .m4s）。嗅探面板里看到的「300 B」
        「279 KB」那些就是其中一个分片 —— 直接推送只能下到一个碎片。
        ffmpeg 自带 hls / dash demuxer：跟着 playlist 取分片、AES-128
        加密流也能解密，最后封装成 mp4。Ghost Downloader 用 N_m3u8DL-RE
        做的是同一件事。

        音视频分开的站（实测 X/Twitter：视频是 /pl/avc1/<分辨率>/xxx.m3u8、
        音频是 /pl/mp4a/128000/xxx.m3u8，两份独立的播放列表）：只拉视频
        那份就是「有画面没声音」，只拉音频那份就是「只有声音」—— 用户
        两种都撞到过。audio_url 也是播放列表时，两份各拉一遍再合流；
        合流失败就退回「无声但能播」的视频文件（和 _mux_audio 同一哲学）。
        """
        import subprocess
        import threading

        ffmpeg = _find_ffmpeg()
        if not ffmpeg:
            self.status = "error"
            self.error = "找不到 ffmpeg（app 的 bin/ffmpeg 缺失），无法下载流媒体"
            self._emit("error")
            return

        stem = "zdm-" + self.id
        out = os.path.join(self.directory, stem + ".mp4")
        # 自定义请求头：m3u8 所在的 CDN 经常要 Referer 才给 playlist，
        # 直播/会员流还要 Cookie。ffmpeg 的 hls demuxer 用 -headers 传。
        hdrs = self._http_headers()
        hdr_blob = ""
        if hdrs:
            hdr_blob = "".join("%s: %s\r\n" % (k, v) for k, v in hdrs.items())

        audio_url = (self.audio_url or "").strip()
        audio_is_pl = bool(_PLAYLIST_URL_RE.search(audio_url))
        tmpdir = ""
        v_tmp = a_tmp = ""
        if audio_is_pl:
            import tempfile
            tmpdir = tempfile.mkdtemp(prefix="zdmstream")
            # 中间产物用 mkv：-c copy 对乱七八糟的编码组合最宽容，
            # 最后合成 mp4 那一步再挑错。
            v_tmp = os.path.join(tmpdir, "video.mkv")
            a_tmp = os.path.join(tmpdir, "audio.mka")

        def pull(url: str, dst: str, lo: float, hi: float, errs: list) -> int:
            """跑一段 ffmpeg 拉流，进度线性映射到 [lo, hi]。返回退出码。"""
            # 暂停可能正好落在两段之间：别再启动下一段 ffmpeg。
            if self._stop.is_set():
                return -1
            cmd = [
                ffmpeg, "-hide_banner", "-nostdin", "-y",
                # info 级别才会打印 "Duration: 00:10:00.00" —— 那是流媒体唯一的
                # 总长度线索（事先拿不到 Content-Length），靠它算百分比。
                # -nostats 已经把逐帧的 stats 行关掉了，剩下的输出很少。
                "-loglevel", "info",
                "-progress", "pipe:1", "-nostats",
            ]
            if hdr_blob:
                cmd += ["-headers", hdr_blob]
            if self.proxy:
                cmd += ["-http_proxy", self.proxy]
            cmd += ["-i", url, "-c", "copy"]
            # 直接产出 mp4 的那一路（单播放列表路径）保留 faststart；
            # 中间暂存是 mkv，不需要。
            if dst.lower().endswith(".mp4"):
                cmd += ["-movflags", "+faststart"]
            cmd += [dst]

            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL,
                    **_no_window(),
                )
            except Exception as e:  # noqa: BLE001
                errs.append("启动 ffmpeg 失败：%s" % e)
                return -1
            self._stream_proc = proc

            def read_err() -> None:
                try:
                    for raw in proc.stderr:
                        line = raw.decode("utf-8", "ignore").strip()
                        if not line:
                            continue
                        errs.append(line)
                        if len(errs) > 40:
                            del errs[:20]
                        m = _RE_DURATION.search(line)
                        if m:
                            self._stream_duration = _parse_hms(m.group(1))
                except Exception:  # noqa: BLE001
                    pass

            def read_out() -> None:
                try:
                    for raw in proc.stdout:
                        line = raw.decode("utf-8", "ignore").strip()
                        if "=" not in line:
                            continue
                        k, _, v = line.partition("=")
                        k = k.strip()
                        v = v.strip()
                        try:
                            if k == "out_time_us":
                                self._stream_time = int(v) / 1e6
                            elif k == "total_size":
                                self._stream_bytes = int(v)
                            elif k == "speed" and v.endswith("x"):
                                self._yt_speed = float(v[:-1] or 0)
                        except ValueError:
                            continue
                        if self._stop.is_set():
                            return
                        # 多阶段进度：本阶段「已播时长/总时长」线性映射到
                        # [lo, hi] —— 视频轨 0-85%，音频轨 85-95%。
                        if self._stream_duration > 0:
                            frac = min(1.0, self._stream_time / self._stream_duration)
                            self._stream_progress = lo + (hi - lo) * frac
                        self._emit("progress")
                except Exception:  # noqa: BLE001
                    pass

            t_err = threading.Thread(target=read_err, daemon=True)
            t_out = threading.Thread(target=read_out, daemon=True)
            t_err.start()
            t_out.start()
            try:
                proc.wait()
            finally:
                t_err.join(timeout=1.0)
                t_out.join(timeout=1.0)
                self._stream_proc = None
            return proc.returncode

        errs: list = []
        self._stream_progress = 0.0
        if audio_is_pl:
            # 阶段一：视频轨（大头，0-85%）
            rc = pull(self.url, v_tmp, 0.0, 85.0, errs)
            if self._stop.is_set():
                self._cleanup_stream_tmp(tmpdir)
                self.status = "paused" if self.status != "canceled" else "canceled"
                self._emit(self.status)
                return
            if rc != 0 or not os.path.exists(v_tmp):
                self._cleanup_stream_tmp(tmpdir)
                msg = "；".join(errs[-6:]) if errs else ""
                self.status = "error"
                self.error = (
                    "流媒体下载失败（ffmpeg 退出码 %s）%s"
                    % (rc, ("：" + msg) if msg else "")
                )[:600]
                self._emit("error")
                return
            # 阶段二：音频轨（85-95%）。失败不致命 —— 退回无声视频。
            a_rc = pull(audio_url, a_tmp, 85.0, 95.0, errs)
            if self._stop.is_set():
                # 以前这里不检查：音频阶段一暂停，流程会继续「合流」并用
                # 视频轨收尾，把暂停直接变成「已完成」。
                self._cleanup_stream_tmp(tmpdir)
                self.status = "paused" if self.status != "canceled" else "canceled"
                self._emit(self.status)
                return
            has_audio = a_rc == 0 and os.path.exists(a_tmp)
            if not has_audio:
                errs.append("音频轨拉取失败（退出码 %s），退回无声视频" % a_rc)
            # 阶段三：合流（95-100%）
            self._stream_progress = 95.0
            self.status = "merging"
            self._emit("progress")
            final_ok = False
            if has_audio:
                mux_cmd = [
                    ffmpeg, "-hide_banner", "-nostdin", "-y",
                    "-loglevel", "error",
                    "-i", v_tmp, "-i", a_tmp,
                    "-c", "copy", "-movflags", "+faststart", out,
                ]
                try:
                    mproc = subprocess.Popen(
                        mux_cmd,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        stdin=subprocess.DEVNULL,
                        **_no_window(),
                    )
                    self._stream_proc = mproc  # 暂停/取消时能立刻杀掉合流进程
                    try:
                        _, merr = mproc.communicate(timeout=600)
                    except subprocess.TimeoutExpired:
                        mproc.kill()
                        _, merr = mproc.communicate()
                        errs.append("合流超时（600 秒）")
                    finally:
                        self._stream_proc = None
                    final_ok = mproc.returncode == 0 and os.path.exists(out)
                    if not final_ok and merr:
                        errs.append(
                            merr.decode("utf-8", "ignore").strip()[-400:]
                        )
                except Exception as e:  # noqa: BLE001
                    errs.append("合流失败：%s" % e)
            if self._stop.is_set():
                self._cleanup_stream_tmp(tmpdir)
                try:
                    if os.path.exists(out):
                        os.remove(out)
                except OSError:
                    pass
                self.status = "paused" if self.status != "canceled" else "canceled"
                self._emit(self.status)
                return
            if not final_ok:
                # 合流不成 → 无声但能播，总比全丢好。视频轨暂存是 mkv，
                # 直接改后缀成 mp4 大多数播放器都打不开，先重封装一次。
                remux_cmd = [
                    ffmpeg, "-hide_banner", "-nostdin", "-y",
                    "-loglevel", "error", "-i", v_tmp,
                    "-c", "copy", "-movflags", "+faststart", out,
                ]
                try:
                    rproc = subprocess.Popen(
                        remux_cmd,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        stdin=subprocess.DEVNULL,
                        **_no_window(),
                    )
                    self._stream_proc = rproc
                    try:
                        _, rerr = rproc.communicate(timeout=600)
                    finally:
                        self._stream_proc = None
                    final_ok = rproc.returncode == 0 and os.path.exists(out)
                    if not final_ok and rerr:
                        errs.append(rerr.decode("utf-8", "ignore").strip()[-300:])
                except Exception as e:  # noqa: BLE001
                    errs.append("重封装失败：%s" % e)
                if self._stop.is_set():
                    try:
                        if os.path.exists(out):
                            os.remove(out)
                    except OSError:
                        pass
                    self._cleanup_stream_tmp(tmpdir)
                    self.status = "paused" if self.status != "canceled" else "canceled"
                    self._emit(self.status)
                    return
            if not final_ok:
                try:
                    os.replace(v_tmp, out)
                except OSError as e:
                    self._cleanup_stream_tmp(tmpdir)
                    self.status = "error"
                    self.error = (
                        "流媒体下载失败（合流失败且视频暂存不可用）：%s；%s"
                        % (e, "；".join(errs[-4:]))
                    )[:600]
                    self._emit("error")
                    return
            self._cleanup_stream_tmp(tmpdir)
        else:
            rc = pull(self.url, out, 0.0, 100.0, errs)
            if self._stop.is_set():
                try:
                    if os.path.exists(out):
                        os.remove(out)
                except OSError:
                    pass
                self.status = "paused" if self.status != "canceled" else "canceled"
                self._emit(self.status)
                return
            if rc != 0 or not os.path.exists(out):
                # ffmpeg -y 失败时可能留下半截 mp4；别把它当成品摆在那。
                try:
                    if os.path.exists(out):
                        os.remove(out)
                except OSError:
                    pass
                msg = "；".join(errs[-6:]) if errs else ""
                self.status = "error"
                self.error = (
                    "流媒体下载失败（ffmpeg 退出码 %s）%s"
                    % (rc, ("：" + msg) if msg else "")
                )[:600]
                self._emit("error")
                return

        # 换成用户看得懂的文件名
        want = str(self.filename or "").strip()
        if want.lower() in ("", "download"):
            want = os.path.basename(out)
        final = _unique_name(self.directory, want)
        target = os.path.join(self.directory, final)
        try:
            os.replace(out, target)
        except OSError:
            try:
                shutil.move(out, target)
            except OSError as e:
                self.status = "error"
                self.error = "文件下完了但改名失败：%s" % e
                self._emit("error")
                return
        self.filename = final
        try:
            self.size = os.path.getsize(target)
        except OSError:
            pass
        self._stream_bytes = self.size
        self._stream_progress = 100.0
        self.status = "completed"
        self.completed_at = time.time()
        self.error = None
        self._emit("completed")

    @staticmethod
    def _cleanup_stream_tmp(tmpdir: str) -> None:
        """删掉多阶段拉流的中间产物目录（视频/音频暂存）。"""
        if not tmpdir:
            return
        try:
            import shutil as _sh
            _sh.rmtree(tmpdir, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass

    def _yt_progress(self, d: dict) -> None:
        """yt-dlp 子进程的进度回报（格式见 ytdlp_bin._parse_progress）。"""
        if self._stop.is_set():
            return
        st = d.get("status")
        if st == "downloading":
            self.status = "downloading"
            done = int(d.get("downloaded") or 0)
            total = int(d.get("total") or 0)
            self._yt_bytes = self._yt_completed + done
            if total:
                self.size = self._yt_completed + total
            sp = d.get("speed") or 0
            if sp:
                self._yt_speed = float(sp)
            self._emit("progress")
        elif st == "finished":
            got = int(d.get("downloaded") or d.get("total") or 0)
            if got:
                self._yt_completed += got
                self._yt_bytes = max(self._yt_bytes, self._yt_completed)
            # 视频轨下完还要下音频 + ffmpeg 合流，别在这时报 100%
            self.status = "merging"
            self._emit("progress")
        elif st == "merging":
            self.status = "merging"
            self._emit("progress")

    def _yt_take_proc(self, proc) -> None:
        self._yt_proc = proc

    def _run_ytdlp(self) -> None:
        """YouTube 任务专用下载通道。

        为什么不复用下面的分块引擎：Google 从 2025 年起给 googlevideo 直链
        加了「顺序分段」限制。实测同一个直链（带 cookie 也一样）：
            Range: bytes=0-262143      → 206
            Range: bytes=524288-786431 → 403 Forbidden
            Range: bytes=0-（开放式）  → 403 Forbidden
        也就是说直链只认「从头开始的一小段」，后续分段要按 JS 挑战现算签名。
        自研引擎按 8 段并发去取，每个线程都撞 403 —— 这就是「YouTube 每次都
        下载失败/进度 0 卡死」的真正原因。yt-dlp 会解挑战、按段重取、下完
        自动合并音轨，所以整条任务交给它跑（用独立可执行文件，绕开 app 内嵌
        的 Python 3.9 —— 新版 yt-dlp 要求 3.10+）。
        """
        from . import yt_resolve, ytdlp_bin

        binp = ytdlp_bin.binary_path()
        if not binp:
            self.status = "error"
            self.error = "找不到 yt-dlp 可执行文件（app 的 bin/yt-dlp 缺失或不可执行）"
            self._emit("error")
            return

        stem = "zdm-" + self.id
        cookie = (self.headers or {}).get("Cookie") or ""
        cookie_path = None
        if cookie:
            try:
                # 域要按**页面**来铺，不是媒体直链的域：YouTube 的 cookie
                # 属于 .youtube.com，媒体却在 googlevideo.com 上。
                cookie_path = yt_resolve._cookiefile(
                    cookie, yt_resolve._host_of(self.yt_page or self.url or "")
                )
            except Exception:  # noqa: BLE001
                cookie_path = None
        # Cookie 已经写成 cookiefile 了，别再用 -add-header 重复塞一遍
        headers = {
            k: v for k, v in (self.headers or {}).items() if k.lower() != "cookie"
        }

        common = dict(
            fmt=yt_resolve.format_selector(self.yt_height),
            outtmpl=os.path.join(self.directory, stem + ".%(ext)s"),
            ffmpeg=_find_ffmpeg(),
            cookie_path=cookie_path,
            proxy=self.proxy,
            headers=headers,
            ratelimit=self.speed_limit,
            timeout=self.timeout,
            on_progress=self._yt_progress,
            should_stop=self._stop.is_set,
            on_proc=self._yt_take_proc,
        )
        # 客户端重试链（2026-10 用随包 yt-dlp 2026.08.19 实测各客户端）：
        #   default      → 唯一稳定给完整 144~2160 阶梯、且直链真能下载的客户端；
        #   tv_embedded  → 同一次实测里也拿到完整阶梯，可作为默认被拦时的替补；
        #   android_vr   → **已退化**成「只有 5 条格式、最高 360p，直链必 403」。
        #                  旧代码拿它当唯一替补，于是默认客户端一旦被 SABR 拦，
        #                  重试就把「可恢复的拦截」变成「必然失败」，而且 403
        #                  不在换客户端特征里，重试链当场 break —— 这就是
        #                  「有些机器上永远下不动」的根因之一。
        # YouTube 的拦截是**按请求**随机判的（同一台机器同一视频，解析能过、
        # 下载被拦，隔几分钟又自愈），所以两个客户端各试两次、中间隔 3 秒。
        attempts = [
            (None, 0.0),
            (("--extractor-args", "youtube:player_client=tv_embedded"), 0.0),
            (None, 3.0),
            (("--extractor-args", "youtube:player_client=tv_embedded"), 3.0),
        ]
        rc, err = 1, ""
        try:
            login_blocked = 0
            for extra_args, delay in attempts:
                if self._stop.is_set():
                    break
                if delay:
                    time.sleep(delay)
                    if self._stop.is_set():
                        break
                rc, err = ytdlp_bin.download(
                    binp, self.yt_page, extra=list(extra_args) if extra_args else None,
                    **common,
                )
                if rc == 0 and not err:
                    break
                # 只有「视频本身不可用」才值得跳过剩下的尝试；403/空格式
                # 这类可能下一次就恢复，必须把整条链走完。
                if _yt_permanent_error(err):
                    break
                # 两个不同客户端都被要求「登录确认不是机器人」→ 这是账号/
                # 网络层面的判定，换客户端不会变好。再跑完剩下的尝试只会让
                # 用户盯着 0% 干等一分多钟（Mac 实测 81 秒）。
                if _yt_login_required(err):
                    login_blocked += 1
                    if login_blocked >= 2:
                        break
            if rc != 0 and err and ("403" in err or _yt_should_swap_client(err)):
                err += "（已自动换 tv_embedded 客户端重试仍未成功；建议在浏览器登录 YouTube、或在扩展设置里填写 Cookie 后重试）"
        finally:
            self._yt_proc = None
            if cookie_path:
                try:
                    os.remove(cookie_path)
                except OSError:
                    pass

        if self.status == "canceled":
            self._yt_cleanup(stem)
            self._emit("canceled")
            return
        if self._stop.is_set():
            self._yt_cleanup(stem, keep_partial=True)
            self.status = "paused"
            self._emit("paused")
            return

        path = self._yt_output(stem)
        if (rc != 0 or err) or not path:
            self._yt_cleanup(stem)
            # 最后兜底：yt-dlp 的下载通道被 SABR 拦死（重试链全灭）时，
            # 解析通道往往还拿得到可用直链 —— 原地切换成内置引擎顺序
            # 下载直链（嗅探推送就是这条路，全平台实测可用）。
            # 以前这里还要求「最后一次错误必须命中换客户端特征」，于是重试
            # 客户端一旦报 403（它现在必 403），兜底就永远不执行：明明手里
            # 有能下的直链，任务还是直接失败。
            if not _yt_permanent_error(err or "") and self._yt_fallback_direct():
                return
            self.status = "error"
            msg = (err or f"yt-dlp 退出码 {rc}，没有产出文件").strip()
            # yt-dlp 的原文只有英文和 GitHub 链接，用户看不出该做什么。
            # 实测这个报错的唯一原因是「这条任务没带登录 cookie」——
            # 扩展里点「下载此视频」会自动带，手填链接或网页版则没有。
            if "not a bot" in msg or "Sign in to confirm" in msg:
                msg += (
                    "\n\n提示：YouTube 要求登录态才能下载。"
                    "请在浏览器里登录 YouTube，用扩展的「下载此视频」重试；"
                    "手填链接的任务不会带浏览器 cookie。"
                )
            msg += _yt_runtime_hint(binp, err or "")
            self.error = msg[:900]
            self._emit("error")
            return

        # 换成用户看得懂的文件名（扩展已经按标题算好了）
        want = str(self.filename or "").strip()
        if want.lower() in ("", "download"):
            # The Download default. Without this a CLI/web video-page task was
            # renamed to a file literally called "download" (no .mp4), which
            # players cannot open by double-click.
            want = os.path.basename(path)
        final = _unique_name(self.directory, want)
        target = os.path.join(self.directory, final)
        moved = None
        last: Optional[Exception] = None
        for attempt in (os.replace, shutil.move):
            try:
                attempt(path, target)
                moved = target
                break
            except OSError as e:
                moved = None
                last = e
        if not moved:
            self.status = "error"
            self.error = f"文件下完了但改名失败：{last}"
            self._emit("error")
            return
        self.filename = final
        try:
            self.size = os.path.getsize(target)
        except OSError:
            pass
        self._yt_bytes = self.size
        try:
            os.remove(self.meta_path)
        except OSError:
            pass
        self.status = "completed"
        self.completed_at = time.time()
        self.error = None
        self._emit("completed")

    def _yt_output(self, stem: str) -> Optional[str]:
        """找出 yt-dlp 真正产出的那个文件（合并后是 stem.mp4）。"""
        import glob

        cands = []
        for p in glob.glob(os.path.join(self.directory, stem + ".*")):
            if not os.path.isfile(p):
                continue
            if p.endswith((".part", ".ytdl", ".temp", ".tmp")):
                continue
            cands.append(p)
        if not cands:
            return None
        # 多轨时选最大的那个（视频轨），音频轨通常小一个量级
        return max(cands, key=os.path.getsize)

    def _yt_cleanup(self, stem: str, keep_partial: bool = False) -> None:
        """清掉 yt-dlp 的临时文件。暂停时保留 .part，方便续传。"""
        import glob

        for p in glob.glob(os.path.join(self.directory, stem + ".*")):
            base = os.path.basename(p)
            if keep_partial and (base.endswith(".part") or base.endswith(".ytdl")):
                continue
            try:
                os.remove(p)
            except OSError:
                pass

    def _run_segmented(self) -> None:
        threads = []
        for i in range(self.connections):
            t = threading.Thread(target=self._worker, args=(i,), daemon=True)
            t.start()
            threads.append(t)
        self._threads = threads

    def _run_single(self) -> None:
        self._close_fd()
        t = threading.Thread(target=self._worker_single, daemon=True)
        t.start()
        self._threads = [t]

    def _worker(self, wid: int) -> None:
        assert self.segmenter is not None
        while not self._stop.is_set():
            chunk = self.segmenter.get_work(wid)
            if chunk is None:
                return
            try:
                if self._download_range(chunk):
                    self.segmenter.done(chunk)
                else:
                    # interrupted by pause/cancel: keep the real progress
                    self.segmenter.release(chunk)
                    return
            except Exception as e:  # noqa: BLE001
                self.segmenter.release(chunk)
                chunk.attempts += 1
                if self._stop.is_set():
                    return
                if chunk.attempts > self.max_retries:
                    self.status = "error"
                    self.error = str(e)
                    return
                time.sleep(min(2 ** chunk.attempts, 10))

    def _download_range(self, chunk: Chunk) -> bool:
        """Fill one segment; returns True only when the whole range was written."""
        url = self.final_url or self.url
        offset = chunk.pos
        while offset < chunk.end and not self._stop.is_set():
            gen = self._transport.stream(
                url, offset, chunk.end, self._http_headers(), self.timeout, self.proxy
            )
            try:
                for buf in gen:
                    if self._stop.is_set():
                        return False
                    # The segment may have been split while this response was in
                    # flight, so never write past the (possibly shrunk) boundary.
                    room = chunk.end - offset
                    if room <= 0:
                        break
                    if len(buf) > room:
                        buf = buf[:room]
                    if self.global_limiter:
                        self.global_limiter.consume(len(buf))
                    self.limiter.consume(len(buf))
                    written = _write_at(self._fd, self._tls, self.part_path, buf, offset)
                    offset += written
                    chunk.pos = min(offset, chunk.end)
                    self.meter.add(written)
            finally:
                try:
                    gen.close()
                except Exception:
                    pass
                self._close_tls()
        return chunk.pos >= chunk.end

    def _worker_single(self) -> None:
        """Stream download for servers without Range support / unknown size.

        不支持 Range 就没法断点续传：每次开始都必须从头写，所以这里用
        "wb" 截断旧的 .part 是正确的。暂停时**不能**把 size 改成已下字节数
        或伪造一个「已完成」的 Segmenter —— 那会让恢复时把旧进度当成总长，
        下载完整文件后反而报 "incomplete body"，进度也会显示成 100% 假死。
        """
        url = self.final_url or self.url
        offset = 0
        expected = self.size
        try:
            with open(self.part_path, "wb") as f:
                for buf in self._transport.stream(
                    url, 0, None, self._http_headers(), self.timeout, self.proxy
                ):
                    if self._stop.is_set():
                        return
                    if self.global_limiter:
                        self.global_limiter.consume(len(buf))
                    self.limiter.consume(len(buf))
                    f.write(buf)
                    offset += len(buf)
                    self.meter.add(len(buf))
            if expected and offset != expected:
                raise ConnectionError(f"incomplete body: {offset}/{expected} bytes")
            if offset == 0:
                raise ConnectionError("empty response body")
            self.size = offset
            self.segmenter = Segmenter(offset, [Chunk(0, offset, offset)])
        except Exception as e:  # noqa: BLE001
            if not self._stop.is_set():
                self.status = "error"
                self.error = str(e)

    def _finish(self) -> None:
        self.status = "merging"
        self._emit("progress")
        self._close_fd()
        if self.size:
            try:
                actual = os.path.getsize(self.part_path)
            except OSError:
                actual = -1
            if actual != self.size:
                self.status = "error"
                self.error = f"size mismatch: got {actual}, expected {self.size}"
                self._emit("error")
                return
        target = self.target_path
        err = self._move_into_place(target)
        if err:
            self.status = "error"
            self.error = err
            self._emit("error")
            return
        try:
            os.remove(self.meta_path)
        except OSError:
            pass
        # A DASH video track has no sound until the audio track is muxed in.
        # 嗅探路径只拿得到画面轨。B站可以用 URL 里的 cid + Referer 里的 BV
        # 号反查一条音轨，先试着补齐，补到了就照常合流。
        if not self.audio_url and self.kind == "video":
            self.audio_url = self._bili_audio_url() or ""
        if self.audio_url:
            if not self._mux_audio():
                # 视频本身是好的，只是没有声音：保留文件，但把失败的
                # **具体一步**报出来（下载 / ffmpeg / 复查），否则用户
                # 只能看到「合并失败」这种毫无信息量的结论。
                self.error = "视频已保存但无声音 —— " + (
                    self.mux_error or "音视频合并失败"
                )
        elif self.kind == "video":
            # 调用方没配音频轨。DASH 视频轨天生无声 —— 以前这里静默当成功，
            # 用户只看到「下载完成」却听不到声音，完全无从判断。
            has_audio = _probe_has_audio(target)
            if has_audio is False:
                self.error = (
                    "只下到了画面、没有音轨（这条直链是纯视频轨，"
                    "没配上配套音频）。B站可点「下载此视频」重下；"
                    "嗅探请等列表里出现音频条目后再推送。"
                )
        self.status = "completed"
        self.completed_at = time.time()
        self._emit("completed")

    def _move_into_place(self, target: str) -> Optional[str]:
        """把下完的 .part 落成正式文件，返回 None 表示成功。

        三个台阶（实测用户机器上 os.replace 会报 [Errno 1] Operation not
        permitted —— 同目录改名被拒，典型原因是同名旧文件正被播放器占用/
        加锁，或被杀毒/iCloud 云同步瞬时锁住。整个 100% 下载白跑是不可
        接受的，所以按下面的顺序兜底）：
          1. 目标已存在就先换成唯一名（绝不覆盖用户已有的文件）；
          2. 原子改名，对瞬时锁（EPERM/EBUSY/ETXTBSY）重试 3 次；
          3. copy2 + 删 .part —— 改名被永久拒绝时也能保住已下载的字节。
        """
        # 1) 目标已存在：换一个 "(1)" "(2)" 的名字，不覆盖旧文件
        #    （旧文件可能正是上次下载的同名视频，正被播放器占用）。
        if os.path.lexists(target):
            stem, dot, ext = self.filename.rpartition(".")
            if not dot:
                stem, ext = self.filename, ""
            for i in range(1, 1000):
                cand = f"{stem}({i})" + (("." + ext) if ext else "")
                p = os.path.join(self.directory, cand)
                if not os.path.lexists(p):
                    target = p
                    break

        # 2) 原子改名，瞬时锁重试
        last: Optional[OSError] = None
        for attempt in range(3):
            try:
                os.replace(self.part_path, target)
                self.filename = os.path.basename(target)
                return None
            except OSError as e:
                last = e
                if e.errno not in (
                    errno.EPERM, errno.EACCES, errno.EBUSY,
                    errno.ETXTBSY, errno.EAGAIN,
                ):
                    break  # 换 copy 兜底也没意义（比如路径本身坏了）
                time.sleep(0.5 * (attempt + 1))

        # 3) copy 兜底（跨卷/被锁都能走）
        try:
            shutil.copy2(self.part_path, target)
            try:
                os.remove(self.part_path)
            except OSError:
                pass
            self.filename = os.path.basename(target)
            return None
        except OSError as e:
            return f"cannot save file: {last or e}（文件已在 {self.directory} 保留为 {os.path.basename(self.part_path)}）"

    def _bili_audio_url(self) -> str:
        """B站：从已下载的视频直链反查配套音频直链。

        嗅探面板只能给出画面轨（音频是另一次请求，可能早于扩展注入或被
        浏览器合并）。这时扩展侧配不到音频，但 URL 路径里有 cid
        （/upgcxcode/99/91/<cid>/），Referer 里有 BV 号 —— 用这两个就能
        调 playurl 自取一条音轨，不需要登录态。
        """
        import re as _re

        url = self.url or ""
        if not _re.search(r"/upgcxcode/|bilivideo\.(com|cn)", url, _re.I):
            return ""
        referer = ""
        for k, v in (self.headers or {}).items():
            if k.lower() == "referer":
                referer = v or ""
                break
        bv = ""
        for src in (referer, url):
            m = _re.search(r"(BV[0-9A-Za-z]{10})", src or "")
            if m:
                bv = m.group(1)
                break
        m = _re.search(r"/upgcxcode/\d+/\d+/(\d+)/", url)
        cid = m.group(1) if m else ""
        if not bv or not cid:
            return ""

        api_headers = {
            "User-Agent": DEFAULT_UA,
            "Referer": "https://www.bilibili.com/",
            "Origin": "https://www.bilibili.com",
        }
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler(
                {"http": self.proxy, "https": self.proxy} if self.proxy else {}
            )
        )

        def get_json(u: str):
            req = urllib.request.Request(u, headers=api_headers)
            with opener.open(req, timeout=min(self.timeout or 10, 10)) as resp:
                return json.loads(resp.read().decode("utf-8", "ignore"))

        try:
            view = get_json(
                "https://api.bilibili.com/x/web-interface/view?bvid=" + bv
            )
            aid = (view.get("data") or {}).get("aid")
            if not aid:
                return ""
            pu = get_json(
                "https://api.bilibili.com/x/player/playurl"
                f"?fnval=4048&fnver=0&fourk=1&avid={aid}&cid={cid}"
            )
            dash = ((pu.get("data") or {}).get("dash")) or {}
            audios = [a for a in (dash.get("audio") or []) if a.get("baseUrl")]
            if not audios:
                return ""
            audios.sort(key=lambda a: a.get("bandwidth") or 0, reverse=True)
            return audios[0]["baseUrl"]
        except Exception:
            return ""

    def _mux_audio(self) -> bool:
        """Download the audio track and mux it into the video with ffmpeg.

        Returns True on success. On failure the video file is left untouched
        (silent but playable) and ``self.mux_error`` records *which* step
        broke — 单纯一句「合并失败」在音轨 CDN 403 时毫无用处。
        """
        import subprocess
        import tempfile

        self.mux_error = ""
        ffmpeg = _find_ffmpeg()
        if not ffmpeg:
            self.mux_error = "找不到 ffmpeg，无法把音轨合进视频"
            return False
        video = self.target_path
        if not os.path.exists(video):
            self.mux_error = "视频文件不在预期位置"
            return False

        headers = dict(self.headers or {})
        # CDN 对 Referer 极敏感（B站 bilivideo / YouTube googlevideo 缺了就
        # 403）。没带就按站点补一个 —— 否则音轨必然下不下来。
        # B站 / YouTube 有固定主页可写；其它站点（抖音 / 快手 / Facebook /
        # X / 各种新闻站）用「视频轨自己的来源页」当 Referer —— 音轨和页面
        # 同源时这一定是服务器认得的那个值，比留空好。
        if not any(k.lower() == "referer" for k in headers):
            host = (urllib.parse.urlparse(self.audio_url).hostname or "").lower()
            if "bilivideo" in host or "bilibili" in host:
                headers["Referer"] = "https://www.bilibili.com/"
            elif "googlevideo" in host or "youtube" in host:
                headers["Referer"] = "https://www.youtube.com/"
            else:
                # 其它站点：用任务自带页面地址的 origin。页面地址在
                # yt_page 里（粘贴链接解析进来的任务都带），音轨 CDN 与
                # 它同源时这个 Referer 一定被服务器认。
                page = self.yt_page or ""
                if page:
                    try:
                        p = urllib.parse.urlparse(page)
                        if p.scheme and p.netloc:
                            headers["Referer"] = "%s://%s/" % (p.scheme, p.netloc)
                    except Exception:
                        pass
        if not any(k.lower() == "user-agent" for k in headers):
            headers["User-Agent"] = DEFAULT_UA

        tmpdir = tempfile.mkdtemp(prefix="zdmmux")
        audio_path = os.path.join(tmpdir, "audio.bin")
        out_path = video + ".merged.mp4"
        audio_is_pl = bool(_PLAYLIST_URL_RE.search(self.audio_url))
        try:
            audio_input: list = []
            if audio_is_pl:
                # 音频轨本身是 m3u8/mpd：下载到的是播放列表文本，交给
                # ffmpeg 只会报「Invalid data」。让 ffmpeg 直接跟着列表拉。
                hdr_blob = "".join(
                    "%s: %s\r\n" % (k, v) for k, v in headers.items()
                )
                if hdr_blob:
                    audio_input += ["-headers", hdr_blob]
                if self.proxy:
                    audio_input += ["-http_proxy", self.proxy]
                audio_input += ["-i", self.audio_url]
            else:
                # 1) 音轨下载。Honour the task proxy (googlevideo 需要) but
                #    never the sandbox's own proxy settings.
                proxy = self.proxy
                if proxy:
                    handler = urllib.request.ProxyHandler(
                        {"http": proxy, "https": proxy}
                    )
                else:
                    handler = urllib.request.ProxyHandler({})
                try:
                    req = urllib.request.Request(self.audio_url, headers=headers)
                    opener = urllib.request.build_opener(handler)
                    with opener.open(req, timeout=self.timeout) as resp, open(
                        audio_path, "wb"
                    ) as fh:
                        shutil.copyfileobj(resp, fh)
                except Exception as e:
                    self.mux_error = f"音轨下载失败：{e}"
                    return False
                try:
                    if not os.path.getsize(audio_path):
                        self.mux_error = "音轨下载到 0 字节（直链可能已过期）"
                        return False
                except OSError:
                    self.mux_error = "音轨文件写入失败"
                    return False
                audio_input = ["-i", audio_path]

            # 2) 合并
            cmd = [
                ffmpeg, "-y", "-loglevel", "error",
                "-i", video,
                *audio_input,
                "-c", "copy", "-map", "0:v:0", "-map", "1:a:0",
                out_path,
            ]
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, timeout=600,
                    env={**os.environ, "PATH": os.environ.get("PATH", "")},
                    **_no_window(),
                )
            except Exception as e:
                self.mux_error = f"ffmpeg 执行失败：{e}"
                return False
            if proc.returncode != 0 or not os.path.exists(out_path):
                raw = (proc.stderr or b"").decode("utf-8", "ignore").strip()
                tail = raw.splitlines()[-1] if raw else f"返回码 {proc.returncode}"
                self.mux_error = f"ffmpeg 合并失败：{tail}"
                return False

            # 3) 落到正式文件名（目标可能正被播放器占用 → EPERM）
            try:
                os.replace(out_path, video)
            except OSError:
                try:
                    os.remove(video)
                    os.replace(out_path, video)
                except OSError:
                    shutil.copy2(out_path, video)
            try:
                self.size = os.path.getsize(video)
            except OSError:
                pass

            # 4) 复查：音轨真的进去了吗（ffmpeg 返回 0 也可能没合上）
            if _probe_has_audio(video) is False:
                self.mux_error = "合并完成但文件里仍没有音轨"
                return False
            return True
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
            # ffmpeg 失败/复查失败时 out_path 会留下来，变成视频旁边一个
            # 用户看不懂的 xxx.mp4.merged.mp4；成功时 os.replace 已经搬走。
            try:
                if os.path.exists(out_path):
                    os.remove(out_path)
            except OSError:
                pass

    def _close_tls(self) -> None:
        """Close the per-thread handle used on platforms without os.pwrite."""
        h = getattr(self._tls, "fd", None)
        if h is not None:
            try:
                os.close(h)
            except OSError:
                pass
            self._tls.fd = None

    def _close_fd(self) -> None:
        if self._fd is not None:
            try:
                os.fsync(self._fd)
            except OSError:
                pass
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    def _join_workers(self, timeout: float = 15.0) -> bool:
        """Wait for connection threads to exit; resume must never race them."""
        alive = [t for t in self._threads if t.is_alive()]
        deadline = time.time() + timeout
        for t in alive:
            remain = max(0.1, deadline - time.time())
            t.join(timeout=remain)
        self._threads = [t for t in self._threads if t.is_alive()]
        return not self._threads

    def pause(self) -> None:
        if self.status in ("downloading", "queued"):
            # 先记意图再停线程：调度器可能正在把 queued 改成 downloading，
            # 单靠 _stop 会被 start() 清掉。
            self._pause_requested.set()
            self._stop.set()
            self._kill_yt_proc()
            # 流媒体任务是 ffmpeg 子进程在写文件，不杀掉它暂停要等整条流下完。
            self._kill_stream_proc()
            self.status = "paused"
            self._join_workers()

    def resume(self) -> None:
        if self.status in ("paused", "error", "canceled"):
            self._join_workers()
            self._pause_requested.clear()
            self.error = None
            self.status = "queued"

    def cancel(self, remove_files: bool = True) -> None:
        self._pause_requested.clear()
        self._stop.set()
        self._kill_yt_proc()
        self._kill_stream_proc()
        self.status = "canceled"
        self._close_fd()
        if remove_files:
            for p in (self.part_path, self.meta_path):
                try:
                    os.remove(p)
                except OSError:
                    pass
        self._emit("canceled")

    def is_active(self) -> bool:
        return self.status == "downloading"

    def _kill_yt_proc(self) -> None:
        """停掉 yt-dlp 子进程。不 terminate 的话「暂停」要等它下完才生效。"""
        proc = self._yt_proc
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
        except Exception:  # noqa: BLE001
            pass

    def _kill_stream_proc(self) -> None:
        """停掉 ffmpeg 子进程（流媒体任务）。"""
        proc = getattr(self, "_stream_proc", None)
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except Exception:  # noqa: BLE001
                    proc.kill()
        except Exception:  # noqa: BLE001
            pass


def _log_download_error(d) -> None:
    """把失败原因落盘（配置目录下的 download-errors.log）。

    GUI 的错误只在弹窗里显示一次，用户一关就没了；而「下载失败」是必须
    拿到原文才能定位的问题（yt-dlp 的报错、403、磁盘错误……），尤其是
    Windows/Linux 用户没法像 mac 那样直接翻控制台。日志按大小轮转，
    不会无限增长。

    错误日志写不进去（权限/磁盘满）绝不能反过来影响下载本身，所以整段
    都吞异常。
    """
    import json

    try:
        from .manager import config_dir

        path = os.path.join(config_dir(), "download-errors.log")
        try:
            if os.path.getsize(path) > 256 * 1024:
                os.replace(path, path + ".1")
        except OSError:
            pass
        rec = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "id": d.id,
            "status": d.status,
            "url": (d.url or "")[:500],
            "yt_page": d.yt_page,
            "yt_height": d.yt_height,
            "kind": d.kind,
            "error": (d.error or "")[:2000],
            "mux_error": (d.mux_error or "")[:400],
            "directory": d.directory,
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 - 诊断日志失败不影响下载
        pass


def _unique_name(directory: str, filename: str) -> str:
    base = filename
    path = os.path.join(directory, base)
    if not os.path.exists(path) and not os.path.exists(path + ".part"):
        return base
    stem, dot, ext = base.rpartition(".")
    if not dot:
        stem, ext = base, ""
    for i in range(1, 1000):
        cand = f"{stem}({i})" + (("." + ext) if ext else "")
        p = os.path.join(directory, cand)
        if not os.path.exists(p) and not os.path.exists(p + ".part"):
            return cand
    return f"{stem}-{int(time.time())}" + (("." + ext) if ext else "")
