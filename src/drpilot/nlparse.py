# -*- coding: utf-8 -*-
"""自然语言 -> 参数：把用户的一句中文需求解析成 CLI 参数。

定位：**只填有把握的**。解析不出来的一律不猜，留给 drpilot ask / 交互式提问去补。
所以这里宁可少解析，也不要解析错——错误参数比缺参数危险得多。

    drpilot ask --text "药理学第二章 47 到 81 题，存到 D:\\题库" --json
"""

from __future__ import annotations

import re
from typing import Any, Callable

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


def _clean_chapter_tail(name: str) -> str:
    text = name.strip()
    while True:
        trimmed = _CHAPTER_TAIL_RE.sub("", text)
        if trimmed == text:
            return text
        text = trimmed


def parse_request(text: Any) -> dict[str, Any]:
    """解析一句自然语言，返回 {"params": {...}, "evidence": {...}}。

    支持的参数：page_from / page_to / output_dir / textbook / chapter / device_address，
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
        ("dry_run", lambda v: "只校验（dry-run）"),
        ("force_start", lambda v: "强制继续"),
    ]
    parts = [formatter(params[key]) for key, formatter in labels if key in params]
    return "；".join(parts)


__all__ = ["describe_params", "parse_request"]
