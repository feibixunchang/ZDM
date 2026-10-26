"""同一份扩展源码出两个包：Chrome/Edge 用原样 manifest.json，Firefox 只改 manifest。

为什么不需要第二份 JS：这套扩展只用 alarms / contextMenus / cookies / downloads /
notifications / runtime / scripting / storage / tabs / webRequest 这十个命名空间，
Firefox 的 MV3 全都实现了（代码里的 chrome.* 在 Firefox 也是同一批对象）。真正的
差异全在清单里，所以这里只做「翻译清单 + 打 zip」，不复制源码 —— 一份源码意味着
不会有「Chrome 修了 bug、Firefox 还带着坏版本」这种事。

改哪几处，以及为什么：

- `key` / `minimum_chrome_version`：Chrome 用来钉死扩展 ID 和最低版本，Firefox
  不认识（未知字段会让审核和安装期冒警告），去掉。Firefox 的 ID 由
  `browser_specific_settings.gecko.id` 定，见 GECKO_ID。
- `permissions` 里的 `downloads.open`：那是 Chrome 独有的权限名，Firefox 会当成
  非法项；API 本身在 Firefox 不可用，代码路径靠运行时探测（见 background.js 里
  对 chrome.scripting 的同款处理）。
- `background.service_worker` → `background.scripts`：Firefox 读事件页面清单。
- host_permissions 里的 `http://127.0.0.1:*/`：匹配模式的端口写成 `*` 时 Firefox
  的校验比 Chrome 严，而省略端口本来就等于「任意端口」，所以去掉 `:*` 不影响语义。
"""

from __future__ import annotations

import io
import json
import os
import zipfile
from typing import Dict, Tuple

# 固定 ID：原生消息主机的 allowed_extensions 要写它，装完才知道就晚了。
# Firefox 只要求是「字符串形式的邮箱型 ID」，自托管时由我们定。
GECKO_ID = "zdm-connector@feibixunchang.github.io"
# 115 是当前的 ESR 大版本：更早的 Firefox 没有 action / MV3 内容脚本注册这些能力。
STRICT_MIN_VERSION = "115.0"

_DROP_KEYS = ("key", "minimum_chrome_version")
_DROP_PERMISSIONS = ("downloads.open",)


def transform_manifest(manifest: Dict[str, object]) -> Dict[str, object]:
    """Chrome 清单 → Firefox 清单（不改动传入对象）。"""
    out: Dict[str, object] = {k: v for k, v in manifest.items() if k not in _DROP_KEYS}
    out["permissions"] = [
        p for p in (manifest.get("permissions") or []) if p not in _DROP_PERMISSIONS
    ]
    # Firefox 的 MV3 事件页面写法；service_worker 那一条会被整段换掉，不并存。
    # ui18n.js 必须在 background.js 之前：事件页面没有 importScripts，服务页里的
    # 文案（通知、右键菜单）靠 ZDMUI.t，加载顺序错了就是「Chrome 英文、Firefox 中文」。
    out["background"] = {"scripts": ["ui18n.js", "background.js"]}
    hp = out.get("host_permissions") or []
    out["host_permissions"] = [str(p).replace(":*/", "/*") for p in hp]
    out["browser_specific_settings"] = {
        "gecko": {"id": GECKO_ID, "strict_min_version": STRICT_MIN_VERSION}
    }
    return out


def read_manifest(ext_dir: str) -> Dict[str, object]:
    with open(os.path.join(ext_dir, "manifest.json"), encoding="utf-8") as fh:
        return json.load(fh)


def gecko_manifest(ext_dir: str) -> Dict[str, object]:
    return transform_manifest(read_manifest(ext_dir))


def package_files(ext_dir: str) -> Tuple[str, ...]:
    """要打进 xpi 的文件：清单 + 全部 js/html + icons/（排除 .ico，那是 Windows 快捷方式用的）。"""
    names = []
    for root, dirs, files in os.walk(ext_dir):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in sorted(files):
            if name.endswith(".ico") or name.endswith(".pyc"):
                continue
            full = os.path.join(root, name)
            names.append(os.path.relpath(full, ext_dir).replace(os.sep, "/"))
    return tuple(sorted(names))


def build_zip(ext_dir: str) -> bytes:
    """在内存里打出 Firefox 的 .xpi（就是个 zip，清单换成 gecko 版）。"""
    manifest = gecko_manifest(ext_dir)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2)
        )
        for rel in package_files(ext_dir):
            if rel == "manifest.json":
                continue
            zf.write(os.path.join(ext_dir, rel), rel)
    return buf.getvalue()


def build_file(ext_dir: str, out_path: str) -> str:
    data = build_zip(ext_dir)
    with open(out_path, "wb") as fh:
        fh.write(data)
    return out_path


def default_ext_dir() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "extension")


def main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="把 extension/ 打成 Firefox 的 .xpi")
    ap.add_argument("--src", default=default_ext_dir(), help="扩展源码目录")
    ap.add_argument("--out", default="", help="输出 .xpi 路径（默认按版本号命名）")
    ap.add_argument(
        "--gecko-id",
        action="store_true",
        help="只打印固定 ID：安装脚本拿它写原生消息主机的 allowed_extensions，"
        "两边必须是同一个字符串，所以让它从这里读，不在 shell 里抄一份",
    )
    args = ap.parse_args(list(argv) if argv is not None else None)

    if args.gecko_id:
        print(GECKO_ID)
        return 0

    m = read_manifest(args.src)
    out = args.out or f"zdm-connector-{m.get('version')}-firefox.xpi"
    build_file(args.src, out)
    geo = gecko_manifest(args.src)
    print(f"==> {out}（{os.path.getsize(out) // 1024} KB）")
    print(f"    gecko id: {geo['browser_specific_settings']['gecko']['id']}")
    print(f"    文件数: {len(package_files(args.src)) + 1}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
