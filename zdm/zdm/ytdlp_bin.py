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
from typing import Callable, Dict, List, Optional, Tuple

# 二进制可能放在这些位置（源码树 / bundle 压平后都兼容）
_PROBE_SUBDIRS = ("bin", "vendor", "yt-dlp")
_BINARY_NAMES = ("yt-dlp", "yt-dlp_macos", "yt-dlp.exe", "yt-dlp_linux")


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


def child_env() -> Dict[str, str]:
    """给子进程补齐 PATH。

    ZDM.app 不继承用户 shell 的 PATH（没有 /opt/homebrew/bin），而 yt-dlp 要靠
    那里的 deno 解 YouTube 的 JS 挑战、靠那里的 ffmpeg 合流。缺任何一个都会
    直接失败，所以在唯一的入口处补全，而不是每个调用点各补一次。
    """
    env = dict(os.environ)
    extra = [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
    parts = [p for p in (env.get("PATH") or "").split(os.pathsep) if p]
    for p in extra:
        if p not in parts:
            parts.append(p)
    env["PATH"] = os.pathsep.join(parts)
    return env


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
    if cookie_path:
        args += ["--cookies", cookie_path]
    if proxy:
        args += ["--proxy", proxy]
    if ratelimit:
        args += ["--limit-rate", str(int(ratelimit))]
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
