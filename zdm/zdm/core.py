"""Multi-connection segmented download engine (dependency free)."""

from __future__ import annotations

import errno
import json
import os
import shutil
import threading
import time
import urllib.request
from typing import Callable, Dict, List, Optional, Tuple

from . import transport as transport_mod
from .http import HttpError, filename_from_headers

MIN_SPLIT = 1 << 20  # 1 MiB: minimum remaining size to justify splitting
HAS_PWRITE = hasattr(os, "pwrite")
O_BINARY = getattr(os, "O_BINARY", 0)  # Windows: without it files open in text mode


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
SOURCES = ("manual", "browser", "sniffer", "webui", "protocol")
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
    ):
        self.id = id
        self.url = url
        self.final_url = url
        self.directory = directory
        self.filename = filename or "download"
        self.connections = max(1, int(connections))
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
            "downloaded": self.downloaded,
            "speed": round(self.meter.speed()),
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
            "chunks": self.segmenter.snapshot() if self.segmenter else [],
        }

    def load_meta(self, data: dict) -> None:
        self.url = data.get("url", self.url)
        self.final_url = data.get("final_url", self.url)
        self.filename = data.get("filename", self.filename)
        self.size = int(data.get("size", 0) or 0)
        self.connections = int(data.get("connections", self.connections) or 1)
        self.supports_range = bool(data.get("supports_range"))
        self.transport_name = data.get("transport") or "native"
        self._transport = transport_mod.make(self.transport_name)
        self.headers = data.get("headers") or {}
        self.proxy = data.get("proxy")
        self.speed_limit = int(data.get("speed_limit", 0) or 0)
        self.audio_url = str(data.get("audio_url") or "")
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
        if self.segmenter:
            return self.segmenter.downloaded()
        return self.meter.total

    @property
    def progress(self) -> float:
        if not self.size:
            return 0.0
        return round(self.downloaded / self.size * 100, 2)

    @property
    def eta(self) -> Optional[int]:
        sp = self.meter.speed()
        if not sp or not self.size:
            return None
        return int((self.size - self.downloaded) / sp)

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
        if self.audio_url and not self._mux_audio():
            # The video itself is fine; only the audio is missing. Report it
            # instead of throwing the whole file away.
            self.error = "音频轨合并失败（缺少 ffmpeg 或音频下载失败），视频已保存但无声音"
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

    def _mux_audio(self) -> bool:
        """Download the audio track and mux it into the video with ffmpeg.

        Returns True on success. On failure the video file is left untouched
        (silent but playable) and the caller reports it.
        """
        import shutil
        import subprocess
        import tempfile

        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return False
        video = self.target_path
        if not os.path.exists(video):
            return False
        tmpdir = tempfile.mkdtemp(prefix="zdmmux")
        audio_path = os.path.join(tmpdir, "audio.bin")
        try:
            # The audio track is a small file; a plain GET is enough.
            req = urllib.request.Request(self.audio_url, headers=self.headers or {})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=self.timeout) as resp, open(audio_path, "wb") as fh:
                shutil.copyfileobj(resp, fh)
            out_path = video + ".merged.mp4"
            cmd = [
                ffmpeg, "-y", "-loglevel", "error",
                "-i", video,
                "-i", audio_path,
                "-c", "copy", "-map", "0:v:0", "-map", "1:a:0",
                out_path,
            ]
            proc = subprocess.run(
                cmd, capture_output=True, timeout=600,
                env={**os.environ, "PATH": os.environ.get("PATH", "")},
            )
            if proc.returncode != 0 or not os.path.exists(out_path):
                return False
            os.replace(out_path, video)
            return True
        except Exception:
            return False
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
