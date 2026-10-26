"""「浏览器扩展还活着吗」的极简记录。

桌面端 GUI 要回答用户一个问题：扩展装上了没有？与其去猜（读注册表、
翻 Chrome 的配置目录都既脆又慢，还牵扯每个浏览器的不同路径），不如
反过来——扩展每次被唤醒都向本地服务报一个到
（``GET /api/extension/ping``，无鉴权，只记录「来过」）；服务只需记下
最近一次的时间戳，GUI 轮询 ``GET /api/extension`` 就能给出可靠的回答。

独立成模块（只依赖标准库）是为了可以直接单测：server.py 牵着 manager、
desktop 一大串，import 即重；而这个文件的逻辑足够小，值得单独验。
"""

from __future__ import annotations

import threading
import time
from typing import Optional

_lock = threading.Lock()
_last_seen = 0.0
# 心跳里带过来的扩展版本号（chrome.runtime.getManifest().version）。
# 桌面端状态栏要说「扩展是几点几」，那只能是扩展自己报的那个数 ——
# 随包 manifest 的版本只说明磁盘上放着哪一份，装进浏览器的可能是旧的。
_ext_version = ""

# 多久之内算「刚活跃」。扩展的 service worker 空闲 30 秒就被回收，
# 但 chrome.alarms 每 30 分钟会唤醒它一次补一拍心跳——所以只要浏览器
# 还开着（扩展没被禁用），这个窗口就不会超时；浏览器关了，时间戳就
# 停格在最后一次心跳上，GUI 会如实展示「最近一次活跃时间」。
RECENT_WINDOW = 3600.0


def note_seen(ts: Optional[float] = None, version: str = "") -> None:
    """扩展打来一次电话。任意线程可调，开销约等于一次时间读取。

    ``version`` 只在非空时覆盖：老版扩展不带版本号，不能因为它没带就把
    上一次记下的真版本抹掉（那会让状态栏突然只显示桌面端版本）。
    """
    global _last_seen, _ext_version
    with _lock:
        _last_seen = time.time() if ts is None else ts
        text = str(version or "").strip()
        if text:
            _ext_version = text


def ext_version() -> str:
    """最近一次心跳里扩展自报的版本号；没收到过就是空串。"""
    with _lock:
        return _ext_version


def last_seen() -> float:
    """最近一次心跳的 Unix 时间戳；从未见过扩展时是 0。"""
    with _lock:
        return _last_seen


def recent(max_age: float = RECENT_WINDOW) -> bool:
    """RECENT_WINDOW 秒内有过心跳。"""
    last = last_seen()
    return last > 0 and (time.time() - last) <= max_age


def reset() -> None:
    """仅测试用：清空记录。"""
    global _last_seen, _ext_version
    with _lock:
        _last_seen = 0.0
        _ext_version = ""
