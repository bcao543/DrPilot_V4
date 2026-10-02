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


def render_markdown(items: Sequence[Any]) -> str:
    """按用户给定格式渲染 Markdown。

    格式：
        ## 第 N 题 【单选题】
        题干
        A. 选项内容␠␠
        ### **答案：A**
    """
    blocks: list[str] = []
    for item in items:
        record = _record_dict(item)
        label = TYPE_LABEL.get(record.get("type"), "单选题")
        lines = [f"## 第 {record.get('id', '?')} 题 【{label}】"]
        stem = str(record.get("stem") or "").strip()
        if stem:
            lines.append(stem)
        options = record.get("options") or {}
        for letter in sorted(options):
            lines.append(f"{letter}. {options[letter]}  ")
        answer = record.get("answer") or "未识别"
        lines.append(f"### **答案：{answer}**")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + ("\n" if blocks else "")


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
    ) -> None:
        self.paths = paths
        self.metadata = dict(metadata)
        self.log = log or (lambda message: None)
        self._records: dict[int, dict[str, Any]] = {}
        self._load_existing()

    def _load_existing(self) -> None:
        path = self.paths.jsonl
        if not os.path.isfile(path):
            return
        loaded = 0
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
                        loaded += 1
        except OSError as exc:
            self.log(f"[警告] 读取已有文件失败，将直接覆盖：{exc}")
            return
        if loaded:
            self.log(f"[读取] 已有 {loaded} 道题，按题号合并后写回")

    def _serialize(self, question: Question) -> dict[str, Any]:
        data = question.to_dict()
        return {
            "id": data["id"],
            "textbook": self.metadata.get("textbook", ""),
            "chapter": self.metadata.get("chapter", ""),
            "chapter_no": self.metadata.get("chapter_no"),
            "total": self.metadata.get("total"),
            "type": data["type"],
            "stem": data["stem"],
            "options": data["options"],
            "answer": data["answer"],
        }

    def _sorted_records(self) -> list[dict[str, Any]]:
        return [self._records[key] for key in sorted(self._records)]

    def _index_entry(self, records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
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

    def flush(self, questions: Sequence[Question]) -> dict[str, str | None]:
        """合并本次结果并写出全部文件。"""
        for question in questions:
            if question.id > 0:
                self._records[question.id] = self._serialize(question)
        records = self._sorted_records()

        atomic_write_text(self.paths.jsonl, render_jsonl(records))

        markdown_path: str | None = None
        if self.paths.markdown:
            markdown_path = atomic_write_text(self.paths.markdown, render_markdown(records))

        index_path: str | None = None
        if self.paths.index:
            index_path = update_index(self.paths.index, self._index_entry(records))

        self.log(
            f"[写入] {len(records)} 道题 -> {Path(self.paths.jsonl).name}"
            + (f" / {Path(markdown_path).name}" if markdown_path else "")
            + (f" / {Path(index_path).name}" if index_path else "")
        )
        return {"jsonl": self.paths.jsonl, "markdown": markdown_path, "index": index_path}

    def finalize(self, questions: Sequence[Question]) -> dict[str, str | None]:
        return self.flush(questions)
