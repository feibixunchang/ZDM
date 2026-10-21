"""Recognise video *page* URLs (as opposed to direct media URLs).

Why this module exists
----------------------
Pasting ``https://www.youtube.com/watch?v=...`` into 下载 used to create a
perfectly ordinary download task: the engine fetched the URL and saved the
HTML page, so the user ended up with a file literally named ``watch`` and a
task that reported success. The desktop window, the web UI and ``zdm add``
all had this problem because the check lived nowhere.

A page URL must become a *page task* (``yt_page``), which routes the download
to yt-dlp and produces the real video stream. Direct media URLs
(``googlevideo.com``, ``bilivideo.com``, ``.../file.mp4``) never match these
patterns, so they keep going through the multi-connection engine.

Only stdlib + no project imports: this module is loaded by the GUI, the
server and the CLI, including from the frozen bundles.
"""
from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qs, urlparse

# YouTube 的几种页面形态：watch / shorts / live / embed / youtu.be 短链。
_YT = re.compile(
    r"^https?://(?:[a-z0-9-]+\.)*"
    r"(?:youtube\.com/(?:watch|shorts|live|embed)|"
    r"youtu\.be/|"
    r"youtube-nocookie\.com/embed)",
    re.I,
)

# 其它「一个链接背后是一段视频、而不是一个文件」的站点。B站/抖音等国内站点
# 用 yt-dlp 的对应 extractor，画质/音轨由它自己处理。
_OTHER = re.compile(
    r"^https?://(?:[a-z0-9-]+\.)*"
    r"(?:bilibili\.com/video/|b23\.tv/|"
    r"vimeo\.com/\d|"
    r"twitter\.com/(?:[^/]+)/status/|x\.com/(?:[^/]+)/status/|"
    r"douyin\.com/video/|tiktok\.com/)",
    re.I,
)


def is_youtube(url: str) -> bool:
    return bool(_YT.match((url or "").strip()))


def is_video_page(url: str) -> bool:
    u = (url or "").strip()
    return bool(_YT.match(u) or _OTHER.match(u))


def video_page_url(url: str) -> str:
    """普通链接返回空串，视频页面链接返回它自己（便于直接塞进 payload）。"""
    u = (url or "").strip()
    return u if is_video_page(u) else ""


def _youtube_id(url: str) -> str:
    try:
        p = urlparse(url)
        host = (p.hostname or "").lower()
        if host == "youtu.be":
            return p.path.strip("/").split("/")[0]
        if p.path == "/watch":
            return (parse_qs(p.query).get("v") or [""])[0]
        for prefix in ("/shorts/", "/live/", "/embed/"):
            if p.path.startswith(prefix):
                return p.path[len(prefix):].split("/")[0]
    except Exception:
        pass
    return ""


def video_page_filename(url: str) -> str:
    """A readable fallback filename for a video-page task.

    The GUI resolves the real title and sends it explicitly; this is for the
    web UI and ``zdm add``, where nobody resolved the title. Without it the
    yt-dlp channel renames the finished file to ``download`` (the Download
    default) and the user gets an extension-less file.
    """
    u = (url or "").strip()
    if is_youtube(u):
        vid = _youtube_id(u)
        return f"YouTube-{vid}.mp4" if vid else "YouTube.mp4"
    try:
        p = urlparse(u)
        m = (
            re.search(r"/video/([A-Za-z0-9_-]+)", p.path)
            or re.search(r"/status/(\d+)", p.path)
            or re.search(r"/(\d{5,})(?:/|$)", p.path)
        )
        if m:
            return f"Video-{m.group(1)}.mp4"
    except Exception:
        pass
    digest = hashlib.sha1(u.encode("utf-8", "replace")).hexdigest()[:8]
    return f"Video-{digest}.mp4"
