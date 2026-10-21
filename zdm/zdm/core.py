"""Multi-connection segmented download engine (dependency free)."""

from __future__ import annotations

import errno
import json
import os
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


def _yt_should_swap_client(err: str) -> bool:
    """下载失败的报错是否值得换 player_client 再试一次。

    只认实测特征：SABR 拦截（"page needs to be reloaded"）、人机验证
    （"Sign in to confirm" / "not a bot"），以及 SABR 只回分段不给格式时
    的 "Requested format is not available"（此时默认客户端的格式列表是
    空的，任何 -f 表达式都落空，换 android_vr 才有完整阶梯 —— 第二台
    Windows 与 Linux 实测）。其它错误（403 音轨 CDN、磁盘满等）换客户端
    没用，不重试。"""
    if not err:
        return False
    e = err.lower()
    return (
        "page needs to be reloaded" in e
        or "sign in to confirm" in e
        or "not a bot" in e
        or "requested format is not available" in e
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
KINDS = ("", "video", "audio")


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
        self._pause = threading.Event()
        self._threads: List[threading.Thread] = []
        self._fd: Optional[int] = None
        self._on_event: Optional[Callable[[str, "Download"], None]] = None
        self._lock = threading.Lock()

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
            "speed": round(self._yt_speed if self.yt_page else self.meter.speed()),
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
        # 合流/完成阶段：字节已经全部到手，百分比只能是 100。
        if self.status in ("merging", "completed"):
            return 100.0
        if not self.size:
            return 0.0
        return min(100.0, round(self.downloaded / self.size * 100, 2))

    @property
    def eta(self) -> Optional[int]:
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
                    url, self.headers, self.timeout, self.proxy
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
            self.segmenter = Segmenter(total)

        elif self.size and not os.path.exists(self.part_path):
            # metadata survived but the partial file is gone
            self.segmenter = Segmenter(self.size)

        if self.size and self.supports_range:
            self.segmenter.balance(self.connections)

        fd = os.open(self.part_path, os.O_RDWR | os.O_CREAT | O_BINARY, 0o644)
        self._fd = fd
        if self.size:
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
        if self.yt_page:
            self._start_ytdlp()
            return
        try:
            self._prepare()
        except Exception as e:
            self.status = "error"
            self.error = str(e)
            self._emit("error")
            return

        self.status = "downloading"
        if self.started_at is None:
            self.started_at = time.time()
        self._stop.clear()
        self._pause.clear()
        self.meter.reset()
        self._emit("started")

        if self.size and self.supports_range:
            self._run_segmented()
        else:
            self._run_single()

        while True:
            if self._stop.is_set():
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
    def _start_ytdlp(self) -> None:
        self.status = "downloading"
        if self.started_at is None:
            self.started_at = time.time()
        self._stop.clear()
        self._pause.clear()
        self._emit("started")
        self._run_ytdlp()

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
                cookie_path = yt_resolve._cookiefile(cookie)
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
        # 客户端重试链：YouTube 的 SABR 拦截/空格式列表是**按请求**随机判的
        # （同一台机器同一视频，解析能过、下载被拦，隔几分钟又自愈）。
        # 所以默认与 android_vr 各试两次、中间隔 3 秒 —— android_vr 是
        # 2026-10 实测能完整下载的客户端（见 yt_resolve._STRATEGIES 注释）。
        # 命中特征错误才继续换；其它错误（403 音轨 CDN、磁盘满等）立即放弃。
        attempts = [
            (None, 0.0),
            (("--extractor-args", "youtube:player_client=android_vr"), 0.0),
            (None, 3.0),
            (("--extractor-args", "youtube:player_client=android_vr"), 3.0),
        ]
        rc, err = 1, ""
        try:
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
                if not _yt_should_swap_client(err):
                    break
            if rc != 0 and err and _yt_should_swap_client(err):
                err += "（已自动用 android_vr 客户端重试仍未成功；建议在浏览器登录 YouTube、或在扩展设置里填写 Cookie 后重试）"
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
            self.error = msg[:600]
            self._emit("error")
            return

        # 换成用户看得懂的文件名（扩展已经按标题算好了）
        final = _unique_name(self.directory, self.filename or os.path.basename(path))
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
                url, offset, chunk.end, self.headers, self.timeout, self.proxy
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
        """Stream download for servers without Range support / unknown size."""
        url = self.final_url or self.url
        offset = 0
        expected = self.size
        try:
            with open(self.part_path, "wb") as f:
                for buf in self._transport.stream(
                    url, 0, None, self.headers, self.timeout, self.proxy
                ):
                    if self._stop.is_set():
                        self.size = offset
                        self.segmenter = Segmenter(0, [Chunk(0, offset, offset)])
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
        if not any(k.lower() == "referer" for k in headers):
            host = (urllib.parse.urlparse(self.audio_url).hostname or "").lower()
            if "bilivideo" in host or "bilibili" in host:
                headers["Referer"] = "https://www.bilibili.com/"
            elif "googlevideo" in host or "youtube" in host:
                headers["Referer"] = "https://www.youtube.com/"
        if not any(k.lower() == "user-agent" for k in headers):
            headers["User-Agent"] = DEFAULT_UA

        tmpdir = tempfile.mkdtemp(prefix="zdmmux")
        audio_path = os.path.join(tmpdir, "audio.bin")
        out_path = video + ".merged.mp4"
        try:
            # 1) 音轨下载。Honour the task proxy (googlevideo 需要) but never
            #    the sandbox's own proxy settings.
            proxy = self.proxy
            if proxy:
                handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            else:
                handler = urllib.request.ProxyHandler({})
            try:
                req = urllib.request.Request(self.audio_url, headers=headers)
                opener = urllib.request.build_opener(handler)
                with opener.open(req, timeout=self.timeout) as resp, open(audio_path, "wb") as fh:
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

            # 2) 合并
            cmd = [
                ffmpeg, "-y", "-loglevel", "error",
                "-i", video,
                "-i", audio_path,
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
            self._stop.set()
            self._kill_yt_proc()
            if self.status == "downloading":
                self.status = "paused"
            self._join_workers()

    def resume(self) -> None:
        if self.status in ("paused", "error", "canceled"):
            self._join_workers()
            self.error = None
            self.status = "queued"

    def cancel(self, remove_files: bool = True) -> None:
        self._stop.set()
        self._kill_yt_proc()
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
