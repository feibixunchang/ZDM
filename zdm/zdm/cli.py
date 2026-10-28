"""Command line interface for ZDM."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Optional

from . import apitoken, desktop, runtime, server
from .manager import Manager, config_dir, load_config

PID_FILE = os.path.join(config_dir(), "zdm.pid")


def _detached() -> dict:
    """Popen kwargs that detach a background process on POSIX and Windows."""
    if os.name == "nt":
        return {"creationflags": 0x00000008}  # DETACHED_PROCESS
    return {"start_new_session": True}


def _api(method: str, path: str, payload: Optional[dict] = None, port: int = 0,
         timeout: int = 10) -> dict:
    import urllib.error
    import urllib.request

    port = port or 0
    url = f"http://127.0.0.1:{port}/api/{path.lstrip('/')}"
    data = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("X-ZDM-Token", apitoken.get_token())
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return json.loads(body)
        except Exception:
            return {"ok": False, "error": body}
    except Exception as e:  # noqa: BLE001
        text = str(e) or type(e).__name__
        # 超时不等于「服务没起」：删除、暂停这类请求很可能已经在服务端做完了，
        # 只是回话赶在客户端关门之后。说成「连不上服务」会让人以为程序坏了并反复点。
        if isinstance(e, TimeoutError) or "timed out" in text.lower():
            return {
                "ok": False,
                "error": "服务在超时前没有回话；操作可能已经生效，用 zdm list 确认",
            }
        return {"ok": False, "error": f"service not reachable: {e}"}


def _err(r: dict, fallback: str = "服务没有说明原因") -> str:
    """把 ok:false 响应里的理由取成一句人话。

    直接 print(r.get('error')) 在服务端漏写理由时会打成「error: None」——
    用户看到的是「程序坏了」，实际往往只是那条任务已经不在了。
    """
    e = r.get("error")
    if isinstance(e, dict):
        e = e.get("message") or json.dumps(e, ensure_ascii=False)
    text = str(e).strip() if e is not None else ""
    return text or fallback


def _resolve_port(args) -> int:
    port = getattr(args, "port", 0) or 0
    if port:
        return int(args.port)
    m = Manager()
    return int(m.config.get("port") or 9000)


def _fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024
    return f"{n:.1f}TB"


def cmd_start(args) -> int:
    port = int(getattr(args, "port", 0) or 0) or int(load_config().get("port") or 9000)
    state = server.ping(port=port)
    if state and not args.force:
        print(f"service already running on port {port}")
        return 0
    if args.daemon:
        # Re-run ourselves, not "python3 zdm.py": in a frozen build there is no
        # script on disk and sys.executable is the bundle's own executable.
        cmd = runtime.cli_cmd("serve", "--port", str(port))
        log = os.path.join(config_dir(), "zdm.log")
        with open(log, "a", encoding="utf-8") as lf:
            p = subprocess.Popen(
                cmd,
                stdout=lf,
                stderr=lf,
                stdin=subprocess.DEVNULL,
                **_detached(),
            )
        with open(PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(p.pid))
        for _ in range(40):
            time.sleep(0.15)
            if server.ping(port=port):
                print(f"ZDM started in background (pid {p.pid}) -> http://127.0.0.1:{port}/")
                return 0
        print(f"started (pid {p.pid}), waiting for service... check {log}")
        return 0
    m = Manager()
    return server.serve(m, port=port, open_ui=not args.no_open)


def cmd_serve(args) -> int:
    m = Manager()
    port = int(getattr(args, "port", 0) or 0) or int(m.config.get("port") or 9000)
    # 容器/NAS 部署：--host 0.0.0.0 + --allow-lan（或同名环境变量 ZDM_HOST /
    # ZDM_ALLOW_LAN）。allow_lan 只放宽 Host 校验，写操作照旧逐条验令牌。
    host = str(getattr(args, "host", "") or os.environ.get("ZDM_HOST") or "127.0.0.1")
    allow_lan = bool(getattr(args, "allow_lan", False)) or str(
        os.environ.get("ZDM_ALLOW_LAN") or ""
    ).lower() in ("1", "true", "yes", "on")
    if allow_lan:
        m.config["allow_lan"] = True
    return server.serve(m, host=host, port=port, open_ui=False)


def _pid_is_zdm(pid: int) -> bool:
    """True when the pid still belongs to a ZDM process.

    ``zdm stop`` used to SIGTERM whatever number sat in the pid file. Pids get
    reused, and killing an unrelated process is unforgivable, so verify the
    command line first. When the check is impossible (no ``ps``), refuse to
    kill: the API shutdown above already covers the normal case.
    """
    if pid <= 0:
        return False
    try:
        r = subprocess.run(
            ["ps", "-o", "args=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return False
    if r.returncode != 0:
        return False
    cmd = (r.stdout or "").strip().lower()
    if not cmd:
        return False
    return "zdm" in cmd


def cmd_stop(args) -> int:
    port = _resolve_port(args)
    r = _api("POST", "action", {"action": "shutdown"}, port=port)
    try:
        if os.path.exists(PID_FILE):
            with open(PID_FILE, encoding="utf-8") as f:
                pid = int(f.read().strip())
            if _pid_is_zdm(pid):
                os.kill(pid, 15)
            os.remove(PID_FILE)
    except Exception:
        pass
    print("stopped" if r.get("ok") else f"no running service ({_err(r, 'not reachable')})")
    return 0


def cmd_status(args) -> int:
    port = _resolve_port(args)
    st = server.ping(port=port)
    if not st:
        print("service: stopped")
        return 1
    print(f"service: running on http://127.0.0.1:{port}/")
    r = _api("GET", "tasks", port=port)
    for t in r.get("tasks", []):
        print(
            f"  {t['id']}  {t['status']:<11} {t['progress']:>6.1f}%  "
            f"{_fmt_size(t['downloaded'])}/{_fmt_size(t['size'])}  {t['filename']}"
        )
    return 0


def cmd_add(args) -> int:
    port = _resolve_port(args)
    if not server.ping(port=port):
        print("service not running. Start it first:  zdm start --daemon")
        return 1
    payload = {"url": args.url}
    if args.dir:
        payload["dir"] = os.path.abspath(os.path.expanduser(args.dir))
    if args.name:
        payload["filename"] = args.name
    if args.connections:
        payload["connections"] = int(args.connections)
    if args.priority:
        payload["priority"] = int(args.priority)
    if args.start_at:
        payload["start_at"] = args.start_at
    if args.section:
        payload["section"] = args.section
    for _flag, _key in (
        ("select_video", "stream_video"),
        ("select_audio", "stream_audio"),
        ("select_subtitle", "stream_subtitle"),
        ("sub_format", "stream_sub_format"),
    ):
        _v = str(getattr(args, _flag, "") or "").strip()
        if _v:
            payload[_key] = _v
    # 上限给 0 也要照传（= 不限时长）；只有这个参数完全没出现才不改默认。
    if getattr(args, "live_limit", None) is not None:
        payload["stream_live_limit"] = args.live_limit
    if args.header:
        headers = {}
        for h in args.header:
            k, _, v = h.partition(":")
            headers[k.strip()] = v.strip()
        payload["headers"] = headers
    if getattr(args, "playlist", None) is not None:
        # 清单链接建成**一条**任务是错的：yt-dlp 的 --no-playlist 对纯清单地址
        # 不起作用（实测），于是整份清单下进同一个 outtmpl，一集覆盖一集，最后
        # 只剩一个「已完成」的文件。--playlist 改走展开通道：列名单 → 按范围建 N 条。
        return _add_playlist(args, port, payload)
    r = _api("POST", "tasks", payload, port=port)
    if not r.get("ok"):
        print(f"error: {_err(r)}")
        return 1
    task = r.get("task") or {}
    note = ""
    # 服务端会把越界值夹到区间内（连接数 1~32、优先级 ±99）。不吭一声的话，
    # 用户以为自己真的开了 9999 条连接。
    if args.connections and task.get("connections") not in (None, int(args.connections)):
        note += f"（连接数按上限 {task['connections']}）"
    if args.priority and task.get("priority") not in (None, int(args.priority)):
        note += f"（优先级按上限 {task['priority']}）"
    print(f"queued {r['id']}: {args.url}{note}")
    if r.get("warning"):
        print("warning: " + str(r["warning"]), file=sys.stderr)
    # Make sure the user actually sees the download: open the desktop client if
    # it is not running (the service already does this for extension requests;
    # the CLI talks to the same API but with auto_show_gui it would be a no-op
    # only when ZDM is already visible).
    if load_config().get("auto_show_gui", True):
        # No url here: the API call above already queued it. Passing it again
        # makes the GUI (or the already-running instance it wakes) add a
        # duplicate task.
        desktop.ensure_desktop([], port)
    return 0


def _fmt_dur(seconds) -> str:
    """清单条目的时长：65 分钟以上也给的读出来的形状。"""
    try:
        s = int(seconds or 0)
    except (TypeError, ValueError):
        return ""
    if s <= 0:
        return ""
    if s >= 3600:
        return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"
    return f"{s // 60}:{s % 60:02d}"


def _playlist_body(args, payload: dict, action: str) -> dict:
    """把 add 的参数挑出「对整份清单也成立」的那些，交给 playlist 端点。

    逐条任务级的参数（-n 文件名、--section 选段、轨道选择、直播时长）在这里
    **没有意义**：N 条任务不可能共用一个文件名或一个选段区间。与其悄悄丢掉，
    不如打印一句「按条命名/选段请对单条链接用」。
    """
    body = {"url": args.url, "source": "cli"}
    for key in ("dir", "priority", "start_at", "headers"):
        if payload.get(key) is not None:
            body[key] = payload[key]
    if getattr(args, "max_items", None):
        body["max_items"] = int(args.max_items)
    if getattr(args, "no_subdir", False):
        body["subdir"] = False
    if action == "add":
        body["range"] = str(args.playlist or "all")
        if getattr(args, "paused", False):
            body["paused"] = True
    ignored = [
        label
        for label, present in (("-n/--name", payload.get("filename")),
                               ("--section", payload.get("section")),
                               ("--select-*", payload.get("stream_video") or
                                payload.get("stream_audio") or
                                payload.get("stream_subtitle")),
                               ("--live-limit", payload.get("stream_live_limit")))
        if present
    ]
    if ignored:
        print("note: %s do not apply to a whole playlist and were ignored "
              "(set them per episode by adding that episode's own URL)"
              % ", ".join(ignored), file=sys.stderr)
    return body


def _print_playlist(r: dict) -> None:
    """把展开结果打成一页能看的名单。"""
    items = r.get("items") or []
    head = r.get("title") or "(no title)"
    print(f"{head}  ·  {len(items)} entries")
    if r.get("skipped"):
        print(f"  ({r['skipped']} entries had no downloadable URL and were skipped)")
    if r.get("truncated"):
        print(f"  (capped at {r.get('limit')} — raise --max-items or the rest is missing)")
    for it in items:
        dur = _fmt_dur(it.get("duration"))
        print(f"{it['index']:>4}  {dur:>9}  {it.get('title') or it.get('id') or ''}"
              f"\n        {it.get('url') or ''}")


def cmd_playlist(args) -> int:
    """zdm playlist URL：只列名单，不建任务（先看清有多少集再决定范围）。"""
    port = _resolve_port(args)
    if not server.ping(port=port):
        print("service not running. Start it first:  zdm start --daemon")
        return 1
    body = _playlist_body(args, {}, "preview")
    # 展开是**联网翻清单**：频道页一页 30 条、一次请求在本机实测要 10~90 秒
    # （TLS 到 youtube.com 的握手在这台机器上就要 20 秒），10 秒超时必然误报。
    r = _api("POST", "playlist/preview", body, port=port, timeout=600)
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r.get("ok") else 1
    if not (r.get("items") or []):
        print(f"error: {_err(r, 'could not expand this link')}")
        return 1
    _print_playlist(r)
    print("add with:  zdm add \"%s\" --playlist 1-%d" % (args.url, len(r["items"])))
    return 0


def _add_playlist(args, port: int, payload: dict) -> int:
    """zdm add URL --playlist [RANGE]：展开 + 一次性建 N 条任务。"""
    body = _playlist_body(args, payload, "add")
    r = _api("POST", "playlist/add", body, port=port, timeout=600)
    if not r.get("added"):
        err = _err(r, "no task created")
        print(f"error: {err}")
        if r.get("failed"):
            for f in r["failed"][:5]:
                print(f"  #{f.get('index')}: {_err(f, '未知原因')}", file=sys.stderr)
        return 1
    print(f"queued {r['added']} task(s) from \"{r.get('title') or args.url}\""
          f" -> {r.get('directory')}")
    if r.get("skipped"):
        print(f"  ({r['skipped']} entries in the list had no downloadable URL)")
    if r.get("truncated"):
        print(f"  (list capped at {r.get('limit')} entries; raise --max-items for more)")
    for f in (r.get("failed") or [])[:5]:
        print(f"  #{f.get('index')} failed: {_err(f, '未知原因')}", file=sys.stderr)
    if len(r.get("failed") or []) > 5:
        print(f"  ({len(r['failed'])} failures in total)", file=sys.stderr)
    if load_config().get("auto_show_gui", True):
        desktop.ensure_desktop([], port)
    return 0 if not r.get("failed") else 1


def cmd_list(args) -> int:
    port = _resolve_port(args)
    r = _api("GET", "tasks", port=port)
    if not r.get("ok"):
        print(f"error: {_err(r)}")
        return 1
    tasks = r["tasks"]
    if args.json:
        print(json.dumps(tasks, ensure_ascii=False, indent=2))
        return 0
    if not tasks:
        print("(empty)")
        return 0
    print(f"{'ID':<14}{'STATUS':<12}{'%':>7}{'SIZE':>18}  NAME")
    for t in tasks:
        print(
            f"{t['id']:<14}{t['status']:<12}{t['progress']:>6.1f}%"
            f"{_fmt_size(t['downloaded']) + '/' + _fmt_size(t['size']):>18}  {t['filename']}"
        )
    return 0


def _task_action(args, action: str) -> int:
    port = _resolve_port(args)
    r = _api("POST", f"tasks/{args.id}/{action}", {}, port=port)
    print("ok" if r.get("ok") else f"error: {_err(r)}")
    return 0 if r.get("ok") else 1


def cmd_remove(args) -> int:
    port = _resolve_port(args)
    suffix = "?files=0" if args.keep_files else ""
    r = _api("DELETE", f"tasks/{args.id}{suffix}", port=port)
    print("ok" if r.get("ok") else f"error: {_err(r)}")
    return 0 if r.get("ok") else 1


def cmd_config(args) -> int:
    port = _resolve_port(args)
    if args.key and args.value is not None:
        raw = args.value
        text = raw.strip().lower()
        if text in ("true", "false", "yes", "no", "on", "off"):
            val = text in ("true", "yes", "on")
        else:
            try:
                val = int(raw)
            except ValueError:
                try:
                    val = float(raw)
                except ValueError:
                    val = raw
        r = _api("POST", "config", {args.key: val}, port=port)
        if not r.get("ok"):
            # 写错一个字母却打印出一份原样配置，看上去像「改了没生效」，
            # 其实是请求被拒了。把服务端的理由直说。
            print(f"error: {_err(r)}")
            return 1
        print(json.dumps(r.get("config", {}), ensure_ascii=False, indent=2))
        # 生效但没落盘（磁盘满 / 目录没权限）：不打印的话下次重启才发现设置全丢。
        if r.get("warning"):
            print("warning: " + str(r["warning"]), file=sys.stderr)
        return 0
    print(json.dumps(load_config(), ensure_ascii=False, indent=2))
    return 0


def _python_with_qt() -> Optional[str]:
    """Find an interpreter that can import PyQt5 (desktop UI)."""
    import shutil

    candidates = [sys.executable, shutil.which("python3"), "/usr/bin/python3"]
    seen = set()
    for py in candidates:
        if not py or py in seen:
            continue
        seen.add(py)
        try:
            r = subprocess.run(
                [py, "-c", "import PyQt5.QtWidgets"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
                **runtime.silent_proc_kwargs(),
            )
            if r.returncode == 0:
                return py
        except Exception:
            continue
    return None


def cmd_archive(args) -> int:
    """zdm archive：把归档规则补用到**已经下好**的文件上。

    默认只列清单不动手，`--apply` 才真搬。规则本来就只管之后下的文件，
    仓库里散着的成品是历史欠账 —— 但一次移动几百个文件这种事，必须先让人
    看清「哪些文件要去哪儿、哪些为什么没动」。
    """
    port = _resolve_port(args)
    if not server.ping(port=port):
        print("service not running. Start it first:  zdm start --daemon")
        return 1
    body = {"apply": bool(args.apply)}
    if args.dir:
        body["directory"] = args.dir
    r = _api("POST", "archive/sweep", body, port=port, timeout=600)
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r.get("ok") else 1
    if not r.get("ok"):
        print(f"error: {_err(r, '整理失败')}")
        return 1
    print(f"规则：{r.get('rules') or '（未配置）'}")
    print(f"目录：{r.get('directory')}")
    moves = r.get("moves") or []
    moved = r.get("moved") or []
    failed = r.get("failed") or []
    skipped = r.get("skipped") or []
    if args.apply:
        tail = f"，{len(failed)} 个没动" if failed else ""
        print(f"已移动 {len(moved)} 个文件{tail}")
        for it in moved[:30]:
            print(f"  {it['name']}  →  {it['dst']}")
        if len(moved) > 30:
            print(f"  （另外 {len(moved) - 30} 个略）")
    else:
        if moves:
            print(f"将移动 {len(moves)} 个文件（还没动手，加 --apply 才真搬）：")
            for it in moves[:30]:
                print(f"  {it['name']}  →  {it['dst']}  ({_fmt_size(it.get('size') or 0)})")
            if len(moves) > 30:
                print("  （清单只列前 30 个，--json 看全部）")
        else:
            print("没有需要移动的文件。")
    for it in skipped[:30]:
        print(f"  跳过：{it['name']} —— {it['reason']}")
    if len(skipped) > 30:
        print(f"  （另外 {len(skipped) - 30} 条跳过记录略）")
    if r.get("truncated"):
        print("  （待移动的文件超过上限，先跑一次再跑一次）")
    for it in failed[:10]:
        print(f"  没动：{it['name']} —— {it['reason']}", file=sys.stderr)
    return 1 if failed else 0


def cmd_gui(args) -> int:
    """Launch the native desktop window (falls back to the web UI)."""
    import webbrowser

    port = _resolve_port(args)
    if not server.ping(port=port):
        subprocess.run(
            runtime.cli_cmd("start", "--daemon", "--port", str(port)),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            **runtime.silent_proc_kwargs(),
        )
        for _ in range(30):
            time.sleep(0.2)
            if server.ping(port=port):
                break

    # desktop.ensure_desktop knows about the macOS app bundle / Windows exe and
    # wakes an already-running instance instead of starting a second one.
    desktop._last_launch = 0.0  # explicit request: bypass the throttle
    if desktop.ensure_desktop([], port):
        print(f"ZDM 桌面端已启动（端口 {port}）；若已在运行则已激活现有窗口")
        return 0
    print(f"未找到可用的桌面端（{desktop.last_error() or '缺少 PyQt5'}），改用浏览器界面")
    webbrowser.open(f"http://127.0.0.1:{port}/")
    return 0


def cmd_open(args) -> int:
    port = _resolve_port(args)
    import webbrowser

    webbrowser.open(f"http://127.0.0.1:{port}/")
    return 0


def cmd_firefox_ext(args) -> int:
    """把随包扩展打成 Firefox 的 .xpi。

    同一份 extension/ 源码，只翻译清单（见 firefox_ext.py）——所以这个命令打的包
    和「桌面端导出」「网页版下载」拿到的是同一个东西，不存在第二个版本。
    """
    from . import firefox_ext

    src = runtime.extension_dir()
    if not src or not os.path.isdir(src):
        print("!! 找不到随包的扩展目录，打不了 Firefox 包", file=sys.stderr)
        return 1
    out = args.out or "zdm-connector-%s-firefox.xpi" % (
        firefox_ext.read_manifest(src).get("version") or ""
    )
    try:
        firefox_ext.build_file(src, out)
    except OSError as e:
        print(f"!! 写入 {out} 失败：{e}", file=sys.stderr)
        return 1
    print(f"==> {out}（{os.path.getsize(out) // 1024} KB）")
    print("    在 Firefox 里 about:debugging#/runtime/this-firefox → 「加载临时附加组件…」")
    return 0


# ----------------------------------------------------------------- native host
def _native_host_api(port: int, path: str, body=None, timeout: float = 5.0):
    import urllib.request

    url = f"http://127.0.0.1:{port}/api/{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    req.add_header("Content-Type", "application/json")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def cmd_native_host(args) -> int:
    """Chrome/Chromium native-messaging host (``--native-host``).

    The browser extension calls connectNative("com.zdm.launcher") when the
    local service is not running. Chrome launches this process with a
    length-prefixed JSON message on stdin and expects a length-prefixed JSON
    reply on stdout - nothing else may ever touch stdout.

    This lives in the CLI (and not only in zdm_native_host.py) because the
    frozen Linux/Windows builds have no loose script on disk: the manifest
    points at the bundled executable itself, which must answer the protocol
    by name.
    """
    import struct

    def send(obj) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        sys.stdout.buffer.write(struct.pack("=I", len(data)))
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()

    try:
        header = sys.stdin.buffer.read(4)
        if len(header) < 4:
            return 1
        length = struct.unpack("=I", header)[0]
        if length <= 0 or length > 4 * 1024 * 1024:
            return 1
        msg = json.loads(sys.stdin.buffer.read(length).decode("utf-8", "replace"))
    except Exception:
        return 1

    port = int(load_config().get("port") or 9000)

    def ping() -> bool:
        try:
            return bool(_native_host_api(port, "state", None, timeout=1.0).get("ok"))
        except Exception:
            return False

    if not ping():
        try:
            subprocess.Popen(
                runtime.cli_cmd("start", "--daemon", "--no-open"),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                **_detached(),
            )
        except Exception:
            pass
        for _ in range(80):  # up to 20s for a cold client
            time.sleep(0.25)
            if ping():
                break
        else:
            send({"ok": False, "error": "ZDM service failed to start"})
            return 0

    url = str(msg.get("url") or "").strip()
    if not url:
        send({"ok": True, "started": True})
        return 0

    body = {"url": url}
    if msg.get("filename"):
        body["filename"] = msg["filename"]
    if msg.get("connections"):
        body["connections"] = msg["connections"]
    if msg.get("dir"):
        body["dir"] = msg["dir"]
    if msg.get("referer"):
        body["headers"] = {"Referer": msg["referer"]}
    # The extension tags every hand-off; without these a sniffer push that had
    # to go through native messaging would show up as a plain manual task.
    if msg.get("cookie"):
        body.setdefault("headers", {})["Cookie"] = msg["cookie"]
    if msg.get("source"):
        body["source"] = msg["source"]
    if msg.get("kind"):
        body["kind"] = msg["kind"]
    try:
        res = _native_host_api(port, "tasks", body, timeout=10.0)
        send({"ok": bool(res.get("ok")), "id": res.get("id"), "error": res.get("error")})
    except Exception as e:  # noqa: BLE001
        send({"ok": False, "error": str(e)})
    return 0


# ----------------------------------------------------------------- zdm:// scheme
def _handle_protocol_url(target: str) -> int:
    """``zdm://add?url=...`` - the fallback wake-up path used by the extension."""
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(target)
    if parsed.netloc not in ("add", "open", ""):
        return 1
    q = parse_qs(parsed.query)
    url = (q.get("url") or [""])[0].strip()
    if url:
        port = int(load_config().get("port") or 9000)
        body = {"url": url}
        headers = {}
        for key in (
            "filename",
            "referer",
            "source",
            "kind",
            "cookie",
            "audio_url",
            "audio_only",
            # YouTube：页面地址 + 目标高度，后端据此走 yt-dlp 通道
            "yt_page",
            "yt_height",
        ):
            v = (q.get(key) or [""])[0].strip()
            if not v:
                continue
            if key in ("referer", "cookie"):
                headers["Referer" if key == "referer" else "Cookie"] = v
            else:
                body[key] = v
        if headers:
            body["headers"] = headers
        # _api 自己把所有异常都变成了 {"ok": false}，所以这里只需要看结果。
        # 以前失败被整个丢掉、照样退出 0：坏链接（400）、服务没起、token 不对
        # 都表现为「双击链接什么都没发生」。
        res = _api("POST", "tasks", body, port=port)
        failed = "" if res.get("ok") else str(res.get("error") or "服务拒绝了这次添加")
    else:
        failed = ""
    desktop._last_launch = 0.0
    desktop.ensure_desktop([], int(load_config().get("port") or 9000))
    if failed:
        print(f"error: 添加任务失败：{failed}", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="zdm", description="ZDM download manager")
    p.add_argument("--port", type=int, default=0, help="service port (default 9000)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start", help="start the service")
    s.add_argument("--port", type=int, default=argparse.SUPPRESS)
    s.add_argument("-d", "--daemon", action="store_true", help="run in background")
    s.add_argument("--no-open", action="store_true", help="do not open the web UI")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_start)

    s = sub.add_parser("serve", help="run service in foreground (internal)")
    s.add_argument("--port", type=int, default=argparse.SUPPRESS)
    s.add_argument("--host", default="", help="绑定地址（容器/NAS 用 0.0.0.0）")
    s.add_argument("--allow-lan", action="store_true",
                   help="放行局域网 Host（写操作仍需令牌；配合 --host 0.0.0.0 用）")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("stop", help="stop the service")
    s.add_argument("--port", type=int, default=argparse.SUPPRESS)
    s.set_defaults(func=cmd_stop)
    s = sub.add_parser("status", help="show service and tasks")
    s.add_argument("--port", type=int, default=argparse.SUPPRESS)
    s.set_defaults(func=cmd_status)

    s = sub.add_parser(
        "add",
        help="add a download",
        description="add a download (HLS/DASH track selectors need "
        "stream_engine=auto/re in settings)",
        epilog='track selector keys: id lang name codecs res frame channel '
        'range url role for segsMin segsMax pkdMin pkdMax bwoff bwmin bwmax, '
        'shorthands: all best best2 worst1',
    )
    s.add_argument("url")
    s.add_argument("-o", "--dir", help="output directory")
    s.add_argument("-n", "--name", help="file name")
    s.add_argument("-c", "--connections", type=int)
    s.add_argument("-H", "--header", action="append", help="extra header, e.g. 'Referer: https://x'")
    s.add_argument("-p", "--priority", type=int, help="queue priority, higher goes first")
    s.add_argument("--start-at", help="scheduled start: Unix time or ISO-8601")
    s.add_argument("--section", help="video section, e.g. 00:01:00-00:05:00")
    # 播放列表/合集/频道投稿页：展开成 N 条任务，而不是自相覆盖的一条。
    s.add_argument(
        "--playlist",
        nargs="?",
        const="all",
        default=None,
        metavar="RANGE",
        help="treat URL as a playlist: RANGE like 1-20 or 1,3,5-8 (default: all)",
    )
    s.add_argument("--max-items", type=int, dest="max_items", metavar="N",
                   help="how many entries to expand (default 100, max 500)")
    s.add_argument("--no-subdir", action="store_true", dest="no_subdir",
                   help="put files straight in --dir instead of a playlist-named subfolder")
    s.add_argument("--paused", action="store_true",
                   help="with --playlist: create the tasks queued but not started")
    # HLS/DASH 轨道选择（只有 N_m3u8DL-RE 认）：值原样传给引擎的 -sv/-sa/-ss。
    s.add_argument(
        "--select-video",
        dest="select_video",
        help='HLS/DASH video track, e.g. \'res="1280"\' or best / all',
    )
    s.add_argument(
        "--select-audio",
        dest="select_audio",
        help='HLS/DASH audio track, e.g. \'lang="spa"\' or best',
    )
    s.add_argument(
        "--select-subtitle",
        dest="select_subtitle",
        help='HLS/DASH subtitle track, e.g. \'lang="eng"\' or all',
    )
    s.add_argument("--sub-format", dest="sub_format", help="subtitle format: SRT or VTT")
    # 直播录制时长上限：秒或 HH:MM:SS 都收（校验在 manager 那一层，四端同一口径）。
    s.add_argument(
        "--live-limit",
        dest="live_limit",
        metavar="SECONDS|HH:MM:SS",
        help="live stream only: stop recording after this long "
        "(0 or omit = record until you stop it)",
    )
    s.add_argument("--port", type=int, default=argparse.SUPPRESS)
    s.set_defaults(func=cmd_add)

    s = sub.add_parser("list", help="list downloads")
    s.add_argument("--json", action="store_true")
    s.add_argument("--port", type=int, default=argparse.SUPPRESS)
    s.set_defaults(func=cmd_list)

    s = sub.add_parser(
        "playlist",
        help="show what is inside a playlist/collect/channel link (no download)",
    )
    s.add_argument("url")
    s.add_argument("--max-items", type=int, dest="max_items", metavar="N",
                   help="how many entries to expand (default 100, max 500)")
    s.add_argument("--json", action="store_true")
    s.add_argument("--port", type=int, default=argparse.SUPPRESS)
    s.set_defaults(func=cmd_playlist)

    for name, action in (("pause", "pause"), ("resume", "resume"), ("cancel", "cancel"), ("restart", "restart")):
        s = sub.add_parser(name, help=f"{action} a download")
        s.add_argument("id")
        s.add_argument("--port", type=int, default=argparse.SUPPRESS)
        s.set_defaults(func=lambda a, act=action: _task_action(a, act))

    s = sub.add_parser("remove", help="remove a download from the list")
    s.add_argument("id")
    s.add_argument("--keep-files", action="store_true")
    s.add_argument("--port", type=int, default=argparse.SUPPRESS)
    s.set_defaults(func=cmd_remove)

    s = sub.add_parser("config", help="show or set config")
    s.add_argument("key", nargs="?")
    s.add_argument("value", nargs="?")
    s.add_argument("--port", type=int, default=argparse.SUPPRESS)
    s.set_defaults(func=cmd_config)

    s = sub.add_parser(
        "archive",
        help="apply the archive rules to files already downloaded (list first, --apply to move)",
    )
    s.add_argument("--apply", action="store_true", help="真的移动文件（默认只列清单）")
    s.add_argument("-o", "--dir", default="", help="要整理的目录（默认＝设置里的下载目录）")
    s.add_argument("--json", action="store_true")
    s.add_argument("--port", type=int, default=argparse.SUPPRESS)
    s.set_defaults(func=cmd_archive)

    s = sub.add_parser("gui", help="open the native desktop window")
    s.add_argument("--port", type=int, default=argparse.SUPPRESS)
    s.set_defaults(func=cmd_gui)

    s = sub.add_parser("open", help="open the web UI")
    s.add_argument("--port", type=int, default=argparse.SUPPRESS)
    s.set_defaults(func=cmd_open)

    s = sub.add_parser(
        "firefox-ext",
        help="package the bundled extension into a Firefox .xpi (same source as the Chrome one)",
    )
    s.add_argument("--out", default="", help="output file (default: zdm-connector-<version>-firefox.xpi)")
    s.set_defaults(func=cmd_firefox_ext)
    return p


def main(argv=None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        argv = ["start"]
    # Chrome native-messaging host mode: stdio protocol, never argparse.
    if argv and argv[0] == "--native-host":
        return cmd_native_host(argv)
    # Launched through the registered zdm:// scheme handler.
    if argv and argv[0].startswith("zdm://"):
        return _handle_protocol_url(argv[0])
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
