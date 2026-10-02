# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import json
import tempfile
import unittest

from drpilot.config import OutputPaths
from drpilot.models import Question
from drpilot.output import ResultWriter, render_jsonl, render_markdown

META = {
    "textbook": "药理学",
    "chapter": "第二章 药物代谢动力学",
    "chapter_no": 2,
    "total": 53,
}


class OutputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = OutputPaths(
            jsonl=os.path.join(self.tmp.name, "药理学_02_药物代谢动力学.jsonl"),
            markdown=os.path.join(self.tmp.name, "药理学_02_药物代谢动力学.md"),
            index=os.path.join(self.tmp.name, "index.json"),
            directory=self.tmp.name,
            base_name="药理学_02_药物代谢动力学",
        )
        self.questions = [
            Question(id=1, type="single", stem="题干 A", options={"A": "甲", "B": "乙"}, answer="A"),
            Question(id=2, type="multi", stem="题干 B", options={"B": "乙", "C": "丙"}, answer="BC"),
        ]

    def _records(self):
        with open(self.paths.jsonl, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def test_render_jsonl_one_object_per_line(self):
        text = render_jsonl([question.to_dict() for question in self.questions])
        lines = text.strip().split("\n")
        self.assertEqual(len(lines), 2)
        self.assertNotIn("```", text)
        self.assertEqual(json.loads(lines[0])["id"], 1)

    def test_markdown_format(self):
        markdown = render_markdown(self.questions)
        self.assertIn("## 第 1 题 【单选题】", markdown)
        self.assertIn("## 第 2 题 【多选题】", markdown)
        self.assertIn("A. 甲  \n", markdown)  # 选项后两个空格
        self.assertIn("### **答案：BC**", markdown)
        self.assertNotIn("---", markdown)

    def test_writer_metadata_index_and_markdown(self):
        writer = ResultWriter(self.paths, META)
        result = writer.finalize(self.questions)
        records = self._records()
        self.assertEqual([record["id"] for record in records], [1, 2])
        self.assertEqual(records[0]["textbook"], "药理学")
        self.assertEqual(records[0]["chapter_no"], 2)
        self.assertEqual(records[0]["total"], 53)
        self.assertTrue(os.path.isfile(result["markdown"]))
        with open(self.paths.index, encoding="utf-8") as handle:
            index = json.load(handle)
        self.assertEqual(index["entries"][0]["question_ids"], [1, 2])
        self.assertEqual(index["entries"][0]["file"], os.path.basename(self.paths.jsonl))

    def test_writer_merges_by_id(self):
        ResultWriter(self.paths, META).finalize(self.questions)
        ResultWriter(self.paths, META).finalize(
            [
                Question(id=2, type="single", stem="补录后的题干", options={"A": "甲"}, answer="A"),
                Question(id=3, type="single", stem="新增题", options={"A": "甲"}, answer="A"),
            ]
        )
        records = self._records()
        self.assertEqual([record["id"] for record in records], [1, 2, 3])
        self.assertEqual(records[1]["stem"], "补录后的题干")

    def test_writer_without_markdown_or_index(self):
        paths = OutputPaths(
            jsonl=self.paths.jsonl,
            markdown=None,
            index=None,
            directory=self.tmp.name,
            base_name="x",
        )
        result = ResultWriter(paths, META).finalize(self.questions)
        self.assertIsNone(result["markdown"])
        self.assertIsNone(result["index"])
        self.assertFalse(os.path.exists(self.paths.markdown))
        self.assertFalse(os.path.exists(self.paths.index))


if __name__ == "__main__":
    unittest.main()
