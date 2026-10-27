"""播放列表展开：把「一份清单」的链接变成 N 条单视频任务。

为什么单独开一条通道
--------------------
``yt_resolve.resolve()`` 走的是 ``--no-playlist`` / ``noplaylist=True``，因为扩展
菜单要的是「这一段视频的清晰度阶梯」，不是整个列表。于是粘贴播放列表时它只取
第一条（``_shape`` 里那句 entries 截断），用户点一次「下载」拿到一集，却并不
知道自己丢掉了几十集。Ghost Downloader 的做法是先列出名单、让用户挑范围、
一次性建 N 条任务；这里照做，并把「展开 + 建任务」放在服务端，网页版 / Qt
桌面端 / CLI / 扩展四条路共用同一份语义。

只列名单，不逐条解格式
----------------------
flat 提取（``--flat-playlist``）是**一次**请求，几百集也只是几秒；不带 flat 的话
yt-dlp 会为清单里每一条视频跑一遍 player 解析 —— 几百集就是几十分钟，还必然撞上
YouTube 的按请求风控。建任务之后每条任务自己走 yt-dlp 通道，那时候才解格式。
"""
from __future__ import annotations

import re
import time
import urllib.parse as up
from typing import Dict, List, Optional, Tuple

# 与 yt_resolve 共用 cookie / 二进制 / 错误文案的处理：那几段是踩坑换来的
# （Netscape 与 header 形态的 cookie 要分开处理，否则等于没登录），
# 复制一份迟早会分叉。
from .yt_resolve import (
    _bin_module,
    _clean_err,
    _cookiefile,
    _host_of,
    _import_yt_dlp,
)
# 站点清单形态的判断表在 videopage：和「这条地址能不能建成一条任务」共用一份，
# 两处各写一份正则迟早分叉（分叉的表现是界面提示能展开、服务端却把清单当单条
# 任务下成一集覆盖一集）。
from .videopage import playlist_hint as looks_like_playlist  # noqa: F401

# 展开结果缓存：预览一次、随后建任务要用同一份名单；每次联网都重列一边既慢
# 又会被站点风控。key 是 (清单 URL, 上限)，因为上限会真的改变抓回来的名单
# ——「按 100 条缓存」被「按 500 条」的请求命中，用户会看到一份缺尾的清单。
_CACHE: Dict[Tuple[str, int], Tuple[float, Dict]] = {}
_CACHE_TTL = 300.0

# 一次最多展开多少条。上限存在的原因是「整份清单」是个无界请求：某些频道
# 有几千条视频，全部建任务会把队列和 state.json 压垮。
MAX_ITEMS = 500

# 默认只展开前这么多条。频道视频页**一页 30 条**、翻页约 3~4 秒（实测 NASA
# 取 26 条花了 87 秒），所以默认值同时是「翻几页」的成本：100 条约 4 页、
# 十几秒能回，500 条要一两分钟。界面上想要更多由用户显式填 max_items。
DEFAULT_ITEMS = 100


def parse_range(text, count: int) -> List[int]:
    """把 "1-20"、"1,3,5-8"、"all"、"" 解析成 1 基的序号列表（升序去重）。

    越界、倒写（"20-1"）、非数字都直接 ValueError：用户以为选了 20 集、
    实际建了 1 条，比报错更伤人。
    """
    if count <= 0:
        raise ValueError("播放列表是空的")
    raw = str(text or "").strip().lower()
    if raw in ("", "all", "*", "全部", "整份", "全"):
        return list(range(1, count + 1))
    picked: List[int] = []
    for piece in re.split(r"[,，;；\s]+", raw):
        piece = piece.strip()
        if not piece:
            continue
        m = re.match(r"^(\d+)(?:\s*[-~到至]\s*(\d+))?$", piece)
        if not m:
            raise ValueError(f"无法识别的序号或范围：{piece!r}")
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else a
        if b < a:
            raise ValueError(f"范围要从小到大，收到 {a}-{b}")
        if a < 1 or b > count:
            raise ValueError(f"范围超出清单条数（共 {count} 条，收到 {piece}）")
        picked.extend(range(a, b + 1))
    out = sorted(set(picked))
    if not out:
        raise ValueError("没有选中任何一条")
    return out


def safe_dirname(text: str, fallback: str = "") -> str:
    """把站点给的清单标题变成一个安全的**相对**子目录名。

    标题是站点控制的文本，所以：路径分隔符、`..`、绝对路径前缀、控制字符、
    Windows 保留字符一律去掉；只允许落在下载目录之内。拿不到可用名字时返回
    ``fallback``（空串 = 不建子目录），而不是硬造一个「未命名」占住用户目录。
    """
    name = str(text or "").strip()
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]+', "_", name)
    name = name.replace("..", "_")
    name = name[:80].strip(" ._")
    # 保留名后缀不能再被 strip(" ._") 吃掉：那等于没加（先测过一次，'CON' 原样返回）。
    if re.match(r"^(con|prn|aux|nul|com\d|lpt\d)$", name, re.I):
        name += "0"
    return name or str(fallback or "")


def item_filename(item: Dict, base_url: str = "") -> str:
    """一条清单条目 → 任务的文件名。

    优先用 flat 提取拿到的标题（比 ``Video-BQ3x…`` 有用得多，几十条任务排在
    一起时这是唯一能认出谁是谁的东西）；没有标题才退回按链接推导的名字。
    后缀先写 .mp4，下载完成时 core 会按实际容器改成 .m4a/.webm。
    """
    title = safe_dirname(str((item or {}).get("title") or ""), fallback="")
    if title:
        return title + ".mp4"
    from .videopage import video_page_filename

    return video_page_filename(str((item or {}).get("url") or base_url or ""))


def _origin(url: str) -> str:
    try:
        p = up.urlparse(url)
        if p.scheme and p.netloc:
            return f"{p.scheme}://{p.netloc}"
    except Exception:  # noqa: BLE001
        pass
    return ""


def _abs_url(base: str, value: str) -> str:
    """flat 条目的 url 有时是相对路径（/watch?v=…），有时是别的站点绝对地址。"""
    v = (value or "").strip()
    if not v:
        return ""
    if v.startswith("//"):
        return ("https:" + v) if _origin(base) else v
    if re.match(r"^https?://", v, re.I):
        return v
    o = _origin(base)
    if v.startswith("/") and o:
        return o + v
    return v


def _item_entry(base: str, idx: int, e: Optional[dict]) -> Optional[Dict]:
    """一条 flat entry → {index,title,url,id}；拿不到可下载地址就返回 None。"""
    if not isinstance(e, dict):
        return None
    url = _abs_url(base, str(e.get("webpage_url") or e.get("url") or ""))
    ident = str(e.get("id") or "")
    if not url:
        ext = str(e.get("extractor") or e.get("extractor_key") or "").lower()
        if ident and "youtube" in ext:
            url = "https://www.youtube.com/watch?v=" + ident
        elif ident and "bilibili" in ext:
            url = "https://www.bilibili.com/video/" + ident
    if not url or not url.lower().startswith(("http://", "https://")):
        return None
    title = str(e.get("title") or "").strip()
    return {
        "index": idx,
        "title": title,
        "url": url,
        "id": ident[:80],
        # flat entry 常带 duration/ filesize 的近似值；给界面用来估算总量
        "duration": int(e.get("duration") or 0),
    }


def _shape_result(info, base_url: str, max_items: int) -> Dict:
    """把 yt-dlp 的 -J 输出整理成 {is_playlist, title, items}。"""
    if not isinstance(info, dict):
        return {"ok": False, "is_playlist": False, "title": "", "items": [],
                "error": "没有解析到任何内容"}
    etype = str(info.get("_type") or "")
    entries = info.get("entries")
    if etype != "playlist" and not entries:
        # 单条视频（_type == "url"）：不是列表，但界面可以照着建一条任务
        one = _item_entry(base_url, 1, info) or (
            _item_entry(base_url, 1, dict(info, url=info.get("webpage_url") or base_url))
        )
        return {
            "ok": bool(one),
            "is_playlist": False,
            "title": str(info.get("title") or ""),
            "items": [one] if one else [],
            "error": "" if one else "这条链接里没有可下载的视频",
        }
    items: List[Dict] = []
    skipped = 0
    truncated = False
    for raw in entries or []:
        if len(items) >= max_items:
            # 不是「拿完之后丢掉多余的」：上限到了就明确说清单被截断，
            # 否则用户以为下完了一整份。
            truncated = True
            break
        e = _item_entry(base_url, len(items) + 1, raw)
        if e is None:
            skipped += 1
            continue
        items.append(e)
    return {
        "ok": bool(items),
        "is_playlist": True,
        "title": str(info.get("title") or ""),
        "items": items,
        # 清单里被跳过/被上限截掉的部分必须说出来，否则用户以为下全了
        "skipped": skipped,
        "truncated": truncated,
        "error": "" if items else "这份清单里没有可下载的条目（可能是私密/地区限制）",
    }


def _via_python(url: str, cookie: Optional[str], proxy: Optional[str],
                limit: int = 0) -> Tuple[Optional[dict], str]:
    """没有 yt-dlp 可执行文件时的回退：用随包的 python 模块做 flat 提取。"""
    try:
        yt_dlp = _import_yt_dlp()
    except Exception as e:  # noqa: BLE001
        return None, f"yt-dlp 不可用：{e}"
    opts: Dict = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "simulate": True,
        # 关键：noplaylist=False + extract_flat，一次请求只拿名单
        "noplaylist": False,
        "extract_flat": "in_playlist",
        "ignoreerrors": True,
        "playliststart": 0,
        "socket_timeout": 20,
        "retries": 1,
        "cachedir": False,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 "
                "Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    }
    if limit and int(limit) > 0:
        # 同二进制通道的 --playlist-items：让 yt-dlp 别把整频道翻到底。
        opts["playlistend"] = int(limit)
    cookie_path = _cookiefile(cookie, _host_of(url))
    try:
        if cookie_path:
            opts["cookiefile"] = cookie_path
        elif cookie and "\t" not in (cookie or "") and "Netscape" not in (cookie or ""):
            opts["http_headers"]["Cookie"] = cookie
        if proxy:
            opts["proxy"] = proxy
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False), ""
    except Exception as e:  # noqa: BLE001
        return None, _clean_err(str(e))
    finally:
        if cookie_path:
            try:
                import os as _os

                _os.remove(cookie_path)
            except OSError:
                pass


def list_items(
    url: str,
    cookie: Optional[str] = None,
    proxy: Optional[str] = None,
    max_items: int = DEFAULT_ITEMS,
) -> Dict:
    """展开一份播放列表。

    返回 ``{"ok", "is_playlist", "title", "items": [{"index","title","url"}],
    "count", "skipped", "truncated", "limit", "error"}``。``items`` 里的 url 是
    **单条视频**地址，可以直接交给 ``POST /api/tasks``（yt_page 通道）。
    """
    url = (url or "").strip()
    if not url:
        return {"ok": False, "is_playlist": False, "title": "", "items": [],
                "count": 0, "error": "缺少链接"}
    cap = max(1, min(MAX_ITEMS, int(max_items or DEFAULT_ITEMS)))
    hit = _CACHE.get((url, cap))
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]

    info: Optional[dict] = None
    err = ""
    binp = None
    try:
        binp = _bin_module().binary_path()
    except Exception:  # noqa: BLE001
        binp = None
    if binp:
        cookie_path = _cookiefile(cookie, _host_of(url))
        try:
            # cap + 1：多取一条只为了知道「后面还有没有」。拿不到第 cap+1 条
            # 说明清单正好这么长，界面就不该报「被截断」。
            info, err = _bin_module().flat_playlist_json(
                binp, url, cookie_path=cookie_path, proxy=proxy,
                items_limit=cap + 1,
            )
            err = _clean_err(err)
        except Exception as e:  # noqa: BLE001
            info, err = None, _clean_err(str(e))
        finally:
            if cookie_path:
                try:
                    import os as _os

                    _os.remove(cookie_path)
                except OSError:
                    pass
    if info is None:
        info, fb_err = _via_python(url, cookie, proxy, limit=cap + 1)
        # 两条路的失败原因只留第一个：二进制才是主路，它报的
        # 「Sign in to confirm you're not a bot」才是用户该看到的那句；
        # python 模块那句「yt-dlp 不可用（3.9）」只是本机环境的边角信息。
        if info is None and not err:
            err = fb_err

    if info is None:
        return {"ok": False, "is_playlist": False, "title": "", "items": [],
                "count": 0, "error": err or "展开播放列表失败"}

    shaped = _shape_result(info, url, cap)
    shaped["count"] = len(shaped.get("items") or [])
    shaped["limit"] = cap
    shaped.setdefault("skipped", 0)
    shaped.setdefault("truncated", False)
    if shaped.get("ok"):
        # 失败不缓存：用户改登录状态/换网络后应该立刻能再试一次
        _CACHE[(url, cap)] = (time.time(), shaped)
    return shaped
