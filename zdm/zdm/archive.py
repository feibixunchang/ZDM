"""按后缀自动归档：下完的文件不再散在同一个目录里。

规则是一份「后缀 → 子目录」对照表，例如

    mp4,mkv = Video
    srt,ass = Subtitles

命中哪条，成品就落进 ``<任务目录>/<子目录>``；一条都没命中就按原目录落。
默认是空表 —— 也就是**关闭**：ZDM 从不擅自改动用户的目录结构，想要分档
的人自己写规则。

这里只管「该落哪儿」，不碰落盘：建目录失败由调用方兜住（文件留在原处），
下载本身永远不因归档规则而失败。
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
