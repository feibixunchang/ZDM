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

from .core import Chunk, Download, RateLimiter, Segmenter

APP_NAME = "zdm"
DEFAULT_PORT = 9000


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
    # Cookies for sites that need a logged-in session (YouTube / Bilibili).
    # Shape: {"youtube": "SID=...", "bilibili": "SESSDATA=..."}
    # The browser extension normally sends the page's own cookie along with
    # each task; these are the fallback for tasks started from the desktop app
    # or for servers that only accept a first-party session.
    "site_cookies": {},
}


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
    return cfg


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
    ) -> Download:
        if not url or not str(url).startswith(("http://", "https://")):
            raise ValueError("只支持 http/https 链接")
        cfg = self.config
        if filename:
            from .http import sanitize_filename

            filename = sanitize_filename(filename.strip()) or None
        d = Download(
            id=uuid.uuid4().hex[:12],
            url=url,
            directory=directory or str(cfg["download_dir"]),
            filename=filename,
            connections=int(connections or cfg["connections"]),
            headers=headers,
            proxy=proxy if proxy is not None else (str(cfg["proxy"]) or None),
            max_retries=int(cfg["max_retries"]),  # type: ignore[arg-type]
            timeout=float(cfg["timeout"]),  # type: ignore[arg-type]
            speed_limit=int(speed_limit or 0),
            source=source or "manual",
            kind=kind or "",
            audio_url=audio_url or "",
            audio_only=bool(audio_only),
            # 有页面地址就走 yt-dlp 通道（YouTube 直链不能分块下载）
            yt_page=(yt_page or "") if str(yt_page or "").startswith("http") else "",
            yt_height=int(yt_height or 0),
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
        d.resume()
        self._emit("resumed", d)
        return True

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
        max_active = int(self.config.get("max_active") or 3)
        with self._lock:
            running = [d for d in self.downloads.values() if d.is_active()]
            if len(running) >= max_active:
                return
            candidates = [
                d
                for d in self.downloads.values()
                if d.status == "queued" and d.id not in self._workers
            ]
            candidates.sort(key=lambda x: x.created_at)
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
    def set_config(self, patch: Dict[str, object]) -> Dict[str, object]:
        with self._lock:
            for k, v in patch.items():
                if k in DEFAULT_CONFIG and v is not None:
                    if k in ("connections", "max_active", "max_retries", "speed_limit", "port", "history_limit"):
                        v = int(v)  # type: ignore[arg-type]
                    elif k == "timeout":
                        v = float(v)  # type: ignore[arg-type]
                    elif k == "auto_resume":
                        v = bool(v)
                    self.config[k] = v
            if "download_dir" in patch:
                os.makedirs(str(self.config["download_dir"]), exist_ok=True)
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
        cfg = data.get("config") or {}
        for k, v in cfg.items():
            if k not in DEFAULT_CONFIG or v is None:
                continue
            try:
                if k in (
                    "connections",
                    "max_active",
                    "max_retries",
                    "speed_limit",
                    "port",
                    "history_limit",
                ):
                    v = int(v)
                elif k == "timeout":
                    v = float(v)
                elif k == "auto_resume":
                    v = bool(v)
            except (TypeError, ValueError):
                continue
            self.config[k] = v
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
                filename=t.get("filename"), connections=int(t.get("connections") or 1),
                source=t.get("source") or "manual",
                kind=t.get("kind") or "",
                audio_only=bool(t.get("audio_only")),
            )
            d.size = int(t.get("size") or 0)
            d.status = "completed"
            d.completed_at = t.get("completed_at") or time.time()
            d.created_at = t.get("created_at", time.time())
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
            connections=int(t.get("connections") or self.config["connections"]),
            headers=t.get("headers") or {},
            proxy=t.get("proxy"),
            max_retries=int(self.config["max_retries"]),  # type: ignore[arg-type]
            timeout=float(self.config["timeout"]),  # type: ignore[arg-type]
            speed_limit=int(t.get("speed_limit") or 0),
            source=t.get("source") or "manual",
            kind=t.get("kind") or "",
            audio_url=t.get("audio_url") or "",
            audio_only=bool(t.get("audio_only")),
        )
        d.set_event_callback(self._on_task_event)
        d.global_limiter = self.global_limiter
        d.size = int(t.get("size") or 0)
        d.created_at = t.get("created_at", time.time())
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
