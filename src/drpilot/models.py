# -*- coding: utf-8 -*-
"""题目数据结构。

写入 JSONL 的固定字段由 output 模块负责拼装（含教材/章节元数据），
这里只保留题目本身的五个字段，外加 AI 读到的屏幕题号用于校验。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .modules import ModuleSpec, match_module_name

OPTION_LETTERS = "ABCDEFGH"
TYPE_SINGLE = "single"
TYPE_MULTI = "multi"

_MULTI_ALIASES = {"multi", "multiple", "multiple_choice", "多选", "多选题", "多项选择题"}
_SINGLE_ALIASES = {"single", "single_choice", "radio", "单选", "单选题", "单项选择"}
_ANSWER_SEP_RE = re.compile(r"[\s,，、;；/|]+")


def normalize_type(value: Any) -> str:
    """把各种写法的题型统一成 single / multi。"""
    if value is None:
        return TYPE_SINGLE
    text = str(value).strip().lower()
    if text in _MULTI_ALIASES:
        return TYPE_MULTI
    if text in _SINGLE_ALIASES:
        return TYPE_SINGLE
    if "多选" in text or "multiple" in text or "multi" in text:
        return TYPE_MULTI
    return TYPE_SINGLE


def normalize_options(value: Any) -> dict[str, str]:
    """把选项统一成 {大写字母: 文本}，并按字母排序。"""
    options: dict[str, str] = {}
    if value is None:
        return options
    if isinstance(value, Mapping):
        items = list(value.items())
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        # 选项列表不带字母，只能按 A..H 依次贴标签；超过 8 个的部分无处可标。
        # 这里显式写 strict=False，把「有意截断」和「漏读选项」区分开。
        items = list(zip(OPTION_LETTERS, value, strict=False))
    else:
        return options

    for raw_key, raw_val in items:
        key = str(raw_key).strip().upper()
        if not key:
            continue
        key = key[0] if key[0] in OPTION_LETTERS else key
        text = "" if raw_val is None else str(raw_val).strip()
        if not text:
            continue
        options[key] = text
    return {k: options[k] for k in sorted(options)}


def normalize_answer(value: Any) -> str:
    """把答案统一成大写字母串，例如 "b, c" -> "BC"。"""
    if value is None:
        return ""
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        parts = [str(v) for v in value]
    else:
        parts = _ANSWER_SEP_RE.split(str(value).strip())
    letters: list[str] = []
    for part in parts:
        for ch in part.upper():
            if ch in OPTION_LETTERS and ch not in letters:
                letters.append(ch)
    return "".join(letters)


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def normalize_modules_map(value: Any) -> dict[str, str]:
    """把模块字段统一成 {模块名: 正文}，丢掉空值。"""
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, str] = {}
    for raw_key, raw_val in value.items():
        key = str(raw_key or "").strip()
        text = "" if raw_val is None else str(raw_val).strip()
        if key and text:
            result[key] = text
    return result


def extract_modules(data: Mapping[str, Any], specs: Sequence[ModuleSpec] | None = None) -> dict[str, str]:
    """从 AI 返回的元素里取出模块正文。

    同时兼容两种写法：
        * 模块与 stem/answer 平级：{"考点还原": "…"}
        * 收在 modules 对象里：{"modules": {"考点还原": "…"}}
    配了模块清单时，名字/别名都会归一到规范模块名。
    """
    found: dict[str, str] = {}
    nested = normalize_modules_map(data.get("modules"))
    for key, value in nested.items():
        name = match_module_name(specs, key) if specs else None
        found[name or key] = value
    if specs:
        for spec in specs:
            for key, value in data.items():
                if not isinstance(key, str) or key == "modules":
                    continue
                if not spec.matches(key):
                    continue
                text = "" if value is None else str(value).strip()
                if text:
                    found[spec.name] = text
                break
    return found


@dataclass
class Question:
    """一道题。"""

    id: int = 0
    type: str = TYPE_SINGLE
    stem: str = ""
    options: dict[str, str] = field(default_factory=dict)
    answer: str = ""
    # 用户自定义的提取模块（考点还原 / 标准解析 …）：{模块名: 正文}
    modules: dict[str, str] = field(default_factory=dict)
    # AI 从截图上读到的题号/总题数，仅用于校验与纠错，不写入 JSONL 的固定五字段
    screen_id: int | None = None
    screen_total: int | None = None

    def normalize(self) -> "Question":
        try:
            self.id = int(self.id)
        except (TypeError, ValueError):
            self.id = 0
        self.type = normalize_type(self.type)
        self.stem = "" if self.stem is None else str(self.stem).strip()
        self.options = normalize_options(self.options)
        self.answer = normalize_answer(self.answer)
        self.modules = normalize_modules_map(self.modules)
        self.screen_id = _optional_int(self.screen_id)
        self.screen_total = _optional_int(self.screen_total)
        return self

    def to_dict(self) -> dict[str, Any]:
        """题目本身的字段（教材/章节元数据由 writer 追加）。

        modules 为空时不写这个键 —— 保证不用模块的用户，输出与旧版逐字节一致。
        """
        data: dict[str, Any] = {
            "id": self.id,
            "type": self.type,
            "stem": self.stem,
            "options": dict(self.options),
            "answer": self.answer,
        }
        if self.modules:
            data["modules"] = dict(self.modules)
        return data

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
        modules: Sequence[ModuleSpec] | None = None,
    ) -> "Question":
        """宽容地从任意 dict 构造。

        modules 是本次运行的模块清单：给了就会把「考点还原」这类自定义键收进
        Question.modules（别名也算），不给则只认固定字段。
        """
        if not isinstance(data, Mapping):
            raise TypeError("题目必须是对象")
        q = cls(
            id=data.get("id", 0),
            type=data.get("type", TYPE_SINGLE),
            stem=data.get("stem", data.get("question", data.get("title", ""))),
            options=data.get("options", data.get("choices", {})),
            answer=data.get("answer", data.get("answers", data.get("correct", ""))),
            screen_id=(
                data.get("screen_id")
                if data.get("screen_id") is not None
                else data.get("screen_no", data.get("screen_number", data.get("display_id")))
            ),
            screen_total=(
                data.get("screen_total")
                if data.get("screen_total") is not None
                else data.get("total", data.get("total_questions", data.get("total_count")))
            ),
        )
        q.modules = extract_modules(data, modules)
        return q.normalize()

    @classmethod
    def placeholder(cls, qid: int, reason: str = "识别失败") -> "Question":
        """识别失败时的占位对象，保证题号不丢。"""
        try:
            qid = int(qid)
        except (TypeError, ValueError):
            qid = 0
        return cls(
            id=qid,
            type=TYPE_SINGLE,
            stem=f"[识别失败] 第 {qid} 题：{reason}",
            options={},
            answer="",
        )

    @property
    def is_placeholder(self) -> bool:
        return self.stem.startswith("[识别失败]")
