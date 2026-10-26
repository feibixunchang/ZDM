"""Transport backends: pure-stdlib HTTP/1.1 client and optional curl fallback.

Some servers (CDN/WAF) only answer HTTP/2 clients with 200 and reject HTTP/1.1
with 403. `http.client` cannot speak HTTP/2, so when curl is available we fall
back to it for those hosts while keeping the same segmented download logic.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from typing import Dict, Iterator, Optional, Tuple

from . import http as httpmod
from .http import HttpError, request
from .runtime import silent_proc_kwargs

CURL = shutil.which("curl")

# ZDM 认得的链接前缀。三端（桌面/网页/扩展）与 aria2 兼容层都以此为唯一口径，
# 免得某一层放行了 ftp、另一层还在报「只支持 http/https」—— 用户看到的就是
# 「粘贴进去说链接不对，但另一个入口却能下」。
SUPPORTED_SCHEMES = ("http://", "https://", "ftp://", "ftps://", "sftp://")
URL_SCHEME_HINT = "只支持 http(s)://、ftp(s)://、sftp:// 链接"


def has_supported_scheme(url) -> bool:
    u = str(url or "").strip().lower()
    return u.startswith(SUPPORTED_SCHEMES)


def scheme_of(url) -> str:
    u = str(url or "").strip().lower()
    for prefix in SUPPORTED_SCHEMES:
        if u.startswith(prefix):
            return prefix[: -len("://")]
    return ""


def _curl_env() -> dict:
    """Environment for the curl child, with the frozen bundle's libs removed.

    PyInstaller puts its ``_internal`` directory on LD_LIBRARY_PATH. That is
    right for the bundled interpreter, but every child process inherits it:
    running the system ``/usr/bin/curl`` then loads the bundle's OpenSSL
    instead of the system one and dies with

        libssl.so.3: version `OPENSSL_3.2.0' not found

    Stripping our own runtime directory from LD_LIBRARY_PATH makes curl use the
    system libraries it was built against.
    """
    env = os.environ.copy()
    lp = env.get("LD_LIBRARY_PATH")
    if not lp:
        return env
    drop = set()
    try:
        from . import runtime

        if runtime.is_frozen():
            drop.add(os.path.realpath(runtime.app_root()))
    except Exception:
        pass
    keep = [p for p in lp.split(":") if p and os.path.realpath(p) not in drop]
    if keep:
        env["LD_LIBRARY_PATH"] = ":".join(keep)
    else:
        env.pop("LD_LIBRARY_PATH", None)
    return env


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
    # Only an actual 206 / Content-Range proves the server honours Range.
    # Some servers advertise "Accept-Ranges: bytes" and then ignore the header
    # and send the whole file with 200; treating those as segmented corrupts
    # the download (every worker writes bytes from offset 0 into its slice).
    supports = status == 206 or "content-range" in hdrs
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
        if end is not None and r.status == 200:
            # We asked for a byte range but got the whole body: writing it would
            # corrupt the file. Abort instead of silently producing garbage.
            r.close()
            raise ConnectionError("server ignored Range (200 instead of 206)")
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
        marker = "__ZDM_META__"
        cmd = self._base(proxy) + [
            "--max-time", str(max(5, int(timeout))),
            "-r", "0-0", "-D", "-", "-o", os.devnull,
            "-w", marker + '%{http_code} %{url_effective}',
        ]
        for k, v in headers.items():
            cmd += ["-H", f"{k}: {v}"]
        cmd.append(url)
        out = subprocess.run(
            cmd, capture_output=True, timeout=timeout + 15, env=_curl_env(),
            **silent_proc_kwargs(),
        )
        text = out.stdout.decode("utf-8", "replace")
        if marker not in text:
            raise ConnectionError(f"curl probe failed: {out.stderr.decode('utf-8', 'replace')[:200]}")
        head, _, meta = text.rpartition(marker)
        status_s, _, final = meta.strip().partition(" ")
        try:
            status = int(status_s)
        except ValueError:
            status = 0
        # curl 连不上（DNS 失败、TLS 断、拒绝连接）时仍然会输出 -w 模板，
        # http_code 是 "000"：以前这被当成一次正常探测（status<400 直接放行），
        # 任务于是带着 size=0 / 不支持 Range 排队空转一轮，报错还很晚才出现。
        # returncode 才是 curl 的真实答案；有真实 HTTP 响应时（哪怕 curl 因为
        # 传输中断返回 18）保留探测结果，那比重新探测更有价值。
        if out.returncode != 0 and status == 0:
            raise ConnectionError(
                f"curl probe failed ({out.returncode}): "
                f"{out.stderr.decode('utf-8', 'replace')[:200]}"
            )
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
        hdr_file = tempfile.NamedTemporaryFile(
            prefix="zdmcurl", suffix=".hdr", delete=False
        )
        hdr_file.close()
        cmd = self._base(proxy) + [
            "--fail",
            "--connect-timeout", str(max(5, int(timeout or 30))),
            # A peer that stops sending must not pin a worker forever. 60s at
            # <1KB/s is far below any healthy download but catches black holes.
            "--speed-limit", "1024",
            "--speed-time", "60",
            "-D", hdr_file.name,
            "-o", "-",
        ]
        if end is not None:
            cmd += ["-r", f"{start}-{end - 1}"]
        for k, v in headers.items():
            cmd += ["-H", f"{k}: {v}"]
        cmd.append(url)
        p = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
            env=_curl_env(), **silent_proc_kwargs(),
        )
        err = ""
        checked = False
        try:
            assert p.stdout is not None
            while True:
                buf = p.stdout.read(65536)
                if not buf:
                    break
                if not checked:
                    checked = True
                    # curl follows a 200 even when we asked for a range; writing
                    # that whole body at ``start`` would corrupt the file. This
                    # mirrors NativeTransport's explicit guard.
                    if end is not None and _dump_status(hdr_file.name) == 200:
                        p.kill()
                        raise ConnectionError("server ignored Range (200 instead of 206)")
                yield buf
        finally:
            if p.poll() is None:
                p.kill()
            try:
                p.wait(timeout=5)
            except Exception:
                pass
            try:
                os.remove(hdr_file.name)
            except OSError:
                pass
            if p.returncode not in (0, None) and p.returncode != -9:
                err = p.stderr.read().decode("utf-8", "replace")[:200] if p.stderr else ""
        if err:
            raise ConnectionError(f"curl failed ({p.returncode}): {err}")


def _dump_status(path: str) -> int:
    """HTTP status from curl's ``-D`` dump (last response block)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return 0
    for line in reversed(text.replace("\r", "").split("\n")):
        if line.startswith("HTTP/"):
            parts = line.split()
            if len(parts) >= 2:
                try:
                    return int(parts[1])
                except ValueError:
                    return 0
            break
    return 0


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


def transport_for_url(url: str) -> Optional["Transport"]:
    """非 HTTP 协议在这里分流；返回 None 表示按 http/https 走。

    延迟 import：ftp.py / sftp.py 反过来要继承本模块的 Transport，模块级互相
    import 会形成循环。
    """
    u = str(url or "").lower()
    if u.startswith(("ftp://", "ftps://")):
        from .ftp import FtpTransport

        return FtpTransport(tls=u.startswith("ftps://"))
    if u.startswith("sftp://"):
        from .sftp import SftpTransport

        return SftpTransport()
    return None


def probe_with_fallback(
    url: str, headers: Dict[str, str], timeout: float, proxy: Optional[str]
) -> Tuple[Transport, dict]:
    """Probe with the stdlib client, falling back to curl only when needed.

    ``curl`` is the HTTP/2 escape hatch for CDNs that reject HTTP/1.1, so it is
    only tried for 403/406/429 or a connection-level failure. Ordinary HTTP
    errors (404, 500, ...) are real answers and must be reported as such -
    retrying them through curl used to hide the status behind a confusing
    "curl probe failed" message (and, in the frozen bundle, behind an OpenSSL
    loading error).

    ftp/ftps/sftp 在进 HTTP 那套之前先分流：它们的「探测」是问服务器 SIZE/stat，
    没有 HTTP 状态码，也不该被 403/406/429 的 curl 兜底逻辑一遍遍重放。
    """
    t = transport_for_url(url)
    if t is not None:
        return t, t.probe(url, headers, timeout, proxy)

    native = NativeTransport()
    fallback_status = (403, 406, 429)

    def via_curl() -> Tuple[Transport, dict]:
        curl = CurlTransport()
        info = curl.probe(url, headers, timeout, proxy)
        if info["status"] >= 400:
            raise HttpError(info["status"], url, "")
        return curl, info

    try:
        info = native.probe(url, headers, timeout, proxy)
    except HttpError as e:
        if CURL and e.status in fallback_status:
            try:
                return via_curl()
            except Exception:
                pass
        raise
    except (ConnectionError, OSError):
        if not CURL:
            raise
        return via_curl()

    if info["status"] < 400:
        return native, info
    if CURL and info["status"] in fallback_status:
        try:
            return via_curl()
        except Exception:
            pass
    raise HttpError(info["status"], url, "")


def make(name: str) -> Transport:
    """按 .part 元数据里记下的通道名重建 Transport（续传时不会重新探测）。

    分块名丢了就退回 NativeTransport 等于拿 HTTP 客户端去读 FTP 地址，
    所以这里必须认得每一条通道。
    """
    if name == "curl" and CURL:
        return CurlTransport()
    if name in ("ftp", "ftps"):
        from .ftp import FtpTransport

        return FtpTransport(tls=name == "ftps")
    if name == "sftp":
        from .sftp import SftpTransport

        return SftpTransport()
    return NativeTransport()
