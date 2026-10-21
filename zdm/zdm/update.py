"""Self-update: check GitHub Releases and download the matching installer.

The desktop client shows a banner when a newer release exists and can fetch the
installer for the current platform, then hand it to the OS (opening a ``.dmg``
starts the macOS installer, opening a ``.exe`` starts the Windows wizard).

Everything here is dependency-free (urllib) and must stay importable when the
network is unreachable - every entry point returns None/"" on failure instead of
raising, so the GUI never breaks because GitHub is down.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from typing import List, Optional

REPO = "feibixunchang/ZDM"
PROJECT_URL = f"https://github.com/{REPO}"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
USER_AGENT = f"ZDM-Updater (+{PROJECT_URL})"


# --------------------------------------------------------------------------- versions
def parse_version(text) -> tuple:
    """'v1.3.0' -> (1, 3, 0). Missing parts become 0; junk becomes 0."""
    nums: List[int] = []
    for part in str(text or "").strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        nums.append(int(digits) if digits else 0)
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums[:3])


def is_newer(candidate, current) -> bool:
    return parse_version(candidate) > parse_version(current)


# --------------------------------------------------------------------------- transport
def _open(url: str, timeout: float, proxy: str = "", headers=None):
    """Open a URL, preferring the configured proxy, then a direct connection.

    The GUI talks to GitHub, not to localhost, so unlike the REST client it must
    *not* blanket-disable proxies. We try the explicit proxy first (when the user
    configured one), then a direct connection, then environment proxies.
    """
    hdrs = {"User-Agent": USER_AGENT}
    hdrs.update(headers or {})
    attempts = []
    if proxy:
        attempts.append(
            urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            )
        )
    attempts.append(urllib.request.build_opener(urllib.request.ProxyHandler({})))
    attempts.append(urllib.request.build_opener())  # honours http(s)_proxy env
    last = None
    for opener in attempts:
        req = urllib.request.Request(url, headers=hdrs)
        try:
            return opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            # A definitive answer from the API - don't retry other routes.
            if e.code in (403, 404):
                raise
            last = e
        except Exception as e:  # noqa: BLE001
            last = e
    if last is not None:
        raise last
    raise RuntimeError("no transport available")


def _get_json(url: str, timeout: float = 8.0, proxy: str = "") -> Optional[dict]:
    try:
        with _open(
            url,
            timeout,
            proxy,
            {"Accept": "application/vnd.github+json", "Cache-Control": "no-cache"},
        ) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


# --------------------------------------------------------------------------- release
def _latest_from_html(timeout: float = 8.0, proxy: str = "") -> Optional[dict]:
    """api.github.com 被限流/被墙时的兜底：走 github.com 普通网页。

    api.github.com 未登录限流 60 次/小时/IP —— 走共享代理出口的用户几乎
    必然 403，表现就是「检查更新永远连不上服务器」。而 github.com 的网页
    没有这个限制：``releases/latest`` 302 重定向到 ``/tag/<tag>`` 即可拿到
    最新版本号；再用 ``releases/expanded_assets/<tag>``（纯 HTML）解析资产。
    """
    try:
        with _open(f"{PROJECT_URL}/releases/latest", timeout, proxy) as r:
            final = r.geturl()
        m = re.search(r"/releases/tag/([^/?#]+)", final)
        if not m:
            return None
        tag = m.group(1)
    except Exception:  # noqa: BLE001
        return None
    version = tag.lstrip("vV")
    if not version:
        return None

    assets: List[dict] = []
    try:
        with _open(f"{PROJECT_URL}/releases/expanded_assets/{tag}", timeout, proxy) as r:
            html = r.read().decode("utf-8", "replace")
        for href in re.findall(r'href="([^"]*?/releases/download/[^"]+)"', html):
            path = href if href.startswith("/") else "/" + href.lstrip("/")
            url = f"https://github.com{path}" if path.startswith("/") else path
            name = path.rsplit("/", 1)[-1]
            if name and not any(a["name"] == name for a in assets):
                assets.append({"name": name, "url": url, "size": 0})
    except Exception:  # noqa: BLE001
        pass  # 资产拿不到没关系 —— 版本号已经到手，「有新版」的提示照常弹

    return {
        "version": version,
        "tag": tag,
        "url": f"{PROJECT_URL}/releases/tag/{tag}",
        "notes": "",
        "published": "",
        "assets": assets,
    }


def latest_release(timeout: float = 8.0, proxy: str = "") -> Optional[dict]:
    """Newest published Release as ``{version, url, notes, assets}`` or None."""
    data = _get_json(API_LATEST, timeout, proxy)
    if not isinstance(data, dict):
        # API 限流/网络不通 → 网页兜底（见 _latest_from_html 注释）
        return _latest_from_html(timeout, proxy)
    tag = str(data.get("tag_name") or data.get("name") or "").strip()
    version = tag.lstrip("vV")
    if not version:
        return None
    assets = []
    for a in data.get("assets") or []:
        name = str(a.get("name") or "")
        url = str(a.get("browser_download_url") or "")
        if name and url:
            assets.append({"name": name, "url": url, "size": int(a.get("size") or 0)})
    return {
        "version": version,
        "tag": tag,
        "url": str(data.get("html_url") or PROJECT_URL),
        "notes": (data.get("body") or "")[:6000],
        "published": str(data.get("published_at") or ""),
        "assets": assets,
    }


def pick_asset(assets, platform: str = "") -> Optional[dict]:
    """Choose the installer that matches this OS."""
    plat = platform or sys.platform
    names = [(a, a["name"].lower()) for a in (assets or [])]

    def first(pred):
        for asset, low in names:
            if pred(low):
                return asset
        return None

    if plat == "darwin":
        # Prefer the plain universal dmg; skip arch-tagged variants.
        return (
            first(lambda n: n.endswith(".dmg") and "arm" not in n and "x86" not in n and "intel" not in n)
            or first(lambda n: n.endswith(".dmg"))
        )
    if plat in ("win32", "cygwin"):
        # Windows 只有一个安装包：ZDM-Setup-*.exe（PyQt5 + 自带 Python 运行时）。
        # 文件名刻意不带 pyqt5 —— 它与 1.14.7 及更早的 C#/WPF 包共用同一个目录
        # 和同一个 AppId，这样老版本能被原地覆盖升级而不是并存两份。
        # 历史上那个 ZDM-PyQt5-Setup-* 已经下线，但旧 Release 里还留着，所以
        # 仍然把它排除掉。
        return (
            first(lambda n: n.endswith(".exe") and "pyqt5" not in n and n.startswith("zdm-setup"))
            or first(lambda n: n.endswith(".exe") and "pyqt5" not in n)
            or first(lambda n: n.endswith(".exe"))
        )
    # linux: prefer a deb matching this architecture, then a tarball
    import platform as _plat

    machine = _plat.machine().lower()
    arch = "arm64" if machine in ("aarch64", "arm64") else "amd64"
    return (
        first(lambda n: n.endswith(".deb") and arch in n)
        or first(lambda n: n.endswith(".deb"))
        or first(lambda n: n.endswith(".tar.gz"))
        or first(lambda n: "linux" in n and n.endswith(".zip"))
    )


def download(
    url: str,
    dest: str,
    timeout: float = 30.0,
    proxy: str = "",
    expected_size: int = 0,
) -> Optional[str]:
    """Stream ``url`` into ``dest`` (atomically). Returns the path or None.

    ``expected_size`` (the size GitHub reported for the asset) is checked after
    the transfer: a truncated installer used to be opened as if it were fine.
    """
    tmp = dest + ".part"
    try:
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        with _open(url, timeout, proxy) as r, open(tmp, "wb") as f:
            while True:
                chunk = r.read(262144)
                if not chunk:
                    break
                f.write(chunk)
        if expected_size and os.path.getsize(tmp) != expected_size:
            raise IOError(
                f"size mismatch: got {os.path.getsize(tmp)}, expected {expected_size}"
            )
        os.replace(tmp, dest)
        return dest
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass
        return None


# --------------------------------------------------------------------------- install
def install_windows(setup_path: str, relaunch_exe: str = "") -> bool:
    """Windows：静默升级，装完把新版重新拉起来。返回是否成功拉起安装器。

    为什么不用 Inno 的 ``[Run]`` + ``Check: WizardSilent`` 来做重启：静默安装时
    那条**真的会执行**，Setup 会一直等被拉起的 GUI 进程退出，安装器于是永不返回
    （CI 的 Install and verify 就这么卡了 20 分钟，日志里只剩一句
    "Terminate orphan process: pid (9100) (Zdm)"）。

    所以改成：退出前起一个游离的 powershell —— 先等安装器结束，再启动新版，
    与应用自身的生命周期彻底解耦。调用方拿到 True 后应该立刻退出进程。
    """
    if os.name != "nt" or not setup_path:
        return False
    exe = relaunch_exe or sys.executable
    if not exe:
        return False

    def q(p: str) -> str:  # PowerShell 单引号字符串：内部的 ' 要写成 ''
        return "'" + str(p).replace("'", "''") + "'"

    script = (
        f"$p = Start-Process -FilePath {q(setup_path)} -ArgumentList "
        "'/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/CLOSEAPPLICATIONS' -PassThru;"
        " Wait-Process -Id $p.Id -ErrorAction SilentlyContinue;"
        " Start-Sleep -Seconds 2;"
        f" Start-Process -FilePath {q(exe)}"
    )
    try:
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP：本进程退出后它继续活着。
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
            creationflags=0x00000008 | 0x00000200,
            close_fds=True,
        )
        return True
    except Exception:  # noqa: BLE001 - 交付给调用方处理（提示手动安装）
        return False


# --------------------------------------------------------------------------- ignore list
def _conf_path() -> str:
    from .manager import config_dir

    return os.path.join(config_dir(), "update.json")


def load_ignored() -> str:
    """Version the user asked to skip ('忽略此版本')."""
    try:
        with open(_conf_path(), "r", encoding="utf-8") as f:
            return str(json.load(f).get("ignored") or "")
    except Exception:
        return ""


def save_ignored(version: str) -> None:
    try:
        data = {}
        try:
            with open(_conf_path(), "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        except Exception:
            data = {}
        data["ignored"] = str(version or "")
        with open(_conf_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
