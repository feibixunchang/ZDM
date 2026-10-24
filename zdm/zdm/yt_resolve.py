"""YouTube format resolution via yt-dlp (the robust, desktop-side approach).

Why desktop-side: YouTube serves most adaptive streams as signatureCipher /
n-parameter protected URLs that can only be expanded by running the site's
player JS. Doing that inside a browser extension is brittle (base.js changes
weekly, CORS, login-gated). ghostdownload and similar tools instead resolve
formats on the desktop with yt-dlp and let the extension just forward the
already-decrypted URLs. We mirror that: the extension asks this endpoint for a
video URL, we return plain, downloadable stream URLs (video paired with the
best audio track so the engine can mux them).

Two things matter for actually getting formats out of YouTube today:

1. **Login state.** Anonymous requests from a normal residential IP are often
   answered with "Sign in to confirm you're not a bot". The extension forwards
   the browser's cookies (including the HttpOnly ones) as a Netscape cookie
   file; that makes the request look like the user's own logged-in session.

2. **Player client.** yt-dlp's default client set changes between releases and
   YouTube restricts different clients differently at any given time. Rather
   than betting on one, we try a short list of client configurations and keep
   the first one that yields real video streams. This is what makes the
   difference between "only a crippled 360p entry" and the full ladder.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import time
from typing import Dict, List, Optional, Tuple

# yt-dlp is bundled under the app so the download engine has no network/runtime
# dependency on pip being present on the user's machine. The app flattens the
# python package (zdm/zdm/ -> Resources/zdm/), so yt_dlp_pkg may sit one or two
# directories above this file depending on whether we're in the source tree or
# the bundled app. Probe both.
def _yt_dlp_dir():
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (
        os.path.join(here, "yt_dlp_pkg"),
        os.path.join(os.path.dirname(here), "yt_dlp_pkg"),
    ):
        if os.path.isdir(os.path.join(cand, "yt_dlp")):
            return cand
    return os.path.join(here, "yt_dlp_pkg")


def _import_yt_dlp():
    d = _yt_dlp_dir()
    if d not in sys.path:
        sys.path.insert(0, d)
    import yt_dlp  # noqa: F401

    return yt_dlp


# Client configurations, tried in order.
#
# 顺序不是随手排的，是实测出来的（2026-10，随包 yt-dlp 2026.08.19）：
#   - default 给完整阶梯（144~2160 + 3 条纯音频），直链可直接下载；
#   - tv_embedded 也给完整阶梯，是默认客户端被拦时的可靠替补；
#   - tv / ios / mweb / web_safari / web_embedded 大多直接报
#     "The page needs to be reloaded" 或 "Requested format is not available"；
#   - android / android_vr 已经退化：只回 5 条格式（最高 360p），
#     而且用它的直链下载必 403 —— 不能再当第一选择（旧版本正好相反，
#     这就是「Mac 能下、Linux/Windows 不能」的其中一个原因）。
_STRATEGIES: List[Tuple[str, Optional[List[str]]]] = [
    ("default", None),
    ("tv_embedded", ["tv_embedded"]),
    ("android", ["android"]),
    ("android_vr", ["android_vr"]),
]

# Bounded so a wedged attempt cannot hang the extension's menu forever.
_TOTAL_BUDGET = 40.0

# 「够好了」的判据：至少 3 个不同高度，且最高不低于 720p。
# 不够就继续试下一个客户端，最后把所有客户端的结果合并 —— 宁可多花几秒
# 也要给用户完整阶梯，而不是一条 360p 就收工。
_ENOUGH_TIERS = 3

# Small TTL cache: the menu can be opened several times in a row and each
# resolve costs seconds. Keyed by URL because that is what the caller has.
_CACHE: Dict[str, Tuple[float, Dict]] = {}
_CACHE_TTL = 300.0


def _bin_module():
    """取 ytdlp_bin 模块（兼容包导入与按文件路径加载两种调用方式）。"""
    try:
        from . import ytdlp_bin  # type: ignore

        return ytdlp_bin
    except Exception:  # noqa: BLE001
        import importlib.util

        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ytdlp_bin.py")
        spec = importlib.util.spec_from_file_location("_zdm_ytdlp_bin", p)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m


def _resolve_via_binary(
    binp: str, url: str, cookie: Optional[str], proxy: Optional[str],
    extra: Optional[List[str]] = None,
) -> Dict:
    """用 yt-dlp 可执行文件解析（`-J`，一次调用就能拿到完整阶梯）。

    比「逐个 player_client 试 python 模块」快得多：新版默认客户端本身就给
    完整 adaptive 列表，一次 3~5 秒；多客户端轮换是不得已的重试手段。
    """
    mod = _bin_module()
    cookie_path = _cookiefile(cookie, _host_of(url))
    try:
        info, err = mod.extract_json(
            binp, url, cookie_path=cookie_path, proxy=proxy, extra=extra
        )
    except Exception as e:  # noqa: BLE001
        info, err = None, str(e)
    finally:
        _cleanup(cookie_path)
    if not info:
        return {"ok": False, "formats": [], "title": "", "error": err or "解析失败"}
    shaped = _shape(info, "cli")
    merged = _merge(shaped.get("formats") or [], shaped.get("title") or "")
    if not merged.get("ok"):
        return {
            "ok": False,
            "formats": [],
            "title": shaped.get("title") or "",
            "error": merged.get("error") or "没有可直接下载的直链",
        }
    merged["client"] = "cli"
    return merged


def _resolve_retryable(err: str) -> bool:
    """解析失败的报错是否值得换客户端/再试一次（与下载侧同源的随机拦截）。"""
    if not err:
        return False
    e = err.lower()
    return (
        "not a bot" in e
        or "sign in to confirm" in e
        or "page needs to be reloaded" in e
        or "requested format is not available" in e
        or "没有可直接下载的直链" in err
        or "没有可用的" in err
        or "未解析到视频信息" in err
        or "解析失败" in err
    )


def _canonical_page_url(url: str) -> str:
    """把站点特有的播放弹窗 URL 归一成 yt-dlp 认得的规范页地址。

    抖音信息流的播放弹窗是 douyin.com/jingxuan?modal_id=<aweme_id>
    （/discover 等同理），yt-dlp 只认 douyin.com/video/<aweme_id>，
    原样丢过去就是 "Unsupported URL"。modal_id 就是 aweme_id，换过去即可。
    """
    # 局部 import：本模块只用这一处 urllib，且 import 放在 try 外面 ——
    # 放在里面一旦拼错名字，NameError 会被下面的 except 吞掉、静默返回原 URL。
    import urllib.parse as up

    try:
        p = up.urlparse(url)
    except Exception:  # noqa: BLE001
        return url
    host = (p.hostname or "").lower()
    # 注意用 endswith 而不是 re.match：re.match 从头匹配，"www.douyin.com"
    # 匹配不上 "(^|\.)douyin\.com$"（JS 的 /.../.test 是搜索语义，没这个问题）。
    if not (host == "douyin.com" or host.endswith(".douyin.com")):
        return url
    if p.path.startswith("/video/"):
        return url
    try:
        q = up.parse_qs(p.query)
    except Exception:  # noqa: BLE001
        return url
    mid = (q.get("modal_id") or [""])[0]
    if mid.isdigit():
        return "https://www.douyin.com/video/" + mid
    return url


def resolve(url: str, cookie: Optional[str] = None, proxy: Optional[str] = None) -> Dict:
    """Resolve a YouTube watch/shorts URL into a ZDM-format list.

    Returns {"ok", "formats", "title", "error"}. Formats are dicts shaped like
    the browser adapter's output so the extension's menu code is unchanged.
    Each "video" entry carries ``audio_url`` (best audio-only stream) so the
    engine muxes sound in.
    """
    # 归一化放在缓存查询之前：同一视频的弹窗 URL 和规范 URL 应命中同一份缓存。
    url = _canonical_page_url(url)
    if os.environ.get("ZDM_DEBUG_COOKIE"):
        import sys

        domains = sorted(
            {
                ln.split("\t")[0].lstrip("#HttpOnly_.")
                for ln in (cookie or "").splitlines()
                if "\t" in ln
            }
        )
        print(
            "[cookie] host=%s len=%d netscape_domains=%s head=%r"
            % (
                _host_of(url),
                len(cookie or ""),
                domains[:8],
                (cookie or "")[:160],
            ),
            file=sys.stderr,
            flush=True,
        )
    hit = _CACHE.get(url)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]

    # 优先用独立可执行文件：app 内嵌的 Python 可能是 3.9，而新版 yt-dlp
    # 要求 3.10+，模块根本 import 不了（直接抛 unsupported version）。
    binp = None
    try:
        binp = _bin_module().binary_path()
    except Exception:  # noqa: BLE001
        binp = None
    if binp:
        # YouTube 的拦截是**按请求**随机判的（bot check / 空格式列表 /
        # page needs to be reloaded），且同一时刻不同客户端能拿到的档位差别
        # 很大。所以解析和下载一样轮换：默认 → tv_embedded → 各再试一次
        # （隔 3 秒冷却）。android_vr 已经退化（只回 360p 且直链 403），
        # 不再参与重试。
        last_bin_err = ""
        attempts = [
            (None, 0.0),
            (["--extractor-args", "youtube:player_client=tv_embedded"], 0.0),
            (None, 3.0),
            (["--extractor-args", "youtube:player_client=tv_embedded"], 3.0),
        ]
        login_blocked = 0
        for extra, delay in attempts:
            if delay:
                time.sleep(delay)
            res = _resolve_via_binary(binp, url, cookie, proxy, extra=extra)
            if res.get("ok") and res.get("formats"):
                _CACHE[url] = (time.time(), res)
                return res
            last_bin_err = res.get("error") or last_bin_err
            if not _resolve_retryable(last_bin_err):
                break
            # 两个不同客户端都要求登录/人机验证：换客户端不会变好，别让扩展
            # 菜单干等一分多钟。
            if _is_bot_check(last_bin_err):
                login_blocked += 1
                if login_blocked >= 2:
                    break
    else:
        last_bin_err = ""

    try:
        yt_dlp = _import_yt_dlp()
    except Exception as e:  # yt-dlp missing -> graceful degrade
        return {
            "ok": False,
            "formats": [],
            "title": "",
            "error": last_bin_err or f"yt-dlp 不可用：{e}",
        }

    cookie_path = _cookiefile(cookie, _host_of(url))
    deadline = time.time() + _TOTAL_BUDGET
    last_err = "未解析到视频信息"
    title = ""
    pool: List[Dict] = []
    used: List[str] = []
    try:
        for name, clients in _STRATEGIES:
            if time.time() > deadline:
                last_err = last_err or "解析超时"
                break
            opts = _base_opts(clients)
            if cookie_path:
                opts["cookiefile"] = cookie_path
            elif cookie and "\t" not in cookie and "Netscape" not in cookie:
                # A bare Cookie: header (not Netscape format) still helps some
                # clients and costs nothing.
                # Netscape 形态**不能**这么塞：它带 tab 和换行，requests
                # 校验 header 值时会抛 "Invalid header value b'# Netscape
                # HTTP Cookie File\\n...'"，整个提取直接失败。
                opts["http_headers"]["Cookie"] = cookie
            if proxy:
                opts["proxy"] = proxy
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
            except Exception as e:
                last_err = _clean_err(str(e))
                # 注意：**不要**在这里 break。人机验证是按客户端判的 ——
                # 默认客户端被要求验证时 tv_embedded 往往照常放行，
                # 一命中就放弃等于自己把后面的路全堵上了。
                continue

            result = _shape(info, name)
            if not title:
                title = result.get("title") or ""
            if result["ok"] and result["formats"]:
                # 累积而不是立即返回：单条 360p 合轨也算「成功」，但那不是
                # 用户要的东西。先并入池子，够好了才收工。
                pool.extend(result["formats"])
                used.append(name)
                tiers = _tiers(pool)
                if len(tiers) >= _ENOUGH_TIERS and max(tiers) >= 720:
                    break
            else:
                # Extracted, but no usable direct streams (age gate, members-only,
                # live HLS-only, ...). Keep the message and let the next client try.
                last_err = result.get("error") or last_err
    finally:
        _cleanup(cookie_path)

    merged = _merge(pool, title)
    if merged["ok"]:
        merged["client"] = "+".join(used)
        _CACHE[url] = (time.time(), merged)
        return merged
    return {
        "ok": False,
        "formats": [],
        "title": title,
        "error": last_err + _missing_runtime_hint(),
    }


def _missing_runtime_hint() -> str:
    """本机没有 deno/node 时，给解析失败补一句真正的原因。

    没有 JS 运行时，YouTube 的签名/n 挑战解不开，表现是「只有故事板」或
    "The page needs to be reloaded" —— 用户和扩展菜单只会显示一句英文，
    完全不知道要装什么。
    """
    try:
        mod = _bin_module()
        binp = mod.binary_path()
        if not binp or mod.js_runtime_args(binp):
            return ""
    except Exception:  # noqa: BLE001
        return ""
    return (
        "\n\n（本机没找到 deno/node 等 JS 运行时，YouTube 的签名挑战解不开。"
        "安装 deno 后重启 ZDM 即可；装到 ~/.deno/bin 或 ~/.local/bin 都能被自动找到。）"
    )


def _cookiefile(cookie: Optional[str], host: str = "") -> Optional[str]:
    """把调用方给的 cookie 变成 yt-dlp 能真正吃的 Netscape 文件。

    三种来源都要认：
      1. Netscape 文本（扩展导出的，含 tab）；
      2. 浏览器 F12 里复制的 `a=b; c=d` 请求头文本（设置里最常见）；
      3. JSON 数组（部分插件导出格式）。

    **第 2 种以前是直接塞进 http_headers["Cookie"] 的，那等于没登录**：
    YouTube 的鉴权除了 Cookie 还要 `Authorization: SAPISIDHASH ...`，而
    SAPISIDHASH 是 yt-dlp 用 cookiejar 里的 SAPISID/__Secure-3PAPISID 现算的。
    只有 Cookie 头、Jar 是空的 → 算不出授权头 → 依旧 "confirm you're not a bot"。
    这就是「明明填了 cookie 还是不行」的原因。

    ``host`` 是目标站点的主机名：header / JSON 形态的 cookie 要按它铺域，
    否则非 YouTube 站点的 cookie 不会随请求发出去（抖音会报
    "Fresh cookies are needed"）。Netscape 形态自带域，不受影响。
    """
    if not cookie:
        return None
    text = cookie.strip()
    if not text:
        return None

    if "\t" in text or "Netscape" in text:
        body = _normalize_netscape(text)
    elif text.startswith("[") or text.startswith("{"):
        body = _netscape_from_json(text, host)
    elif "=" in text:
        body = _netscape_from_header(text, host)
    else:
        return None
    if not body or body.count("\n") < 2:
        return None

    fd, path = tempfile.mkstemp(prefix="zdmyc", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(body)
    return path


def _netscape_line(domain: str, name: str, value: str) -> str:
    return "\t".join(
        [
            domain,
            "TRUE" if domain.startswith(".") else "FALSE",
            "/",
            "TRUE",
            "0",
            name,
            value,
        ]
    )


def _host_of(url: str) -> str:
    """取 URL 的主机名（小写）。给 cookie 铺域用。"""
    import urllib.parse as up

    try:
        return (up.urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return ""


def _cookie_domains(host: str) -> List[str]:
    """目标站点该铺哪个 Netscape 域。

    **绝不能返回互相嵌套的两个域**（如 .www.youtube.com + .youtube.com）：
    http.cookiejar 对二者都 match，同名 cookie 会被发送两遍，
    YouTube 直接返回一张没有 ytcfg 的页面 → yt-dlp 报
    KeyError('INNERTUBE_CONTEXT')；抖音等站点则是 "Fresh cookies are needed"。
    注册域一个条目即可覆盖所有子域，是唯一安全的基础写法。

    YouTube 额外补 ".google.com"：登录态/人机验证（SAPISIDHASH）横跨
    这两个域，只带 .youtube.com 会触发 "Sign in to confirm you're not a bot"。
    两个域互不嵌套，不会重名。
    """
    h = (host or "").strip().lower().lstrip(".")
    if not h:
        # 不知道目标站点时保持旧行为（YouTube / Google 登录态）
        return [".youtube.com", ".google.com"]
    parts = h.split(".")
    reg = "." + (".".join(parts[-2:]) if len(parts) >= 2 else h)
    doms = [reg]
    if reg in (".youtube.com", ".youtube-nocookie.com") and ".google.com" not in doms:
        doms.append(".google.com")
    return doms


def _netscape_from_header(text: str, host: str = "") -> str:
    """`a=b; c=d` → Netscape。

    域必须跟目标站点对上。以前这里固定铺 .youtube.com / .google.com，
    于是抖音 / 快手 / X / Facebook 的 cookie 根本不会随请求发出去 ——
    yt-dlp 直接报 "Fresh cookies are needed"，而 cookie 明明传了。
    """
    pairs = []
    for chunk in text.replace("\n", ";").split(";"):
        if "=" not in chunk:
            continue
        k, _, v = chunk.partition("=")
        k, v = k.strip(), v.strip()
        if k and not any(ch.isspace() for ch in k):
            pairs.append((k, v))
    if not pairs:
        return ""
    lines = ["# Netscape HTTP Cookie File"]
    for dom in _cookie_domains(host):
        for k, v in pairs:
            lines.append(_netscape_line(dom, k, v))
    return "\n".join(lines) + "\n"


def _netscape_from_json(text: str, host: str = "") -> str:
    import json

    try:
        data = json.loads(text)
    except Exception:
        return ""
    if isinstance(data, dict):
        data = data.get("cookies") or []
    if not isinstance(data, list):
        return ""
    default_dom = (_cookie_domains(host) or [".youtube.com"])[0]
    lines = ["# Netscape HTTP Cookie File"]
    for c in data:
        if not isinstance(c, dict):
            continue
        name = c.get("name") or ""
        if not name:
            continue
        dom = c.get("domain") or default_dom
        lines.append(
            "\t".join(
                [
                    ("#HttpOnly_" if c.get("httpOnly") else "") + dom,
                    "TRUE" if dom.startswith(".") else "FALSE",
                    c.get("path") or "/",
                    "TRUE" if c.get("secure") else "FALSE",
                    str(int(c.get("expirationDate") or 0)),
                    name,
                    str(c.get("value") or ""),
                ]
            )
        )
    return "\n".join(lines) + "\n" if len(lines) > 1 else ""


def _normalize_netscape(text: str) -> str:
    """Make a Netscape cookie file survivable.

    http.cookiejar asserts ``domain_specified == domain.startswith('.')`` and
    raises ``invalid Netscape format cookies file`` otherwise — which makes
    yt-dlp abort the whole extraction. Chrome exports ``domain=.youtube.com``
    with "whether a domain was specified" = FALSE if the writer is careless,
    so repair that column instead of trusting the caller.
    """
    out = ["# Netscape HTTP Cookie File"]
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        if line.startswith("#") and not line.startswith("#HttpOnly_"):
            continue  # comments (including a duplicate header) are dropped
        prefix = ""
        body = line
        if body.startswith("#HttpOnly_"):
            prefix, body = "#HttpOnly_", body[len("#HttpOnly_") :]
        parts = body.split("\t")
        if len(parts) >= 7:
            parts[1] = "TRUE" if parts[0].startswith(".") else "FALSE"
            body = "\t".join(parts)
        out.append(prefix + body)
    return "\n".join(out) + "\n"


def _cleanup(path: Optional[str]) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


def _base_opts(clients: Optional[List[str]]) -> Dict:
    opts: Dict = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "skip_download": True,
        "simulate": True,
        "noplaylist": True,
        "extract_flat": False,
        "allowed_extractors": ["youtube"],
        "nocheckcertificate": True,
        "ignoreerrors": False,
        "retries": 1,
        "socket_timeout": 15,
        "cachedir": False,
        # A desktop browser UA reads as far less suspicious than yt-dlp's own.
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 "
                "Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    }
    if clients:
        opts["extractor_args"] = {"youtube": {"player_client": clients}}
    return opts


def _clean_err(msg: str) -> str:
    msg = (msg or "").strip()
    # yt-dlp prefixes everything with "ERROR: "; drop it, the UI adds context.
    if msg.startswith("ERROR:"):
        msg = msg[6:].strip()
    return msg[:400]


def format_selector(height: int) -> str:
    """下载时给 yt-dlp 的 -f 表达式。

    优先级：H.264 + m4a（合出来的 .mp4 任何播放器都能放）→ 其它 mp4 编码
    （AV1 在 QuickTime/部分播放器里打不开）→ 任意编码 + 最佳音频
    （1440p/2160p 只有 VP9/AV1，没得挑）→ 该高度以下的合轨。
    """
    h = int(height or 0)
    if h <= 0:
        return "bv*[vcodec^=avc1]+ba[ext=m4a]/bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b"
    return (
        f"bv*[height<={h}][vcodec^=avc1]+ba[ext=m4a]/"
        f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/"
        f"bv*[height<={h}]+ba/"
        f"b[height<={h}]/"
        f"bv*[height<={h}]+ba/"
        # 最后必须有无条件兜底：解析端（多客户端合并）报的高度在下载端
        # （单个客户端）可能一档都对不上，缺了这级就报
        # 「Requested format is not available」直接失败（另一台 Windows
        # 与 Linux 实测）。宁可下到不匹配的最近档，也比失败强。
        f"b"
    )


def js_runtimes() -> Dict:
    """给 yt-dlp 找一个 JS 运行时。

    2026 年的 yt-dlp 解 YouTube 签名（n/sig）已经改成「跑页面里的 JS」，
    默认只启用 deno。而 ZDM.app 进程不继承用户 shell 的 PATH，`deno`
    在 /opt/homebrew/bin 下根本找不到 —— 找不到运行时的后果不是慢，而是
    解析直接失败。所以这里显式把常见安装位置探一遍。
    """
    import shutil

    for name in ("deno", "node", "bun"):
        for cand in (
            shutil.which(name),
            f"/opt/homebrew/bin/{name}",
            f"/usr/local/bin/{name}",
            os.path.expanduser(f"~/.{name}/bin/{name}"),
            # Deno's official installer uses ~/.deno/bin, but a plain binary
            # dropped into ~/.local/bin is just as common (and is what the
            # "install deno" hint suggests on Linux).
            os.path.expanduser(f"~/.local/bin/{name}"),
            f"/usr/bin/{name}",
        ):
            if cand and os.path.isfile(cand):
                return {name: {"path": cand}}
    return {}


def _is_bot_check(msg: str) -> bool:
    return bool(re.search(r"not a bot|Sign in to confirm|confirm you", msg or "", re.I))


# ---------------------------------------------------------------- 合并

# 兼容性排序：合并出来的文件是 .mp4（ffmpeg -c copy），H.264 在其中任何
# 播放器都能放；VP9 次之；AV1 最靠后（老设备/QuickTime 打不开）。
_CODEC_RANK = {"avc1": 0, "avc3": 0, "h264": 0, "vp9": 1, "vp09": 1, "av01": 2}


def _codec_rank(codec: str) -> int:
    c = (codec or "").lower()
    for prefix, rank in _CODEC_RANK.items():
        if c.startswith(prefix):
            return rank
    return 3


def _codec_name(codec: str) -> str:
    c = (codec or "").lower()
    if c.startswith(("avc1", "avc3", "h264")):
        return "H.264"
    if c.startswith(("vp9", "vp09")):
        return "VP9"
    if c.startswith("av01"):
        return "AV1"
    return ""


def _label_video(height: int, codec: str, muxed: bool) -> str:
    """菜单里那一行的文案。清晰度 + 编码就够了，别把 note/fps 全堆上。"""
    if muxed:
        return f"{height}P · 音视频合轨"
    name = _codec_name(codec)
    return f"{height}P · {name}" if name else f"{height}P"


def _tiers(pool: List[Dict]) -> set:
    """池子里已经有多少个不同的视频高度。"""
    return {
        int(e.get("height") or 0)
        for e in pool
        if e.get("kind") in ("video", "video+muxed") and e.get("height")
    }


def _better(a: Dict, b: Dict) -> bool:
    ra, rb = _codec_rank(a.get("codec")), _codec_rank(b.get("codec"))
    if ra != rb:
        return ra < rb
    return (a.get("bitrate") or 0) > (b.get("bitrate") or 0)


def _is_mp4_audio(e: Dict) -> bool:
    mime = (e.get("mime") or "").lower()
    return "mp4" in mime or "m4a" in mime


def _merge(pool: List[Dict], title: str) -> Dict:
    """把多个客户端拿到的格式并成一份阶梯。

    为什么不直接用某一个客户端的结果：同一时刻不同 client 能拿到的档位
    差别很大（default / tv_embedded 给全量、android_vr 只给 360p）。
    并集 + 按高度去重，既不会出现重复的「1080P」两行，也不会因为某个
    client 限流就丢掉高清档。
    """
    if not pool:
        return {
            "ok": False,
            "formats": [],
            "title": title or "",
            "error": "没拿到任何可下载的直链（可能需要登录或该视频有地区限制）",
        }

    videos: Dict[int, Dict] = {}
    muxed: Dict[int, Dict] = {}
    audios: List[Dict] = []
    streams: List[Dict] = []   # HLS / DASH 播放列表
    for e in pool:
        kind = e.get("kind")
        h = int(e.get("height") or 0)
        if kind == "audio":
            audios.append(e)
        elif kind == "video":
            cur = videos.get(h)
            if h and (cur is None or _better(e, cur)):
                videos[h] = e
        elif kind == "video+muxed":
            cur = muxed.get(h)
            if h and (cur is None or (e.get("bitrate") or 0) > (cur.get("bitrate") or 0)):
                muxed[h] = e
        elif kind in ("hls", "dash"):
            # 播放列表：不能按高度归并（常常没有 height），原样保留，
            # 交给 ffmpeg 拉。丢掉它就等于「没拿到任何可下载的直链」。
            streams.append(e)

    # 音频优先 m4a：和 H.264 视频一起 copy 进 mp4 才不会有兼容问题。
    best_audio = None
    if audios:
        best_audio = sorted(
            audios,
            key=lambda a: (0 if _is_mp4_audio(a) else 1, -(a.get("bitrate") or 0)),
        )[0]

    out: List[Dict] = []
    for h in sorted(set(videos) | set(muxed), reverse=True):
        if h in videos:
            e = dict(videos[h])
            if best_audio:
                # DASH 视频轨没有声音，必须带上配套音轨，否则下完是默片
                e["audio_url"] = best_audio.get("url")
            e["label"] = _label_video(h, e.get("codec"), False)
            out.append(e)
        else:
            e = dict(muxed[h])
            # 合轨本身就带声音，再配 audio_url 会被重复合并
            e.pop("audio_url", None)
            e["label"] = _label_video(h, e.get("codec"), True)
            out.append(e)

    # 播放列表（HLS / DASH）：一条就够，交给 ffmpeg 自己挑码率
    for e in streams:
        item = dict(e)
        item.pop("audio_url", None)
        item["label"] = "HLS 直播流" if item.get("kind") == "hls" else "DASH 自适应流"
        out.append(item)

    return {
        "ok": bool(out),
        "formats": out,
        "title": title or "YouTube 视频",
        "error": "" if out else "只拿到流清单，没有可直接下载的直链（可能需要登录或该地区不可用）",
    }


def _shape(info, client_name: str) -> Dict:
    if not info:
        return {"ok": False, "formats": [], "title": "", "error": "未解析到视频信息"}

    # A playlist URL resolves to entries; just take the first video.
    entries = info.get("entries")
    if entries:
        info = next((e for e in entries if e), info)

    if info.get("live_status") in ("is_live", "is_upcoming"):
        return {
            "ok": False,
            "formats": [],
            "title": info.get("title") or "",
            "error": "直播流暂不支持按清晰度下载",
        }

    streams = info.get("formats") or []
    audios = [
        s
        for s in streams
        if s.get("acodec") not in (None, "none")
        and s.get("vcodec") in (None, "none")
        and s.get("url")
    ]
    best_audio = None
    if audios:
        best_audio = max(audios, key=lambda s: (s.get("abr") or 0, s.get("tbr") or 0))

    out: List[Dict] = []
    for s in streams:
        vcodec = s.get("vcodec")
        acodec = s.get("acodec")
        if s.get("has_drm"):
            continue
        u = s.get("url")
        if not u or not str(u).startswith("http"):
            continue
        proto = (s.get("protocol") or "").split("+")[0]
        # HLS / DASH 的 url 指的是**播放列表**，不是媒体文件本体。它们不能
        # 直接下载，但可以整条交给 ffmpeg 拉（跟着 playlist 取分片、解密、
        # 封装成 mp4）。以前在这里被当成「非 http 直链」过滤掉，于是
        # 抖音 / 西瓜 / 大部分直播与自适应流站点一律报
        # 「没拿到任何可下载的直链」——yt-dlp 其实已经拿到了。
        if proto in ("m3u8", "m3u8_native"):
            kind = "hls"
        elif proto in ("http_dash", "dash"):
            kind = "dash"
        elif vcodec in (None, "none") and acodec in (None, "none"):
            continue
        elif vcodec not in (None, "none") and acodec not in (None, "none"):
            kind = "video+muxed"
        elif vcodec not in (None, "none"):
            kind = "video"
        else:
            kind = "audio"
        # Keep only direct https streams; skip HLS/DASH manifest wrappers.
        if proto and proto not in (
            "https", "http", "https_web", "m3u8", "m3u8_native",
            "http_dash", "dash",
        ):
            continue
        entry = {
            "label": _label(s, kind),
            "url": u,
            "kind": kind,
            "protocol": proto,
            "size": int(s.get("filesize") or s.get("filesize_approx") or 0),
            "mime": s.get("mime_type") or "",
            "width": int(s.get("width") or 0),
            "height": int(s.get("height") or 0),
            "itag": _safe_itag(s.get("format_id")),
            "audioQuality": "",
            "bitrate": int((s.get("tbr") or 0) * 1000),
            # 合并阶段要靠它判断「同一高度该留哪条」：H.264 优先
            "codec": "" if vcodec in (None, "none") else str(vcodec),
        }
        # DASH video track has no sound — pair it with the best audio stream so
        # the engine can mux them into one audible file.
        if kind == "video" and best_audio:
            entry["audio_url"] = best_audio.get("url")
        out.append(entry)

    if not out:
        return {
            "ok": False,
            "formats": [],
            "title": info.get("title") or "",
            "error": "只拿到流清单，没有可直接下载的直链（可能需要登录或该地区不可用）",
        }

    # Video by resolution high→low, audio by bitrate high→low.
    out.sort(
        key=lambda e: (e["kind"] != "video", -(e.get("height") or 0), -(e.get("bitrate") or 0))
    )

    return {
        "ok": True,
        "formats": out,
        "title": info.get("title") or "YouTube 视频",
        "error": "",
        "client": client_name,
    }


def _safe_itag(v) -> int:
    """format_id 不保证是纯数字。

    2026-10 起 YouTube 给一批音轨/视频轨发了 ``249-drc`` 这样的 DRC 变体
    （还有 sb0~sb3 故事板），``int("249-drc")`` 直接抛 ValueError ——
    而且是在拿到完整阶梯**之后**的整形阶段才炸，表现就是「明明解析到了
    却永远报错、面板大小全是播放器的假数据」。这里只取前导数字。
    """
    try:
        return int(v)
    except (TypeError, ValueError):
        m = re.match(r"(\d+)", str(v or ""))
        return int(m.group(1)) if m else 0


def _label(s, kind):
    h = int(s.get("height") or 0)
    w = int(s.get("width") or 0)
    if kind == "audio":
        abr = s.get("abr") or 0
        parts = ["音频"]
        if abr:
            parts.append(f"{abr:.0f} kbps")
        codec = (s.get("acodec") or "").split(".")[0]
        if codec:
            parts.append(codec)
        return " · ".join(parts)
    if h:
        return _label_video(h, s.get("vcodec") or "", kind == "video+muxed")
    if w:
        return f"{w}×{h or '?'}"
    return "默认"
