"""yt-dlp 独立可执行文件的封装（不依赖宿主 Python 版本）。

为什么不用 python 模块：
    ZDM.app 内嵌的是 Python 3.9，而 yt-dlp 2026.x 要求 3.10+。把新版 pip 装进
    bundle，import 的瞬间就会抛
        "You are using an unsupported version of Python."
    用户看到的就是「点下载 → 立刻失败」，连 URL 都没解析。
    官方发布的单文件可执行（yt-dlp_macos，约 35MB）自带解释器，所以解析和
    下载都改成调它 —— 这样 app 内嵌的 Python 版本是多少都不影响。

约定：本模块只依赖标准库，不 import 项目内其它模块（避免循环导入，
也方便被 `spec_from_file_location` 直接加载）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from typing import Callable, Dict, List, Optional, Tuple

# 二进制可能放在这些位置（源码树 / bundle 压平后都兼容）
_PROBE_SUBDIRS = ("bin", "vendor", "yt-dlp")
_BINARY_NAMES = ("yt-dlp", "yt-dlp_macos", "yt-dlp.exe", "yt-dlp_linux")

# yt-dlp 解 YouTube 的 JS 挑战需要外部运行时。2026 起默认只启用 deno，
# node / bun / quickjs 必须用 --js-runtimes 显式开启（见 yt-dlp --help）。
_JS_RUNTIMES = ("deno", "node", "bun", "quickjs")
_JS_FLAG_SUPPORT: Dict[str, bool] = {}


def _no_window() -> dict:
    """Windows 上不创建控制台窗口（GUI 进程拉起 yt-dlp 会弹黑窗）。

    与 runtime.silent_proc_kwargs 同义，但本模块约定只依赖标准库，
    所以在这里放一份本地实现（runtime 也不能反过来 import 本模块）。
    """
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}


def binary_path() -> Optional[str]:
    """找到可用的 yt-dlp 可执行文件。"""
    here = os.path.dirname(os.path.abspath(__file__))
    roots = [here, os.path.dirname(here)]
    for root in roots:
        for sub in _PROBE_SUBDIRS:
            for name in _BINARY_NAMES:
                p = os.path.join(root, sub, name)
                if os.path.isfile(p) and os.access(p, os.X_OK):
                    return p
    for p in (
        "/opt/homebrew/bin/yt-dlp",
        "/usr/local/bin/yt-dlp",
        os.path.expanduser("~/.local/bin/yt-dlp"),
    ):
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return shutil.which("yt-dlp")


def bundle_bin_dir() -> str:
    """Directory holding the binaries shipped inside the app (yt-dlp/ffmpeg/deno).

    PyInstaller puts them in ``_internal/bin``; a source checkout has them in
    ``zdm/bin``. Both are one ``bin`` below the bundle root / package parent.
    """
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "bin")


def _extra_bin_dirs() -> List[str]:
    """「工具装在 PATH 之外」的常见目录（deno / node 安装器默认落点）。

    Linux / Windows 上这些目录几乎永远不在 .desktop / 资源管理器启动的
    进程 PATH 里 —— 以前这里只补了 macOS 的 /opt/homebrew/bin，于是 Mac 的
    yt-dlp 能找到 deno、Linux / Windows 找不到：同一个视频在那两端解析降级
    甚至直链 403，表现就是「只有 Mac 能下」。
    """
    home = os.path.expanduser("~")
    # The bundled deno comes first: it is the exact runtime the build tested.
    own = [bundle_bin_dir()]
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "")
        roaming = os.environ.get("APPDATA", "")
        user = os.environ.get("USERPROFILE") or home
        return own + [
            os.path.join(user, ".deno", "bin"),
            os.path.join(local, "Programs", "deno"),
            os.path.join(user, ".bun", "bin"),
            os.path.join(roaming, "npm"),
            os.path.join(local, "Programs", "nodejs"),
            r"C:\Program Files\nodejs",
            os.path.join(user, "scoop", "shims"),
            os.path.join(local, "Microsoft", "WinGet", "Links"),
        ]
    if sys.platform == "darwin":
        return own + [
            "/opt/homebrew/bin",
            "/usr/local/bin",
            os.path.join(home, ".deno", "bin"),
            os.path.join(home, ".bun", "bin"),
        ]
    return own + [
        os.path.join(home, ".deno", "bin"),
        os.path.join(home, ".bun", "bin"),
        os.path.join(home, ".local", "bin"),
        "/usr/local/bin",
        "/snap/bin",
    ]


def child_env() -> Dict[str, str]:
    """给子进程补齐 PATH、剥掉代理环境变量。

    ZDM 的桌面进程不继承用户 shell 的 PATH，而 yt-dlp 要靠那里的 deno/node
    解 YouTube 的 JS 挑战、靠那里的 ffmpeg 合流。缺任何一个都会失败，所以
    在唯一的入口处补全，而不是每个调用点各补一次。

    代理**只认 config 里的显式设置**（_run_ytdlp 用 --proxy 传给 yt-dlp），
    环境变量一律剥掉：app 可能被从带代理的终端/沙箱里启动（`open -a` 会
    继承调用方环境），子进程留着 http_proxy 就会把 YouTube 的页面请求送进
    代理，拿到 consent/错误页 —— 表现为解析报
    `KeyError('INNERTUBE_CONTEXT')`，且换 yt-dlp 版本、换客户端都没用。
    内置下载通道早就以 `proxy=""` 绕过环境变量，这里对齐同一语义。
    """
    env = dict(os.environ)
    for k in (
        "http_proxy", "https_proxy", "all_proxy", "ftp_proxy",
        "no_proxy",
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY",
        "NO_PROXY",
    ):
        env.pop(k, None)
    extra = _extra_bin_dirs() + [
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
    # Bundled bin first: the shipped deno/yt-dlp/ffmpeg is the exact version
    # the release was tested with. System copies remain as a fallback.
    parts = [p for p in extra if p]
    for p in (env.get("PATH") or "").split(os.pathsep):
        if p and p not in parts:
            parts.append(p)
    env["PATH"] = os.pathsep.join(parts)
    return env


def _supports_js_flag(binp: str) -> bool:
    """yt-dlp 是否认识 ``--js-runtimes``。

    老版本（2025.11 之前）没有这个参数，传进去会直接 "no such option" 退出，
    比不传更糟。所以先问一次 --help 并缓存结果（每进程一次）。
    """
    hit = _JS_FLAG_SUPPORT.get(binp)
    if hit is not None:
        return hit
    ok = False
    try:
        p = subprocess.run(
            [binp, "--help"],
            capture_output=True,
            text=True,
            timeout=20,
            env=child_env(),
            check=False,
            **_no_window(),
        )
        ok = "--js-runtimes" in ((p.stdout or "") + (p.stderr or ""))
    except Exception:  # noqa: BLE001
        ok = False
    _JS_FLAG_SUPPORT[binp] = ok
    return ok


def find_js_runtime(env: Optional[Dict[str, str]] = None) -> str:
    """Path of the first usable JS runtime (deno/node/bun/quickjs), or ""."""
    env = env or child_env()
    for name in _JS_RUNTIMES:
        cand = shutil.which(name, path=env.get("PATH"))
        if cand and os.path.isfile(cand):
            return cand
    return ""


def js_runtime_args(binp: str, env: Optional[Dict[str, str]] = None) -> List[str]:
    """找到的 JS 运行时 → ``--js-runtimes name:path`` 参数。

    官方单文件 yt-dlp 只默认启用 deno：装了 Node.js 的 Windows 机器（很常见）
    不会被自动使用。这里把找到的都显式开启；一个都没有时返回空列表，参数表
    保持原样。
    """
    if not _supports_js_flag(binp):
        return []
    env = env or child_env()
    args: List[str] = []
    for name in _JS_RUNTIMES:
        cand = shutil.which(name, path=env.get("PATH"))
        if cand and os.path.isfile(cand):
            args += ["--js-runtimes", f"{name}:{cand}"]
    return args


def extract_json(
    binp: str,
    url: str,
    *,
    cookie_path: Optional[str] = None,
    proxy: Optional[str] = None,
    extra: Optional[List[str]] = None,
    timeout: float = 70.0,
) -> Tuple[Optional[dict], str]:
    """`yt-dlp -J`：把视频信息当 JSON 拿回来。返回 (info, 错误文案)。"""
    args = [binp, "-J", "--no-warnings", "--no-playlist"]
    args += js_runtime_args(binp)
    if cookie_path:
        args += ["--cookies", cookie_path]
    if proxy:
        args += ["--proxy", proxy]
    args += extra or []
    args.append(url)
    try:
        p = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=child_env(),
            check=False,
            **_no_window(),
        )
    except subprocess.TimeoutExpired:
        return None, "解析超时（YouTube 可能在做风控）"
    except OSError as e:
        return None, f"无法执行 yt-dlp：{e}"
    if p.returncode != 0:
        lines = [x for x in (p.stderr or "").strip().splitlines() if x.strip()]
        return None, (lines[-1] if lines else f"yt-dlp 退出码 {p.returncode}")[:400]
    try:
        return json.loads(p.stdout), ""
    except Exception as e:  # noqa: BLE001
        return None, f"yt-dlp 输出不是 JSON：{e}"


def flat_playlist_json(
    binp: str,
    url: str,
    *,
    cookie_path: Optional[str] = None,
    proxy: Optional[str] = None,
    items_limit: int = 0,
    timeout: float = 240.0,
) -> Tuple[Optional[dict], str]:
    """`yt-dlp -J --flat-playlist`：只取播放列表的**名单**，不逐条解格式。

    和 ``extract_json`` 的区别正是这条通道存在的原因：
      * ``extract_json`` 带 ``--no-playlist``，为的是「一段视频的清晰度阶梯」；
      * 展开几百集的清单如果也逐条解析，会为每条跑一遍 player 提取
        （几十分钟 + 必被 YouTube 按请求风控）。flat 一次请求只给
        id/标题/地址，格式留给之后每条任务自己解。

    ``--yes-playlist`` 是给 ``watch?v=…&list=…`` 这种「既是视频也是列表」的地址用的：
    用户点的是「按播放列表下载」，这里不能又退回单条。

    ``items_limit`` 不是「拿完之后丢掉多余的」，而是**让 yt-dlp 别去翻后面的页**：
    实测频道视频页一页只有 30 条，一次翻页约 3~4 秒（NASA 频道取 26 条 = 87 秒），
    不限就是整频道几千条一路翻到底。超时也因此按「翻清单」而不是「解一条视频」给。
    """
    args = [
        binp,
        "-J",
        "--no-warnings",
        "--flat-playlist",
        "--yes-playlist",
        "--ignore-errors",
    ]
    if items_limit and int(items_limit) > 0:
        args += ["--playlist-items", "1:%d" % int(items_limit)]
    args += js_runtime_args(binp)
    if cookie_path:
        args += ["--cookies", cookie_path]
    if proxy:
        args += ["--proxy", proxy]
    args.append(url)
    try:
        p = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=child_env(),
            check=False,
            **_no_window(),
        )
    except subprocess.TimeoutExpired:
        return None, "展开播放列表超时（清单太大或站点在风控）"
    except OSError as e:
        return None, f"无法执行 yt-dlp：{e}"
    if p.returncode != 0:
        lines = [x for x in (p.stderr or "").strip().splitlines() if x.strip()]
        return None, (lines[-1] if lines else f"yt-dlp 退出码 {p.returncode}")[:400]
    try:
        return json.loads(p.stdout), ""
    except Exception as e:  # noqa: BLE001
        return None, f"yt-dlp 输出不是 JSON：{e}"


def download(
    binp: str,
    page_url: str,
    *,
    fmt: str,
    outtmpl: str,
    ffmpeg: Optional[str] = None,
    cookie_path: Optional[str] = None,
    proxy: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
    ratelimit: int = 0,
    timeout: float = 30.0,
    extra: Optional[List[str]] = None,
    on_progress: Optional[Callable[[dict], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    on_proc: Optional[Callable[[object], None]] = None,
) -> Tuple[int, str]:
    """同步下载。进度用 on_progress 回报，暂停/取消靠 should_stop + terminate。

    进度不解析给人看的那一行（"%(progress._percent_str)s" 会被终端宽度、
    单位、颜色影响），而是用 --progress-template 输出固定格式自己拆。
    """
    args = [
        binp,
        "--no-warnings",
        "--no-playlist",
        "--newline",
        "--progress-template",
        "zdm:%(progress.status)s|%(progress.downloaded_bytes)s|"
        "%(progress.total_bytes)s|%(progress.total_bytes_estimate)s|"
        "%(progress.speed)s",
        "-f",
        fmt,
        "--merge-output-format",
        "mp4",
        "-o",
        outtmpl,
        "--socket-timeout",
        str(max(10, int(timeout or 30))),
    ]
    if ffmpeg:
        args += ["--ffmpeg-location", ffmpeg]
    args += js_runtime_args(binp)
    if cookie_path:
        args += ["--cookies", cookie_path]
    if proxy:
        args += ["--proxy", proxy]
    if ratelimit:
        args += ["--limit-rate", str(int(ratelimit))]
    # 这里曾经默认加 `--throttled-rate 100K`（低速就重新取直链），实测是毒药：
    # yt-dlp 对这个参数的处理是「重新提取 → 从头重下」，而 YouTube 现在的限速
    # 是**按 IP / 按时段**的（同一时刻换任何客户端、换任何直链都只有 20~30KB/s），
    # 于是任务变成 下载一点 → 被判低速 → 重取 → 从头再来，永远到不了 100%。
    # 用户看到的就是「挂了半小时、进度反而退回去、文件被删掉重下」。
    # 慢但能跑完 >> 快但永远重来；而且每次重取都是一次新的页面请求，还会把
    # 出口 IP 更快推进人机验证名单。换客户端只在**失败**时做（见 core._run_ytdlp
    # 的重试链），不在低速时做。
    for k, v in (headers or {}).items():
        if v:
            args += ["--add-header", f"{k}:{v}"]
    # 调用方指定的提取器参数（如 player_client 轮换）放在 URL 之前
    args += extra or []
    args.append(page_url)

    errs: List[str] = []
    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=child_env(),
            **_no_window(),
        )
    except OSError as e:
        return 1, f"无法执行 yt-dlp：{e}"

    if on_proc:
        on_proc(proc)
    try:
        if proc.stdout is not None:
            for raw in proc.stdout:
                line = raw.rstrip("\n")
                if line.startswith("zdm:"):
                    if on_progress:
                        on_progress(_parse_progress(line[4:]))
                elif "Merging" in line or "[Merger]" in line:
                    if on_progress:
                        on_progress({"status": "merging"})
                elif "ERROR" in line:
                    errs.append(line.strip())
                if should_stop and should_stop():
                    proc.terminate()
                    break
    finally:
        try:
            proc.wait(timeout=15)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
    return proc.returncode if proc.returncode is not None else 0, "\n".join(errs)[:600]


def _parse_progress(payload: str) -> dict:
    parts = payload.split("|")

    def num(idx: int) -> float:
        if idx >= len(parts):
            return 0.0
        try:
            return float(parts[idx])
        except (TypeError, ValueError):
            return 0.0

    status = (parts[0] if parts else "").strip()
    total = int(num(2)) or int(num(3))
    return {
        "status": "finished" if status == "finished" else "downloading",
        "downloaded": int(num(1)),
        "total": total,
        "speed": num(4),
    }
