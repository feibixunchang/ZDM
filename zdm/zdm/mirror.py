"""Optional public-mirror acceleration for GitHub / Hugging Face downloads.

Both hosts are frequently slow or unreachable from mainland China. When the
user turns on ``mirror_accel``, links are rewritten through a public mirror at
add time; the original URL is stored on the task so the engine can fall back to
it when the mirror fails (see Download._prepare / start).

Only file-ish GitHub URLs are rewritten. Repo pages, API calls and issue
attachments are not binaries and would just confuse the mirror.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

GH_PROXY = "https://gh-proxy.com/"
HF_MIRROR_HOST = "hf-mirror.com"

# raw/codeload/objects serve file bytes directly and work through the proxy.
_GH_DIRECT_HOSTS = (
    "raw.githubusercontent.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
)
_GH_PATH_HINTS = (
    "/releases/download/",
    "/releases/latest/download/",
    "/archive/",
    "/raw/",
)


def accelerate(url: str) -> str:
    """Return a mirror URL for *url*, or "" when there is nothing to do."""
    try:
        p = urlsplit(str(url or "").strip())
    except ValueError:
        return ""
    if p.scheme not in ("http", "https"):
        return ""
    host = (p.hostname or "").lower()
    if not host:
        return ""

    if host == "github.com":
        path = p.path or ""
        if any(h in path for h in _GH_PATH_HINTS):
            return GH_PROXY + str(url).strip()
        return ""

    if host in _GH_DIRECT_HOSTS:
        return GH_PROXY + str(url).strip()

    if host == "huggingface.co" and "/resolve/" in (p.path or ""):
        # hf-mirror mirrors the site path-for-path; signed query params (if
        # any) are preserved so private/gated links keep working when the
        # token is carried in the query string.
        return urlunsplit((p.scheme, HF_MIRROR_HOST, p.path, p.query, p.fragment))

    return ""
