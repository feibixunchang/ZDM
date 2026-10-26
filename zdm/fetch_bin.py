#!/usr/bin/env python3
"""Fetch the pinned third-party binaries into ``zdm/bin`` at build time.

Why this exists
---------------
Two externals make features work that ZDM cannot ship from pure Python:

* deno — yt-dlp 2026.x needs a JavaScript runtime to solve YouTube's
  signature and ``n`` challenges. Without one YouTube only returns storyboards,
  and users see "the page needs to be reloaded".
* N_m3u8DL-RE — the HLS/DASH engine. ffmpeg can pull a playlist, but it can't
  pick a bitrate/voice/subtitle track, retries whole segments on one bad chunk,
  and gives no per-segment progress.

Asking every user to install these is not acceptable for a desktop app, so the
installers ship a copy.

Versions are pinned and each asset carries a sha256. Both guards exist for the
same reason: if GitHub serves something different than what we tested, the
build must stop here instead of quietly releasing a bundle that breaks
YouTube or every m3u8 download. Bump a version deliberately, after re-running
the streaming tests, and update the digest in the same commit.

Usage:
    python3 fetch_bin.py --dest ../zdm/bin --os linux --tool deno
    python3 fetch_bin.py --dest ../zdm/bin --os darwin --arch aarch64 --tool all
"""
from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

# Windows 控制台默认是代码页（CI 上是 cp1252），本脚本的进度文案是中文：
# 打印时 UnicodeEncodeError 会让「已经下载好」的那一步崩掉，构建随之失败。
# 输出流改成 UTF-8 且对编码不了的字符用 replace —— 少一行漂亮的中文，
# 也远比整条 Windows 流水线红掉好。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - 老 Python / 被重定向成非文本流
        pass

DENO_VERSION = "2.9.8"
RE_VERSION = "0.6.0-beta"
# 上游把构建日期写在资产名里；换版本时这一串要一起对上新 release 的资产名。
RE_STAMP = "20260629"

# (os, arch) -> (资产名, 压缩包 sha256)
DENO_ASSETS = {
    ("linux", "x86_64"): (
        "deno-x86_64-unknown-linux-gnu.zip",
        "e890975bc2549807bbcc0b584ffafda72cc7c93349dd99c80483dd784f6c4609",
    ),
    ("linux", "aarch64"): (
        "deno-aarch64-unknown-linux-gnu.zip",
        "d49596c20b9788cf8f5d8ddf644103d4bde56ed239753f0bdb92ba9361d51905",
    ),
    ("darwin", "x86_64"): (
        "deno-x86_64-apple-darwin.zip",
        "4b2f5edd2639b7470f034238b321d7002d8dfb4726d8558f52c17afac370eb2c",
    ),
    ("darwin", "aarch64"): (
        "deno-aarch64-apple-darwin.zip",
        "9f0bf660393da2eaff044cdc7444cb08c73f235849b61d224d825a194ce7c98e",
    ),
    ("windows", "x86_64"): (
        "deno-x86_64-pc-windows-msvc.zip",
        "cba84b26e0df5ae7f74fc66b86a95b866872a86292ebd84fa1e959927edd93bf",
    ),
}

# (os, arch) -> (资产名, sha256)
RE_ASSETS = {
    ("linux", "x86_64"): (
        f"N_m3u8DL-RE_v{RE_VERSION}_linux-x64_{RE_STAMP}.tar.gz",
        "2ebcd450594a08c0de0bcd502a80c5d3f89a702a0fa6604199527bb8d6eee4db",
    ),
    ("linux", "aarch64"): (
        f"N_m3u8DL-RE_v{RE_VERSION}_linux-arm64_{RE_STAMP}.tar.gz",
        "17f3c2c2372cbb859bf42e77bf59f7a049e5642fc4f6d234195e70bbea178299",
    ),
    ("darwin", "x86_64"): (
        f"N_m3u8DL-RE_v{RE_VERSION}_osx-x64_{RE_STAMP}.tar.gz",
        "4dab8d256ae3164557f48a3326c8fa60da99e37536ee1589ce6c841180fa8204",
    ),
    ("darwin", "aarch64"): (
        f"N_m3u8DL-RE_v{RE_VERSION}_osx-arm64_{RE_STAMP}.tar.gz",
        "ca181b7e8976564766c34e3cefe48fff409f58289ca970ade062668c01c53918",
    ),
    ("windows", "x86_64"): (
        f"N_m3u8DL-RE_v{RE_VERSION}_win-x64_{RE_STAMP}.zip",
        "3825fd42ee502f98a9378f6fdddb2f7822709f521806214f466db6935c950f1a",
    ),
    ("windows", "aarch64"): (
        f"N_m3u8DL-RE_v{RE_VERSION}_win-arm64_{RE_STAMP}.zip",
        "3a13527812a5f18b9981b3cd6f7f36bd17cd7d76b5f3273281a58354e5fcebd6",
    ),
}

RE_EXE = "N_m3u8DL-RE"
RE_LICENSE_URL = (
    f"https://raw.githubusercontent.com/nilaoda/N_m3u8DL-RE/v{RE_VERSION}/LICENSE"
)

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


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dest: str, sha256: str = "") -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "ZDM-build"})
    with urllib.request.urlopen(req, timeout=180) as src, open(dest, "wb") as out:
        shutil.copyfileobj(src, out, length=1 << 20)
    if sha256:
        got = sha256_of(dest)
        if got != sha256:
            raise SystemExit(
                f"校验和不匹配：{os.path.basename(url)}\n"
                f"  期望 {sha256}\n  实际 {got}\n"
                f"上游悄悄换了包，或下载被截断。别用这个产物发布。"
            )


def extract_exe(archive: str, names: tuple, target: str) -> None:
    """Pull the single executable member out of a .zip or .tar.gz."""
    if archive.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            member = next((n for n in zf.namelist() if os.path.basename(n) in names), None)
            if member is None:
                raise SystemExit(f"{os.path.basename(archive)} 里没有 {'/'.join(names)}")
            with zf.open(member) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
        return
    with tarfile.open(archive, "r:*") as tf:
        member = next(
            (m for m in tf.getmembers() if m.isfile() and os.path.basename(m.name) in names),
            None,
        )
        if member is None:
            raise SystemExit(f"{os.path.basename(archive)} 里没有 {'/'.join(names)}")
        src = tf.extractfile(member)
        if src is None:
            raise SystemExit(f"{os.path.basename(archive)} 里的 {member.name} 读不出来")
        with src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)


def fetch_deno(dest_dir: str, os_name: str, arch: str) -> str:
    asset, digest = _asset_for(DENO_ASSETS, os_name, arch, "deno")
    exe = "deno.exe" if os_name == "windows" else "deno"
    target = os.path.join(dest_dir, exe)
    os.makedirs(dest_dir, exist_ok=True)
    url = f"https://github.com/denoland/deno/releases/download/v{DENO_VERSION}/{asset}"
    print(f"==> downloading deno {DENO_VERSION} ({os_name}/{arch})")
    with tempfile.TemporaryDirectory(prefix="zdm-deno-") as tmp:
        _place(target, os_name, tmp, asset, url, digest, ("deno", "deno.exe"), "deno")
    # MIT licence: keep the notice next to the binary we redistribute.
    try:
        download(
            f"https://raw.githubusercontent.com/denoland/deno/v{DENO_VERSION}/LICENSE.md",
            os.path.join(dest_dir, "DENO-LICENSE.md"),
        )
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: 下载 deno LICENSE 失败：{e}")
    return target


def fetch_re(dest_dir: str, os_name: str, arch: str) -> str:
    asset, digest = _asset_for(RE_ASSETS, os_name, arch, "N_m3u8DL-RE")
    exe = RE_EXE + (".exe" if os_name == "windows" else "")
    target = os.path.join(dest_dir, exe)
    os.makedirs(dest_dir, exist_ok=True)
    url = f"https://github.com/nilaoda/N_m3u8DL-RE/releases/download/v{RE_VERSION}/{asset}"
    print(f"==> downloading N_m3u8DL-RE {RE_VERSION} ({os_name}/{arch})")
    with tempfile.TemporaryDirectory(prefix="zdm-re-") as tmp:
        _place(target, os_name, tmp, asset, url, digest,
               (RE_EXE, RE_EXE + ".exe"), "N_m3u8DL-RE")
    try:
        download(RE_LICENSE_URL, os.path.join(dest_dir, "RE-LICENSE.txt"))
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: 下载 N_m3u8DL-RE LICENSE 失败：{e}")
    return target


def _asset_for(table: dict, os_name: str, arch: str, label: str):
    key = (os_name, arch)
    if key not in table:
        raise SystemExit(
            f"{label} 没有 {os_name}/{arch} 的固定包。"
            f"要支持这个平台，先去上游 release 页登记资产名和 sha256。"
        )
    return table[key]


def _place(target: str, os_name: str, tmp: str, asset: str, url: str,
           digest: str, names: tuple, label: str) -> None:
    archive = os.path.join(tmp, asset)
    download(url, archive, digest)
    extract_exe(archive, names, target)
    if os_name != "windows":
        os.chmod(target, 0o755)
    print(f"    -> {target} ({label}, sha256 已核对)")


TOOLS = {
    "deno": fetch_deno,
    "re": fetch_re,
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", required=True)
    ap.add_argument("--os", default=sys.platform)
    ap.add_argument("--arch", default=platform.machine())
    ap.add_argument(
        "--tool",
        default="deno",
        choices=list(TOOLS) + ["all"],
        help="deno=YouTube JS 运行时；re=HLS/DASH 引擎；all=两者",
    )
    args = ap.parse_args()
    os_name = norm_os(args.os)
    arch = norm_arch(args.arch)
    names = list(TOOLS) if args.tool == "all" else [args.tool]
    dest = os.path.abspath(args.dest)
    for name in names:
        TOOLS[name](dest, os_name, arch)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
