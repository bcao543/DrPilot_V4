# -*- coding: utf-8 -*-
"""自然语言 -> 参数：把用户的一句中文需求解析成 CLI 参数。

定位：**只填有把握的**。解析不出来的一律不猜，留给 drpilot ask / 交互式提问去补。
所以这里宁可少解析，也不要解析错——错误参数比缺参数危险得多。

    drpilot ask --text "药理学第二章 47 到 81 题，存到 D:\\题库" --json
"""

from __future__ import annotations

import re
from typing import Any, Callable

from .modules import PRESETS

# 题号区间：47到81 / 第47-81题 / 47~81 / 47 至 81
_RANGE_RE = re.compile(
    r"(?:第)?\s*(\d{1,4})\s*(?:题)?\s*(?:到|至|~|～|-|－|—|–)\s*(?:第)?\s*(\d{1,4})\s*题?"
)
# 单题：第17题（补录时最常用）
_SINGLE_RE = re.compile(r"第\s*(\d{1,4})\s*题")
# 输出目录：存到 X / 输出到 X / …
_OUTPUT_RE = re.compile(
    r"(?:存到|存至|保存到|保存至|输出到|输出至|导出到|导出至|放到|写进|写入|放在)\s*"
    r"[「『\"\']?([^\s,，。;；)）\"\'」』]+)"
)
# 裸的 Windows 路径：D:\题库
_WIN_PATH_RE = re.compile(r"\b([A-Za-z]:[\\/][^\s,，。;；\"\'）)]+)")
# 章节：第二章 / 第2章 / 第3节（后面可跟章节名；空白分隔要保留，题号要丢掉）
_CHAPTER_RE = re.compile(
    r"第\s*([0-9]{1,3}|[零一二两三四五六七八九十百]{1,3})\s*([章节课])"
    r"(?:(\s*)([^\s，。；;,、]{1,24}))?"
)
# 教材：《药理学》 或 「药理学」或 药理学第二章
_BOOK_QUOTE_RE = re.compile(r"[《「『]([^》」』]{1,20})[》」』]")
_BOOK_PREFIX_RE = re.compile(
    r"(?:^|[，,。；;\s])([\u4e00-\u9fa5A-Za-z0-9]{2,12})"
    r"(?:的)?\s*第\s*(?:[0-9]{1,3}|[零一二两三四五六七八九十百]{1,3})\s*[章节课]"
)
# 无线调试地址：192.168.1.5:5555
_ADDR_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})(?::(\d{2,5}))?\b")
# 收尾词：章节名后面经常跟着「的题目 / 的题 / 题」
_CHAPTER_TAIL_RE = re.compile(r"(的?题目?|的?内容|的?题|的)$")

_DRY_RUN_HINTS = ("先别跑", "先不要跑", "别真的跑", "不要真的跑", "只看配置", "先看看", "看看配置", "试跑", "dry-run", "dry run")
_FORCE_HINTS = ("强制", "force", "不管题号", "忽略预检")

# 提取模块：必须同时命中「模块词」和「触发词」才认，宁可少解析也不要解析错
_MODULE_HINTS = ("提取", "要", "带", "包括", "包含", "连", "还有", "以及", "额外", "需要", "加上", "module")
_MODULE_NEGATIONS = ("不要", "不用", "不需要", "别", "去掉", "除了", "没有", "只看")

# 加长主屏：开启 / 关闭的说法
_DISPLAY_ON_HINTS = ("加长屏", "加长主屏", "加长屏幕", "拉长屏幕", "长屏", "wide", "一次截全", "整页截图")
_DISPLAY_OFF_HINTS = ("不要加长", "不用加长", "别动显示", "不要改显示", "不改显示", "保持原样")
_SCALE_RE = re.compile(r"([0-9]+(?:.[0-9]+)?)[ ]*倍")


def _clean_chapter_tail(name: str) -> str:
    text = name.strip()
    while True:
        trimmed = _CHAPTER_TAIL_RE.sub("", text)
        if trimmed == text:
            return text
        text = trimmed


def find_modules(text: str) -> list[str]:
    """从一句话里找出用户点名要提取的模块（需要触发词，且排除否定说法）。

    例：「要考点还原和标准解析」-> ["考点还原", "标准解析"]；
        「不要解析」-> []。
    """
    raw = str(text or "")
    if not raw:
        return []
    candidates: list[tuple[str, Any]] = []
    for spec in PRESETS:
        for key in [spec.name, *spec.aliases]:
            if key:
                candidates.append((key, spec))
    # 长词优先，避免「解析」把「标准解析」拆掉
    candidates.sort(key=lambda item: len(item[0]), reverse=True)

    used = [False] * len(raw)
    found: list[str] = []
    for key, spec in candidates:
        start = 0
        while True:
            index = raw.find(key, start)
            if index < 0:
                break
            span = slice(index, index + len(key))
            if not any(used[span]):
                before = raw[max(0, index - 6):index]
                if not any(word in before for word in _MODULE_NEGATIONS):
                    used[span] = [True] * len(key)
                    if spec.name not in found:
                        found.append(spec.name)
            start = index + len(key)
    return found


def parse_request(text: Any) -> dict[str, Any]:
    """解析一句自然语言，返回 {"params": {...}, "evidence": {...}}。

    支持的参数：page_from / page_to / output_dir / textbook / chapter / device_address /
    modules_text / display_mode / display_scale，
    以及 dry_run / force_start 两个布尔意图。解析不出来就不放进 params。
    """
    raw = "" if text is None else str(text)
    params: dict[str, Any] = {}
    evidence: dict[str, str] = {}
    if not raw.strip():
        return {"params": params, "evidence": evidence}

    def take(key: str, value: Any, matched: str) -> None:
        if value in (None, "", 0):
            return
        if key in params:
            return
        params[key] = value
        evidence[key] = matched

    match = _RANGE_RE.search(raw)
    if match:
        low, high = int(match.group(1)), int(match.group(2))
        if 0 < low <= high:
            take("page_from", low, match.group(0))
            take("page_to", high, match.group(0))
    if "page_from" not in params:
        single = _SINGLE_RE.search(raw)
        if single:
            number = int(single.group(1))
            if number > 0:
                take("page_from", number, single.group(0))
                take("page_to", number, single.group(0))

    output = _OUTPUT_RE.search(raw)
    if output:
        take("output_dir", output.group(1).rstrip("。.！!？?"), output.group(0))
    if "output_dir" not in params:
        win = _WIN_PATH_RE.search(raw)
        if win:
            take("output_dir", win.group(1).rstrip("。.！!？?"), win.group(0))

    chapter_match = _CHAPTER_RE.search(raw)
    if chapter_match:
        prefix = f"第{chapter_match.group(1)}{chapter_match.group(2)}"
        separator = chapter_match.group(3) or ""
        tail = _clean_chapter_tail(chapter_match.group(4) or "")
        if re.match(r"^\d", tail):        # 「第二章 1 到 20 题」里的 1 是题号，不是章节名
            tail, separator = "", ""
        take("chapter", (prefix + separator + tail).strip(), chapter_match.group(0).strip())

    quoted = _BOOK_QUOTE_RE.search(raw)
    if quoted:
        take("textbook", quoted.group(1).strip(), quoted.group(0))
    else:
        prefix = _BOOK_PREFIX_RE.search(raw)
        if prefix:
            book = prefix.group(1).strip()
            if book and not re.fullmatch(r"第[0-9零一二两三四五六七八九十百]+[章节课]", book):
                take("textbook", book, prefix.group(0).strip())

    address = _ADDR_RE.search(raw)
    if address:
        host, port = address.group(1), address.group(2)
        take("device_address", f"{host}:{port}" if port else host, address.group(0))

    module_hits = find_modules(raw)
    if module_hits and any(hint in raw.lower() for hint in _MODULE_HINTS):
        take("modules_text", ",".join(module_hits), "、".join(module_hits))

    if any(hint in raw.lower() for hint in _DISPLAY_OFF_HINTS):
        take("display_mode", "off", next(h for h in _DISPLAY_OFF_HINTS if h in raw))
    elif any(hint in raw.lower() for hint in _DISPLAY_ON_HINTS):
        take("display_mode", "wide", next(h for h in _DISPLAY_ON_HINTS if h in raw.lower()))
        scale = _SCALE_RE.search(raw)
        if scale:
            try:
                value = float(scale.group(1))
            except ValueError:
                value = 0.0
            if 1.0 < value <= 4.0:
                take("display_scale", value, scale.group(0))

    lowered = raw.lower()
    if any(hint in lowered for hint in _DRY_RUN_HINTS):
        take("dry_run", True, next(hint for hint in _DRY_RUN_HINTS if hint in lowered))
    if any(hint in lowered for hint in _FORCE_HINTS):
        take("force_start", True, next(hint for hint in _FORCE_HINTS if hint in lowered))
    return {"params": params, "evidence": evidence}


def describe_params(params: dict[str, Any]) -> str:
    """把解析结果写成一行中文，方便人读输出。"""
    if not params:
        return "没有解析出任何参数"
    labels: list[tuple[str, Callable[[Any], str]]] = [
        ("page_from", lambda v: f"起始题号 {v}"),
        ("page_to", lambda v: f"结束题号 {v}"),
        ("output_dir", lambda v: f"输出目录 {v}"),
        ("textbook", lambda v: f"教材 {v}"),
        ("chapter", lambda v: f"章节 {v}"),
        ("device_address", lambda v: f"手机 {v}"),
        ("modules_text", lambda v: f"模块 {v}"),
        ("display_mode", lambda v: "加长主屏" if v == "wide" else "不加长屏幕"),
        ("display_scale", lambda v: f"加长 {v} 倍"),
        ("dry_run", lambda v: "只校验（dry-run）"),
        ("force_start", lambda v: "强制继续"),
    ]
    order = {
        "page_from": 0, "page_to": 1, "output_dir": 2, "textbook": 3,
        "chapter": 4, "device_address": 5, "modules_text": 6,
        "display_mode": 7, "display_scale": 8, "dry_run": 9, "force_start": 10,
    }
    labels.sort(key=lambda item: order.get(item[0], 99))
    parts = [formatter(params[key]) for key, formatter in labels if key in params]
    return "；".join(parts)


__all__ = ["describe_params", "find_modules", "parse_request"]
