# -*- coding: utf-8 -*-
"""输出层：

    * `<教材>_<章号>_<章节>.jsonl`：一行一题，每行自包含教材/章节元数据
    * `<教材>_<章号>_<章节>.md`：按用户给定格式渲染，供人阅读
    * `index.json`：教材 -> 章节 -> 文件/题号范围 的索引，供 AI 检索

同一个章节文件再次提取时会按题号合并，方便补录漏掉的那一道题。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .config import OutputPaths
from .fileio import atomic_write_text, read_json_file
from .models import TYPE_MULTI, Question
from .modules import (
    ModuleSpec,
    module_names,
    modules_to_payload,
    normalize_output_fields,
    output_field_enabled,
)

TYPE_LABEL = {TYPE_MULTI: "多选题", "single": "单选题"}


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _record_dict(item: Any) -> dict[str, Any]:
    if isinstance(item, Mapping):
        return dict(item)
    to_dict = getattr(item, "to_dict", None)
    if callable(to_dict):
        return dict(to_dict())
    raise TypeError(f"无法序列化的题目对象：{type(item)!r}")


def _record_id(record: Mapping[str, Any]) -> int:
    try:
        number = int(record.get("id", 0))
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def render_markdown(
    items: Sequence[Any],
    modules: Sequence[ModuleSpec] | None = None,
    output_fields: Any = None,
) -> str:
    """按用户给定格式渲染 Markdown。

    格式：
        ## 第 N 题 【单选题】
        题干
        A. 选项内容␠␠
        ### **答案：A**
        ### **考点还原**
        正文（配了模块时才有这一段）

    modules 给出本次配置的模块清单：按配置顺序渲染，并遵守 in_markdown；
    不给时按记录里 modules 的书写顺序全渲染。
    output_fields 是题目本体字段（题号 / 题目 / 答案）的开关，见 modules.BASE_FIELDS：
    关掉哪个就不渲染哪一段；三个都关掉时这一段只剩自定义模块。
    """
    fields = normalize_output_fields(output_fields)
    show_id = output_field_enabled(fields, "id", "markdown")
    show_stem = output_field_enabled(fields, "stem", "markdown")
    show_answer = output_field_enabled(fields, "answer", "markdown")
    order = module_names(modules) if modules else None
    skip = {spec.name for spec in (modules or ()) if not spec.in_markdown}
    blocks: list[str] = []
    for item in items:
        record = _record_dict(item)
        label = TYPE_LABEL.get(record.get("type"), "单选题")
        heading = f"第 {record.get('id', '?')} 题" if show_id else ""
        lines = [f"## {heading} 【{label}】".replace("  ", " ").strip()]
        if show_stem:
            stem = str(record.get("stem") or "").strip()
            if stem:
                lines.append(stem)
            options = record.get("options") or {}
            for letter in sorted(options):
                lines.append(f"{letter}. {options[letter]}  ")
        if show_answer:
            answer = record.get("answer") or "未识别"
            lines.append(f"### **答案：{answer}**")
        for name, text in _ordered_modules(record.get("modules"), order, skip):
            lines.append(f"### **{name}**")
            lines.append(text)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def _ordered_modules(
    value: Any,
    order: Sequence[str] | None,
    skip: set[str],
) -> list[tuple[str, str]]:
    """把题目上的模块正文按配置顺序排好（配置外的模块排在后面）。"""
    if not isinstance(value, Mapping):
        return []
    items = [(str(key), str(text)) for key, text in value.items() if str(text).strip()]
    items = [(key, text) for key, text in items if key not in skip]
    if not order:
        return items
    ranked = {name: index for index, name in enumerate(order)}
    return sorted(items, key=lambda item: (ranked.get(item[0], len(ranked)), item[0]))


def render_jsonl(records: Sequence[Mapping[str, Any]]) -> str:
    """一行一个 JSON 对象，不带代码块标记。"""
    return "".join(json.dumps(dict(record), ensure_ascii=False) + "\n" for record in records)


def update_index(index_path: str, entry: Mapping[str, Any]) -> str:
    """把一条章节条目合并进 index.json 并写回。"""
    data: dict[str, Any] = {}
    if os.path.isfile(index_path):
        try:
            loaded = read_json_file(index_path)
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = {}
    entries = [
        item
        for item in data.get("entries", [])
        if isinstance(item, dict) and item.get("file") != entry.get("file")
    ]
    entries.append(dict(entry))
    entries.sort(
        key=lambda item: (
            str(item.get("textbook", "")),
            item.get("chapter_no") or 0,
            str(item.get("file", "")),
        )
    )
    payload = {"version": 1, "generated_at": now_text(), "entries": entries}
    atomic_write_text(index_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return index_path


class ResultWriter:
    """负责把识别结果写入 .jsonl / .md / index.json，并按题号合并历史结果。"""

    def __init__(
        self,
        paths: OutputPaths,
        metadata: Mapping[str, Any],
        log: Callable[[str], None] | None = None,
        modules: Sequence[ModuleSpec] | None = None,
        output_fields: Any = None,
    ) -> None:
        self.paths = paths
        self.metadata = dict(metadata)
        self.log = log or (lambda message: None)
        # 本次配置的提取模块：决定 JSONL 里模块字段的顺序与 Markdown 渲染哪些
        self.modules = list(modules or ())
        # 题目本体字段（题号 / 题目 / 答案）写不写进两个文件
        self.output_fields = normalize_output_fields(output_fields)
        # JSONL 用过滤后的记录；Markdown / index 用「全字段」记录 ——
        # 「只写 Markdown 不写 JSONL」的字段在渲染时还得拿得到原文
        self._records: dict[int, dict[str, Any]] = {}
        self._full_records: dict[int, dict[str, Any]] = {}
        # 已有文件里没有题号的行：按题号合并不了，原样保留、新结果追加在后面
        self._raw_lines: list[str] = []
        self._load_existing()

    def _load_existing(self) -> None:
        path = self.paths.jsonl
        if not os.path.isfile(path):
            return
        loaded = 0
        orphans: list[str] = []
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    number = _record_id(record)
                    if number:
                        self._records[number] = record
                        self._full_records[number] = dict(record)
                        loaded += 1
                    else:
                        orphans.append(line)
        except OSError as exc:
            self.log(f"[警告] 读取已有文件失败，将直接覆盖：{exc}")
            return
        if orphans:
            # 「不写题号」的用户再跑同一章节时，绝不能把上一次的内容冲掉
            self._raw_lines = orphans
            self.log(
                f"[警告] 已有文件里有 {len(orphans)} 行没有题号（id），无法按题号合并："
                "这些行原样保留，本次结果追加在后面"
            )
        if loaded:
            self.log(f"[读取] 已有 {loaded} 道题，按题号合并后写回")

    def _serialize(self, question: Question, *, for_markdown: bool = False) -> dict[str, Any]:
        """题目 -> 一行记录。

        for_markdown=True 时保留全部本体字段（Markdown 该渲染哪几段由
        render_markdown 里的 output_fields 决定）；JSONL 则按开关过滤。
        """
        data = question.to_dict()
        fields = self.output_fields
        record: dict[str, Any] = {}
        # 教材/章节元数据永远写：它们是任务范围的一部分，不属于可关的本体字段
        if for_markdown or output_field_enabled(fields, "id", "json"):
            record["id"] = data["id"]
        record.update({
            "textbook": self.metadata.get("textbook", ""),
            "chapter": self.metadata.get("chapter", ""),
            "chapter_no": self.metadata.get("chapter_no"),
            "total": self.metadata.get("total"),
        })
        if for_markdown or output_field_enabled(fields, "stem", "json"):
            record["type"] = data["type"]
            record["stem"] = data["stem"]
            record["options"] = data["options"]
        if for_markdown or output_field_enabled(fields, "answer", "json"):
            record["answer"] = data["answer"]
        # 模块按配置顺序写；modules 为空时不写这个键（与旧版输出逐字节一致）
        order = module_names(self.modules) if self.modules else None
        skip = set() if for_markdown else {spec.name for spec in self.modules if not spec.in_json}
        ordered = _ordered_modules(data.get("modules"), order, skip)
        if ordered:
            record["modules"] = {name: text for name, text in ordered}
        return record

    def _sorted_records(self) -> list[dict[str, Any]]:
        return [self._records[key] for key in sorted(self._records)]

    def _sorted_full_records(self) -> list[dict[str, Any]]:
        return [self._full_records[key] for key in sorted(self._full_records)]

    def _index_entry(
        self,
        records: Sequence[Mapping[str, Any]],
        modules: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        entry = self._index_entry_base(records)
        names = [str(item.get("name") or "") for item in (modules or [])]
        names = [name for name in names if name]
        if not names:
            # 调用方没给清单时，从记录里推断（保持索引可读）
            found: list[str] = []
            for record in records:
                for name in (record.get("modules") or {}):
                    if name not in found:
                        found.append(str(name))
            names = found
        if names:
            entry["modules"] = names
        return entry

    def _index_entry_base(self, records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        return {
            "textbook": self.metadata.get("textbook", ""),
            "chapter": self.metadata.get("chapter", ""),
            "chapter_no": self.metadata.get("chapter_no"),
            "total": self.metadata.get("total"),
            "file": os.path.basename(self.paths.jsonl),
            "markdown_file": (
                os.path.basename(self.paths.markdown) if self.paths.markdown else None
            ),
            "count": len(records),
            "question_ids": [_record_id(record) for record in records],
            "updated_at": now_text(),
        }

    def _has_real_record(self, number: int) -> bool:
        """文件里这一题是不是「真的识别过」（不是上次留下的失败占位）。"""
        record = self._full_records.get(number)
        if not record:
            return False
        return not str(record.get("stem") or "").startswith("[识别失败]")

    def flush(self, questions: Sequence[Question]) -> dict[str, str | None]:
        """合并本次结果并写出全部文件。"""
        for question in questions:
            if question.id > 0:
                if question.is_placeholder and self._has_real_record(question.id):
                    # 补录时这一题这次没认出来：保留上一次的好内容，别把成果冲成「[识别失败]」
                    self.log(
                        f"[保留] 第 {question.id} 题本次识别失败，保留文件里已有的内容（未覆盖）"
                    )
                    continue
                self._records[question.id] = self._serialize(question)
                self._full_records[question.id] = self._serialize(question, for_markdown=True)
        records = self._sorted_records()
        full_records = self._sorted_full_records()

        # 没有题号的老内容原样保留（见 _load_existing），本次结果追加在后面
        lines = list(self._raw_lines) + [
            json.dumps(record, ensure_ascii=False) for record in records
        ]
        atomic_write_text(self.paths.jsonl, "".join(line + "\n" for line in lines))

        markdown_path: str | None = None
        if self.paths.markdown:
            markdown_path = atomic_write_text(
                self.paths.markdown,
                render_markdown(full_records, self.modules, self.output_fields),
            )

        index_path: str | None = None
        if self.paths.index:
            index_path = update_index(
                self.paths.index,
                # 索引按「全字段」记录算：题号列表不该因为关了 JSON 的 id 就变成 0
                self._index_entry(full_records, modules=modules_to_payload(self.modules)),
            )

        self.log(
            f"[写入] {len(records)} 道题 -> {Path(self.paths.jsonl).name}"
            + (f" / {Path(markdown_path).name}" if markdown_path else "")
            + (f" / {Path(index_path).name}" if index_path else "")
        )
        return {"jsonl": self.paths.jsonl, "markdown": markdown_path, "index": index_path}

    def finalize(self, questions: Sequence[Question]) -> dict[str, str | None]:
        return self.flush(questions)
