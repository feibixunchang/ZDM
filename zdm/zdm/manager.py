"""Download queue manager: scheduling, persistence and event broadcasting."""

from __future__ import annotations

import json
import os
import queue
import sys
import tempfile
import threading
import time
import uuid
from typing import Dict, List, Optional

from .core import (
    MAX_CONNECTIONS,
    MAX_PRIORITY,
    Chunk,
    Download,
    RateLimiter,
    Segmenter,
    normalize_section,
    record_float,
    record_int,
)
from .videopage import is_video_page

APP_NAME = "zdm"
DEFAULT_PORT = 9000

# 命令行/JSON 里写下来的「假」：`zdm config mirror_accel off` 必须是关掉。
# 直接用 bool() 会把 "off"/"no" 这类非空字符串当成 True，等于反着来。
FALSY_TEXT = {"0", "false", "no", "off", ""}
TRUTHY_TEXT = {"1", "true", "yes", "on"}


def to_bool(value, name: str = "配置项") -> bool:
    if isinstance(value, str):
        text = value.strip().lower()
        if text in FALSY_TEXT:
            return False
        if text in TRUTHY_TEXT:
            return True
        raise ValueError(f"{name} 需要 true/false（收到 {value!r}）")
    return bool(value)


# 优先级只在排序里用（-priority, created_at），但网页版/桌面端/命令行都按
# ±99 约束（上限见 core.MAX_PRIORITY，恢复老记录时用的是同一个数）；服务端
# 兜住同一个区间，扩展或 curl 传 999999 时才不至于把名字列撑坏。


def to_int(value, name: str, default: int = 0) -> int:
    """数字型参数统一在这里换算：裸 int() 抛的是「int() with base 10: 'abc'」，
    用户看不出是哪一个字段写错了。"""
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{name}需要整数（收到 {value!r}）") from e


def to_priority(value, name: str = "优先级") -> int:
    return max(-MAX_PRIORITY, min(MAX_PRIORITY, to_int(value, name, 0)))


def to_connections(value, default: int, name: str = "连接数") -> int:
    """任务级连接数：0/空 = 用设置里的默认，其余夹进 1~MAX_CONNECTIONS。"""
    if value in (None, "", 0, "0"):
        num = int(default or DEFAULT_CONFIG["connections"])
    else:
        num = to_int(value, name)
        if num < 1:
            raise ValueError(f"{name}至少 1（收到 {value!r}）")
    # 上限必须夹：_run_segmented 按连接数直接开线程，几千个会把机器拖死。
    return max(1, min(MAX_CONNECTIONS, num))


IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"


def config_dir() -> str:
    """Per-user config directory for the current platform."""
    home = os.path.expanduser("~")
    if IS_WIN:
        base = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
        path = os.path.join(base, "ZDM")
    elif IS_MAC:
        path = os.path.join(home, "Library", "Application Support", "ZDM")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
        path = os.path.join(base, APP_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def default_download_dir() -> str:
    home = os.path.expanduser("~")
    for cand in (os.path.join(home, "Downloads"), os.path.join(home, "下载")):
        if os.path.isdir(cand):
            return cand
    return os.path.join(home, "Downloads")


DEFAULT_CONFIG: Dict[str, object] = {
    "download_dir": default_download_dir(),
    "connections": 8,
    "max_active": 3,
    "speed_limit": 0,  # bytes/s, 0 = unlimited
    "proxy": "",
    "max_retries": 5,
    "timeout": 30,
    "port": DEFAULT_PORT,
    "auto_resume": True,
    "intercept_browser": False,
    "history_limit": 200,
    # Bring the desktop window up when a download is created while it is closed.
    "auto_show_gui": True,
    # Look for a newer release on startup (GitHub Releases).
    "check_updates": True,
    # Watch the system clipboard for http(s) links and offer them in the add
    # bar. Off by default: reading the clipboard of every app is a real
    # privacy trade-off and should be an explicit opt-in.
    "clipboard_monitor": False,
    # Rewrite GitHub / Hugging Face links through a public mirror. Off by
    # default; the original URL is kept so failures fall back to direct.
    "mirror_accel": False,
    # Cookies for sites that need a logged-in session (YouTube / Bilibili).
    # Shape: {"youtube": "SID=...", "bilibili": "SESSDATA=..."}
    # The browser extension normally sends the page's own cookie along with
    # each task; these are the fallback for tasks started from the desktop app
    # or for servers that only accept a first-party session.
    "site_cookies": {},
}


# 配置项的类型表与合法区间。写入（set_config 报错）和读取（heal_config 兜底）
# 必须共用同一份数字，否则旧版/手改的 state.json 会在重启后原样生效。
CONFIG_BOOL_KEYS = (
    "auto_resume",
    "clipboard_monitor",
    "mirror_accel",
    "intercept_browser",
    "auto_show_gui",
    "check_updates",
)
CONFIG_INT_KEYS = (
    "connections", "max_active", "max_retries", "speed_limit", "port",
    "history_limit",
)
CONFIG_FLOAT_KEYS = ("timeout",)
# 数值项的合法区间（None = 不设上限）。网页版的输入框留空时
# Number('') === 0，以前会连着存进去：连接数 0 让下载悄悄退化成单线程，
# max_active 0 会把队列卡死，端口 0 则服务根本起不来。
CONFIG_RANGES = {
    "connections": (1, MAX_CONNECTIONS),
    "max_active": (1, 32),
    "max_retries": (0, 50),
    "speed_limit": (0, None),
    "port": (1, 65535),
    "history_limit": (1, 100000),
    "timeout": (1, 3600),
}


def clamp_config_number(k: str, num):
    rng = CONFIG_RANGES.get(k)
    if rng is None:
        return num
    low, high = rng
    if num < low:
        return low
    if high is not None and num > high:
        return high
    return num


def heal_config(cfg: Dict[str, object]) -> Dict[str, object]:
    """把已落盘的配置拉回合法取值：类型写坏退回默认值，数字越界就夹到边界。

    set_config 只保证「新写入」的值合法；老版本存下的越界值（max_active=-5
    会让队列永远起不来，500 会一口气开几百个下载线程；port=0 让服务起不来、
    客户端却还在敲 9000）只能在这条唯一的读取路径上兜住。启动时能跑起来比
    报错更值钱，所以这里不抛异常。
    """
    healed = dict(cfg)
    for k in CONFIG_BOOL_KEYS:
        # null / 缺字段表示「这一项没给」，要退回默认值：to_bool(None) 会是
        # False，等于悄悄把「重启后自动继续」「自动检查更新」关掉。
        if healed.get(k) is None:
            healed[k] = DEFAULT_CONFIG[k]
            continue
        try:
            healed[k] = to_bool(healed[k], k)
        except ValueError:
            healed[k] = DEFAULT_CONFIG[k]
    for k in CONFIG_INT_KEYS + CONFIG_FLOAT_KEYS:
        cast = int if k in CONFIG_INT_KEYS else float
        try:
            num = cast(healed.get(k))
        except (TypeError, ValueError):
            healed[k] = DEFAULT_CONFIG[k]
            continue
        clamped = cast(clamp_config_number(k, num))
        # 端口越界不夹到边界：0/99999 说明这份配置本身是坏的，夹到 1 只会让
        # 服务绑在一个谁都找不到的端口上；退回默认端口才是各端客户端都会试的地址。
        healed[k] = DEFAULT_CONFIG[k] if k == "port" and clamped != num else clamped
    text = str(healed.get("download_dir") or "").strip()
    healed["download_dir"] = text or DEFAULT_CONFIG["download_dir"]
    if not isinstance(healed.get("site_cookies"), dict):
        healed["site_cookies"] = {}
    healed["proxy"] = str(healed.get("proxy") or "")
    return healed


def load_config(state_file: Optional[str] = None) -> Dict[str, object]:
    """Read persisted config only — never starts schedulers or loads tasks."""
    cfg: Dict[str, object] = dict(DEFAULT_CONFIG)
    path = state_file or os.path.join(config_dir(), "state.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in (data.get("config") or {}).items():
            if k in DEFAULT_CONFIG:
                cfg[k] = v
    except Exception:
        pass
    return heal_config(cfg)


class Manager:
    def __init__(self, state_file: Optional[str] = None):
        self._lock = threading.RLock()
        # 串行化 state.json 的快照与落盘：并发的 save_state 以前共用同一个
        # state.json.tmp，两次 dump 交错会写出坏 JSON；后写的旧快照还可能
        # 覆盖新快照。
        self._state_lock = threading.Lock()
        self._listeners: List[queue.Queue] = []
        self._workers: Dict[str, threading.Thread] = {}
        self.downloads: Dict[str, Download] = {}
        self.config: Dict[str, object] = dict(DEFAULT_CONFIG)
        self.state_file = state_file or os.path.join(config_dir(), "state.json")
        self._stop = threading.Event()
        self.global_limiter = RateLimiter(int(self.config.get("speed_limit") or 0))
        self._scheduler = threading.Thread(target=self._loop, daemon=True)
        self.load_state()
        self._scheduler.start()

    # ---------------- events ----------------
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=64)
        with self._lock:
            self._listeners.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._listeners:
                self._listeners.remove(q)

    def _emit(self, event: str, d: Optional[Download] = None) -> None:
        payload = {
            "event": event,
            "id": d.id if d else None,
            "task": d.to_dict() if d else None,
            "ts": time.time(),
        }
        with self._lock:
            dead = []
            for q in self._listeners:
                try:
                    q.put_nowait(payload)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self._listeners.remove(q)
        if event in ("completed", "error", "canceled", "paused", "started"):
            self.save_state()

    def _on_task_event(self, event: str, d: Download) -> None:
        self._emit(event, d)

    # ---------------- tasks ----------------
    def add(
        self,
        url: str,
        directory: Optional[str] = None,
        filename: Optional[str] = None,
        connections: Optional[int] = None,
        headers: Optional[Dict[str, str]] = None,
        proxy: Optional[str] = None,
        speed_limit: Optional[int] = None,
        start_now: bool = True,
        source: Optional[str] = None,
        kind: Optional[str] = None,
        audio_url: Optional[str] = None,
        audio_only: bool = False,
        yt_page: Optional[str] = None,
        yt_height: Optional[int] = None,
        section: Optional[str] = None,
        priority: int = 0,
        start_at: float = 0,
    ) -> Download:
        if not url or not str(url).startswith(("http://", "https://")):
            raise ValueError("只支持 http/https 链接")
        cfg = self.config
        original_url = ""
        if cfg.get("mirror_accel"):
            try:
                from .mirror import accelerate

                mirrored = accelerate(str(url))
            except Exception:  # noqa: BLE001
                mirrored = ""
            if mirrored:
                original_url = str(url)
                url = mirrored
        if filename:
            from .http import sanitize_filename

            filename = sanitize_filename(filename.strip()) or None
        d = Download(
            id=uuid.uuid4().hex[:12],
            url=url,
            directory=directory or str(cfg["download_dir"]),
            filename=filename,
            connections=to_connections(connections, int(cfg["connections"])),
            headers=headers,
            proxy=proxy if proxy is not None else (str(cfg["proxy"]) or None),
            max_retries=int(cfg["max_retries"]),  # type: ignore[arg-type]
            timeout=float(cfg["timeout"]),  # type: ignore[arg-type]
            speed_limit=max(0, to_int(speed_limit, "限速", 0)),
            source=source or "manual",
            kind=kind or "",
            audio_url=audio_url or "",
            audio_only=bool(audio_only),
            # 有页面地址就走 yt-dlp 通道（YouTube 直链不能分块下载）
            yt_page=(yt_page or "") if str(yt_page or "").startswith("http") else "",
            yt_height=to_int(yt_height, "清晰度", 0),
            section=normalize_section(section),
            priority=to_priority(priority),
            start_at=float(start_at or 0),
            original_url=original_url,
        )
        d.set_event_callback(self._on_task_event)
        d.global_limiter = self.global_limiter
        with self._lock:
            self.downloads[d.id] = d
        if start_now:
            d.status = "queued"
        else:
            d.status = "paused"
        self._emit("added", d)
        return d

    def get(self, task_id: str) -> Optional[Download]:
        with self._lock:
            return self.downloads.get(task_id)

    def list(self) -> List[dict]:
        with self._lock:
            items = list(self.downloads.values())
        items.sort(key=lambda x: x.created_at, reverse=True)
        return [d.to_dict() for d in items]

    def _wait_idle(self, d: Download, timeout: float = 12.0) -> None:
        """Wait until the task's runner thread has fully exited."""
        t = self._workers.get(d.id)
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=timeout)

    def pause(self, task_id: str) -> bool:
        d = self.get(task_id)
        if not d:
            return False
        # 只有这两态能暂停；merging / completed 上「暂停」只会发出一条
        # 假的 paused 事件，界面显示暂停、任务却继续跑完。
        if d.status not in ("downloading", "queued"):
            return False
        d.pause()
        self._wait_idle(d)
        self._emit("paused", d)
        return True

    def resume(self, task_id: str) -> bool:
        d = self.get(task_id)
        if not d:
            return False
        if d.status not in ("paused", "error", "canceled"):
            return False
        self._wait_idle(d)
        # 保留 start_at：定时任务被「全部暂停」后再继续，应该回到原定时间，
        # 而不是整批挤到现在一起下。要立刻开始请编辑任务把时间清空。
        d.resume()
        self._emit("resumed", d)
        return True

    def edit(
        self,
        task_id: str,
        url: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        proxy: Optional[str] = None,
        start_at: Optional[float] = None,
        priority: Optional[int] = None,
        section: Optional[str] = None,
        connections: Optional[int] = None,
    ) -> Dict[str, object]:
        """Edit a task in place. Partial data survives when the URL still fits.

        The caller is responsible for normalising headers; this only decides
        what to do with the running state. An actively running task is paused
        first (so no worker is mid-write), edited, then queued again — the
        user did not ask for a pause, so it must keep running afterwards.
        """
        d = self.get(task_id)
        if not d:
            raise ValueError("任务不存在")
        if d.status in ("completed", "merging"):
            raise ValueError("已完成或正在合并的任务不能修改，请先重新下载")
        if url is not None:
            url = str(url).strip()
            if url and not url.startswith(("http://", "https://")):
                raise ValueError("只支持 http/https 链接")
        # None 表示「这次没改选段」，不能顺手清空；给了值才校验并归一成
        # yt-dlp 认的 *HH:MM:SS-HH:MM:SS，让文件名标签和实际下的区间一致。
        if section is not None:
            section = normalize_section(section)
        if section and not (d.yt_page or (url and is_video_page(url))):
            # 普通文件任务没有 yt-dlp 通道，选了段也只会下整包。与其让用户
            # 以为限制生效，不如直接说清楚。
            raise ValueError("选段只适用于视频页面链接（YouTube/B站等）")
        was_active = d.status in ("downloading", "queued")
        was_error = d.status == "error"
        if was_active and not self.pause(task_id):
            raise ValueError("任务状态已变化，请稍后再试")
        kept = False
        if url:
            res = d.retarget(url=url, headers=headers, proxy=proxy, section=section)
            kept = bool(res.get("kept"))
            if d.yt_page or is_video_page(url):
                # yt-dlp 通道认的是页面地址；任务列表里那一条 URL 也得跟着换，
                # 否则用户看到的新链接和实际下载的页面是两回事。普通文件
                # 改成视频页链接时，这里顺带把任务切到 yt-dlp 通道。
                if not d.yt_page:
                    # 旧分块半成品属于被换掉的那个文件，yt-dlp 不会接着写：
                    # 留着既占空间，进度条也会从旧文件的百分比开始。
                    d._retarget_pending = False
                    d.segmenter = None
                    for p in (d.part_path, d.meta_path):
                        try:
                            os.remove(p)
                        except OSError:
                            pass
                    d.size = 0
                    d.completed_at = None
                    d.started_at = None
                    d.meter.reset()
                    if not d.audio_only:
                        d.kind = "video"
                d.yt_page = url
        elif headers is not None or proxy is not None or section is not None:
            d.retarget(headers=headers, proxy=proxy, section=section)
        if connections:
            d.connections = to_connections(connections, d.connections)
            if d.segmenter is not None:
                d.segmenter.balance(d.connections)
        if priority is not None:
            d.priority = to_priority(priority)
        if start_at is not None:
            d.start_at = max(0.0, float(start_at or 0))
        # 正在跑 / 出错的任务编辑完直接回队列，别让用户再点一次「继续」。
        # 原来是暂停的则保持暂停，除非刚设了定时开始时间。
        if was_active or was_error or (d.status == "paused" and d.start_at):
            d.error = None
            d.resume()
        self._emit("edited", d)
        self.save_state()
        return {
            "kept": kept,
            "resumed": d.status == "queued",
            "downloaded": d.downloaded,
        }

    def restart(self, task_id: str) -> bool:
        d = self.get(task_id)
        if not d:
            return False
        # 合并中/已完成的任务已经（或正在）写出目标文件。不先停掉合并并删掉它，
        # 重下会另存成「文件名(1).mp4」，用户会以为旧文件没删干净。
        was = d.status
        produced = was == "completed" or (
            was == "merging" and getattr(d, "file_ready", False)
        )
        d.cancel(remove_files=True)
        self._wait_idle(d)
        # 取消发生在「.part 已改名、ffmpeg 还没跑完」的窗口里时，file_ready
        # 是等线程退出后才有答案的，所以要在 wait 之后再看一次。
        produced = produced or (
            was == "merging" and getattr(d, "file_ready", False)
        )
        if produced:
            try:
                os.remove(d.target_path)
            except OSError:
                pass
        d.segmenter = None
        d.meter.reset()
        d.retries = 0
        d.error = None
        d._retarget_pending = False
        d._mirror_fallback_used = False
        if d.yt_page:
            # 上一轮 yt-dlp 的已完成字节/总大小还是旧视频的；不清零，
            # 重下会从旧百分比开始，甚至一直显示 100%。
            d._yt_bytes = 0
            d._yt_completed = 0
            d._yt_speed = 0.0
            d.size = 0
            d.completed_at = None
            d.started_at = None
        if self.config.get("mirror_accel") and d.original_url:
            # 上次镜像失败已退回原地址；「重新下载」应该再给镜像一次机会。
            try:
                from .mirror import accelerate

                mirrored = accelerate(d.original_url)
            except Exception:  # noqa: BLE001
                mirrored = ""
            if mirrored:
                d.url = d.final_url = mirrored
        d.status = "queued"
        self._emit("restarted", d)
        return True

    def cancel(self, task_id: str, remove_files: bool = True) -> bool:
        d = self.get(task_id)
        if not d:
            return False
        was = d.status
        produced = was == "merging" and getattr(d, "file_ready", False)
        d.cancel(remove_files=remove_files)
        self._wait_idle(d)
        produced = produced or (
            was == "merging" and getattr(d, "file_ready", False)
        )
        if remove_files and produced:
            try:
                os.remove(d.target_path)
            except OSError:
                pass
        return True

    def remove(self, task_id: str, remove_files: bool = True) -> bool:
        d = self.get(task_id)
        if not d:
            return False
        was = d.status
        produced = was == "completed" or (
            was == "merging" and getattr(d, "file_ready", False)
        )
        if was in ("downloading", "queued", "merging"):
            # 合并中也是「还在写文件」的状态。以前只把它当普通记录删掉：
            # ffmpeg 随后仍会把成品写回目录，任务却已经从列表里消失。
            d.cancel(remove_files=remove_files)
            self._wait_idle(d)
            produced = produced or (
                was == "merging" and getattr(d, "file_ready", False)
            )
            if remove_files:
                paths = [d.part_path, d.meta_path]
                if produced:
                    paths.append(d.target_path)
                for p in paths:
                    try:
                        if p and os.path.isfile(p):
                            os.remove(p)
                    except OSError:
                        pass
        else:
            if remove_files:
                # ``part_path``/``meta_path`` only exist for unfinished tasks;
                # a completed download owns ``target_path``. Deleting the task
                # used to leave the finished file behind (the GUI's "删除" then
                # looked like a no-op on disk).
                paths = [d.part_path, d.meta_path]
                if produced:
                    paths.append(d.target_path)
                for p in paths:
                    try:
                        if p and os.path.isfile(p):
                            os.remove(p)
                    except OSError:
                        pass
        with self._lock:
            self.downloads.pop(task_id, None)
        self._emit("removed", d)
        self.save_state()
        return True

    def pause_all(self) -> None:
        for d in list(self.downloads.values()):
            if d.status in ("downloading", "queued"):
                self.pause(d.id)

    def resume_all(self) -> None:
        for d in list(self.downloads.values()):
            if d.status in ("paused", "error"):
                self.resume(d.id)

    def clear_finished(self) -> int:
        n = 0
        for d in list(self.downloads.values()):
            if d.status in ("completed", "canceled", "error"):
                with self._lock:
                    self.downloads.pop(d.id, None)
                n += 1
        self.save_state()
        return n

    # ---------------- scheduling ----------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.5)

    def _tick(self) -> None:
        for tid in [k for k, v in self._workers.items() if not v.is_alive()]:
            self._workers.pop(tid, None)
        # 调度器唯一的并发闸门，读取时再夹一次：config 里若残留 -5，
        # 「len(running) >= max_active」恒成立，队列会永久卡死；残留 500
        # 则一次开几百个线程。
        stored = int(self.config.get("max_active") or 3)
        max_active = int(clamp_config_number("max_active", stored))
        now = time.time()
        with self._lock:
            running = [d for d in self.downloads.values() if d.is_active()]
            if len(running) >= max_active:
                return
            candidates = [
                d
                for d in self.downloads.values()
                if d.status == "queued"
                and d.id not in self._workers
                and (not d.start_at or d.start_at <= now)
            ]
            # 高优先级先下；同级按创建时间先来先下。定时只是「不早于」，
            # 不是插队 —— 想插队请调优先级。
            candidates.sort(key=lambda x: (-x.priority, x.created_at))
            for d in candidates[: max_active - len(running)]:
                d.status = "downloading"
                t = threading.Thread(target=self._run_task, args=(d,), daemon=True)
                self._workers[d.id] = t
                t.start()

    def _run_task(self, d: Download) -> None:
        try:
            d.start()
        except Exception as e:  # noqa: BLE001
            d.status = "error"
            d.error = str(e)
            self._emit("error", d)
        finally:
            self._workers.pop(d.id, None)

    # ---------------- config / state ----------------
    def _coerce_config(self, k: str, v):
        """Convert one config value; raise instead of half-applying the batch."""
        if k in CONFIG_INT_KEYS:
            try:
                num = int(v)
            except (TypeError, ValueError) as e:
                raise ValueError(f"{k} 需要整数（收到 {v!r}）") from e
        elif k in CONFIG_FLOAT_KEYS:
            try:
                num = float(v)
            except (TypeError, ValueError) as e:
                raise ValueError(f"{k} 需要数字（收到 {v!r}）") from e
        elif k in CONFIG_BOOL_KEYS:
            return to_bool(v, k)
        elif k == "download_dir":
            # 清空目录框存进去的是 ""：os.makedirs("") 当场抛错，界面只会显示
            # 「保存失败」，看不出是自己把目录删空了。
            text = str(v).strip()
            if not text:
                raise ValueError("下载目录不能为空")
            return text
        else:
            return v
        rng = CONFIG_RANGES.get(k)
        if rng is not None:
            low, high = rng
            if num < low or (high is not None and num > high):
                limit = f"{low}~{high}" if high is not None else f">= {low}"
                raise ValueError(f"{k} 需要在 {limit} 之间（收到 {v!r}）")
        return num

    def set_config(self, patch: Dict[str, object]) -> Dict[str, object]:
        # 先全部换算再落盘：一处写错（speed_limit 填了「快点」）不能让前面
        # 几项已经生效、客户端却只看到一句失败。
        resolved = [
            (k, self._coerce_config(k, v))
            for k, v in patch.items()
            if k in DEFAULT_CONFIG and v is not None
        ]
        # 目录要在改配置之前建：写一个没权限的路径以前会先把其它项改掉、
        # 再在 makedirs 里抛错，配置变成半新半旧。
        for k, v in resolved:
            if k == "download_dir":
                try:
                    os.makedirs(str(v), exist_ok=True)
                except OSError as e:
                    reason = getattr(e, "strerror", None) or e
                    raise ValueError(f"下载目录无法创建：{v}（{reason}）") from e
        with self._lock:
            for k, v in resolved:
                self.config[k] = v
        self.global_limiter.set_rate(int(self.config.get("speed_limit") or 0))
        self._emit("config")
        self.save_state()
        return self.config

    def save_state(self) -> None:
        with self._state_lock:
            with self._lock:
                tasks = []
                limit = max(1, int(self.config.get("history_limit") or 200))
                # 失败/取消也是历史记录。以前只裁剪 completed，错误和取消
                # 的任务会无限堆积在 state.json 里，越启动越慢。
                terminal = [
                    d for d in self.downloads.values()
                    if d.status in ("completed", "error", "canceled")
                ]
                terminal.sort(
                    key=lambda x: x.completed_at or x.created_at or 0,
                    reverse=True,
                )
                keep = {d.id for d in terminal[:limit]}
                for d in self.downloads.values():
                    if d.status in ("completed", "error", "canceled") and d.id not in keep:
                        continue
                    meta = d.to_dict()
                    meta.pop("chunks", None)
                    meta["chunks"] = (
                        d.segmenter.snapshot() if d.segmenter else []
                    )
                    tasks.append(meta)
                data = {"config": self.config, "tasks": tasks}
            tmp = None
            try:
                parent = os.path.dirname(self.state_file) or "."
                os.makedirs(parent, exist_ok=True)
                fd, tmp = tempfile.mkstemp(
                    prefix=".state-", suffix=".tmp", dir=parent
                )
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.state_file)
                tmp = None
            except Exception:
                pass
            finally:
                if tmp:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass

    def load_state(self) -> None:
        if not os.path.exists(self.state_file):
            return
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return
        merged = dict(self.config)
        for k, v in (data.get("config") or {}).items():
            if k in DEFAULT_CONFIG and v is not None:
                merged[k] = v
        # 与 load_config 共用同一套兜底：老版本存下的越界值、手改过的
        # state.json、以及 bool("off") == True 这类反直觉写法都在这里修好。
        self.config = heal_config(merged)
        # 限速器在 __init__ 里按默认值 0 建好；不在这里同步，重启后
        # config 里存的限速就完全失效（用户看到「限速 1 MB/s」却满速跑）。
        self.global_limiter.set_rate(
            int(self.config.get("speed_limit") or 0)
        )
        tasks = data.get("tasks")
        if not isinstance(tasks, list):
            return
        for t in tasks:
            # 单条记录损坏不能拖垮整个历史：以前一条坏数据就 return/抛异常，
            # 后面所有任务都静默消失。
            try:
                self._load_task(t)
            except Exception:
                continue

    def _load_task(self, t) -> None:
        if not isinstance(t, dict) or not t.get("id") or not t.get("url"):
            return
        if t.get("status") == "completed":
            d = Download(
                id=t["id"], url=t["url"],
                directory=t.get("directory") or str(self.config["download_dir"]),
                filename=t.get("filename"),
                connections=record_int(t.get("connections"), 1),
                headers=t.get("headers") or {},
                proxy=t.get("proxy"),
                speed_limit=max(0, record_int(t.get("speed_limit"), 0)),
                source=t.get("source") or "manual",
                kind=t.get("kind") or "",
                audio_url=t.get("audio_url") or "",
                audio_only=bool(t.get("audio_only")),
                # 历史任务也可能是 YouTube/B站：不恢复这几个字段，「重新下载」
                # 会拿页面 HTML 当视频存下来（页面前的任务地址是 page URL）。
                yt_page=t.get("yt_page") or "",
                yt_height=max(0, record_int(t.get("yt_height"), 0)),
                section=t.get("section") or "",
                # 1.16.5 之前存下的越界优先级会在这里复活，然后一直插队；
                # 写入侧夹过 ±99，读取侧用同一个函数，两边不会各说各话。
                priority=to_priority(record_int(t.get("priority"), 0)),
                start_at=record_float(t.get("start_at"), 0.0),
                original_url=t.get("original_url") or "",
            )
            d.size = max(0, record_int(t.get("size"), 0))
            d.final_url = t.get("final_url") or t["url"]
            d.supports_range = bool(t.get("supports_range"))
            d.status = "completed"
            d.completed_at = record_float(t.get("completed_at"), 0.0) or time.time()
            d.created_at = record_float(t.get("created_at"), time.time())
            d.global_limiter = self.global_limiter
            if d.size:
                d.segmenter = Segmenter(d.size, [Chunk(0, d.size, d.size)])
            self.downloads[d.id] = d
            return
        d = Download(
            id=t["id"],
            url=t["url"],
            directory=t.get("directory") or str(self.config["download_dir"]),
            filename=t.get("filename"),
            connections=record_int(
                t.get("connections"), int(self.config["connections"])
            ),
            headers=t.get("headers") or {},
            proxy=t.get("proxy"),
            max_retries=int(self.config["max_retries"]),  # type: ignore[arg-type]
            timeout=float(self.config["timeout"]),  # type: ignore[arg-type]
            speed_limit=max(0, record_int(t.get("speed_limit"), 0)),
            source=t.get("source") or "manual",
            kind=t.get("kind") or "",
            audio_url=t.get("audio_url") or "",
            audio_only=bool(t.get("audio_only")),
            yt_page=t.get("yt_page") or "",
            yt_height=max(0, record_int(t.get("yt_height"), 0)),
            section=t.get("section") or "",
            priority=to_priority(record_int(t.get("priority"), 0)),
            start_at=record_float(t.get("start_at"), 0.0),
            original_url=t.get("original_url") or "",
        )
        d.set_event_callback(self._on_task_event)
        d.global_limiter = self.global_limiter
        d.size = max(0, record_int(t.get("size"), 0))
        d.created_at = record_float(t.get("created_at"), time.time())
        d.load_meta(t)
        prev = t.get("status")
        if prev == "downloading":
            d.status = "queued" if self.config["auto_resume"] else "paused"
        elif prev in ("queued", "paused", "error"):
            d.status = "queued" if self.config["auto_resume"] else "paused"
        else:
            return
        self.downloads[d.id] = d

    def shutdown(self) -> None:
        self._stop.set()
        for d in list(self.downloads.values()):
            if d.is_active():
                d.pause()
        self.save_state()

    # ---------------- shell helpers ----------------
    def can_open_path(self, path: str) -> bool:
        """True when *path* belongs to a task or the configured download dir.

        ``/api/open`` used to launch arbitrary paths; even with the token in
        place, keeping the reachable set small means a leaked token cannot be
        used to run a random application.
        """
        try:
            p = os.path.realpath(os.path.abspath(os.path.expanduser(str(path or ""))))
        except Exception:
            return False
        if not p:
            return False
        roots = set()
        try:
            roots.add(os.path.realpath(str(self.config.get("download_dir") or "")))
        except Exception:
            pass
        for d in self.downloads.values():
            try:
                if d.directory:
                    roots.add(os.path.realpath(d.directory))
            except Exception:
                continue
        for root in roots:
            if not root:
                continue
            try:
                if os.path.commonpath([p, root]) == root:
                    return True
            except ValueError:
                continue
        return False

    @staticmethod
    def open_file(path: str) -> bool:
        """Open the *file itself* with its default application.

        This is what the "打开" button means: a downloaded ``.dmg`` should bring up
        the macOS installer, a ``.pdf`` should open in Preview, and so on. When
        the file is missing we fall back to revealing its folder so the user can
        see what happened instead of getting a silent no-op.
        """
        from .desktop import open_external

        if not path:
            return False
        if os.path.isdir(path):
            return open_external(path)[0]
        if not os.path.exists(path):
            return Manager.reveal_path(path)
        return open_external(path)[0]

    @staticmethod
    def reveal_path(path: str) -> bool:
        """Show the file (or folder) inside the OS file manager, selected."""
        from .desktop import open_external

        return open_external(path or os.path.expanduser("~"), reveal=True)[0]

    @staticmethod
    def open_path(path: str) -> bool:
        """Open a file's folder (or the folder itself) in the OS file manager."""
        from .desktop import open_external

        return open_external(path)[0]
