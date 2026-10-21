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


def _extra_bin_dirs() -> List[str]:
    """「工具装在 PATH 之外」的常见目录（deno / node 安装器默认落点）。

    Linux / Windows 上这些目录几乎永远不在 .desktop / 资源管理器启动的
    进程 PATH 里 —— 以前这里只补了 macOS 的 /opt/homebrew/bin，于是 Mac 的
    yt-dlp 能找到 deno、Linux / Windows 找不到：同一个视频在那两端解析降级
    甚至直链 403，表现就是「只有 Mac 能下」。
    """
    home = os.path.expanduser("~")
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "")
        roaming = os.environ.get("APPDATA", "")
        user = os.environ.get("USERPROFILE") or home
        return [
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
        return [
            "/opt/homebrew/bin",
            "/usr/local/bin",
            os.path.join(home, ".deno", "bin"),
            os.path.join(home, ".bun", "bin"),
        ]
    return [
        os.path.join(home, ".deno", "bin"),
        os.path.join(home, ".bun", "bin"),
        os.path.join(home, ".local", "bin"),
        "/usr/local/bin",
        "/snap/bin",
    ]


def child_env() -> Dict[str, str]:
    """给子进程补齐 PATH。

    ZDM 的桌面进程不继承用户 shell 的 PATH，而 yt-dlp 要靠那里的 deno/node
    解 YouTube 的 JS 挑战、靠那里的 ffmpeg 合流。缺任何一个都会失败，所以
    在唯一的入口处补全，而不是每个调用点各补一次。
    """
    env = dict(os.environ)
    extra = _extra_bin_dirs() + [
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
    parts = [p for p in (env.get("PATH") or "").split(os.pathsep) if p]
    for p in extra:
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
    else:
        # 未登录/无 PO token 的 YouTube 直链常被限速到几百 KB/s。yt-dlp 探测到
        # 这种异常低速会换一个客户端重新取直链，速度通常明显回升。用户自己
        # 设了限速时不加 —— 否则会把「主动限速」误判成「被限速」而反复重取。
        args += ["--throttled-rate", "100K"]
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
