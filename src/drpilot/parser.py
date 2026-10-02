# -*- coding: utf-8 -*-
"""解析 AI 返回内容。

两种用途：
    * parse_ai_response / parse_questions：解析识别结果（题目数组）
    * parse_screen_info：解析预检结果（截图右上角的题号 / 总题数）

AI 可能返回：纯 JSON 数组、代码块、前后带说明文字、JSONL、包装对象。
本模块尽量容错，并保证结果与输入图片数量对齐。
"""

from __future__ import annotations

import json
import re
from typing import Any, Sequence

from .errors import ParseError
from .models import Question

_CODE_FENCE_RE = re.compile(r"```[a-zA-Z0-9_+-]*\s*\n?(.*?)```", re.DOTALL)
_WRAPPER_KEYS = ("questions", "data", "items", "result", "results", "list")
_SCREEN_ID_RE = re.compile(r'"?\s*(?:screen_)?id\s*"?\s*[:=]\s*"?\s*(\d+)', re.IGNORECASE)
_SCREEN_TOTAL_RE = re.compile(r'"?\s*(?:screen_)?total\s*"?\s*[:=]\s*"?\s*(\d+)', re.IGNORECASE)
_FRACTION_RE = re.compile(r"(\d+)\s*/\s*(\d+)")
_FIRST_INT_RE = re.compile(r"(\d+)")


def strip_code_fences(text: str) -> str:
    """如果存在 Markdown 代码块，取其中最长的那个作为候选内容。"""
    if not text:
        return ""
    matches = _CODE_FENCE_RE.findall(text)
    if matches:
        return max(matches, key=len).strip()
    return text.strip()


def _try_loads(text: str) -> tuple[Any, bool]:
    try:
        return json.loads(text), True
    except (json.JSONDecodeError, ValueError):
        return None, False


def _raw_decode_at(text: str, index: int) -> Any:
    try:
        obj, _ = json.JSONDecoder().raw_decode(text, index)
        return obj
    except (json.JSONDecodeError, ValueError):
        return None


def _decode_first_json(text: str) -> Any:
    """整段解析失败时，从第一个 [ 或 { 开始尝试解析一段 JSON。"""
    obj, ok = _try_loads(text)
    if ok:
        return obj
    starts = sorted({i for i in (text.find("["), text.find("{")) if i >= 0})
    for start in starts:
        obj = _raw_decode_at(text, start)
        if obj is not None:
            return obj
    return None


def _unwrap(data: Any) -> list[Any]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in _WRAPPER_KEYS:
            value = data.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                return [value]
        return [data]
    raise ParseError("AI 返回的 JSON 顶层既不是数组也不是对象")


def extract_items(text: str) -> list[Any]:
    """从原始文本中提取 JSON 元素列表，失败抛 ParseError。"""
    cleaned = strip_code_fences(text)
    if not cleaned:
        raise ParseError("AI 返回内容为空")
    data = _decode_first_json(cleaned)
    if data is not None:
        return _unwrap(data)

    # 逐行解析兜底（JSONL 场景）
    objects: list[Any] = []
    for line in cleaned.splitlines():
        line = line.strip().rstrip(",")
        if not line or line[0] not in "[{":
            continue
        obj = _decode_first_json(line)
        if obj is None:
            continue
        if isinstance(obj, list):
            objects.extend(obj)
        else:
            objects.append(obj)
    if objects:
        return objects
    raise ParseError(f"无法从 AI 返回中解析出 JSON：{cleaned[:200]}")


def _is_meaningful(question: Question) -> bool:
    return bool(question.stem or question.options or question.answer or question.id or question.screen_id)


def parse_ai_response(
    text: str,
    expected_count: int | None = None,
    expected_ids: Sequence[int | None] | None = None,
) -> list[Question | None]:
    """解析并返回与图片一一对应的列表；无法识别的槽位为 None。"""
    raw_items = extract_items(text)
    questions: list[Question | None] = []
    for item in raw_items:
        if not isinstance(item, dict):
            questions.append(None)
            continue
        try:
            question = Question.from_dict(item)
        except Exception:
            questions.append(None)
            continue
        questions.append(question if _is_meaningful(question) else None)

    if expected_count is not None:
        if len(questions) < expected_count:
            questions.extend([None] * (expected_count - len(questions)))
        elif len(questions) > expected_count:
            questions = questions[:expected_count]

    if expected_ids is not None:
        for index, question in enumerate(questions):
            if index >= len(expected_ids):
                break
            expected_id = expected_ids[index]
            if question is not None and expected_id and not question.id:
                question.id = int(expected_id)

    return questions


def parse_questions(text: str, expected_ids: Sequence[int | None]) -> list[Question]:
    """解析并补齐占位，保证长度等于 expected_ids（主要给测试和兜底用）。"""
    ids = list(expected_ids)
    parsed = parse_ai_response(text, expected_count=len(ids), expected_ids=ids)
    result: list[Question] = []
    for index, question in enumerate(parsed):
        if question is None:
            expected_id = ids[index] if index < len(ids) else 0
            result.append(Question.placeholder(expected_id or 0))
        else:
            result.append(question)
    return result


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def parse_screen_info(text: str) -> tuple[int | None, int | None]:
    """解析预检返回的 (当前题号, 总题数)，解析失败的部分为 None。"""
    cleaned = strip_code_fences(text or "")
    if not cleaned:
        return None, None

    data = _decode_first_json(cleaned)
    candidates: list[Any] = []
    if isinstance(data, dict):
        candidates.append(data)
    elif isinstance(data, list):
        candidates.extend(item for item in data if isinstance(item, dict))

    for item in candidates:
        screen_id = _positive_int(
            item.get("screen_id", item.get("screen_no", item.get("id")))
        )
        screen_total = _positive_int(
            item.get("screen_total", item.get("total", item.get("total_questions")))
        )
        if screen_id is not None or screen_total is not None:
            return screen_id, screen_total

    screen_id = None
    screen_total = None
    match = _SCREEN_ID_RE.search(cleaned)
    if match:
        screen_id = _positive_int(match.group(1))
    match = _SCREEN_TOTAL_RE.search(cleaned)
    if match:
        screen_total = _positive_int(match.group(1))
    if screen_id is None or screen_total is None:
        match = _FRACTION_RE.search(cleaned)
        if match:
            screen_id = screen_id or _positive_int(match.group(1))
            screen_total = screen_total or _positive_int(match.group(2))
    if screen_id is None:
        match = _FIRST_INT_RE.search(cleaned)
        if match:
            screen_id = _positive_int(match.group(1))
    return screen_id, screen_total
