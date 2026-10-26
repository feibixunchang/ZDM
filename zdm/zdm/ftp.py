"""FTP / FTPS 通道：把 ftplib 的 SIZE + REST 变成「可分块、可续传」的下载。

ZDM 的下载引擎对 Transport 只有两个要求：probe() 问出文件多大、能不能按字节
范围取；stream(start, end) 取其中一段。FTP 恰好两样都有 —— SIZE 报长度、
REST 报偏移 —— 所以 ftp:// 任务不用另写一套下载逻辑，和 http 任务一样多线程
分块、暂停后续传。

两条纪律值得写在这里：

* REST 的回复必须逐条核对。ftplib 自己的 ``ntransfercmd(rest=...)`` 发完 REST
  就不管回复了，遇到「口头答应、实际从 0 开始发」的服务器，我们会把文件开头
  的字节写进中段偏移 —— 体积对、内容坏，比报错更难发现。这里自己发 REST、
  只接受 3xx，然后才发 RETR。
* 控制命令里的换行要拦。USER/PASS/SIZE/RETR 都是按行发给服务器的，链接里混进
  \\r\\n 就等于让链接作者往控制通道里再塞一条命令。
"""

from __future__ import annotations

import ftplib
import socket
import ssl
import urllib.parse
from typing import Iterator, Optional

from .transport import Transport

# 与 curl 的约定一致：ftps:// 默认是「明文开场再 AUTH TLS」的显式加密，端口 21；
# 只有点名 990 才按隐式 FTPS（一连上就是 TLS）处理。
DEFAULT_PORT = {"ftp": 21, "ftps": 21}
IMPLICIT_PORT = 990
BLOCK = 65536
FTP_SCHEMES = ("ftp://", "ftps://")

# 匿名登录按 RFC 1635 的惯例给个能过审的邮箱占位。
ANON_USER = "anonymous"
ANON_PASS = "zdm@localhost"


class FtpTarget:
    """一条 ftp/ftps 链接拆出来的连接参数。"""

    __slots__ = ("scheme", "host", "port", "user", "password", "path", "implicit", "url")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    @property
    def tls(self) -> bool:
        return self.scheme == "ftps"


class FtpUrlError(ValueError):
    """链接本身有问题（协议不对、缺主机名、夹带控制字符）。"""


def is_ftp(url) -> bool:
    u = str(url or "").lower()
    return u.startswith(FTP_SCHEMES)


def parse(url: str) -> FtpTarget:
    u = str(url or "").strip()
    if not u:
        raise FtpUrlError("FTP 链接为空")
    try:
        p = urllib.parse.urlsplit(u)
    except ValueError as e:
        raise FtpUrlError(f"FTP 链接无法解析：{e}") from e
    scheme = (p.scheme or "").lower()
    if scheme not in ("ftp", "ftps"):
        raise FtpUrlError("不是 ftp/ftps 链接")
    try:
        host = p.hostname
        port = p.port
    except ValueError as e:
        raise FtpUrlError(f"FTP 链接无法解析：{e}") from e
    if not host:
        raise FtpUrlError("FTP 链接里缺少主机名")
    user = urllib.parse.unquote(p.username or "") or ANON_USER
    password = urllib.parse.unquote(p.password or "") or (
        ANON_PASS if user == ANON_USER else ""
    )
    path = urllib.parse.unquote(p.path or "") or "/"
    for label, value in (("用户名", user), ("密码", password), ("路径", path)):
        if any(c in value for c in "\r\n\x00"):
            raise FtpUrlError(f"FTP 链接的{label}里含非法控制字符")
    port = port or DEFAULT_PORT[scheme]
    return FtpTarget(
        scheme=scheme,
        host=host,
        port=port,
        user=user,
        password=password,
        path=path,
        implicit=scheme == "ftps" and port == IMPLICIT_PORT,
        url=u,
    )


class _ImplicitFtps(ftplib.FTP_TLS):
    """990 端口的隐式 FTPS：一连上就是 TLS，控制通道没有明文阶段。"""

    def connect(self, host="", port=0, timeout=None):
        port = port or self.default_port
        raw = socket.create_connection((host, port), timeout)
        try:
            self.sock = self.context.wrap_socket(raw, server_hostname=host)
        except Exception:
            raw.close()
            raise
        self.host = host
        self.port = port
        self.timeout = timeout
        self.af = self.sock.family
        # makefile 的模式必须是 "r" + encoding：ftplib 的行读取按文本处理，
        # 给它 "rb" 会让后面每一行都变成 bytes，报错信息还很难懂。
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome


def _timeout(timeout) -> float:
    try:
        t = float(timeout)
    except (TypeError, ValueError):
        t = 0.0
    return t if t > 0 else 30.0


def connect(target: FtpTarget, timeout) -> ftplib.FTP:
    """建控制连接：登录 +（FTPS 时）把数据通道也升到 TLS。"""
    t = _timeout(timeout)
    if target.scheme == "ftp":
        conn: ftplib.FTP = ftplib.FTP()
        conn.connect(target.host, target.port, t)
    else:
        ctx = ssl.create_default_context()
        if target.implicit:
            conn = _ImplicitFtps(timeout=t, context=ctx)
            conn.connect(target.host, target.port, t)
        else:
            conn = ftplib.FTP_TLS(context=ctx)
            conn.connect(target.host, target.port, t)
            conn.auth()
    conn.login(target.user, target.password)
    if target.tls:
        # 数据通道也必须走 TLS（PROT P）。有些服务器 AUTH 成功但拒绝 PROT P，
        # 那种时候宁可报错，也不要明文传出文件内容。
        conn.prot_p()
    conn.voidcmd("TYPE I")
    return conn


def _close(conn) -> None:
    if conn is None:
        return
    try:
        conn.close()
    except Exception:
        pass


def _probe_size(conn: ftplib.FTP, path: str) -> Optional[int]:
    try:
        return int(conn.size(path))
    except (ftplib.error_reply, ftplib.error_temp, ftplib.error_perm, OSError, ValueError):
        return None


def _rest_works(conn: ftplib.FTP) -> bool:
    """问服务器「能不能从任意字节偏移开始发」。3xx 才算能。

    这里不能顺手 voidresp()：REST 的 330 就是这条命令的全部回复，再读一行会把
    探测卡到超时。
    """
    try:
        resp = conn.sendcmd("REST 0")
    except (ftplib.Error, OSError):
        return False
    return resp[:1] == "3"


class FtpTransport(Transport):
    """一个实例可以服务同一条任务的多个工作线程：每次 stream() 各开各的连接。

    FTP 没有 HTTP 那种复用，分 N 块就是 N 条控制连接 + N 条数据连接，这也正是
    「多线程下载」在 FTP 上的实现方式。
    """

    def __init__(self, tls: bool = False):
        self.name = "ftps" if tls else "ftp"

    def probe(self, url, headers, timeout, proxy):
        target = parse(url)
        conn = connect(target, timeout)
        try:
            size = _probe_size(conn, target.path) or 0
            segmented = size > 0 and _rest_works(conn)
            return {
                # final_url 原样回填：链接里的账号密码要留给后续每一条连接用，
                # 改成「去掉凭据的地址」会让续传直接登不进去。
                "final_url": target.url,
                "status": 226,
                "headers": {},
                "size": size,
                "supports_range": bool(segmented),
            }
        finally:
            _close(conn)

    def stream(self, url, start, end, headers, timeout, proxy) -> Iterator[bytes]:
        target = parse(url)
        return self._read(target, int(start or 0), end, timeout)

    def _read(self, target: FtpTarget, start: int, end, timeout) -> Iterator[bytes]:
        limit = None if end is None else max(0, int(end) - start)
        conn = None
        data = None
        try:
            conn = connect(target, timeout)
            if start > 0:
                try:
                    resp = conn.sendcmd(f"REST {start}")
                except (ftplib.Error, OSError) as e:
                    raise ConnectionError(f"FTP 断点请求失败：{e}") from e
                if resp[:1] != "3":
                    raise ConnectionError(
                        f"FTP 服务器不肯从 {start} 字节开始（REST 回了 {resp}）"
                    )
            try:
                data, _expected = conn.ntransfercmd(f"RETR {target.path}")
            except ftplib.error_perm as e:
                raise FileNotFoundError(f"FTP 上取不到该文件：{e}") from e
            except (ftplib.Error, OSError) as e:
                raise ConnectionError(f"FTP 传输请求失败：{e}") from e
            data.settimeout(_timeout(timeout))
            fp = data.makefile("rb")
            try:
                remaining = limit
                while True:
                    want = BLOCK if remaining is None else min(BLOCK, remaining)
                    if want <= 0:
                        break
                    buf = fp.read(want)
                    if not buf:
                        # 没给上限就是「读到文件尾」，EOF 是正常结局；给了上限
                        # 却没读满，就是服务器少发了 —— 必须报错，否则 .part 里
                        # 是一段空洞，最后照样能凑成同一个体积。
                        if remaining is not None and remaining > 0:
                            raise ConnectionError(
                                f"FTP 传输提前结束，还差约 {remaining} 字节"
                            )
                        break
                    if remaining is not None:
                        remaining -= len(buf)
                    yield buf
            finally:
                try:
                    fp.close()
                except Exception:
                    pass
        finally:
            if data is not None:
                try:
                    data.close()
                except Exception:
                    pass
            # 取完一段就断开：这段之后的字节还在服务器上排着，读 226 只会白等。
            _close(conn)
