"""Minimal dependency-free HTTP client with Range/redirect/timeout support."""

from __future__ import annotations

import email
import http.client
import socket
import ssl
import urllib.parse
from typing import Dict, Iterator, Optional, Tuple

DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
MAX_REDIRECTS = 8
CHUNK = 65536


class HttpError(Exception):
    def __init__(self, status: int, url: str, message: str = ""):
        super().__init__(f"HTTP {status} {message}".strip())
        self.status = status
        self.url = url


class Response:
    """Streaming HTTP response."""

    def __init__(self, conn, resp):
        self._conn = conn
        self._resp = resp
        self.status: int = resp.status
        self.reason: str = resp.reason
        self.headers: Dict[str, str] = {}
        for k, v in resp.getheaders():
            self.headers[k.lower()] = v

    @property
    def content_length(self) -> Optional[int]:
        v = self.headers.get("content-length")
        try:
            return int(v) if v is not None else None
        except ValueError:
            return None

    def iter_content(self, size: int = CHUNK) -> Iterator[bytes]:
        try:
            while True:
                buf = self._resp.read(size)
                if not buf:
                    break
                yield buf
        finally:
            self.close()

    def read(self, size: Optional[int] = None) -> bytes:
        try:
            return self._resp.read(size) if size else self._resp.read()
        finally:
            self.close()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def env_proxy(scheme: str) -> Optional[str]:
    """Read a proxy from the environment (lowercase wins, ALL_PROXY as fallback)."""
    import os

    keys = [f"{scheme}_proxy", f"{scheme.upper()}_PROXY", "all_proxy", "ALL_PROXY"]
    for k in keys:
        v = os.environ.get(k)
        if v:
            return v if "://" in v else "http://" + v
    return None


def _connect(parsed: urllib.parse.ParseResult, timeout: float, proxy: Optional[str]):
    if proxy:
        pu = urllib.parse.urlparse(proxy if "://" in proxy else "http://" + proxy)
        host, port = pu.hostname, pu.port or 8080
    else:
        host, port = parsed.hostname, parsed.port

    if parsed.scheme == "https":
        ctx = ssl.create_default_context()
        if proxy:
            conn = http.client.HTTPSConnection(
                host, port, timeout=timeout, context=ctx, blocksize=CHUNK
            )
            conn.set_tunnel(parsed.hostname, parsed.port or 443)
            return conn
        return http.client.HTTPSConnection(
            host, port, timeout=timeout, context=ctx, blocksize=CHUNK
        )

    port = port or 80
    return http.client.HTTPConnection(host, port, timeout=timeout, blocksize=CHUNK)


def request(
    method: str,
    url: str,
    headers: Optional[Dict[str, str]] = None,
    body: Optional[bytes] = None,
    timeout: float = 20.0,
    proxy: Optional[str] = None,
    follow_redirects: bool = True,
) -> Response:
    """Perform an HTTP request, following redirects.

    proxy=None  -> use http_proxy/https_proxy from the environment
    proxy=""    -> bypass the proxy
    """
    explicit_proxy = proxy is not None
    current = url
    req_headers = dict(headers or {})
    for _ in range(MAX_REDIRECTS + 1):
        parsed = urllib.parse.urlparse(current)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"unsupported scheme: {parsed.scheme}")

        cur_proxy = proxy if explicit_proxy else env_proxy(parsed.scheme)
        conn = _connect(parsed, timeout, cur_proxy)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        h = {
            "Host": parsed.netloc,
            "User-Agent": DEFAULT_UA,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "Connection": "close",
        }
        for k, v in req_headers.items():
            if v is None:
                continue
            h[k] = v
        if body is not None:
            h.setdefault("Content-Length", str(len(body)))

        try:
            conn.request(method, path, body=body, headers=h)
            resp = Response(conn, conn.getresponse())
        except (socket.error, http.client.HTTPException, ssl.SSLError) as e:
            try:
                conn.close()
            except Exception:
                pass
            raise ConnectionError(f"{type(e).__name__}: {e}") from e

        if follow_redirects and resp.status in (301, 302, 303, 307, 308):
            loc = resp.headers.get("location")
            resp.close()
            if not loc:
                raise HttpError(resp.status, current, "redirect without location")
            current = urllib.parse.urljoin(current, loc)
            if resp.status == 303 and method != "HEAD":
                method = "GET"
                body = None
            # drop authorization-like headers on cross-host redirect
            old_host = urllib.parse.urlparse(current).hostname
            if old_host and parsed.hostname and old_host != parsed.hostname:
                for k in ("authorization", "cookie"):
                    req_headers.pop(k, None)
            continue

        resp.url = current  # type: ignore[attr-defined]
        return resp

    raise HttpError(310, url, "too many redirects")


def filename_from_headers(headers: Dict[str, str], url: str) -> str:
    """Resolve a usable filename from Content-Disposition or URL."""
    cd = headers.get("content-disposition") or headers.get("Content-Disposition")
    if cd:
        msg = email.message.Message()
        msg["Content-Disposition"] = cd
        name = msg.get_param("filename", header="content-disposition")
        if not name:
            name = msg.get_param("filename*", header="content-disposition")
        if name:
            if isinstance(name, tuple):  # RFC2231 (charset, lang, value)
                name = name[2]
            try:
                name = urllib.parse.unquote(name, errors="replace")
            except Exception:
                pass
            name = name.strip().strip('"').split("/")[-1].split("\\")[-1]
            if name:
                return sanitize_filename(name)

    path = urllib.parse.urlparse(url).path
    base = path.rstrip("/").split("/")[-1] if path else ""
    if base:
        try:
            base = urllib.parse.unquote(base, errors="replace")
        except Exception:
            pass
        if base:
            return sanitize_filename(base)
    return "download"


_ILLEGAL = r'[<>:"|?*\x00-\x1f]'
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_filename(name: str, maxlen: int = 180) -> str:
    """Strip separators and characters that are illegal on Windows/macOS."""
    import re

    name = name.replace("\x00", "")
    name = name.replace("/", "_").replace("\\", "_")
    name = re.sub(_ILLEGAL, "_", name)
    name = name.strip().strip(".") or "download"
    stem = name.split(".")[0].upper()
    if stem in _RESERVED:
        name = "_" + name
    return name[:maxlen]


def probe(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 20.0,
    proxy: Optional[str] = None,
) -> Tuple[str, Dict[str, str]]:
    """Resolve final URL + headers. Returns (final_url, headers)."""
    h = dict(headers or {})
    h["Range"] = "bytes=0-0"
    try:
        r = request("GET", url, headers=h, timeout=timeout, proxy=proxy)
        hdrs = r.headers
        r.close()
        return getattr(r, "url", url), hdrs
    except Exception:
        r = request("HEAD", url, headers=(headers or {}), timeout=timeout, proxy=proxy)
        hdrs = r.headers
        final = getattr(r, "url", url)
        r.close()
        return final, hdrs
