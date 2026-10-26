"""Self-update: check GitHub Releases and download the matching installer.

The desktop client shows a banner when a newer release exists and can fetch the
installer for the current platform, then hand it to the OS (opening a ``.dmg``
starts the macOS installer, opening a ``.exe`` starts the Windows wizard).

Everything here is dependency-free (urllib) and must stay importable when the
network is unreachable - every entry point returns None/"" on failure instead of
raising, so the GUI never breaks because GitHub is down.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional, Tuple
from urllib.parse import urlparse

REPO = "feibixunchang/ZDM"
PROJECT_URL = f"https://github.com/{REPO}"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
USER_AGENT = f"ZDM-Updater (+{PROJECT_URL})"

# GitHub 的 release 资产会 302 到 objects.githubusercontent.com，国内经常只有
# 几百 KB/s。桌面端更新动辄 100 MB+，用户看到的就是「下载并安装」卡住。
# 这些公共加速前缀直接代理同一个 URL；下载前先并行测速，选最快的一条。
# 设 ZDM_UPDATE_NO_MIRROR=1 可强制直连（企业代理/镜像不可达时）。
UPDATE_MIRRORS = (
    "https://gh-proxy.com/",
    "https://ghproxy.net/",
    "https://ghfast.top/",
)


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
def _assets_from_html(tag: str, timeout: float = 8.0, proxy: str = "") -> List[dict]:
    """从 ``releases/expanded_assets/<tag>`` 的纯 HTML 片段里刮资产列表。

    GitHub 的这个端点不需要 API 配额，但它是**第二个请求**：第一个请求
    （``releases/latest`` 的 302）通了、这一个超时/重置时，调用方就只有
    版本号没有资产 —— 老代码在这种情况下会把「下载并安装」直接变成
    「打开网页」（用户 1.15.24 → 1.15.25 踩到的就是这个）。
    所以把它单独抽出来，下载前还能再要一次。
    """
    assets: List[dict] = []
    if not tag:
        return assets
    try:
        with _open(f"{PROJECT_URL}/releases/expanded_assets/{tag}", timeout, proxy) as r:
            html = r.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return assets
    for href in re.findall(r'href="([^"]*?/releases/download/[^"]+)"', html):
        path = href if href.startswith("/") else "/" + href.lstrip("/")
        url = f"https://github.com{path}" if path.startswith("/") else path
        name = path.rsplit("/", 1)[-1]
        if name and not any(a["name"] == name for a in assets):
            assets.append({"name": name, "url": url, "size": 0})
    return assets


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

    assets = _assets_from_html(tag, timeout, proxy)
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
            assets.append({
                "name": name,
                "url": url,
                "size": int(a.get("size") or 0),
                "digest": str(a.get("digest") or ""),
            })
    if not assets:
        # API 正常但资产为空（很少见：Release 刚创建、资产还没传完）。补一次
        # HTML，让「下载并安装」在 banner 阶段就有地址可用。
        assets = _assets_from_html(tag, timeout, proxy)
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
        # DMGs are per-architecture: the bundle embeds the build machine's
        # CPython framework, so "arm64" and "x64" builds are not interchangeable.
        # Match the running machine first, then accept a legacy untagged DMG
        # (old naming scheme), and only then any DMG so old releases still
        # resolve. Picking blindly used to hand Intel Macs an arm64 installer
        # that refuses to launch.
        import platform as _plat

        machine = _plat.machine().lower()
        if machine in ("arm64", "aarch64"):
            want = ("arm64", "aarch64")
            avoid = ("x64", "x86_64", "amd64", "intel")
        else:
            want = ("x64", "x86_64", "amd64", "intel")
            avoid = ("arm64", "aarch64")
        return (
            first(lambda n: n.endswith(".dmg") and any(k in n for k in want))
            or first(lambda n: n.endswith(".dmg") and not any(k in n for k in avoid))
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
        # the tarball is arch-tagged too; falling straight through to the .deb
        # used to hand an arm64 machine the amd64 bundle whenever the matching
        # .deb was missing.
        or first(lambda n: n.endswith(".tar.gz") and arch in n)
        or first(lambda n: n.endswith(".deb"))
        or first(lambda n: n.endswith(".tar.gz"))
        or first(lambda n: "linux" in n and n.endswith(".zip"))
    )


def _expected_asset_names(version: str, platform: str = "") -> List[str]:
    """本项目的产物命名是固定的，猜名字比「让用户自己去网页找」靠谱。

    只在「拿不到资产列表」时用作兜底，调用方会用 Range 请求验证真的存在。
    """
    v = str(version or "").strip().lstrip("vV")
    if not v:
        return []
    plat = platform or sys.platform
    if plat == "darwin":
        import platform as _plat

        arch = "arm64" if _plat.machine().lower() in ("aarch64", "arm64") else "x64"
        return [f"ZDM-{v}-{arch}.dmg", f"ZDM-{v}.dmg"]
    if plat in ("win32", "cygwin"):
        # 新包叫 ZDM-Setup-*；ZDM-PyQt5-Setup-* 是旧命名，只有在没有新包时才试。
        return [f"ZDM-Setup-{v}.exe", f"ZDM-PyQt5-Setup-{v}.exe"]
    import platform as _plat

    arch = "arm64" if _plat.machine().lower() in ("aarch64", "arm64") else "amd64"
    return [
        f"zdm_{v}_{arch}.deb",
        f"zdm-linux-{arch}.tar.gz",
        "zdm-linux-amd64.tar.gz",
    ]


def _url_exists(url: str, proxy: str = "", timeout: float = 6.0) -> bool:
    """Range 探活：能拿到 200/206 才算这个安装包真的在。"""
    try:
        with _open(url, timeout, proxy, {"Range": "bytes=0-0"}) as r:
            return int(getattr(r, "status", 200) or 200) in (200, 206)
    except Exception:  # noqa: BLE001
        return False


def resolve_asset(info, proxy: str = "") -> Optional[dict]:
    """把「有新版本」变成「一个可以点的下载地址」，三层兜底。

    用户点「下载并安装」时最不能接受的就是被丢到 Releases 网页上自己找 ——
    尤其是他明明看到过「直接下载 + 进度条」的老版本行为。所以这里不允许
    「资产列表为空 ⇒ 直接打开网页」，必须尽力造出一个真实存在的下载地址：

    1. info 里自带资产（API 路径正常时就是这条）；
    2. 再向 ``expanded_assets`` 要一次（HTML 兜底上次可能只求到了版本号）；
    3. 按本项目固定的产物命名拼地址，并用 Range 请求逐个探活。

    三步都失败（连版本号都没有）才返回 None，交给 GUI 明确告知用户
    （而不是静默跳站）。
    """
    info = info or {}
    asset = pick_asset(info.get("assets") or [])
    if asset and asset.get("url"):
        return asset

    version = str(info.get("version") or "").strip().lstrip("vV")
    tag = str(info.get("tag") or "").strip()
    if not tag and version:
        tag = "v" + version

    if tag:
        asset = pick_asset(_assets_from_html(tag, proxy=proxy))
        if asset and asset.get("url"):
            return asset

    names = _expected_asset_names(version)
    if not tag or not names:
        return None
    for name in names:
        url = f"{PROJECT_URL}/releases/download/{tag}/{name}"
        if _url_exists(url, proxy):
            return {"name": name, "url": url, "size": 0, "digest": ""}
    # 探活全失败（可能只是这一次超时/被限流）：仍然把首选地址交给下载器 ——
    # 它还有「直连 + 3 个加速镜像」四次机会，比在这里直接认输强。真 404 时
    # 会很快失败并明确提示，不会静默跳站。
    return {
        "name": names[0],
        "url": f"{PROJECT_URL}/releases/download/{tag}/{names[0]}",
        "size": 0,
        "digest": "",
    }


def downloads_dir() -> str:
    """系统的「下载」目录。

    Windows 上``~/Downloads``经常被 OneDrive 重定向到
    ``%USERPROFILE%\\OneDrive\\Downloads``；硬编码会在 C 盘凭空建一个空目录，
    安装包下到那里、用户在资源管理器里根本看不到（表现为「装完界面没了，
    还得自己去找安装包」）。有注册表就读注册表，读不到再猜。
    """
    home = os.path.expanduser("~")
    if os.name == "nt":
        try:
            import winreg  # type: ignore[import-not-found]

            key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                val, _ = winreg.QueryValueEx(
                    k, "{374DE290-123F-4565-9164-39C4925E467B}"
                )
            val = os.path.expandvars(str(val or ""))
            if val and os.path.isdir(val):
                return val
        except Exception:  # noqa: BLE001
            pass
    for name in ("Downloads", "下载"):
        p = os.path.join(home, name)
        if os.path.isdir(p):
            return p
    return os.path.join(home, "Downloads")


def _asset_urls(url: str) -> List[str]:
    """直接地址 + 各加速镜像；顺序固定，测速后再挑。"""
    out = [url]
    for m in UPDATE_MIRRORS:
        cand = m + url
        if cand not in out:
            out.append(cand)
    return out


def _probe_speed(url: str, proxy: str, timeout: float = 5.0) -> float:
    """读 512 KB 估算 B/s；失败返回 0。"""
    t0 = time.monotonic()
    try:
        with _open(url, timeout, proxy, {"Range": "bytes=0-524287"}) as r:
            data = r.read(524288)
        dt = max(0.05, time.monotonic() - t0)
        return len(data) / dt
    except Exception:  # noqa: BLE001
        return 0.0


def fastest_asset_url(url: str, proxy: str = "") -> str:
    """并行测直连/镜像，返回最快的一条；全失败时退回原始地址。"""
    if os.environ.get("ZDM_UPDATE_NO_MIRROR"):
        return url
    candidates = _asset_urls(url)
    if len(candidates) == 1:
        return url
    best_url, best_speed = url, 0.0
    with ThreadPoolExecutor(max_workers=len(candidates)) as ex:
        futs = {ex.submit(_probe_speed, c, proxy): c for c in candidates}
        try:
            for fut in as_completed(futs, timeout=8.0):
                speed = fut.result()
                if speed > best_speed:
                    best_speed, best_url = speed, futs[fut]
        except Exception:  # noqa: BLE001
            pass
        for f in futs:
            f.cancel()
    return best_url


def _official_host(url: str) -> bool:
    """True for GitHub-owned download hosts (never a third-party mirror)."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return host in (
        "github.com",
        "objects.githubusercontent.com",
        "github-releases.githubusercontent.com",
        "release-assets.githubusercontent.com",
    )


def download(
    url: str,
    dest: str,
    timeout: float = 30.0,
    proxy: str = "",
    expected_size: int = 0,
    expected_sha256: str = "",
    progress=None,
) -> Optional[str]:
    """Stream ``url`` into ``dest`` (atomically). Returns the path or None.

    Before the real transfer it probes GitHub and the public mirrors in
    parallel and keeps the fastest one. The old behaviour (direct GitHub only)
    was routinely 200-300 KB/s in mainland China: a 127 MB .deb took 8+
    minutes and looked like the updater had hung.
    """
    # Mirrors are only a transport optimization: without a published sha256
    # there is no way to tell a tampered package from the real one, so fall
    # back to the official GitHub hosts (still HTTPS) and never route an
    # unverifiable download through a third party.
    if expected_sha256:
        order = [fastest_asset_url(url, proxy)]
    else:
        order = [url] if _official_host(url) else []
    for cand in _asset_urls(url):
        if not expected_sha256 and not _official_host(cand):
            continue
        if cand not in order:
            order.append(cand)

    for candidate in order:
        tmp = dest + ".part"
        try:
            os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
            digest = hashlib.sha256()
            with _open(candidate, timeout, proxy) as r, open(tmp, "wb") as f:
                total = expected_size or 0
                try:
                    cl = r.headers.get("Content-Length")
                    if cl and not total:
                        total = int(cl)
                except Exception:  # noqa: BLE001
                    pass
                done = 0
                while True:
                    chunk = r.read(262144)
                    if not chunk:
                        break
                    f.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if progress:
                        try:
                            progress(done, total)
                        except Exception:  # noqa: BLE001
                            pass
            if expected_size and os.path.getsize(tmp) != expected_size:
                raise IOError(
                    f"size mismatch: got {os.path.getsize(tmp)}, expected {expected_size}"
                )
            # 镜像只是加速通道，内容必须和 GitHub 公布的 sha256 一致；
            # 否则第三方代理返回同尺寸的篡改安装包就会被执行。
            if expected_sha256:
                want = expected_sha256.lower().removeprefix("sha256:")
                if digest.hexdigest() != want:
                    raise IOError("sha256 mismatch")
            os.replace(tmp, dest)
            return dest
        except Exception:  # noqa: BLE001
            try:
                os.remove(tmp)
            except Exception:  # noqa: BLE001
                pass
            continue
    return None


# --------------------------------------------------------------------------- install
def install_linux(deb_path: str) -> Tuple[bool, str]:
    """Linux：优先弹出系统包安装器（有真实安装界面），再退回 pkexec+dpkg。

    以前直接 ``pkexec dpkg -i``：从桌面会话启动时它会弹授权框，但从后台服务
    或没有 polkit agent 的环境启动时只会返回 "Not authorized"，用户看到的
    就是「下载完了，什么都没发生」。图形安装器（Deepin 的
    deepin-deb-installer、gdebi）跑在 GUI 自己的会话里，稳定得多，而且用户
    能看到安装进度。
    """
    import shutil

    from . import runtime

    if not sys.platform.startswith("linux") or not deb_path:
        return False, "仅在 Linux 上可用"
    if not os.path.isfile(deb_path):
        return False, "安装包不存在"
    # 冻结包里 LD_LIBRARY_PATH 指向 _internal；不剥掉的话系统安装器会加载
    # 我们的 libssl/libstdc++ 后直接退出，用户看到的是「下载完什么都没发生」。
    env = runtime.child_env()
    for exe in ("deepin-deb-installer", "gdebi-gtk", "gdebi", "xdg-open"):
        path = shutil.which(exe, path=env.get("PATH"))
        if not path:
            continue
        try:
            subprocess.Popen([path, deb_path], close_fds=True, env=env)
            return True, ""
        except Exception:  # noqa: BLE001
            continue
    # 没有图形安装器：最后再试一次 pkexec（有授权框的环境仍可一键装完）。
    pkexec = shutil.which("pkexec")
    dpkg = shutil.which("dpkg") or "/usr/bin/dpkg"
    if not pkexec:
        return False, "系统里没有图形安装器，也没有 pkexec"
    try:
        p = subprocess.run(
            [pkexec, dpkg, "-i", deb_path],
            timeout=600,
            env=env,
            **_silent_kwargs(),
        )
    except subprocess.TimeoutExpired:
        return False, "安装超时"
    except Exception as e:  # noqa: BLE001
        return False, str(e)
    if p.returncode != 0:
        return False, f"dpkg 退出码 {p.returncode}"
    return True, ""


def _silent_kwargs() -> dict:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}


def install_windows(setup_path: str, relaunch_exe: str = "", silent: bool = False) -> bool:
    """Windows：启动安装程序。返回是否成功拉起。

    **默认走可见向导**（silent=False）。历史版本用的是 ``/SILENT``，用户点
    「下载并安装」后看到的是：ZDM 所有窗口/托盘一起消失，然后什么都没有 ——
    静默安装既没有界面也没有「装完了」的反馈，装失败时更是彻底黑箱，用户只能
    自己去下载目录把安装包翻出来双击。那套「一键静默升级」在真实机器上收益
    很低（省掉一次点「下一步」），代价却是更新失败时完全不可诊断。

    可见向导下：Setup 自己在 ``ssInstall`` 阶段 taskkill 掉 ZDM.exe（见
    installer.iss::KillRunningZdm），装完直接拉起界面（``[Run]`` +
    ``nowait``），所以调用方**不需要**再安排重启，也不应该急着 quit ——
    让用户看着向导走完，体验才对。

    ``silent=True`` 保留给无人值守场景（CI/脚本），走 /SILENT 并起一个游离的
    powershell 在装完后拉起新版；那条路径**不要**在交互式更新里用。
    """
    if os.name != "nt" or not setup_path or not os.path.isfile(setup_path):
        return False

    if not silent:
        try:
            # DETACHED_PROCESS：与本进程彻底解耦，GUI 退出/被杀都不影响向导。
            subprocess.Popen(
                [setup_path, "/CLOSEAPPLICATIONS", "/NORESTART"],
                creationflags=0x00000008,
                close_fds=True,
            )
            return True
        except Exception:  # noqa: BLE001 - 交付给调用方处理（提示手动安装）
            return False

    exe = relaunch_exe or sys.executable
    if not exe:
        return False

    def q(p: str) -> str:  # PowerShell 单引号字符串：内部的 ' 要写成 ''
        return "'" + str(p).replace("'", "''") + "'"

    script = (
        # 先杀掉残留进程（serve 是独立的 ZDM.exe，GUI 退出后它还活着）：
        # 不杀的话 Inno 的 /CLOSEAPPLICATIONS 关不掉它们，用户就得开任务
        # 管理器手动结束（Windows 实测）。本 GUI 即将退出，再杀一次无妨。
        # **不要加 /T**：这个 PowerShell 自己就是 ZDM.exe 的子进程，/T 会
        # 把执行脚本的进程一起杀掉，后面的安装器根本起不来。
        "taskkill /F /IM ZDM.exe >$null 2>&1; Start-Sleep -Seconds 1; "
        f"$p = Start-Process -FilePath {q(setup_path)} -ArgumentList "
        "'/SILENT','/SUPPRESSMSGBOXES','/NORESTART','/CLOSEAPPLICATIONS' -PassThru;"
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


# --------------------------------------------------------------------------- local state
def _conf_path() -> str:
    from .manager import config_dir

    return os.path.join(config_dir(), "update.json")


def _load_conf() -> dict:
    try:
        with open(_conf_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_conf(data: dict) -> None:
    try:
        path = _conf_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def load_ignored() -> str:
    """Version the user asked to skip ('忽略此版本')."""
    return str(_load_conf().get("ignored") or "")


def save_ignored(version: str) -> None:
    data = _load_conf()
    data["ignored"] = str(version or "")
    _save_conf(data)


def load_last_check() -> float:
    """上次成功问到「最新版本是多少」的时间戳（0 = 从没查过）。

    用途：桌面端是常驻托盘的，一天可能重启七八次。没有这个记录就只能
    「每次启动都打一次 api.github.com」，未登录限流 60 次/小时，很快 403，
    然后用户看到的就是「检查更新永远连不上」。
    """
    try:
        return float(_load_conf().get("last_check") or 0)
    except Exception:
        return 0.0


def save_last_check(ts: Optional[float] = None) -> None:
    data = _load_conf()
    data["last_check"] = float(ts if ts is not None else time.time())
    _save_conf(data)
