"""N_m3u8DL-RE 可执行文件的封装（HLS / DASH 下载引擎）。

为什么不自己写 m3u8 引擎，也不继续只用 ffmpeg：
    ffmpeg 只会「从头跟着 playlist 拉」——挑不了码率/音轨/字幕，一个分片
    出错就整条重来，进度只有「已播时长」这一个近似值。N_m3u8DL-RE 按分片
    并发取、单片失败只重取那一片、断点续传靠临时目录里的分片文件，这些
    正是 Ghost Downloader 用它的原因。

约定（与 ytdlp_bin 相同）：本模块只依赖标准库，不 import 项目内其它模块，
避免循环导入，也方便被 `spec_from_file_location` 直接加载。

输出解析有个坑：引擎的进度条写在自己的终端渲染器里，**stdout 被重定向时
它既不写 \\r 也不写 \\n**，所有进度帧直接粘成一串（见 parse_progress）。所以
读输出必须用 os.read 按可用字节取，不能按行迭代。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from typing import Callable, Dict, List, Optional, Tuple

# 二进制可能放在这些位置（源码树 / bundle 压平后都兼容）
_PROBE_SUBDIRS = ("bin", "vendor")
_BINARY_NAMES = ("N_m3u8DL-RE", "N_m3u8DL-RE.exe", "m3u8dl", "N_m3u8DL_RE")

# 一帧进度长这样（三种形态都实测到过，分隔符时有时无）：
#   ... 0/6 0.00% -0.00Bps --:--:--
#   ... 4/6 66.67% 5.23MB/7.85MB512.00KBps00:00:04
#   ...6/6 100.00%5.72MB - 00:00:00
_FRAME_RE = re.compile(r"(?P<done>\d+)/(?P<total>\d+)\s+(?P<pct>\d+(?:\.\d+)?)%")
# 直播帧和点播帧**不是一个语法**（同一版引擎实测）：
#   Vid Kbps 00m00s/00m24s 1/12 Recording   8% 0.00Bps
#   Vid Kbps 00m32s/00m32s16/16 Waiting  100% -
# 前半是「已录时长 / 当前窗口预估时长」，没有字节数对，剩余时间也不是 HH:MM:SS；
# 只按 _FRAME_RE 找「分片数 空格 百分比」会一路 miss（分片和百分比之间隔着
# Recording/Waiting），直播任务的进度就永远停在 0。state 这个词是唯一的锚点：
# 点播的横幅「Vid Kbps | 12 Segments | ~00m24s」里也有 00m24s，别拿时长当锚。
_LIVE_FRAME_RE = re.compile(
    r"(?P<h>\d+)m(?P<m>\d+)s\s*/\s*(?P<wh>\d+)m(?P<wm>\d+)s\s*"
    r"(?P<done>\d+)/(?P<total>\d+)\s+(?P<state>Recording|Waiting)\s*"
    r"(?P<pct>\d+(?:\.\d+)?)%"
)
_SIZE_PAIR_RE = re.compile(
    r"(?P<got>[\d.]+)\s*(?P<got_u>[KMG]?B)\s*/\s*(?P<whole>[\d.]+)\s*(?P<whole_u>[KMG]?B)"
)
_SPEED_RE = re.compile(r"(?P<v>-?[\d.]+)\s*(?P<u>[KMG]?B)ps")
_ETA_RE = re.compile(r"(?P<h>\d\d?):(?P<m>\d\d):(?P<s>\d\d)")
# 一帧的数字区到哪里为止：下一帧的 "Vid Kbps" 横幅，或引擎自己的日志时间戳
# （必带毫秒，因此不会和 HH:MM:SS 形式的剩余时长混淆）。不收尾就会把
# "20:10:40.027" 这样的时钟读成「还剩 20 小时」。
_WINDOW_STOP_RE = re.compile(r"Vid |\d\d:\d\d:\d\d\.\d\d\d")
# 引擎的日志行：文本消息本身带换行，但会被进度帧从中间粘住，所以整串扫。
# 文案收到「下一句日志的时间戳」为止：引擎的 WARN 后面经常没有换行，下一条
# 时间戳直接粘在同一行上（实测 404 重试 10 次全是粘住的），若让 msg 贪婪吃满
# 300 字符，第一条 WARN 会把后面 9 条连同级别名一起吞掉。
_LOG_HEAD = r"\d\d:\d\d:\d\d\.\d\d\d\s+(?:INFO|DEBUG|WARN|ERROR)\s*:"
_MSG_RE = re.compile(
    r"(?P<lvl>ERROR|WARN)\s*:\s*(?P<msg>(?:(?!" + _LOG_HEAD + r")[^\n]){0,300})"
)
# 引擎的进度条带 ANSI 控制序列（关掉颜色时仍可能有光标显隐序列粘在文案尾部）。
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
# 合流阶段的锚点。两种写法都要认（实测同一版引擎）：
#   不混流时：20:12:14.035 INFO : ffmpeg merging...
#   开了 -M 后：21:56:32.747 WARN : Muxing to x.MUX.mp4
# 这时进度帧停在最后一帧的分片百分比上不再变化，调用方需要知道「不动了 ≠ 卡住了」。
_MERGE_RE = re.compile(r"ffmpeg merging|Muxing to")
_MERGE_MSG = "ffmpeg merging..."
# 分离轨的混流目标（-M format=）。只认 mkv/mp4/ts 三种容器，选 mp4：
# 和 ffmpeg 兜底通道的产物一致，QuickTime/浏览器也能直接放。
MUX_FORMAT = "mp4"
# 直播录制固定加这三个：--live-real-time-merge 让引擎把分片**边录边追加**到
# 一个 .ts，--live-keep-segments false 让它别再把每片另存一份。
# 为什么非它不可（实测）：直播时传 -M，引擎只在收工那一刻合流，中途
# SIGTERM / SIGKILL 之后保存目录里一个文件都没有（分片只剩在临时目录），
# 用户点「停止录制」等于把这几分钟内容扔掉；而实时合并留下的 .ts 被 -9 杀掉
# 也还是完整的，ffmpeg remux 成 mp4 零警告、时长与录到的内容一致。
# 代价是直播不能靠引擎自己封容器，收尾由 core 用 ffmpeg 做。
LIVE_ARGS = ("--live-real-time-merge", "--live-keep-segments", "false")
# --live-record-limit 的取值上限：引擎按内容时长计，且要到「首个批次取完」
# 才判，所以短于站点播放列表窗口的上限不会生效（录到的仍是整个窗口）。
LIVE_LIMIT_MAX = 24 * 3600
# 引擎自己认出直播时写的那一行（实测：`WARN : Live stream found`）。
# 只认这一句，不认「文案里出现 live stream」：引擎在参数报错、帮助回显里也提
# live stream，按子串匹配会把一个正常点播任务翻成直播，收尾走错分支、
# 报出「直播没有录到任何内容」这种把人带偏的原因。
LIVE_NOTICE_RE = re.compile(r"live\s+stream\s+(found|detected)", re.IGNORECASE)


def is_live_notice(text: object) -> bool:
    """一条引擎 WARN 是否是「引擎判定这是直播流」。"""
    return bool(LIVE_NOTICE_RE.search(str(text or "")))

_UNITS = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3}


def _no_window() -> dict:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}


def bundle_bin_dir() -> str:
    """随包二进制所在目录（PyInstaller 是 _internal/bin，源码树是 zdm/bin）。"""
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "bin")


def binary_path() -> Optional[str]:
    """找到可用的 N_m3u8DL-RE 可执行文件，找不到返回 None（调用方退回 ffmpeg）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    for root in (here, os.path.dirname(here)):
        for sub in _PROBE_SUBDIRS:
            for name in _BINARY_NAMES:
                p = os.path.join(root, sub, name)
                if os.path.isfile(p) and os.access(p, os.X_OK):
                    return p
    for name in _BINARY_NAMES:
        p = shutil.which(name)
        if p:
            return p
    return None


def work_dir(task_id: str) -> str:
    """某个任务的分片暂存目录，跨暂停/恢复保持不变。

    引擎的续传全靠这个目录里的 <序号>.<ext> 分片：进程被杀掉后再跑同样的
    命令，已下完的分片直接跳过（实测 5.7MB 的片段续传只用 0.13 秒收尾）。所以
    它不能放在系统临时目录里 —— 系统会按「久未访问」清理，用户重启电脑也
    可能被清掉，那样每次恢复都等于从头下。

    路径规则和 manager.config_dir() 一致；这里不 import manager 是因为本
    模块约定只依赖标准库（manager 反过来 import core，core 又 import 本模块）。
    """
    home = os.path.expanduser("~")
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
        base = os.path.join(base, "ZDM")
    elif sys.platform == "darwin":
        base = os.path.join(home, "Library", "Application Support", "ZDM")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
        base = os.path.join(base, "zdm")
    return os.path.join(base, "re-work", str(task_id))


def drop_work_dir(task_id: str) -> None:
    """取消任务时删掉分片暂存（下载成功时引擎自己会删，见 --del-after-done）。"""
    try:
        shutil.rmtree(work_dir(task_id), ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass


# 引擎的选择器值是一段「子键=值」串（冒号分隔），或整体简写 all / best / best2 / worst1。
# 键名照 --morehelp 里列的那组来；写错键名（lagu="eng"）引擎不报错，只是命中 0 条，
# 然后甩一句英文异常退出 —— 所以在入口就按这份表拒掉，比让用户猜引擎的语法有用。
SELECTOR_KEYS = frozenset((
    "id", "lang", "name", "codecs", "res", "frame", "channel", "range", "url", "role",
    "for", "segsmin", "segsmax", "pkdmin", "pkdmax", "plstdurmin", "plstdurmax",
    "bwoff", "bwmin", "bwmax",
))
_SELECTOR_SHORT_RE = re.compile(r"^(?:all|best\d*|worst\d*)$", re.I)
_SELECTOR_PAIR_RE = re.compile(r'^(?P<k>[A-Za-z]+)=(?P<v>.+)$')
# 归一空白专用：和上面那条的差别是**允许等号两侧有空格**（值里的空格保留，
# name="Product Info" 这类轨道名本来就带空格）。check_tracks 用的是严格版，
# 先由 clean_track 把空白抹掉，才轮到它判定语法。
_PAIR_SP_RE = re.compile(r'^(?P<k>[A-Za-z_]+)\s*=\s*(?P<v>.+?)\s*$')
TRACK_LEN_LIMIT = 200
TRACK_FIELDS = ("video", "audio", "subtitle")
SUB_FORMATS = ("", "SRT", "VTT")


def clean_track(value: object) -> str:
    """去掉轨道选择里各处可有可无的空白：' res = "1280" : lang = "eng" ' → 'res="1280":lang="eng"'。

    引擎是拿整个值去比对轨道属性的，'res = "1280"' 在它眼里不是 'res="1280"'，
    结果就是「命中 0 条轨道 + 一句 No stream found to download」。用户在输入框
    里多打一个空格是很常见的事，这一层空白必须在交给引擎之前抹掉。
    """
    text = str(value or "").strip()
    if not text or _SELECTOR_SHORT_RE.match(text):
        return text
    parts: List[str] = []
    for seg in text.split(":"):
        seg = seg.strip()
        if not seg:
            continue
        m = _PAIR_SP_RE.match(seg)
        if m:
            seg = "%s=%s" % (m.group("k"), m.group("v"))
        parts.append(seg)
    return ":".join(parts)


def check_tracks(tracks: Dict[str, str]) -> str:
    """校验轨道选择，返回中文错误说明；空串表示通过。

    这里只管**语法**（能不能被引擎理解、键名有没有拼错、有没有塞进换行/控制字符），
    不管轨道在不在 —— 在不在要联网解析播放列表才知道，那是引擎的活。
    """
    tracks = tracks or {}
    for field in TRACK_FIELDS:
        value = str(tracks.get(field) or "").strip()
        if not value:
            continue
        if len(value) > TRACK_LEN_LIMIT:
            return "%s 轨道选择太长（%d 字，上限 %d）" % (field, len(value), TRACK_LEN_LIMIT)
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            return "%s 轨道选择不能包含换行或控制字符" % field
        if _SELECTOR_SHORT_RE.match(value):
            continue
        bad = []
        for part in value.split(":"):
            part = part.strip()
            if not part:
                continue
            m = _SELECTOR_PAIR_RE.match(part)
            if not m or m.group("k").lower() not in SELECTOR_KEYS:
                bad.append(part)
        if bad:
            return ("%s 轨道选择看不懂：%s。写法应是 子键=值（多个用冒号分隔），"
                    "可用子键如 id / lang / name / codecs / res / for，"
                    "或整体简写 all / best / best2 / worst1") % (field, "、".join(bad[:3]))
    fmt = str(tracks.get("sub_format") or "").strip().upper()
    if fmt not in SUB_FORMATS:
        return "字幕格式只能是 SRT 或 VTT，不能是 %r" % fmt
    return ""


def live_hms(seconds: object) -> str:
    """把秒数写成 --live-record-limit 要的 HH:mm:ss；0 / 无效值返回空串（不限时长）。"""
    try:
        n = int(float(seconds))
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    n = min(n, LIVE_LIMIT_MAX)
    return "%02d:%02d:%02d" % (n // 3600, n % 3600 // 60, n % 60)


def build_cmd(
    binp: str,
    url: str,
    *,
    save_dir: str,
    save_name: str,
    tmp_dir: str,
    headers: Optional[Dict[str, str]] = None,
    proxy: str = "",
    threads: int = 0,
    rate_limit: int = 0,
    ffmpeg: str = "",
    tracks: Optional[Dict[str, str]] = None,
    live: bool = False,
    live_limit: int = 0,
    extra: Optional[List[str]] = None,
) -> List[str]:
    """拼一条「下到 save_dir/save_name.<容器>，分片放 tmp_dir」的命令。

    --ui-language en-US：解析锚点（"ffmpeg merging..." / "Done" / ERROR 文案）
    按中文匹配会随上游改词失效；英文文案更稳定，也便于原样写进
    download-errors.log（用户看到的仍是中文，那是引擎自己的本地化，不受影响）。

    tracks 给了任一选择器时**必须撤掉 --auto-select**：实测两者同传时引擎照旧按
    auto-select 的结果下，显式的 -sv/-sa/-ss 被静默忽略 —— 用户勾了「只要西班牙语」
    却拿到默认音轨，比报错更难查。

    live=True 时按直播录制拼参数（见下面 LIVE_ARGS 那段）：不发 -M、发实时合并，
    live_limit 是可选的内容时长上限（秒）。
    """
    tracks = tracks or {}
    sel = {k: str(v or "").strip() for k, v in tracks.items() if str(v or "").strip()}
    cmd = [
        binp,
        url,
        "--tmp-dir",
        tmp_dir,
        "--save-dir",
        save_dir,
        "--save-name",
        save_name,
        # 引擎默认往**当前工作目录**写 Logs/<时间戳>.log，一次一个、永不回收。服务
        # 的工作目录是安装目录里的 bin/，所以不传这个参数的话，装机目录每下一次
        # HLS 就多一个没人看的文件（两天能攒一百多个）。定向到任务自己的分片目录：
        # 失败时日志和分片摆在一起，正是会去看它的时候；删任务时随目录一起清掉。
        # 用户看到的失败原因另有去处（ZDM 自己的 download-errors.log），不依赖这份。
        "--log-file-path",
        os.path.join(tmp_dir, "engine.log"),
        "--disable-update-check",
        "--ui-language",
        "en-US",
    ]
    if sel:
        for key, flag in (
            ("video", "-sv"),
            ("audio", "-sa"),
            ("subtitle", "-ss"),
        ):
            if key in sel:
                cmd += [flag, sel[key]]
    else:
        # 没点名轨道时保持步骤 1 的行为：自动挑最佳，与 ffmpeg 通道等价。
        cmd.append("--auto-select")
    fmt = str(tracks.get("sub_format") or "").strip().upper()
    if fmt in ("SRT", "VTT"):
        cmd += ["--sub-format", fmt]
    # 容器怎么封，直播和点播是两条路：
    #   点播：-M format=mp4，引擎把分离出来的音轨/字幕混进一个文件并清掉散件。
    #         不开 -M 会产出「没声的 mp4 + 几个 .m4a/.srt」（1.17.2 实测）。
    #   直播：不能开 -M（合流只在收工那一刻做，中途停止 = 什么都没留下），改发
    #         LIVE_ARGS，由 core 在录制结束后把不断变长的 .ts 封成 mp4。
    if live:
        cmd += list(LIVE_ARGS)
        hms = live_hms(live_limit)
        if hms:
            cmd += ["--live-record-limit", hms]
    else:
        cmd += ["-M", "format=" + MUX_FORMAT]
    if threads > 0:
        cmd += ["--thread-count", str(max(1, min(int(threads), 64)))]
    if rate_limit > 0:
        # 帮助文本写的是「Mbps 或 Kbps」，实测这版的 K 是**千字节/秒**：
        # --max-speed 250K 时引擎自己渲染的速度就是 272.00KBps（当成 kbit 只有
        # 31KB/s）。按实测换算，否则「设了 400KB/s、实际根本没限速」。
        kbytes = max(1, int(rate_limit / 1024))
        cmd += ["--max-speed", "%dK" % kbytes]
    if ffmpeg:
        cmd += ["--ffmpeg-binary-path", ffmpeg]
    if proxy:
        cmd += ["--custom-proxy", proxy]
    else:
        # 和内置引擎 / ffmpeg 通道保持一致：那两个都不走系统代理。这里如果
        # 让 .NET 按默认值去读系统代理，同一份 m3u8 换个引擎就换一条出口，
        # 「换个引擎突然 403」这种没法解释的差别必须掐掉。
        cmd += ["--use-system-proxy", "false"]
    for k, v in (headers or {}).items():
        if v:
            cmd += ["-H", "%s: %s" % (k, v)]
    cmd += extra or []
    return cmd


_EXTM3U_RE = re.compile(r"^\s*#EXTM3U\b")
_ENDLIST_RE = re.compile(r"^\s*#EXT-X-ENDLIST\b", re.M)
_STREAM_INF_RE = re.compile(r"^\s*#EXT-X-STREAM-INF\b", re.M)


def _open_playlist(url: str, headers: Optional[Dict[str, str]], proxy: str, timeout: float) -> str:
    import urllib.request

    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {})]
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url)
    for k, v in (headers or {}).items():
        if v:
            req.add_header(str(k), str(v))
    resp = opener.open(req, timeout=timeout)
    try:
        body = resp.read(1 << 20)
    finally:
        try:
            resp.close()
        except Exception:  # noqa: BLE001
            pass
    return body.decode("utf-8", "ignore")


def _live_from_media(text: str) -> Optional[bool]:
    """媒体清单（列分片那种）→ 是不是直播；不像清单返回 None。

    判据和引擎一致：没有 #EXT-X-ENDLIST 就是「还在播」。
    """
    if not text or not _EXTM3U_RE.match(text.lstrip()) or "#EXTINF" not in text:
        return None
    return not _ENDLIST_RE.search(text)


def _first_variant(text: str) -> str:
    """master 清单里第一条变体的地址（可能是相对路径）；没有返回空串。"""
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if _STREAM_INF_RE.match(ln):
            for nxt in lines[i + 1:]:
                nxt = nxt.strip()
                if nxt and not nxt.startswith("#"):
                    return nxt
    return ""


def probe_live(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    proxy: str = "",
    timeout: float = 8.0,
) -> Optional[bool]:
    """取一次播放列表，判断它是不是「还在播」：True=直播 / False=点播 / None=问不出来。

    为什么必须在拼命令**之前**问：引擎自己判直播的依据就是清单里有没有 ENDLIST，
    而直播和点播要用两套不同的参数（直播不能发 -M，点播不发 -M 会产出没声的散件）。
    等引擎在 stdout 里说 "Live stream found" 再改参数已经来不及 —— 命令早发出去了。
    这一步多花一个几百字节的 GET，换的是「停止录制后磁盘上到底有没有成品」。

    master（多码率）清单本身永远不写 ENDLIST，直接按它判会把所有 HLS 都误认成直播，
    所以跟进第一条变体再看。取不到、看不懂一律返回 None，调用方按点播处理 ——
    那是这一版之前的行为，不会因为探查失败把事情弄得更糟。
    """
    from urllib.parse import urljoin

    try:
        text = _open_playlist(url, headers, proxy, timeout)
    except Exception:  # noqa: BLE001
        return None
    direct = _live_from_media(text)
    if direct is not None:
        return direct
    if not _STREAM_INF_RE.search(text):
        return None
    variant = _first_variant(text)
    if not variant:
        return None
    try:
        sub = _open_playlist(urljoin(url, variant), headers, proxy, timeout)
    except Exception:  # noqa: BLE001
        return None
    return _live_from_media(sub)


def parse_progress(text: str) -> Optional[dict]:
    """从（可能粘连成一串的）输出里取**最后一帧**进度。

    点播帧返回 {"done","total","pct","downloaded","total_bytes","speed","eta"}；
    直播帧另外带 {"live":True,"recorded":墙钟秒,"state":"Recording"/"Waiting"}，
    且没有字节对和剩余时间（那两个键给 0/None）。没有可辨认的帧时返回 None。
    done/total 是分片数，pct 是引擎算好的百分比；
    total_bytes 只是引擎按当前均速外推的估计，不能当文件真实大小用。
    """
    if not text:
        return None
    lm = None
    for lm in _LIVE_FRAME_RE.finditer(text):
        pass
    if lm is not None:
        # 直播的 pct 是「窗口占满度」（追上边缘时就是 100%），当进度用会显示成
        # 「已经下完了」；真正有用的是已录时长，调用方要看 recorded。
        window = text[lm.end(): lm.end() + 48]
        stop = _WINDOW_STOP_RE.search(window)
        if stop:
            window = window[: stop.start()]
        speed = 0.0
        pm = _SPEED_RE.search(window)
        if pm:
            speed = abs(_to_bytes(pm.group("v"), pm.group("u")))
        return {
            "live": True,
            "state": lm.group("state"),
            "recorded": int(lm.group("h")) * 60 + int(lm.group("m")),
            "done": int(lm.group("done")),
            "total": int(lm.group("total")),
            "pct": float(lm.group("pct")),
            "downloaded": 0,
            "total_bytes": 0,
            "speed": speed,
            "eta": None,
        }
    m = None
    for m in _FRAME_RE.finditer(text):
        pass
    if m is None:
        return None
    window = text[m.end(): m.end() + 48]
    stop = _WINDOW_STOP_RE.search(window)
    if stop:
        window = window[: stop.start()]
    downloaded = total_bytes = 0.0
    sm = _SIZE_PAIR_RE.search(window)
    if sm:
        downloaded = _to_bytes(sm.group("got"), sm.group("got_u"))
        total_bytes = _to_bytes(sm.group("whole"), sm.group("whole_u"))
    speed = 0.0
    pm = _SPEED_RE.search(window)
    if pm:
        # 起始帧渲染成 "-0.00Bps"：那个减号是进度条补位的残留，不是负速度。
        speed = abs(_to_bytes(pm.group("v"), pm.group("u")))
    eta = None
    em = _ETA_RE.search(window)
    if em:
        # 起始帧是 "--:--:--"（正则不会命中）；引擎给的剩余时长直接能用。
        eta = int(em.group("h")) * 3600 + int(em.group("m")) * 60 + int(em.group("s"))
    return {
        "done": int(m.group("done")),
        "total": int(m.group("total")),
        "pct": float(m.group("pct")),
        "downloaded": int(downloaded),
        "total_bytes": int(total_bytes),
        "speed": speed,
        "eta": eta,
    }


def _to_bytes(value: str, unit: str) -> float:
    try:
        return float(value) * _UNITS.get((unit or "B").upper(), 1)
    except (TypeError, ValueError):
        return 0.0


def scan_messages(text: str) -> List[Tuple[str, str]]:
    """输出里的 (级别, 文案) 列表，级别是 "ERROR"、"WARN" 或 "MERGE"。

    "MERGE" 不是引擎的日志级别，是命中合流锚点后补出来的信号：调用方要靠它
    把任务标成「合并中」，否则用户只看到进度停在最后一帧的百分比上不动。
    """
    out: List[Tuple[str, str]] = []
    for m in _MSG_RE.finditer(text):
        msg = _ANSI_RE.sub("", m.group("msg")).strip().split("Vid ")[0].strip()
        if msg and (m.group("lvl"), msg) not in out:
            out.append((m.group("lvl"), msg))
    if _MERGE_RE.search(text):
        out.append(("MERGE", _MERGE_MSG))
    return out


_INNER_EXC_RE = re.compile(r"-{2,4}>")


def fatal_reason(text: str) -> str:
    """退出码非 0、但引擎一条 ERROR 都没打时，从输出尾部找真正的死因。

    实测最常见的一条失败路径（播放列表 404）就是这个形状：先是每轮重试一条
    WARN，最后把 .NET 异常直接甩在 stdout 上，级别行一个都没有 ——

        Unhandled exception: System.Exception: Failed to execute action after 10 retries.
         ---> System.Net.Http.HttpRequestException: Response status code does not
        indicate success: 404 (File not found).
           at System.Net.Http...

    只回「引擎退出码 1」用户什么也看不出来，所以取异常首行，外加 ---> 那句
    根因（堆栈从 "at " 开始，一律不收）。都没有时退回最后一条 WARN。
    """
    if not text:
        return ""
    i = text.rfind("Unhandled exception:")
    if i >= 0:
        lines = [l.strip() for l in _ANSI_RE.sub("", text[i:]).split("\n") if l.strip()]
        parts = [lines[0][len("Unhandled exception:"):].strip()]
        # 根因在紧接着一行的 "---> <异常类型>: <原因>" 里；再往下就是纯堆栈。
        inner = lines[1] if len(lines) > 1 else ""
        if _INNER_EXC_RE.match(inner):
            parts.append(inner.lstrip("- >").strip())
        return "；".join(p for p in parts if p)[:600]
    warns = [msg for lvl, msg in scan_messages(text) if lvl == "WARN"]
    return warns[-1][:600] if warns else ""


def download(
    cmd: List[str],
    *,
    on_progress: Optional[Callable[[dict], None]] = None,
    on_message: Optional[Callable[[str, str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    on_proc: Optional[Callable[[object], None]] = None,
) -> Tuple[int, str]:
    """跑一次引擎下载（同步阻塞）。返回 (退出码, 引擎报的失败原因)。

    失败原因优先取 ERROR 文案；一条 ERROR 都没有时（引擎最常见的死法就是这样）
    退回输出尾部的异常首行 / 最后一条 WARN，见 fatal_reason。

    读输出用 os.read 而不是 `for line in stdout`：进度帧之间没有换行，按行读
    要等缓冲区填满才返回，暂停点了半天没反应、进度也长时间不动。

    消息和进度都从同一块滚动尾部里扫，不看单个 chunk：日志行会被进度帧从中间
    粘住，锚点也可能正好被 8192 字节的读取边界切开。同一个 (级别, 文案) 只上报
    一次（尾部重复扫描），ERROR 文案另存一份用于失败原因。
    """
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            bufsize=0,
            **_no_window(),
        )
    except OSError as e:
        return 1, "无法启动流媒体引擎：%s" % e

    if on_proc:
        on_proc(proc)
    fd = proc.stdout.fileno()
    tail = b""
    seen: List[Tuple[str, str]] = []
    errors: List[str] = []
    last_text = ""
    try:
        while True:
            try:
                chunk = os.read(fd, 8192)
            except OSError:
                break
            if not chunk:
                break
            tail = (tail + chunk)[-16384:]
            text = _ANSI_RE.sub("", tail.decode("utf-8", "ignore"))
            last_text = text
            for lvl, msg in scan_messages(text):
                if (lvl, msg) in seen:
                    continue
                seen.append((lvl, msg))
                if on_message:
                    on_message(lvl, msg)
                if lvl == "ERROR":
                    errors.append(msg)
            frame = parse_progress(text)
            if frame and on_progress:
                on_progress(frame)
            if should_stop and should_stop():
                proc.terminate()
                break
    finally:
        try:
            proc.stdout.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(timeout=20)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
    rc = proc.returncode if proc.returncode is not None else 0
    reason = "；".join(errors[-3:])
    if rc != 0 and not reason:
        reason = fatal_reason(last_text)
    return rc, reason[:600]
