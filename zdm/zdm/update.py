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
def latest_release(timeout: float = 8.0, proxy: str = "") -> Optional[dict]:
    """Newest published Release as ``{version, url, notes, assets}`` or None."""
    data = _get_json(API_LATEST, timeout, proxy)
    if not isinstance(data, dict):
        return None
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
        return first(lambda n: n.endswith(".dmg"))
    if plat in ("win32", "cygwin"):
        return first(lambda n: n.endswith(".exe"))
    # linux: prefer a deb, then a tarball
    return (
        first(lambda n: n.endswith(".deb"))
        or first(lambda n: n.endswith(".tar.gz"))
        or first(lambda n: "linux" in n and n.endswith(".zip"))
    )


def download(url: str, dest: str, timeout: float = 30.0, proxy: str = "") -> Optional[str]:
    """Stream ``url`` into ``dest`` (atomically). Returns the path or None."""
    tmp = dest + ".part"
    try:
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        with _open(url, timeout, proxy) as r, open(tmp, "wb") as f:
            while True:
                chunk = r.read(262144)
                if not chunk:
                    break
                f.write(chunk)
        os.replace(tmp, dest)
        return dest
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass
        return None


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
