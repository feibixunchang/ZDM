"""Transport backends: pure-stdlib HTTP/1.1 client and optional curl fallback.

Some servers (CDN/WAF) only answer HTTP/2 clients with 200 and reject HTTP/1.1
with 403. `http.client` cannot speak HTTP/2, so when curl is available we fall
back to it for those hosts while keeping the same segmented download logic.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Dict, Iterator, Optional, Tuple

from . import http as httpmod
from .http import HttpError, request

CURL = shutil.which("curl")


class Transport:
    name = "native"

    def probe(self, url: str, headers: Dict[str, str], timeout: float, proxy: Optional[str]) -> dict:
        raise NotImplementedError

    def stream(
        self, url: str, start: int, end: int, headers: Dict[str, str],
        timeout: float, proxy: Optional[str],
    ) -> Iterator[bytes]:
        raise NotImplementedError


def _parse_size(hdrs: Dict[str, str], status: int) -> Tuple[int, bool]:
    supports = (hdrs.get("accept-ranges") or "").lower() == "bytes" or status == 206
    total = 0
    cr = hdrs.get("content-range")
    if cr and "/" in cr:
        try:
            total = int(cr.rsplit("/", 1)[1])
        except ValueError:
            total = 0
    if not total:
        try:
            total = int(hdrs.get("content-length") or 0)
        except ValueError:
            total = 0
    return total, supports


class NativeTransport(Transport):
    name = "native"

    def probe(self, url, headers, timeout, proxy):
        h = dict(headers)
        h["Range"] = "bytes=0-0"
        r = request("GET", url, headers=h, timeout=timeout, proxy=proxy)
        try:
            size, supports = _parse_size(r.headers, r.status)
            return {
                "final_url": getattr(r, "url", url),
                "status": r.status,
                "headers": r.headers,
                "size": size,
                "supports_range": supports,
            }
        finally:
            r.close()

    def stream(self, url, start, end, headers, timeout, proxy):
        h = dict(headers)
        if end is not None:
            h["Range"] = f"bytes={start}-{end - 1}"
        r = request("GET", url, headers=h, timeout=timeout, proxy=proxy)
        if r.status not in (200, 206):
            r.close()
            raise HttpError(r.status, url, r.reason)
        return r.iter_content(65536)


def supports_range(hdrs: Dict[str, str]) -> bool:
    return (hdrs.get("accept-ranges") or "").lower() == "bytes"


class CurlTransport(Transport):
    name = "curl"

    def __init__(self, path: str = CURL or "curl"):
        self.path = path

    def _base(self, proxy: Optional[str]) -> list:
        cmd = [self.path, "-sS", "-L"]
        if proxy:
            cmd += ["-x", proxy]
        return cmd

    def probe(self, url, headers, timeout, proxy):
        marker = "__NDM_META__"
        cmd = self._base(proxy) + [
            "--max-time", str(max(5, int(timeout))),
            "-r", "0-0", "-D", "-", "-o", "/dev/null",
            "-w", marker + '%{http_code} %{url_effective}',
        ]
        for k, v in headers.items():
            cmd += ["-H", f"{k}: {v}"]
        cmd.append(url)
        out = subprocess.run(cmd, capture_output=True, timeout=timeout + 15)
        text = out.stdout.decode("utf-8", "replace")
        if marker not in text:
            raise ConnectionError(f"curl probe failed: {out.stderr.decode('utf-8', 'replace')[:200]}")
        head, _, meta = text.rpartition(marker)
        status_s, _, final = meta.strip().partition(" ")
        try:
            status = int(status_s)
        except ValueError:
            status = 0
        hdrs = _parse_header_block(head)
        size, supports = _parse_size(hdrs, status)
        return {
            "final_url": final.strip() or url,
            "status": status,
            "headers": hdrs,
            "size": size,
            "supports_range": supports,
        }

    def stream(self, url, start, end, headers, timeout, proxy):
        cmd = self._base(proxy) + ["--fail", "-o", "-"]
        if end is not None:
            cmd += ["-r", f"{start}-{end - 1}"]
        for k, v in headers.items():
            cmd += ["-H", f"{k}: {v}"]
        cmd.append(url)
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        try:
            assert p.stdout is not None
            while True:
                buf = p.stdout.read(65536)
                if not buf:
                    break
                yield buf
        finally:
            if p.poll() is None:
                p.kill()
            try:
                p.wait(timeout=5)
            except Exception:
                pass
            if p.returncode not in (0, None) and p.returncode != -9:
                err = p.stderr.read().decode("utf-8", "replace")[:200] if p.stderr else ""
                raise ConnectionError(f"curl failed ({p.returncode}): {err}")


def _parse_header_block(text: str) -> Dict[str, str]:
    """Parse the last response header block from curl -D output."""
    hdrs: Dict[str, str] = {}
    blocks = [b for b in text.replace("\r\n", "\n").split("\n\n") if b.strip()]
    block = blocks[-1] if blocks else text
    for line in block.replace("\r", "").split("\n"):
        if ":" in line:
            k, _, v = line.partition(":")
            hdrs[k.strip().lower()] = v.strip()
    return hdrs


def probe_with_fallback(
    url: str, headers: Dict[str, str], timeout: float, proxy: Optional[str]
) -> Tuple[Transport, dict]:
    """Probe with the stdlib client, falling back to curl when the host rejects h1.1."""
    native = NativeTransport()
    try:
        info = native.probe(url, headers, timeout, proxy)
        if info["status"] < 400:
            return native, info
        if info["status"] in (403, 406, 429) and CURL:
            raise HttpError(info["status"], url, "fallback")
        raise HttpError(info["status"], url, "")
    except (HttpError, ConnectionError, OSError):
        if not CURL:
            raise
        curl = CurlTransport()
        info = curl.probe(url, headers, timeout, proxy)
        if info["status"] >= 400:
            raise HttpError(info["status"], url, "")
        return curl, info


def make(name: str) -> Transport:
    if name == "curl" and CURL:
        return CurlTransport()
    return NativeTransport()
