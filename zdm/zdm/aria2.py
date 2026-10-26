"""aria2 兼容的 JSON-RPC 2.0 子集：让 AriaNg / Motrix 一类现成客户端直接驱动 ZDM。

这一层只做「翻译」：任务仍然进同一个 Manager、走同一条事件流，桌面端 / 网页版
和 RPC 客户端看到的是同一份状态，不存在第二套队列。

覆盖 http/https/ftp/ftps/sftp 任务（和 ZDM 本身支持的协议一致）。BT / 磁力 /
Metalink 不在 ZDM 的范围内，问到就明确报错，不返回一个「看起来成功」的空任务。
"""

from __future__ import annotations

import hmac
import json
import os
import uuid
from typing import Callable, Dict, List, Optional

from . import __version__ as VERSION
from .manager import MAX_PRIORITY, Manager
from .transport import URL_SCHEME_HINT, scheme_of

# ZDM 的任务状态 → aria2 客户端认的那几个词。
# canceled 归到 paused：ZDM 里被取消的记录还能重新点「继续」（会从头下），
# 而 aria2 的 complete / error / removed 都是「不会再动」的状态，映射过去等于
# 告诉客户端这条已经结束了。
STATUS_MAP = {
    "downloading": "active",
    "merging": "active",
    "queued": "waiting",
    "paused": "paused",
    "canceled": "paused",
    "completed": "complete",
    "error": "error",
}
# aria2 的 tellActive 里连暂停中的任务也算 active（它们仍在内存里，既不属于
# waiting 也不属于 stopped），照抄这个行为，AriaNg 的列表才不会少一截。
ACTIVE_STATES = ("downloading", "merging", "paused", "canceled")
WAITING_STATES = ("queued",)
STOPPED_STATES = ("completed", "error")

_SIZE_UNITS = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}

# addUri 的 per-task 选项：aria2 用连字符，ZDM 的建任务参数用下划线。
# 值为 "_ignore" 的是「ZDM 没有对应概念，但忽略它不会让结果变假」的选项：
# 认不出的键名一律报错，免得用户以为某个开关生效了。
_TASK_OPTIONS: Dict[str, str] = {
    "dir": "dir",
    "out": "filename",
    "split": "connections",
    "max-connection-per-server": "connections",
    "all-proxy": "proxy",
    "http-proxy": "proxy",
    "https-proxy": "proxy",
    "header": "headers",
    "referer": "referer",
    "user-agent": "useragent",
    "pause": "paused",
    "auto-file-renaming": "_ignore",
    "check-integrity": "_ignore",
    "continue": "_ignore",
    "dry-run": "_ignore",
    "file-allocation": "_ignore",
    "log": "_ignore",
    "no-file-allocation-limit": "_ignore",
    "summary-interval": "_ignore",
    "use-head": "_ignore",
}

# aria2 的全局选项 → ZDM 的设置键。限速类取值带 K/M 后缀，单独换算。
_GLOBAL_OPTIONS: Dict[str, str] = {
    "max-concurrent-downloads": "max_active",
    "max-overall-download-limit": "speed_limit",
    "max-connection-per-server": "connections",
    "split": "connections",
    "max-tries": "max_retries",
    "timeout": "timeout",
    "auto-save-interval": "_ignore",
    "log": "_ignore",
    "summary-interval": "_ignore",
}
_SIZED_GLOBAL = ("max-overall-download-limit",)


class RpcError(Exception):
    """JSON-RPC 错误。code 用 aria2 的约定：-1 是它自己的业务错误。

    http_401 只给鉴权失败用：aria2 也是回 401，客户端才会提示「RPC secret 不对」
    而不是笼统一句「请求失败」。
    """

    def __init__(self, message: str, code: int = -1, http_401: bool = False):
        super().__init__(message)
        self.message = message
        self.code = code
        self.http_401 = http_401


def parse_size(value, name: str = "大小") -> int:
    """aria2 的限速写法：裸字节数，或 10K / 1M / 2G（1024 进制）。"""
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        raise ValueError(f"{name} 需要字节数或 10K/1M 这样的大小（收到 {value!r}）")
    if isinstance(value, (int, float)):
        num = int(value)
    else:
        text = str(value).strip().upper().replace("B", "").replace("IB", "")
        if not text:
            return 0
        unit = text[-1:]
        if unit in _SIZE_UNITS and unit != "":
            digits, mult = text[:-1], _SIZE_UNITS[unit]
        else:
            digits, mult = text, 1
        try:
            num = int(float(digits) * mult)
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"{name} 需要字节数或 10K/1M 这样的大小（收到 {value!r}）"
            ) from e
    if num < 0:
        raise ValueError(f"{name} 不能是负数（收到 {value!r}）")
    return num


def _as_list(value) -> list:
    return list(value) if isinstance(value, (list, tuple)) else []


def _pick(attrs: dict, keys) -> dict:
    """按客户端点名的属性返回；认不出的键不回（aria2 也是这个行为）。"""
    wanted = [str(k) for k in _as_list(keys)]
    if not wanted:
        return attrs
    return {k: attrs[k] for k in wanted if k in attrs}


def attrs_of(task: dict) -> dict:
    """把 ZDM 的任务快照摊成 aria2 的 DownloadResult 属性表。

    aria2 把长度/速度全部当**字符串**返回，客户端按字符串解析，所以这里不给数字。
    """
    status = STATUS_MAP.get(str(task.get("status") or ""), "active")
    total = int(task.get("size") or 0)
    done = int(task.get("downloaded") or 0)
    speed = int(task.get("speed") or 0)
    directory = str(task.get("directory") or "")
    filename = str(task.get("filename") or "")
    url = str(task.get("url") or "")
    error = str(task.get("error") or "")
    files: List[dict] = []
    if filename:
        files = [
            {
                "index": "1",
                "path": os.path.join(directory, filename),
                "length": str(total),
                "selected": "true",
                "completedLength": str(done),
                "uri": [{"status": "completed", "uri": url}],
            }
        ]
    out: Dict[str, object] = {
        "gid": str(task.get("id") or ""),
        # aria2 把 HTTP 和 FTP 算同一种 mode（都是单文件下载引擎），区别只在
        # protocol —— 按 ZDM 这条任务真实的链接协议回填。
        "mode": "http",
        "protocol": scheme_of(url) or "http",
        "status": status,
        "totalLength": str(total),
        "completedLength": str(done),
        "uploadLength": "0",
        "bitfield": "",
        "downloadSpeed": str(speed if status == "active" else 0),
        "uploadSpeed": "0",
        "connections": str(int(task.get("connections") or 0)),
        "numSeeders": "0",
        "seeder": "false",
        "pieceLength": "0",
        "numPieces": "0",
        "handleFile": "true",
        "dir": directory,
        "seq": str(int(task.get("created_at") or 0)),
        "following": "",
        "belongsTo": "",
        "files": files,
        # errorCode 只有 1（aria2 的「未知错误」）：ZDM 的失败原因是一句中文，
        # 塞进 aria2 的数字码里只会误导。真正的说明在 errorMessage。
        "errorCode": "1" if status == "error" else "0",
        "errorMessage": error if status == "error" else "",
        "verifiedLength": str(done),
        "verifyIntegrityPending": "false",
    }
    if status == "active" and speed:
        # 老 aria2 客户端读的字段名是 speed，新版叫 downloadSpeed；两个都给。
        out["speed"] = str(speed)
    if status == "error" and error:
        out["error"] = error
    if status == "complete" and task.get("completed_at"):
        out["completionTime"] = str(int(task.get("completed_at")))
    return out


def _queue_order(tasks: List[dict]) -> List[dict]:
    """排队顺序 = 调度器用的那一把尺子：优先级高在前，同级按创建时间。"""
    return sorted(
        tasks,
        key=lambda t: (
            -int(t.get("priority") or 0),
            float(t.get("created_at") or 0),
        ),
    )


class Aria2Rpc:
    """进程内一个实例：只做方法分发，不持有任务状态。

    ``create_task`` 注入的是 Handler 的建任务通路（POST /api/tasks 走的那一段），
    这样 RPC 建出来的任务和界面 / 扩展建的完全同路：视频页面链接一样转成 yt-dlp
    任务，站点 Cookie 一样兜底，一样受重复下载检查约束。
    """

    def __init__(
        self,
        manager: Manager,
        token: str,
        create_task: Callable[[dict], dict],
    ):
        self.manager = manager
        self.token = token
        self.create_task = create_task
        self.session_id = uuid.uuid4().hex

    # ---------------- entry ----------------
    def handle(self, raw_body: bytes, supplied_token: str = "") -> Tuple[int, object]:
        """Return (http status, json body)；body 为 None 时回 204。"""
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            return 200, self.err(None, f"请求体不是合法的 JSON（{type(e).__name__}）", -32700)
        if isinstance(payload, list):
            bodies = [self._one(req, supplied_token)[1] for req in payload]
            return 200, [b for b in bodies if b is not None]
        return self._one(payload, supplied_token)

    def _one(self, request, supplied_token: str) -> Tuple[int, Optional[dict]]:
        if not isinstance(request, dict):
            return 200, self.err(None, "请求需要是 JSON 对象", -32600)
        req_id = request.get("id")
        method = str(request.get("method") or "")
        params = _as_list(request.get("params"))
        # 没有 id = JSON-RPC 通知：照样执行，但不回内容。
        notification = "id" not in request
        try:
            result = self._authorized(method, params, supplied_token)
        except RpcError as e:
            code = 401 if e.http_401 else 200
            return code, self.err(None if notification else req_id, e.message, e.code)
        except ValueError as e:
            return 200, self.err(None if notification else req_id, str(e), -1)
        except Exception as e:  # noqa: BLE001
            return 200, self.err(
                None if notification else req_id, f"{type(e).__name__}: {e}", -1
            )
        if notification:
            return 204, None
        return 200, {"jsonrpc": "2.0", "id": req_id, "result": result}

    @staticmethod
    def err(req_id, message: str, code: int) -> dict:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}

    # ---------------- auth + dispatch ----------------
    def _authorized(self, method: str, params: list, supplied_token: str):
        if not method:
            raise RpcError("缺少 method", -32600)
        # aria2 的鉴权约定：params[0] 可以是 "token:secret"，剥掉后再传给方法。
        if params and str(params[0]).startswith("token:"):
            supplied_token = str(params[0])[6:]
            params = params[1:]
        if method == "aria2.checkToken":
            # aria2 特意让这个方法来验令牌：令牌错了也不报错，只把收到的原样带回。
            return {"token": f"token:{supplied_token}" if supplied_token else ""}
        if not self.token or not hmac.compare_digest(supplied_token or "", self.token):
            raise RpcError("Unauthorized", -1, http_401=True)
        if method == "system.multicall":
            return self._multicall(params)
        return self._exec(method, params)

    def _exec(self, method: str, params: list):
        if method == "system.listMethods":
            # 客户端拿它来探能力，列出来的必须真的能调。
            return sorted(list(METHODS) + ["system.listMethods", "system.multicall"])
        fn = METHODS.get(method)
        if fn is None:
            raise RpcError(
                f"ZDM 的 aria2 兼容层没有这个方法：{method}"
                "（BT / 磁力 / Metalink 不在支持范围内）",
                -32601,
            )
        return fn(self, params)

    def _multicall(self, params: list) -> list:
        """AriaNg 刷新一次列表就靠它：tellActive + tellWaiting + tellStopped 打包。"""
        calls = _as_list(params[0] if params else [])
        out = []
        for call in calls:
            if not isinstance(call, dict):
                raise RpcError(
                    "system.multicall 的参数需要是 {methodName, params} 数组", -32602
                )
            name = str(call.get("methodName") or "")
            if name.startswith("system."):
                # aria2 明确禁止嵌套 multicall。
                raise RpcError("system.multicall 不能嵌套调用", -32602)
            try:
                out.append([self._exec(name, _as_list(call.get("params")))])
            except RpcError as e:
                out.append({"code": e.code, "message": e.message})
            except ValueError as e:
                out.append({"code": -1, "message": str(e)})
        return out

    # ---------------- tasks ----------------
    def _task(self, gid: str) -> dict:
        d = self.manager.get(str(gid))
        if not d:
            raise RpcError(f"没有这个任务：{gid}", -1)
        return d.to_dict()

    def add_uri(self, params: list) -> str:
        uris = _as_list(params[0] if params else [])
        urls = [str(u).strip() for u in uris if str(u).strip()]
        if not urls:
            raise RpcError("参数 1 需要是包含下载链接的数组", -32602)
        # aria2 的「一个任务多条地址」是同一文件的镜像/后备；ZDM 一条任务只用
        # 一条地址，多余的那几条在这里被丢弃 —— 引擎没有多源回退的概念。
        body: Dict[str, object] = {"url": urls[0], "source": "rpc"}
        options = params[1] if len(params) > 1 else {}
        if options is None:
            options = {}
        if not isinstance(options, dict):
            raise RpcError("参数 2 需要是选项对象", -32602)
        for key, value in options.items():
            name = str(key)
            target = _TASK_OPTIONS.get(name)
            if target is None:
                raise RpcError(f"ZDM 不支持的 aria2 选项：{name}", -32602)
            if target == "_ignore":
                continue
            if name == "header":
                headers = dict(body.get("headers") or {})
                for line in _as_list(value):
                    text = str(line)
                    if ":" in text:
                        k, v = text.split(":", 1)
                        headers[k.strip()] = v.strip()
                body["headers"] = headers
            elif name == "pause":
                body["paused"] = str(value).strip().lower() in ("1", "true", "yes", "on")
            elif name in ("split", "max-connection-per-server"):
                try:
                    body["connections"] = int(value)
                except (TypeError, ValueError) as e:
                    raise RpcError(
                        f"选项 {name} 需要整数（收到 {value!r}）", -32602
                    ) from e
            else:
                body[target] = value
        try:
            reply = self.create_task(body)
        except ValueError as e:
            # REST 侧的 400 在这里就是 RPC 的业务错误，原因原样带出去。
            raise RpcError(str(e), -1) from e
        return str(reply.get("id") or "")
    def tell_active(self, params: list) -> list:
        keys = params[0] if params else []
        tasks = [t for t in self.manager.list() if t.get("status") in ACTIVE_STATES]
        return [_pick(attrs_of(t), keys) for t in tasks]

    def tell_waiting(self, params: list) -> list:
        offset = _int(_at(params, 0, 0), "offset")
        num = _int(_at(params, 1, -1), "num")
        keys = _at(params, 2, [])
        tasks = [t for t in self.manager.list() if t.get("status") in WAITING_STATES]
        return self._page(_queue_order(tasks), offset, num, keys)

    def tell_stopped(self, params: list) -> list:
        offset = _int(_at(params, 0, 0), "offset")
        num = _int(_at(params, 1, -1), "num")
        keys = _at(params, 2, [])
        tasks = [t for t in self.manager.list() if t.get("status") in STOPPED_STATES]
        # ZDM 的 list() 是新→旧，aria2 的 stopped 是「先结束的先出」，反过来。
        return self._page(list(reversed(tasks)), offset, num, keys)

    @staticmethod
    def _page(tasks: list, offset: int, num: int, keys) -> list:
        if offset < 0:
            offset = 0
        window = tasks[offset:] if num < 0 else tasks[offset : offset + max(0, num)]
        return [_pick(attrs_of(t), keys) for t in window]

    def get_download_result(self, params: list) -> dict:
        task = self._task(_first(params))
        if task.get("status") not in STOPPED_STATES:
            raise RpcError("任务还没有结束，请用 aria2.tellActive / tellWaiting", -1)
        return attrs_of(task)

    def tell_status(self, params: list) -> dict:
        """aria2.tellStatus(gid[, keys]) —— 客户端轮询单个任务用的就是它。"""
        return _pick(attrs_of(self._task(_first(params))), _at(params, 1, []))

    def get_task_list(self, params: list) -> list:
        """aria2.getTaskList = 活跃 + 排队，和 aria2 的口径一致。"""
        keys = _at(params, 0, [])
        tasks = [t for t in self.manager.list()
                 if t.get("status") in ACTIVE_STATES or t.get("status") in WAITING_STATES]
        return [_pick(t, keys) for t in _queue_order(tasks)]

    def global_stat(self, params: list) -> dict:
        keys = params[0] if params else []
        tasks = self.manager.list()
        active = [t for t in tasks if t.get("status") in ACTIVE_STATES]
        waiting = [t for t in tasks if t.get("status") in WAITING_STATES]
        stopped = [t for t in tasks if t.get("status") in STOPPED_STATES]
        out = {
            "numActive": str(len(active)),
            "numWaiting": str(len(waiting)),
            "numStopped": str(len(stopped)),
            "numStoppedTotal": str(len(stopped)),
            "downloadSpeed": str(sum(int(t.get("speed") or 0) for t in active)),
            "uploadSpeed": "0",
        }
        return _pick(out, keys)

    # ---------------- control ----------------
    def pause(self, params: list) -> str:
        gid = _first(params)
        task = self._task(gid)
        if task.get("status") not in ("downloading", "queued", "merging"):
            raise RpcError(f"当前状态不能暂停：{task.get('status')}", -1)
        if not self.manager.pause(gid):
            raise RpcError("暂停失败：任务状态已变化，请稍后再试", -1)
        return "OK"

    def pause_all(self, params: list) -> str:
        self.manager.pause_all()
        return "OK"

    def resume(self, params: list) -> str:
        gid = _first(params)
        task = self._task(gid)
        if task.get("status") not in ("paused", "error", "canceled"):
            raise RpcError(f"当前状态不能继续：{task.get('status')}", -1)
        if not self.manager.resume(gid):
            raise RpcError("继续失败：任务状态已变化，请稍后再试", -1)
        return "OK"

    def resume_all(self, params: list) -> str:
        self.manager.resume_all()
        return "OK"

    def remove(self, params: list, force: bool = False) -> str:
        gid = _first(params)
        task = self._task(gid)
        if task.get("status") in ("downloading", "merging") and not force:
            # 和 aria2 一样：正在跑的任务要用 forceRemove，避免手滑删掉在下的文件。
            raise RpcError(
                "正在下载的任务不能直接删除，请先暂停或改用 forceRemove", -1
            )
        if not self.manager.remove(gid, remove_files=False):
            raise RpcError("删除失败：任务状态已变化，请稍后再试", -1)
        return "OK"

    def remove_result(self, params: list) -> str:
        gid = _first(params)
        task = self._task(gid)
        if task.get("status") not in STOPPED_STATES:
            raise RpcError("任务还没有结束，请先用 aria2.remove / forceRemove", -1)
        if not self.manager.remove(gid, remove_files=False):
            raise RpcError("删除失败：任务状态已变化，请稍后再试", -1)
        return "OK"

    def change_position(self, params: list) -> str:
        gid = _first(params)
        pos = _int(_at(params, 1, 0), "position")
        how = str(_at(params, 2, "POS_SET") or "POS_SET").upper()
        tasks = self.manager.list()
        if not any(t.get("id") == gid for t in tasks):
            raise RpcError(f"没有这个任务：{gid}", -1)
        waiting = _queue_order([t for t in tasks if t.get("status") in WAITING_STATES])
        index = next((i for i, t in enumerate(waiting) if t.get("id") == gid), None)
        if index is None:
            raise RpcError("只有排队中的任务能调整位置（正在下载或已暂停的不行）", -1)
        if how == "POS_CUR":
            target = index + pos
        elif how == "POS_END":
            target = len(waiting) - 1 + pos
        else:
            target = pos
        target = max(0, min(len(waiting) - 1, target))
        if target == index:
            return "OK"
        slots = MAX_PRIORITY * 2 + 1
        if len(waiting) > slots:
            # ZDM 的队列顺序由优先级决定，档位只有 ±99。超过这个条数就做不到
            # 「精确落到第几位」，与其给一个错的顺序，不如直接说明。
            raise RpcError(
                f"排队中的任务超过 {slots} 个，无法保证精确位置（队列顺序由优先级决定）",
                -1,
            )
        ordered = list(waiting)
        ordered.insert(target, ordered.pop(index))
        for i, task in enumerate(ordered):
            want = MAX_PRIORITY - i
            if int(task.get("priority") or 0) != want:
                self.manager.set_priority(str(task.get("id")), want)
        return "OK"

    # ---------------- options ----------------
    def change_global_option(self, params: list) -> list:
        options = _at(params, 0, {})
        if not isinstance(options, dict):
            raise RpcError("参数 1 需要是选项对象", -32602)
        patch: Dict[str, object] = {}
        for key, value in options.items():
            name = str(key)
            target = _GLOBAL_OPTIONS.get(name)
            if target is None:
                raise RpcError(f"ZDM 不支持的 aria2 全局选项：{name}", -32602)
            if target == "_ignore":
                continue
            if name in _SIZED_GLOBAL:
                try:
                    patch[target] = parse_size(value, name)
                except ValueError as e:
                    raise RpcError(str(e), -32602) from e
            else:
                patch[target] = value
        try:
            self.manager.set_config(patch)
        except (ValueError, TypeError) as e:
            raise RpcError(str(e), -1) from e
        return ["OK"]

    def change_option(self, params: list) -> list:
        gid = _first(params)
        options = _at(params, 1, {})
        if not isinstance(options, dict) or not options:
            raise RpcError("参数 2 需要是选项对象", -32602)
        unsupported = [
            str(k)
            for k in options
            if str(k) not in ("split", "max-connection-per-server")
        ]
        if unsupported:
            # 目录 / 文件名改的是已经落盘的路径，连接数是 ZDM 唯一能安全改的
            # 任务级参数；其余宁可明确拒绝，也不能「改完参数却仍写到旧位置」。
            raise RpcError(
                "ZDM 的 aria2 兼容层只能改连接数，不支持：" + "、".join(unsupported),
                -32602,
            )
        self._task(gid)
        try:
            connections = int(_at(options, "split", _at(options, "max-connection-per-server")))
        except (TypeError, ValueError) as e:
            raise RpcError("连接数需要整数", -32602) from e
        try:
            self.manager.edit(gid, connections=connections)
        except (ValueError, TypeError) as e:
            raise RpcError(str(e), -1) from e
        return ["OK"]

    # ---------------- info ----------------
    def version(self, params: list) -> dict:
        return {
            "version": VERSION,
            "enabledFeatures": ["http", "https", "tls", "jsonrpc"],
        }

    def get_session_id(self, params: list) -> str:
        return self.session_id


def _refuse_unsupported(kind: str) -> None:
    raise RpcError(f"ZDM 不做{kind}下载（{URL_SCHEME_HINT}）", -1)


METHODS = {
    "aria2.addUri": lambda self, p: self.add_uri(p),
    "aria2.addTorrent": lambda self, p: _refuse_unsupported("BT / 磁力种子"),
    "aria2.addMetalink": lambda self, p: _refuse_unsupported("Metalink"),
    "aria2.remove": lambda self, p: self.remove(p),
    "aria2.forceRemove": lambda self, p: self.remove(p, force=True),
    "aria2.pause": lambda self, p: self.pause(p),
    "aria2.forcePause": lambda self, p: self.pause(p),
    "aria2.pauseAll": lambda self, p: self.pause_all(p),
    "aria2.forcePauseAll": lambda self, p: self.pause_all(p),
    "aria2.unpause": lambda self, p: self.resume(p),
    "aria2.resume": lambda self, p: self.resume(p),
    "aria2.unpauseAll": lambda self, p: self.resume_all(p),
    "aria2.resumeAll": lambda self, p: self.resume_all(p),
    "aria2.changePosition": lambda self, p: self.change_position(p),
    "aria2.getDownloadResult": lambda self, p: self.get_download_result(p),
    "aria2.tellStatus": lambda self, p: self.tell_status(p),
    "aria2.getTaskList": lambda self, p: self.get_task_list(p),
    "aria2.removeDownloadResult": lambda self, p: self.remove_result(p),
    "aria2.tellActive": lambda self, p: self.tell_active(p),
    "aria2.tellWaiting": lambda self, p: self.tell_waiting(p),
    "aria2.tellStopped": lambda self, p: self.tell_stopped(p),
    "aria2.getGlobalStat": lambda self, p: self.global_stat(p),
    "aria2.changeGlobalOption": lambda self, p: self.change_global_option(p),
    "aria2.changeOption": lambda self, p: self.change_option(p),
    "aria2.getVersion": lambda self, p: self.version(p),
    "aria2.newSessionId": lambda self, p: self.get_session_id(p),
    "aria2.getSessionId": lambda self, p: self.get_session_id(p),
    # 真正的回答在 _authorized 里（令牌不对也不报错，这是 aria2 的约定）；
    # 这里登记只为了让 system.listMethods 能把它列出来。
    "aria2.checkToken": lambda self, p: {"token": ""},
}


def _first(params: list, name: str = "gid"):
    if not params or str(params[0]).strip() == "":
        raise RpcError(f"缺少参数：{name}", -32602)
    return str(params[0]).strip()


def _at(params, index, default=None):
    if isinstance(params, dict):
        return params.get(index, default)
    try:
        return params[index]
    except (IndexError, TypeError):
        return default


def _int(value, name: str = "数值") -> int:
    if value is None or value == "":
        return 0
    try:
        return int(value)
    except (TypeError, ValueError) as e:
        raise RpcError(f"{name} 需要整数（收到 {value!r}）", -32602) from e
