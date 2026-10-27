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
from urllib.parse import parse_qs, unquote, urlparse

# YouTube 的几种页面形态：watch / shorts / live / embed / youtu.be 短链。
_YT = re.compile(
    r"^https?://(?:[a-z0-9-]+\.)*"
    r"(?:youtube\.com/(?:watch|shorts|live|embed)|"
    r"youtu\.be/|"
    r"youtube-nocookie\.com/embed)",
    re.I,
)

# 其它「一个链接背后是一段视频/音频、而不是一个文件」的站点。这里覆盖的是
# yt-dlp 明确支持、且用户最常粘贴的主站；没有列到的站点仍可在浏览器里用扩展
# 的「解析此页面」（那条路对任意 URL 都问一次 yt-dlp）。
#
# 只按主机名/路径前缀认，不抓页面内容：判断在 GUI、服务端、CLI 三处共用，
# 必须廉价且不联网。路径尽量收窄到内容页，避免把首页/列表页也送进 yt-dlp。
_SITE = re.compile(
    r"^https?://(?:[a-z0-9-]+\.)*"
    r"(?:"
    # 国际视频
    r"vimeo\.com/\d|dailymotion\.com/video/|dai\.ly/|"
    r"facebook\.com/watch|fb\.watch/|facebook\.com/[^/]+/videos/|"
    r"instagram\.com/(?:p|reel|reels|tv)/|"
    r"twitter\.com/[^/]+/status/|x\.com/[^/]+/status/|"
    r"reddit\.com/r/[^/]+/comments/|v\.redd\.it/|"
    r"streamable\.com/|rumble\.com/|odysee\.com/|bitchute\.com/video/|"
    r"coub\.com/view/|9gag\.com/gag/|imgur\.com/(?:gallery|a)/|"
    r"newgrounds\.com/portal/view/|archive\.org/details/|ted\.com/talks/|"
    # 国内视频
    r"bilibili\.com/(?:video|bangumi/play|audio)/|b23\.tv/|acfun\.cn/v/|"
    r"douyin\.com/(?:video|jingxuan|discover|note)|iesdouyin\.com/share/video/|"
    r"tiktok\.com/|kuaishou\.com/short-video/|v\.kuaishou\.com/|"
    r"xiaohongshu\.com/(?:explore|discovery/item)/|"
    r"weibo\.com/(?:tv/show|status)|video\.weibo\.com/show|"
    r"ixigua\.com/|youku\.com/v_show/|iqiyi\.com/v_|"
    r"v\.qq\.com/x/(?:page|cover)/|mgtv\.com/b/|sohu\.com/v/|"
    r"le\.com/ptv/vplay/|1905\.com/video/|zhihu\.com/zvideo/|"
    r"pearvideo\.com/video_|"
    # 音乐 / 播客
    r"soundcloud\.com/(?!discover/|stream/|you/|charts/|search/)[^/]+/[^/]+|"
    r"bandcamp\.com/(?:track|album)/|"
    r"mixcloud\.com/[^/]+/[^/]+|audiomack\.com/[^/]+/song/|"
    r"hearthis\.at/[^/]+/[^/]+|jamendo\.com/(?:[a-z]{2}/)?track/|"
    r"music\.163\.com/(?:#/)?(?:song|album|playlist|program|m/)|"
    r"y\.qq\.com/(?:n/ryqq/songDetail|songDetail)/|"
    r"kuwo\.cn/play_detail/|kugou\.com/mixsong/|"
    r"music\.migu\.cn/(?:v3/music/song|music/song)|"
    r"5sing\.kugou\.com/(?:fc|yc|bz)/|"
    r"lizhi\.fm/\d|ximalaya\.com/sound/|qingting\.fm/(?:channels|programs)/|"
    r"music\.youtube\.com/watch"
    r")",
    re.I,
)

# 任意站点的内容页路径。只接受「/video/12345」「/watch/xxxx」这类带数字
# 标识的路径：CMS/新闻站的视频详情页基本都是这个形状，yt-dlp 的通用
# extractor 也能从中找到 og:video；而首页、栏目页、/videos/list 不会命中。
_GENERIC = re.compile(
    r"^https?://[a-z0-9-]+(?:\.[a-z0-9-]+)+/"
    r"(?:videos?|watch|shorts|clips?|reels?|episodes?|embed|v)/"
    r"(?=[^/?#]*\d)[^/?#]+/?(?:[?#].*)?$",
    re.I,
)

# 直链媒体永远不当页面：/video/12345.mp4 是一个文件，交给多线程引擎。
# 扩展推送的直链本来就带 kind，不会走到这里；这条兜住手填链接/CLI。
_DIRECT = re.compile(
    r"\.(?:mp4|m4v|webm|mkv|mov|m4a|mp3|aac|flac|wav|ogg|oga|opus|"
    r"m3u8|mpd|ts|m4s)(?:$|[?#])",
    re.I,
)


# 清单/合集页：一条地址背后是**一串**视频。
#
# 为什么单独认这一类：下载通道给 yt-dlp 的一律带 --no-playlist，而实测（随包
# yt-dlp 2026.08.19）它对「清单地址」根本不起作用 —— 只有「既是视频又是清单」
# 的地址（watch?v=…&list=…）才会被它收回一条视频。于是把 /playlist?list= 直接
# 建成一条任务的实际结果是：yt-dlp 把整份清单下完，而 outtmpl 是同一个
# zdm-<任务id>.%(ext)s，每一集覆盖上一集，最后用户拿到**一个**文件、状态还是
# 「已完成」。宁可当场拒绝并告诉他该走哪条路。
_LIST_YT = re.compile(
    r"^https?://(?:[a-z0-9-]+\.)*"
    r"(?:"
    r"youtube\.com/playlist\?|"
    r"music\.youtube\.com/(?:playlist\?|browse/|library/songs)|"
    # 频道/用户主页的视频标签页：整页就是一串视频。
    # 注意斜杠的位置 —— /channel/<id>、/user/<name>、/c/<name> 的 id 在
    # 下一段，而 @handle 是写在同一段的（先写错过一次：`com/(?:channel|c)
    # [^/?#]+` 里 [^/]+ 顶到 `channel` 后面那个 `/`，channel/user/c 三种形态
    # 全都匹配不上，只有 @NASA 这种恰好没有中间斜杠的能用）。
    r"(?:music\.youtube\.com|youtube\.com)/"
    r"(?:(?:channel|user|c)/[^/?#]+|@[^/?#]+)"
    r"/(?:videos|shorts|streams|live)"
    r")",
    re.I,
)
_LIST_BILI = re.compile(
    r"^https?://"
    r"(?:"
    # UP 主投稿列表（space.bilibili.com/<uid> 默认就是「视频」标签页；
    # /homepage、/dynamic 这些别的标签页不算清单，展开它们只会拿到一堆动态）
    r"space\.bilibili\.com/\d+(?:/(?:video|videos|archive|favlist"
    # 收藏夹（favlist?fid=）与合集/系列（channel/collectiondetail?sid=、
    # channel/seriesdetail?sid=）：随包 yt-dlp 2026.08.19 里分别对应
    # BilibiliFavoritesList / BilibiliCollectionList / BilibiliSeriesList，
    # 实测这三个地址都只会被 [generic] 接住或整串失败 —— 建成一条任务同样
    # 只剩最后一集，所以必须归到清单这一类。
    r"|channel/(?:collection|series)detail|channels/[^/?#]+))?(?:[?#]|$)"
    r"|m\.bilibili\.com/space/\d+"
    # 合集 / 收藏夹 / 稍后再看 / 番剧**季**页（ss…=整季，ep…=单集，后者不算清单）
    r"|(?:[a-z0-9-]+\.)*bilibili\.com/(?:medialist/|favorite/|list/|watchlater|bangumi/play/ss)"
    r")",
    re.I,
)
_LIST_MUSIC = re.compile(
    r"^https?://"
    r"(?:"
    r"music\.163\.com/(?:#/)?(?:playlist|album|m/playlist|m/album)"
    r"|y\.qq\.com/(?:n/ryqq/)?(?:playlist|album)/"
    r"|music\.migu\.cn/v3/music/(?:playlist|album)/"
    r"|soundcloud\.com/[^/?#]+/sets/"
    # Bandcamp 的专辑页基本都在艺人的子域上（xxx.bandcamp.com/album/…），
    # 只认主域等于把最常见的形态漏掉。
    r"|(?:[a-z0-9-]+\.)*bandcamp\.com/album/"
    r")",
    re.I,
)


def is_youtube(url: str) -> bool:
    return bool(_YT.match((url or "").strip()))


def is_playlist_page(url: str) -> bool:
    """地址是一份清单/合集/频道视频页（不是一个文件、也不是一段视频）。"""
    u = (url or "").strip()
    if not u or _DIRECT.search(u):
        return False
    return bool(_LIST_YT.match(u) or _LIST_BILI.match(u) or _LIST_MUSIC.match(u))


# watch?v=…&list=… 与 /video/BV…?p=… 这类地址**点到了具体一集**（所以下载时是
# 一条视频），但它们同时属于一份清单 —— 界面该提示「要不要整份都下」。
_IN_LIST_Q = re.compile(r"(?:[?&]list=|[#?&]p=[0-9]+|[?&]play[?&=#])", re.I)


def playlist_hint(url: str) -> bool:
    """值得提示「按播放列表展开」的地址：清单页本身，或带着清单参数的单视频页。

    与 is_playlist_page 分工不同：那个决定「这条地址能不能建成一条任务」，
    这个只决定「要不要给用户看一眼整份清单」。
    """
    u = (url or "").strip()
    if not u or _DIRECT.search(u):
        return False
    if _LIST_YT.match(u) or _LIST_BILI.match(u) or _LIST_MUSIC.match(u):
        return True
    return bool(is_video_page(u) and _IN_LIST_Q.search(u))


def is_video_page(url: str) -> bool:
    u = (url or "").strip()
    if not u or _DIRECT.search(u):
        return False
    # 清单页不算视频页：/bangumi/play/ss… 这种既像「一个页面」又是一串集，
    # 归给播放列表那条路，别建成一条会自相覆盖的页面任务。
    if is_playlist_page(u):
        return False
    return bool(_YT.match(u) or _SITE.match(u) or _GENERIC.match(u))


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


def _playlist_id(url: str) -> str:
    """清单地址里的列表 id（/playlist?list=… 或 watch?v=…&list=… 的 list）。"""
    try:
        p = urlparse(url)
        if p.path in ("/playlist", "/watch", "/embed", "/shorts", "/live"):
            return (parse_qs(p.query).get("list") or [""])[0].strip()
    except Exception:
        pass
    return ""


def _clean_stem(text: str) -> str:
    text = unquote(text or "").strip()
    text = re.sub(r"[\\/:*?\"<>|]+", "_", text)
    return text[:100].strip(" ._")


def video_page_filename(url: str) -> str:
    """A readable fallback filename for a video-page task.

    The GUI resolves the real title and sends it explicitly; this is for the
    web UI and ``zdm add``, where nobody resolved the title. Without it the
    yt-dlp channel renames the finished file to ``download`` (the Download
    default) and the user gets an extension-less file.

    清单页（``/playlist?list=…``）没有视频 id，取列表 id 也比一律叫
    ``YouTube.mp4`` 有用：几十条这样的任务排在列表里谁也认不出谁。
    """
    u = (url or "").strip()
    # _LIST_YT 也要进来：`is_youtube` 只认 watch/shorts/live/embed 那些形态，
    # /playlist?list= 不在其中，落到底下的通用分支就变成 Video-<哈希> ——
    # 而清单地址唯一有意义的名字就是那个列表 id。
    if is_youtube(u) or _LIST_YT.match(u):
        vid = _youtube_id(u)
        if vid:
            return f"YouTube-{vid}.mp4"
        lid = _playlist_id(u)
        if lid:
            return f"YouTube-Playlist-{_clean_stem(lid)[:40]}.mp4"
        return "YouTube.mp4"
    try:
        p = urlparse(u)
        host = (p.hostname or "").lower()
        parts = [seg for seg in p.path.split("/") if seg]
        # 音乐站：用户 + 曲名比一串哈希有用得多。扩展名先写 .mp4，
        # 下载完成时 core 会按实际容器改成 .m4a/.mp3。
        if (
            ("soundcloud.com" in host or "mixcloud.com" in host or
             "audiomack.com" in host or "hearthis.at" in host)
            and len(parts) >= 2
        ):
            name = " - ".join(_clean_stem(x) for x in parts[-2:])
            if name:
                return name + ".mp4"
        if "bandcamp.com" in host and "track" in parts:
            name = _clean_stem(parts[-1])
            if name:
                return name + ".mp4"
        # 国内音乐/播客：路径末段通常就是 id/曲名；至少别让所有任务都叫
        # Video-<hash>，在下载列表里完全分不出谁是谁。
        audio_host = re.search(
            r"(music\.163\.com|y\.qq\.com|kuwo\.cn|kugou\.com|music\.migu\.cn|"
            r"5sing\.kugou\.com|lizhi\.fm|ximalaya\.com|qingting\.fm)",
            host,
        )
        if audio_host and parts:
            name = _clean_stem(parts[-1])
            if name and name.lower() not in ("song", "sound", "track", "detail"):
                return f"{name}.mp4"
        if audio_host:
            # 网易云这类站点把曲目放在 hash 路由或查询串里：
            #   music.163.com/#/song?id=123 / music.163.com/song?id=123
            frag = f"{p.fragment}?{p.query}"
            am = re.search(r"(?:song|album|playlist|program|m)/(\d+)", frag)
            if not am:
                am = re.search(r"(?:^|[?&])id=(\d+)", frag)
            if am:
                return f"Music-{am.group(1)}.mp4"
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
