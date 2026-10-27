"""按后缀自动归档：下完的文件不再散在同一个目录里。

规则是一份「后缀 → 子目录」对照表，例如

    mp4,mkv = Video
    srt,ass = Subtitles

命中哪条，成品就落进 ``<任务目录>/<子目录>``；一条都没命中就按原目录落。
默认是空表 —— 也就是**关闭**：ZDM 从不擅自改动用户的目录结构，想要分档
的人自己写规则。

这里只管「该落哪儿」，不碰落盘：建目录失败由调用方兜住（文件留在原处），
下载本身永远不因归档规则而失败。

规则只管**之后**下的文件，仓库里已经散着的成品是历史欠账；``plan_sweep`` /
``apply_sweep`` 把那笔欠账一次还清 —— 先出清单（默认不动手），冲突只跳过不
改名，还在下的文件一律不碰。
"""
from __future__ import annotations

import os
import re
from typing import Dict

# 一行的分隔符：`mp4 = Video`、`mp4 -> Video`、`mp4 → Video`、`mp4: Video`
_DIV = re.compile(r"\s*(?:=>|->|→|=|:|：)\s*")
_SEPS = re.compile(r"[,，;；、\s]+")
# 界面里允许写注释和空行；剩下的形态（比如把整条路径写成一行）都是笔误。
_COMMENT = re.compile(r"^\s*(?:#|//)")

__all__ = [
    "parse_rules_text",
    "format_rules_text",
    "normalize_rules",
    "prune_rules",
    "subdir_for",
    "describe",
    "plan_sweep",
    "apply_sweep",
    "TEMP_SUFFIXES",
]


def _exts(raw) -> list:
    """一条规则的后缀键 → 规范化的后缀列表（去点、去星号、小写、保序去重）。"""
    out = []
    for piece in _SEPS.split(str(raw or "").strip()):
        piece = piece.strip().lstrip(".*").strip().lower()
        if piece and piece not in out:
            out.append(piece)
    return out


def _dir_of(raw) -> str:
    """规则的值 → 安全的相对子目录。

    绝对路径、盘符、``~`` 一律拒绝，而不是硬改成某个相对目录：用户在别的
    系统上粘一条 ``D:\\Video`` 时，把它变成 ``<下载目录>/D/Video`` 是猜，
    报错才是答。剩下的分隔符、``..``、Windows 保留字符交给 safe_dirname，
    所以规则里怎么写都出不了任务目录。
    """
    from .yt_playlist import safe_dirname

    text = str(raw or "").strip()
    if not text:
        return ""
    if (
        text.startswith(("/", "\\"))
        or text.startswith("~")
        or re.match(r"^[a-zA-Z]:", text)
    ):
        raise ValueError(f"归档目录「{text}」要写成下载目录里面的相对路径")
    parts = [safe_dirname(seg) for seg in re.split(r"[/\\]+", text) if seg.strip()]
    return os.sep.join([p for p in parts if p])


def normalize_rules(raw) -> Dict[str, str]:
    """把配置里的规则拉成 ``{"mp4,mkv": "Video", ...}``（键规范化、值安全）。

    接受 dict（接口和 state.json 的正式形状）和文本（设置界面里那一整栏）。
    认不出的形态（数字、列表套列表）一律 ValueError：静默当成「没有规则」
    等于用户的归档设置没生效却看不出来。
    """
    if raw is None or raw == "":
        return {}
    pairs = []
    if isinstance(raw, dict):
        for k, v in raw.items():
            pairs.append((k, v))
    elif isinstance(raw, str):
        return normalize_rules(parse_rules_text(raw))
    elif isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                pairs.append((item[0], item[1]))
            elif isinstance(item, str):
                pairs.extend(parse_rules_text(item).items())
            else:
                raise ValueError(f"归档规则的形状不认识：{item!r}（要「后缀 = 目录」）")
    else:
        raise ValueError(f"归档规则需要「后缀 = 子目录」的对照表（收到 {raw!r}）")

    out: Dict[str, str] = {}
    seen: Dict[str, str] = {}
    for k, v in pairs:
        exts = _exts(k)
        dest = _dir_of(v)
        if not exts:
            raise ValueError(f"归档规则「{k} = {v}」没写后缀")
        if not dest:
            raise ValueError(f"归档规则「{k}」没写要归档到的子目录")
        for e in exts:
            if e in seen:
                raise ValueError(
                    f"后缀 .{e} 同时出现在「{seen[e]}」和「{dest}」两条规则里，归档到哪条说不清"
                )
            seen[e] = dest
        out[",".join(exts)] = dest
    return out


def prune_rules(raw) -> Dict[str, str]:
    """读取路径上的版本：坏规则丢掉，好规则留下，绝不抛异常。

    ``heal_config`` 的唯一职责是把老 state.json 拉回能用 —— 整表因为一条写错
    就丢掉，等于用户重启后发现归档全不生效。逐条试，冲突时先写的赢。
    """
    try:
        return normalize_rules(raw)
    except (TypeError, ValueError):
        pass
    out: Dict[str, str] = {}
    taken = set()
    items = []
    if isinstance(raw, dict):
        items = list(raw.items())
    elif isinstance(raw, str):
        # 老 state.json 或手写的配置可能直接把「mp4 = Video」那几行存成字符串；
        # 逐行拆开交给下面的通用处理，坏行丢、好行留。
        for line in raw.splitlines():
            if not line.strip() or _COMMENT.match(line) or not _DIV.search(line):
                continue
            k, v = _DIV.split(line, maxsplit=1)
            items.append((k, v))
    elif isinstance(raw, (list, tuple)):
        items = [x for x in raw if isinstance(x, (list, tuple)) and len(x) == 2]
    for k, v in items:
        try:
            exts = _exts(k)
            dest = _dir_of(v)
        except ValueError:
            continue
        exts = [e for e in exts if e not in taken]
        if not exts or not dest:
            continue
        taken.update(exts)
        out[",".join(exts)] = dest
    return out


def parse_rules_text(text: str) -> Dict[str, str]:
    """``mp4,mkv = Video`` 每行一条 → 规则表。空行、``#`` 注释跳过。"""
    pairs = []
    for n, line in enumerate(str(text or "").splitlines(), 1):
        if not line.strip() or _COMMENT.match(line):
            continue
        if not _DIV.search(line):
            raise ValueError(f"第 {n} 行「{line.strip()}」少了等号：要写成「mp4 = Video」")
        k, v = _DIV.split(line, maxsplit=1)
        try:
            pairs.extend(list(normalize_rules({k: v}).items()))
        except ValueError as e:
            raise ValueError(f"第 {n} 行有问题：{e}") from None
    return normalize_rules(pairs)


def format_rules_text(rules) -> str:
    """规则表 → 设置界面里那一栏的文本（每行「mp4,mkv = Video」）。"""
    try:
        norm = normalize_rules(rules)
    except ValueError:
        norm = {str(k): str(v) for k, v in (rules or {}).items()} if isinstance(rules, dict) else {}
    return "\n".join(f"{k} = {v}" for k, v in norm.items())


def subdir_for(name: str, rules) -> str:
    """成品文件名 → 该落进的子目录（相对任务目录）；没命中返回空串。"""
    if not rules or not name:
        return ""
    ext = os.path.splitext(str(name))[1].lstrip(".").lower()
    if not ext:
        return ""
    for key, dest in (rules or {}).items():
        if ext in _exts(key):
            return str(dest or "")
    return ""


def describe(rules) -> str:
    """一行话概括规则，给界面/命令行当提示（没开就说没开）。"""
    norm = rules if isinstance(rules, dict) else normalize_rules(rules)
    if not norm:
        return "未按后缀归档（关闭）"
    return "、".join(f".{k} → {v}" for k, v in norm.items())


# 断点/临时文件：这些不是「下好的文件」，移动它们等于打断一次下载。
TEMP_SUFFIXES = (".part", ".zdm", ".ytdl", ".tmp", ".temp")


def _is_temp(name: str) -> bool:
    low = str(name).lower()
    return any(low.endswith(s) for s in TEMP_SUFFIXES)


def _logical_name(name: str) -> str:
    """断点文件名 → 它下完之后将要叫的那个名字（``a.mp4.part.zdm`` → ``a.mp4``）。

    报告里只提「规则管得着的那份断点」：用户想知道的是「为什么我的 movie.mp4.part
    没被归档」，不是「为什么 notes.txt.part 没被归档」（那条规则本来就不管 txt）。
    """
    out = str(name or "")
    for _ in range(4):
        low = out.lower()
        for suf in TEMP_SUFFIXES:
            if low.endswith(suf):
                out = out[: -len(suf)]
                break
        else:
            break
        if not out:
            break
    return out


def _inside(base: str, path: str) -> bool:
    """归档后的落点还得在下载目录里 —— 规则写歪成「../../x」时宁可不动手。"""
    try:
        return os.path.commonpath([base, path]) == base
    except ValueError:  # 不同盘符（Windows）
        return False


def plan_sweep(directory, rules, blocked=None, limit=5000):
    """把「按后缀归档」的规则补用到目录里已经下好的文件上，先只出清单不动手。

    规则只管新下载，仓库里已经散着的成品是历史欠账 —— 这个把欠账一次还清。
    ``blocked`` 是 ``{绝对路径: 理由}``：调用方（Manager）把还在用的文件标进来，
    这里只负责尊重它，判断不了「这个 .mp4 是不是某个任务的半成品」。

    只看 ``directory`` 的第一层：子目录里的东西已经归过类（或者是用户自己分的），
    递归进去会把「已经归档好的」再套一层，猜错一次的代价是文件找不到。

    返回 ``{"ok", "directory", "moves", "skipped", "error", "truncated"}``；
    冲突（目标位置已有同名文件）只跳过并写明理由，**不自动改名** —— 一次性整理
    里凭空多出 ``video(1).mp4`` 比原地不动更难解释。
    """
    base = os.path.abspath(str(directory or ""))
    moves: list = []
    skipped: list = []
    if not base or not os.path.isdir(base):
        return {
            "ok": False,
            "directory": base,
            "moves": moves,
            "skipped": skipped,
            "error": f"下载目录不存在：{base}",
            "truncated": False,
        }
    norm = prune_rules(rules)
    if not norm:
        return {
            "ok": True,
            "directory": base,
            "moves": moves,
            "skipped": skipped,
            "error": "",
            "truncated": False,
        }
    blocked = {os.path.abspath(str(k)): v for k, v in (blocked or {}).items()}
    try:
        names = sorted(os.listdir(base))
    except OSError as e:
        return {
            "ok": False,
            "directory": base,
            "moves": moves,
            "skipped": skipped,
            "error": "读不了这个目录：%s" % (getattr(e, "strerror", None) or e),
            "truncated": False,
        }
    for name in names:
        src = os.path.join(base, name)
        if _is_temp(name):
            if subdir_for(_logical_name(name), norm):
                skipped.append({"name": name, "reason": "是没下完的断点/临时文件，移动会打断下载"})
            continue
        try:
            if os.path.islink(src):
                skipped.append({"name": name, "reason": "是符号链接，按链接指向整理容易出事"})
                continue
            if not os.path.isfile(src):
                continue  # 子目录：已经是「归好类」的样子，不递归进去
        except OSError as e:
            skipped.append({"name": name, "reason": "stat 失败：%s" % (getattr(e, "strerror", None) or e)})
            continue
        blocked_src = blocked.get(src)
        sub = subdir_for(name, norm)
        if not sub:
            continue
        # 拦下来的理由放在「本来会动它」之后报：规则没管到的文件没必要出现在报告里。
        if blocked_src:
            skipped.append({"name": name, "reason": blocked_src})
            continue
        dst = os.path.join(base, sub, name)
        if os.path.normpath(dst) == os.path.normpath(src):
            skipped.append({"name": name, "reason": "已经在归档目录里了"})
            continue
        if not _inside(base, dst):
            skipped.append({"name": name, "reason": "规则算出的落点跑到了下载目录外面"})
            continue
        if os.path.exists(dst):
            skipped.append({"name": name, "reason": f"{sub}{os.sep} 里已有同名文件，不覆盖"})
            continue
        if len(moves) >= int(limit or 0):
            return {
                "ok": True,
                "directory": base,
                "moves": moves,
                "skipped": skipped,
                "error": "",
                "truncated": True,
            }
        try:
            size = os.path.getsize(src)
        except OSError:
            size = 0
        moves.append({"name": name, "sub": sub, "src": src, "dst": dst, "size": size})
    return {
        "ok": True,
        "directory": base,
        "moves": moves,
        "skipped": skipped,
        "error": "",
        "truncated": False,
    }


def apply_sweep(plan):
    """执行 plan_sweep 的清单。逐个复核再动手：清单是几分钟前看的，目录是活的。

    同一个文件失败不影响别的文件，失败理由留在报告里 —— 一次性整理跑一半崩掉、
    剩下的没影，是比「不动」更糟的结果。
    """
    import shutil

    moved: list = []
    failed: list = []
    for item in (plan or {}).get("moves") or []:
        src, dst = item.get("src"), item.get("dst")
        name = item.get("name") or os.path.basename(str(src or ""))
        if not src or not dst:
            failed.append({"name": name, "reason": "清单项缺路径"})
            continue
        if not os.path.exists(src):
            failed.append({"name": name, "reason": "文件已经不在了"})
            continue
        if os.path.exists(dst):
            failed.append({"name": name, "reason": "目标位置出现了同名文件，不覆盖"})
            continue
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)
        except OSError as e:
            failed.append({"name": name, "reason": getattr(e, "strerror", None) or str(e)})
            continue
        moved.append({"name": name, "sub": item.get("sub") or "", "src": src, "dst": dst})
    return {"moved": moved, "failed": failed, "ok": not failed}

