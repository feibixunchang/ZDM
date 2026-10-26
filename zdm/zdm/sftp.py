"""SFTP 通道：需要 paramiko。

为什么不能像 FTP 那样只用标准库：SFTP 跑在 SSH 通道里，握手要有密码学实现，
标准库没有。有了 paramiko 之后，SFTP 反而是最省事的一路 —— 远端文件可以直接
seek，所以「问大小 + 按范围读」这两件事和 HTTP/FTP 一模一样，分块下载与断点
续传不用打折。

装不上 paramiko 的机器（例如没跑 install.sh、直接跑单文件包）会在添加
sftp:// 链接时就被拒绝，同时告诉用户缺什么、怎么补 —— 而不是建一条永远
下载不完的假任务。
"""

from __future__ import annotations

import errno
import os
import stat as statmod
import urllib.parse
from typing import Iterator, Optional

from .transport import Transport

SFTP_SCHEMES = ("sftp://",)
BLOCK = 65536
DEFAULT_TIMEOUT = 30.0

_paramiko = None
_import_error = ""


class SftpUrlError(ValueError):
    """链接本身有问题（协议不对、缺主机名）。"""


def _load():
    global _paramiko, _import_error
    if _paramiko is not None or _import_error:
        return _paramiko
    try:
        import paramiko

        _paramiko = paramiko
    except Exception as e:  # noqa: BLE001  可选依赖：装不上就如实记录原因
        _import_error = f"{type(e).__name__}: {e}"
    return _paramiko


def is_sftp(url) -> bool:
    return str(url or "").lower().startswith(SFTP_SCHEMES)


def available() -> bool:
    return _load() is not None


def version() -> str:
    """已加载的 paramiko 版本；没装/装不上回空串（给 --selfcheck 和设置页看）。"""
    _load()
    return getattr(_paramiko, "__version__", "") if _paramiko is not None else ""


def missing_hint() -> str:
    why = f"（{_import_error}）" if _import_error else ""
    return (
        "这台机器上的 ZDM 没有 SFTP 通道：它需要 Python 库 paramiko"
        + why
        + "。补上即可：pip install --user paramiko，或重新运行 install.sh。"
        "（ftp:// 与 ftps:// 不需要额外依赖，可直接使用。）"
    )


def parse(url: str) -> dict:
    u = str(url or "").strip()
    try:
        p = urllib.parse.urlsplit(u)
    except ValueError as e:
        raise SftpUrlError(f"SFTP 链接无法解析：{e}") from e
    if (p.scheme or "").lower() != "sftp":
        raise SftpUrlError("不是 sftp:// 链接")
    try:
        host = p.hostname
        port = p.port
    except ValueError as e:
        raise SftpUrlError(f"SFTP 链接无法解析：{e}") from e
    if not host:
        raise SftpUrlError("SFTP 链接里缺少主机名")
    path = urllib.parse.unquote(p.path or "") or "."
    if any(c in path for c in "\r\n\x00"):
        raise SftpUrlError("SFTP 链接的路径里含非法控制字符")
    return {
        "host": host,
        "port": port or 22,
        "username": urllib.parse.unquote(p.username) if p.username else None,
        "password": urllib.parse.unquote(p.password) if p.password else None,
        "path": path,
        "url": u,
    }


def host_keys_path() -> str:
    """ZDM 自己的 known_hosts，避免往用户的 ~/.ssh/known_hosts 里写东西。

    首次连接按 TOFU 记录：之后同一主机换了密钥（中间人、服务器重装）一律拒绝，
    这条判断比「每次都接受」重要。
    """
    try:
        from .manager import config_dir

        d = config_dir()
    except Exception:  # noqa: BLE001  循环导入/权限问题都不该挡住下载
        d = os.path.join(os.path.expanduser("~"), ".config", "zdm")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return os.path.join(d, "sftp-known_hosts")


class _TofuPolicy:
    """接受首次见到的主机密钥，并真的把它记下来。

    「接受」必须是把密钥塞进 client 的 host keys：paramiko 的 save_host_keys
    只写这一份，而它只在这份里查不到主机时才调用本策略。少这一步，落盘的就是
    一个空文件，下次连接仍然是「首次」——每次无条件信任，中间人防线形同虚设。
    """

    def __init__(self):
        self.accepted = False

    def missing_host_key(self, client, hostname, key):
        client.get_host_keys().add(hostname, key.get_name(), key)
        self.accepted = True


def _timeout(timeout) -> float:
    try:
        v = float(timeout)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    return v if v > 0 else DEFAULT_TIMEOUT


def _connect(target: dict, timeout):
    """返回 (SSHClient, SFTPClient)；调用方负责两个都关掉。"""
    if _load() is None:
        raise ValueError(missing_hint())
    client = _paramiko.SSHClient()
    path = host_keys_path()
    try:
        client.load_host_keys(path)
    except Exception:  # noqa: BLE001  文件不存在/坏行都不该挡住首连
        pass
    try:
        client.load_system_host_keys()
    except Exception:  # noqa: BLE001
        pass
    policy = _TofuPolicy()
    client.set_missing_host_key_policy(policy)
    t = _timeout(timeout)
    kw = {
        "hostname": target["host"],
        "port": target["port"],
        "timeout": t,
        "banner_timeout": t,
        "auth_timeout": t,
        "username": target["username"],
    }
    if target["password"]:
        # 链接里写了密码就只用密码：再去问 ssh-agent / 默认私钥只会更慢，
        # 而且可能拿另一个身份登进去，下到的却不是用户要的那份文件。
        kw["password"] = target["password"]
        kw["look_for_keys"] = False
        kw["allow_agent"] = False
    try:
        client.connect(**kw)
    except _paramiko.BadHostKeyException as e:
        # 密钥和 TOFU 记下过的对不上：这可能是中间人，也可能是服务器重装。
        # 无论哪种都不能「先下完再说」，所以这里只报错，并说清怎么解除。
        _close(client)
        raise ConnectionError(
            f"SFTP 主机密钥与首次记录的不一致（{target['host']}:{target['port']}），"
            "已拒绝连接：可能被中间人截了，也可能是服务器换过密钥。"
            f"确认无误后删掉 {path} 里对应主机的那一行，再重新下载。"
        ) from e
    except Exception as e:  # noqa: BLE001  paramiko 异常种类太杂，统一成一句人话
        _close(client)
        raise ConnectionError(
            f"SFTP 连接失败（{target['host']}:{target['port']}）：{e}"
        ) from e
    if policy.accepted:
        try:
            # 不用 client.save_host_keys：它先从 load_host_keys 记住的那个路径重读
            # 一遍，而首次连接时这文件还不存在，重读报错会把整个落盘动作吞掉 ——
            # 结果就是「每次都当首次、每次都信」，TOFU 白做。
            client.get_host_keys().save(path)
        except OSError:
            pass
    try:
        return client, client.open_sftp()
    except Exception as e:  # noqa: BLE001
        _close(client)
        raise ConnectionError(f"SFTP 通道打开失败：{e}") from e


def _close(*objs) -> None:
    for closer in (getattr(o, "close", None) for o in objs):
        if closer is None:
            continue
        try:
            closer()
        except Exception:  # noqa: BLE001
            pass


def _not_readable(e: OSError) -> Exception:
    """paramiko 只给 errno：文件不存在和没权限要分开说，两者用户能做的事不一样。"""
    if getattr(e, "errno", None) == errno.EACCES:
        return ConnectionError(f"SFTP 上没有读取权限：{e}")
    return FileNotFoundError(f"SFTP 上取不到该文件：{e}")


class SftpTransport(Transport):
    name = "sftp"

    def probe(self, url, headers, timeout, proxy):
        target = parse(url)
        client, sftp = _connect(target, timeout)
        try:
            st = sftp.stat(target["path"])
            if st.st_mode and statmod.S_ISDIR(st.st_mode):
                raise ValueError("SFTP 上这个地址是目录，不是文件")
            return {
                "final_url": target["url"],
                "status": 226,
                "headers": {},
                "size": int(st.st_size or 0),
                # SFTP 是随机访问协议：seek 是协议自带的，不需要像 FTP 那样
                # 先问服务器认不认 REST。
                "supports_range": True,
            }
        except OSError as e:
            raise _not_readable(e) from e
        finally:
            _close(sftp, client)

    def stream(self, url, start, end, headers, timeout, proxy) -> Iterator[bytes]:
        target = parse(url)
        return self._read(target, int(start or 0), end, timeout)

    def _read(self, target: dict, start: int, end, timeout) -> Iterator[bytes]:
        limit = None if end is None else max(0, int(end) - start)
        client = sftp = f = None
        try:
            client, sftp = _connect(target, timeout)
            try:
                f = sftp.open(target["path"], "rb")
            except OSError as e:
                # 只翻译「打不开」：读中途的 OSError 是链路问题，要留给重试。
                raise _not_readable(e) from e
            f.set_pipelined(True)
            if start > 0:
                f.seek(start)
            remaining = limit
            while True:
                want = BLOCK if remaining is None else min(BLOCK, remaining)
                if want <= 0:
                    break
                buf = f.read(want)
                if not buf:
                    missing = want if remaining is None else remaining
                    if missing > 0:
                        raise ConnectionError(f"SFTP 传输提前结束，还差约 {missing} 字节")
                    break
                if remaining is not None:
                    remaining -= len(buf)
                yield buf
        finally:
            _close(f, sftp, client)
