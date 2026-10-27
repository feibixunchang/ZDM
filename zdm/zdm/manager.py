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
from .transport import URL_SCHEME_HINT, has_supported_scheme
from .videopage import is_video_page
from . import archive as archive_mod  # 按后缀归档：规则的解释只有一个地方

APP_NAME = "zdm"
DEFAULT_PORT = 9000


def check_url_scheme(url) -> str:
    """三端共用的链接闸门：认得出协议，并且本机真的有这条通道。

    sftp 单列一档，是因为它依赖可选的 paramiko。装不上不该让用户把链接粘进去
    等一个永远下不完的任务 —— 当场说清楚缺什么、怎么补才是有用的报错。
    """
    u = str(url or "").strip()
    if not has_supported_scheme(u):
        raise ValueError(URL_SCHEME_HINT)
    if u.lower().startswith("sftp://"):
        from . import sftp as sftp_mod

        if not sftp_mod.available():
            raise ValueError(sftp_mod.missing_hint())
    return u

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


def normalize_tracks(cfg: Dict[str, object], raw: Dict[str, object]) -> Dict[str, str]:
    """校验并归一轨道选择，返回 Download 的四个 stream_* 关键字参数。

    放在 manager 而不是各端入口：GUI / 网页 / 命令行 / 扩展写的是同一个
    manager.add，校验在这里做一次，四端才不会有人能绕过（和 normalize_section
    一个套路）。引擎看不懂坏语法时不报错，只是命中 0 条轨道然后甩一句英文
    异常，用户根本不知道是自己少打了个引号。
    """
    from . import re_bin

    out = {
        "stream_video": re_bin.clean_track(raw.get("video")),
        "stream_audio": re_bin.clean_track(raw.get("audio")),
        "stream_subtitle": re_bin.clean_track(raw.get("subtitle")),
        "stream_sub_format": str(raw.get("sub_format") or "").strip().upper(),
    }
    if not any(out.values()):
        return out
    engine = str(cfg.get("stream_engine") or "auto").strip().lower()
    if engine == "ffmpeg":
        raise ValueError(
            "轨道选择（视频/音频/字幕）只有 N_m3u8DL-RE 认，"
            "当前设置里的流媒体引擎是 ffmpeg。请把流媒体引擎改成「自动」或 "
            "N_m3u8DL-RE 再试。"
        )
    bad = re_bin.check_tracks(
        {
            "video": out["stream_video"],
            "audio": out["stream_audio"],
            "subtitle": out["stream_subtitle"],
            "sub_format": out["stream_sub_format"],
        }
    )
    if bad:
        raise ValueError(bad)
    return out


def normalize_live_limit(value, name: str = "录制时长上限") -> int:
    """直播录制的时长上限：0 = 一直录到用户手动停止或直播自己结束。

    收两种写法：秒数（1800）和时钟（00:30:00 / 30:00）—— 录直播的人说的是
    「录半小时」，只认秒会让命令行每次都要心算。换算和校验放在这一层，
    GUI / 网页 / 命令行 / 扩展走的是同一条口径。

    超上限要报错而不是夹住：这个值最终拼成引擎的 --live-record-limit HH:MM:SS，
    用户说「录 30 小时」却被悄悄改成 24 小时，等于答应了一件没做的事。
    """
    from . import re_bin

    if value in (None, ""):
        return 0
    if isinstance(value, str) and ":" in value:
        from .core import _clock_to_seconds

        num = _clock_to_seconds(value)
        if num is None:
            raise ValueError(f"{name}需要秒数或 HH:MM:SS（收到 {value!r}）")
        value = num
    num = to_int(value, name, 0)
    if num < 0:
        raise ValueError(f"{name}不能是负数（收到 {value!r}）")
    if num > re_bin.LIVE_LIMIT_MAX:
        raise ValueError(
            f"{name}最长 24 小时（{re_bin.LIVE_LIMIT_MAX} 秒），收到 {value!r}。"
            "要录更久请把上限留空、录完手动停止。"
        )
    return num


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


def _log_state_issue(kind: str, detail: str, base_dir: str = "") -> str:
    """把「state 读写出了问题」留痕到 download-errors.log，并回备份/日志位置。

    静默是这类 bug 里最贵的一种：state.json 解析不了就当「没有历史」启动，随后
    任何一次 save_state 会把空列表覆盖回磁盘，用户的下载记录从此消失，界面上
    连一条痕迹都没有。写日志本身失败（磁盘满）不能反过来拖垮主流程。

    日志写在 state.json 旁边（生产环境就是配置目录，和 core.py 记录下载失败的
    那份是同一个文件）；测试里换过 state_file 时不会污染真实配置目录。
    """
    note = f"{kind}：{str(detail)[:300]}"
    try:
        directory = base_dir or config_dir()
        path = os.path.join(directory, "download-errors.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "status": kind,
                        "detail": str(detail)[:500],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    except OSError:
        pass
    return note


DEFAULT_CONFIG: Dict[str, object] = {
    "download_dir": default_download_dir(),
    "connections": 8,
    "max_active": 3,
    "speed_limit": 0,  # bytes/s, 0 = unlimited
    "proxy": "",
    "max_retries": 5,
    "timeout": 30,
    "port": DEFAULT_PORT,
    # 兼容 aria2 的 JSON-RPC（同一个服务上的 POST /jsonrpc）。AriaNg 一类现成
    # 客户端靠它就能直接驱动 ZDM。默认开：服务只绑 127.0.0.1，每条调用都要带
    # 本地接口那枚同一令牌，关掉并不会更安全，开着才让「换个客户端用」这条路走得通。
    "rpc_enabled": True,
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
    # HLS / DASH（m3u8 / mpd）用哪个引擎：auto（有 N_m3u8DL-RE 就用它，没有
    # 退回 ffmpeg）/ re / ffmpeg。降级发生时任务上会写明理由，不静默换。
    "stream_engine": "auto",
    # Cookies for sites that need a logged-in session (YouTube / Bilibili).
    # Shape: {"youtube": "SID=...", "bilibili": "SESSDATA=..."}
    # The browser extension normally sends the page's own cookie along with
    # each task; these are the fallback for tasks started from the desktop app
    # or for servers that only accept a first-party session.
    "site_cookies": {},
    # 按后缀自动归档：{"mp4,mkv": "Video", "srt,ass": "Subtitles"}。命中的成品
    # 落进 <任务目录>/<子目录>，没命中的按原目录落。默认空表 = 关闭 —— 没人
    # 希望下载器悄悄改动自己的目录结构。子目录只能是相对路径，出不了任务目录。
    "archive_rules": {},
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
    "rpc_enabled",
)
CONFIG_INT_KEYS = (
    "connections", "max_active", "max_retries", "speed_limit", "port",
    "history_limit",
)
CONFIG_FLOAT_KEYS = ("timeout",)
# 取值受限于一个集合的配置项：写错的值既不能存下（set_config 报错），也不能
# 在重启后原样生效（heal_config 兜底）。
CONFIG_ENUM_KEYS = {
    "stream_engine": ("auto", "re", "ffmpeg"),
}
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
    for k, allowed in CONFIG_ENUM_KEYS.items():
        # 认不出的取值（手改的 state.json、别的版本）退回默认值：核心按
        # want in ("auto","re") 分支选通道，留下一个拼错的 "RE-Beta" 会让
        # 两条路都不匹配，行为取决于代码走到哪一步 —— 那不是用户选的。
        val = str(healed.get(k) or "").strip().lower()
        healed[k] = val if val in allowed else DEFAULT_CONFIG[k]
    if not isinstance(healed.get("site_cookies"), dict):
        healed["site_cookies"] = {}
    # 归档规则坏了一条不能整表丢：重启后用户看到的是「规则全不生效」，那比
    # 少一个子目录更难查。prune_rules 只留能用的，后缀冲突时先写的赢。
    healed["archive_rules"] = archive_mod.prune_rules(healed.get("archive_rules"))
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
        # 最近一次 save_state 的失败原因（空串 = 正常）。界面用它把「没落盘」
        # 告诉用户，而不是等下次重启发现设置全丢。
        self._state_error = ""
        self.global_limiter = RateLimiter(int(self.config.get("speed_limit") or 0))
        self._scheduler = threading.Thread(target=self._loop, daemon=True)
        self.load_state()
        self._scheduler.start()

    @property
    def state_error(self) -> str:
        return self._state_error

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
        stream_video: Optional[str] = None,
        stream_audio: Optional[str] = None,
        stream_subtitle: Optional[str] = None,
        stream_sub_format: Optional[str] = None,
        stream_live_limit: Optional[int] = None,
    ) -> Download:
        url = check_url_scheme(url)
        cfg = self.config
        tracks = normalize_tracks(
            cfg,
            {
                "video": stream_video,
                "audio": stream_audio,
                "subtitle": stream_subtitle,
                "sub_format": stream_sub_format,
            },
        )
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
            stream_live_limit=normalize_live_limit(stream_live_limit),
            **tracks
        )
        d.set_event_callback(self._on_task_event)
        d.global_limiter = self.global_limiter
        d.stream_engine = str(cfg.get("stream_engine") or "auto")
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
        if d.status == "downloading" and d.live:
            # 直播录制没有「暂停」：直播的边缘已经过去，续传无从谈起。这里的
            # 动作是停止录制 —— 由下载线程把已经录下的 .ts 封成成品并标成
            # 已完成，所以不能在这外面再发一条 paused（那会把刚完成的任务
            # 在界面上打回「已暂停」）。
            return d.stop_recording()
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

    def set_priority(self, task_id: str, priority: int) -> bool:
        """只改排队档位，不动任务本身。

        编辑通路（``edit``）要先把任务暂停再打回队列，用来自作主张改一下顺序
        太亏；aria2 兼容层的 changePosition 需要的正是「只挪顺序、不下断」。
        """
        d = self.get(task_id)
        if not d:
            return False
        d.priority = to_priority(priority)
        self._emit("edited", d)
        self.save_state()
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
        stream_video: Optional[str] = None,
        stream_audio: Optional[str] = None,
        stream_subtitle: Optional[str] = None,
        stream_sub_format: Optional[str] = None,
        stream_live_limit: Optional[int] = None,
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
        _t_raw = (stream_video, stream_audio, stream_subtitle, stream_sub_format)
        if d.status in ("completed", "merging"):
            raise ValueError("已完成或正在合并的任务不能修改，请先重新下载")
        if d.status == "downloading" and d.live:
            # 录制中的直播不能改：编辑要把任务打回队列重跑，而直播停下就接不回
            # 之前那段 —— 用户以为只是改个参数，实际丢掉的是已经录了半小时的内容。
            # 先停止录制（成品会保住），再新建一条。
            raise ValueError("正在录制的项目不能修改，请先「停止录制」再重新下载")
        if url is not None:
            url = str(url).strip()
            if url:
                url = check_url_scheme(url)
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
        if stream_live_limit is not None:
            # 录制时长上限是拼进引擎命令里的，改完要重跑才生效 —— 正在录的
            # 已经被上面那道检查挡住，这里改的是排队/暂停/出错的任务。
            d.stream_live_limit = normalize_live_limit(stream_live_limit)
        if any(v is not None for v in _t_raw):
            # None = 这次没改这一项，留原值；空串 = 用户明确要清掉选择。
            # 改了轨道等于换了要下的内容，已经下完的分片不能续 —— 先删掉。
            tracks = normalize_tracks(
                self.config,
                {
                    "video": _t_raw[0] if _t_raw[0] is not None else d.stream_video,
                    "audio": _t_raw[1] if _t_raw[1] is not None else d.stream_audio,
                    "subtitle": _t_raw[2] if _t_raw[2] is not None else d.stream_subtitle,
                    "sub_format": (
                        _t_raw[3] if _t_raw[3] is not None else d.stream_sub_format
                    ),
                },
            )
            if tracks != {
                "stream_video": d.stream_video,
                "stream_audio": d.stream_audio,
                "stream_subtitle": d.stream_subtitle,
                "stream_sub_format": d.stream_sub_format,
            }:
                for k, v in tracks.items():
                    setattr(d, k, v)
                self._drop_re_work(task_id)
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
        # 重下按「当下」的设置选流媒体引擎：改了设置再重下，不能用创建时
        # 记下的旧引擎（历史任务加载时可能还是上一个设置）。
        d.stream_engine = str(self.config.get("stream_engine") or "auto")
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
        self._drop_re_work(task_id)
        self._emit("removed", d)
        self.save_state()
        return True

    def _drop_re_work(self, task_id: str) -> None:
        """任务记录删掉时，它在流媒体引擎暂存目录里的分片也没用了。

        暂停的分片要留着给「恢复」续传（引擎靠 <序号>.<ext> 跳过已下完的片），
        所以不能在下线时清；但记录一旦从列表里消失就没有下次了 —— 不删的话
        每个「暂停后删除」的 m3u8 任务都在配置目录里留一份永久的垃圾。
        """
        from . import re_bin

        re_bin.drop_work_dir(task_id)

    def pause_all(self) -> int:
        """暂停所有可暂停的任务，返回真正被暂停的条数。

        返回数量是为了让界面能区分「暂停好了」和「本来就没有可暂停的任务」——
        以前两种情况都只有一句成功，点了没反应用户只能反复点。
        """
        n = 0
        for d in list(self.downloads.values()):
            if d.status in ("downloading", "queued") and self.pause(d.id):
                n += 1
        return n

    def resume_all(self) -> int:
        n = 0
        for d in list(self.downloads.values()):
            if d.status in ("paused", "error") and self.resume(d.id):
                n += 1
        return n

    def clear_finished(self) -> int:
        n = 0
        for d in list(self.downloads.values()):
            if d.status in ("completed", "canceled", "error"):
                with self._lock:
                    self.downloads.pop(d.id, None)
                self._drop_re_work(d.id)
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
        # 归档规则在「这一轮真正开跑」时取，而不是建任务那一刻：设置改了，还
        # 排在队里/暂停过的任务下次起跑就按新规则落盘（已经落了 .part 的不搬，
        # 见 core._prepare）。用 prune_rules 是因为这里不能让一条坏规则把整次
        # 下载炸掉 —— 最坏应该是「不按规则归档」，不是「下不了」。
        d.archive_rules = archive_mod.prune_rules(self.config.get("archive_rules"))
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
        elif k in CONFIG_ENUM_KEYS:
            allowed = CONFIG_ENUM_KEYS[k]
            val = str(v).strip().lower()
            if not val:
                # 留空 = 没做选择，用默认值；界面万一发出空串（老版网页表单）
                # 不该让整批设置都保存不了。
                return DEFAULT_CONFIG[k]
            if val not in allowed:
                raise ValueError(
                    f"{k} 只能是 {'、'.join(allowed)}（收到 {v!r}）"
                )
            return val
        elif k == "download_dir":
            # 清空目录框存进去的是 ""：os.makedirs("") 当场抛错，界面只会显示
            # 「保存失败」，看不出是自己把目录删空了。
            text = str(v).strip()
            if not text:
                raise ValueError("下载目录不能为空")
            return text
        elif k == "archive_rules":
            # 设置界面交上来的是那一整栏文本（每行「mp4,mkv = Video」），接口
            # 和 state.json 交上来的是对照表，两种都认；写错当场报错，不能存
            # 一份「看起来保存了、下东西时一条都不生效」的规则。
            return archive_mod.normalize_rules(v)
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
        # 认不出的键名直接报错：以前 `zdm config conenctions 4`（打错一个字母）
        # 会被静默过滤掉，服务端照样回 ok:true，用户看到「保存成功」却什么都没改。
        # 三端界面只发已知键，所以这里可以严格。
        unknown = [str(k) for k in patch if k not in DEFAULT_CONFIG]
        if unknown:
            raise ValueError("未知配置项：" + "、".join(sorted(unknown)))
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

    def save_state(self) -> bool:
        """写回 state.json；返回值表示是否真的落盘，失败原因读 state_error。"""
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
            except Exception as e:  # noqa: BLE001
                # 磁盘满 / 目录只读 / 权限：以前这里 pass 掉，set_config 照样回
                # 「保存成功」，界面上的设置下次重启全丢。至少要留痕，并把
                # 失败交给调用方显示。
                self._state_error = _log_state_issue(
                    "state_save_failed",
                    f"{type(e).__name__}: {e}",
                    os.path.dirname(self.state_file),
                )
            else:
                self._state_error = ""
            finally:
                if tmp:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
            return self._state_error == ""

    def load_state(self) -> None:
        if not os.path.exists(self.state_file):
            return
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError(f"顶层不是对象（{type(data).__name__}）")
        except Exception as e:  # noqa: BLE001
            # 整份文件坏了（手改、断电时写了一半）。不能只「当没有历史」：
            # 下面任何一次 save_state 都会把空列表覆盖回去，历史就此永久消失。
            # 先把原文件挪到旁边留一份能救的副本，再按默认配置继续启动。
            backup = ""
            try:
                backup = self.state_file + ".bad-" + time.strftime("%Y%m%d-%H%M%S")
                os.replace(self.state_file, backup)
            except OSError:
                backup = ""
            self._state_error = _log_state_issue(
                "state_unreadable",
                f"{type(e).__name__}: {e}"
                + (f"；原件已备份到 {backup}" if backup else "；备份失败，原件仍在原路径"),
                os.path.dirname(self.state_file),
            )
            return
        merged = dict(self.config)
        stored = data.get("config")
        for k, v in (stored.items() if isinstance(stored, dict) else []):
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
            # 「这一轮实际用了哪个引擎、为什么换」也是历史的一部分：不恢复的话，
            # 重启后一条降级完成的记录和正常记录长得一模一样，理由就丢了。
            d.stream_engine_used = str(t.get("stream_engine_used") or "")
            d.stream_note = str(t.get("stream_note") or "")
            d.archive_note = str(t.get("archive_note") or "")
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
        d.stream_engine = str(self.config.get("stream_engine") or "auto")
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
