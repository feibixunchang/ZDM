"""Shared secret for the localhost HTTP API.

The service binds to 127.0.0.1 and used to trust every request: any web page
could read ``/api/config`` (site cookies), write to arbitrary directories or
shut the app down. A random token is generated once per user and stored next
to ``state.json`` with 0600 permissions; the GUI, CLI, native-messaging host
and web UI all present it.

The token is deliberately *not* stored in ``state.json``: that file is copied
around by backups and exported for support, and the secret must not travel
with it.
"""

from __future__ import annotations

import os
import secrets
import stat
from typing import Optional

TOKEN_NAME = "api-token"
_MIN_LEN = 16


def token_path() -> str:
    from .manager import config_dir

    return os.path.join(config_dir(), TOKEN_NAME)


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            value = fh.read().strip()
        return value if len(value) >= _MIN_LEN else ""
    except OSError:
        return ""


def _write(path: str, value: str) -> None:
    """Create the token file atomically, without ever exposing it world-readable."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(value)
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
    except Exception:
        try:
            os.remove(path)
        except OSError:
            pass
        raise


def get_token() -> str:
    """Return this user's API token, creating it on first use."""
    path = token_path()
    value = _read(path)
    if value:
        return value
    candidate = secrets.token_urlsafe(32)
    try:
        _write(path, candidate)
    except OSError:
        # A read-only config dir must not make the service unusable. Fall back
        # to an in-process secret; GUI/CLI share the process or the same user
        # file, so only the current run is affected.
        return candidate
    value = _read(path)
    return value or candidate


def reset_token() -> str:
    """Rotate the secret (used after a suspected local compromise)."""
    path = token_path()
    value = secrets.token_urlsafe(32)
    try:
        os.replace(path, path + ".old")
    except OSError:
        pass
    try:
        _write(path, value)
    except OSError:
        return value
    return _read(path) or value


def header(token: Optional[str] = None) -> dict:
    return {"X-ZDM-Token": token or get_token()}
