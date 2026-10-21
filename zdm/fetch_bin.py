#!/usr/bin/env python3
"""Fetch the pinned Deno binary into ``zdm/bin`` at build time.

Why this exists
---------------
yt-dlp 2026.x needs a JavaScript runtime to solve YouTube's signature and
``n`` challenges. Without one YouTube only returns storyboards, and users see
"the page needs to be reloaded". Asking every user to install Deno themselves
is not acceptable for a desktop app, so the installers ship a copy.

The version is pinned so a YouTube-breaking Deno update cannot silently change
what we release. Bump ``DENO_VERSION`` deliberately after testing.

Usage:
    python3 fetch_bin.py --dest ../zdm/bin --os linux
    python3 fetch_bin.py --dest ../zdm/bin --os darwin --arch aarch64
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import sys
import tempfile
import urllib.request
import zipfile

DENO_VERSION = "2.9.8"
BASE = f"https://github.com/denoland/deno/releases/download/v{DENO_VERSION}"

ASSETS = {
    ("linux", "x86_64"): "deno-x86_64-unknown-linux-gnu.zip",
    ("linux", "aarch64"): "deno-aarch64-unknown-linux-gnu.zip",
    ("darwin", "x86_64"): "deno-x86_64-apple-darwin.zip",
    ("darwin", "aarch64"): "deno-aarch64-apple-darwin.zip",
    ("windows", "x86_64"): "deno-x86_64-pc-windows-msvc.zip",
}

ALIASES = {
    "amd64": "x86_64",
    "x64": "x86_64",
    "arm64": "aarch64",
}


def norm_arch(value: str) -> str:
    v = (value or "").lower()
    return ALIASES.get(v, v)


def norm_os(value: str) -> str:
    v = (value or "").lower()
    if v.startswith("win"):
        return "windows"
    if v in ("mac", "macos", "osx", "darwin"):
        return "darwin"
    return "linux"


def download(url: str, dest: str) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "ZDM-build"})
    with urllib.request.urlopen(req, timeout=180) as src, open(dest, "wb") as out:
        shutil.copyfileobj(src, out, length=1 << 20)


def fetch(dest_dir: str, os_name: str, arch: str, version: str = DENO_VERSION) -> str:
    os_name = norm_os(os_name)
    arch = norm_arch(arch)
    asset = ASSETS.get((os_name, arch))
    if not asset:
        raise SystemExit(f"不支持的平台：{os_name}/{arch}")
    exe = "deno.exe" if os_name == "windows" else "deno"
    target = os.path.join(dest_dir, exe)
    os.makedirs(dest_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="zdm-deno-") as tmp:
        zpath = os.path.join(tmp, "deno.zip")
        url = f"https://github.com/denoland/deno/releases/download/v{version}/{asset}"
        print(f"==> downloading deno {version} ({os_name}/{arch})")
        download(url, zpath)
        with zipfile.ZipFile(zpath) as zf:
            member = next(
                (n for n in zf.namelist() if os.path.basename(n) in ("deno", "deno.exe")),
                None,
            )
            if member is None:
                raise SystemExit(f"{asset} 里没有 deno 可执行文件")
            with zf.open(member) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
    if os_name != "windows":
        os.chmod(target, 0o755)
    # MIT licence: keep the notice next to the binary we redistribute.
    try:
        lic = os.path.join(
            dest_dir, "DENO-LICENSE.md"
        )
        download(
            f"https://raw.githubusercontent.com/denoland/deno/v{version}/LICENSE.md",
            lic,
        )
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: 下载 deno LICENSE 失败：{e}")
    print(f"    -> {target}")
    return target


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", required=True)
    ap.add_argument("--os", default=sys.platform)
    ap.add_argument("--arch", default=platform.machine())
    ap.add_argument("--version", default=DENO_VERSION)
    args = ap.parse_args()
    fetch(os.path.abspath(args.dest), args.os, args.arch, args.version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
