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


class MergeProtectionTests(unittest.TestCase):
    """补录时「本次没认出来」不能把上一次的好内容冲成 [识别失败]。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = OutputPaths(
            jsonl=os.path.join(self.tmp.name, "书_09_球菌.jsonl"),
            markdown=os.path.join(self.tmp.name, "书_09_球菌.md"),
            index=os.path.join(self.tmp.name, "index.json"),
            directory=self.tmp.name,
            base_name="书_09_球菌",
        )
        self.logs: list[str] = []

    def _writer(self) -> ResultWriter:
        return ResultWriter(self.paths, META, log=self.logs.append)

    def _records(self):
        with open(self.paths.jsonl, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def test_placeholder_keeps_existing_good_record(self):
        good = Question(id=14, type="single", stem="真正的题干", options={"A": "甲"}, answer="A")
        self._writer().finalize([good, Question(id=15, stem="另一题", answer="B")])

        writer = self._writer()          # 第二次运行：第 14 题识别失败
        writer.finalize([Question.placeholder(14), Question(id=15, stem="新内容", answer="C")])
        records = {record["id"]: record for record in self._records()}
        self.assertEqual(records[14]["stem"], "真正的题干")     # 保住了
        self.assertEqual(records[15]["stem"], "新内容")        # 正常覆盖
        self.assertTrue(any("保留" in line for line in self.logs))

    def test_placeholder_overwrites_previous_placeholder(self):
        self._writer().finalize([Question.placeholder(7)])
        self._writer().finalize([Question.placeholder(7, reason="换了个错因")])
        records = self._records()
        self.assertEqual(len(records), 1)
        self.assertIn("换了个错因", records[0]["stem"])

    def test_good_record_replaces_placeholder(self):
        self._writer().finalize([Question.placeholder(7)])
        self._writer().finalize([Question(id=7, stem="补录成功", answer="A")])
        records = self._records()
        self.assertEqual(records[0]["stem"], "补录成功")


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


class OutputFieldTests(unittest.TestCase):
    """本体字段开关：JSONL 少写键、Markdown 少渲染段落，且不丢已有内容。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = OutputPaths(
            jsonl=os.path.join(self.tmp.name, "题库.jsonl"),
            markdown=os.path.join(self.tmp.name, "题库.md"),
            index=os.path.join(self.tmp.name, "index.json"),
            directory=self.tmp.name,
            base_name="题库",
        )
        self.questions = [
            Question(id=1, type="single", stem="题干 A", options={"A": "甲"}, answer="A",
                     modules={"考点还原": "正文"}),
        ]

    def _records(self):
        with open(self.paths.jsonl, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def test_json_and_markdown_switches_are_independent(self):
        fields = [
            {"key": "id", "in_json": True, "in_markdown": False},
            {"key": "stem", "in_json": False, "in_markdown": True},
            {"key": "answer", "in_json": True, "in_markdown": False},
        ]
        writer = ResultWriter(self.paths, META, output_fields=fields)
        writer.finalize(self.questions)
        record = self._records()[0]
        self.assertIn("id", record)
        self.assertNotIn("stem", record)
        self.assertNotIn("options", record)
        self.assertIn("answer", record)
        with open(self.paths.markdown, encoding="utf-8") as handle:
            markdown = handle.read()
        self.assertNotIn("第 1 题", markdown)      # 题号关掉了
        self.assertIn("题干 A", markdown)
        self.assertNotIn("答案：", markdown)       # 答案只进 JSONL

    def test_markdown_keeps_heading_when_only_stem_is_dropped(self):
        fields = [
            {"key": "id", "in_json": True, "in_markdown": True},
            {"key": "answer", "in_json": True, "in_markdown": True},
        ]
        markdown = render_markdown(self.questions, output_fields=fields)
        self.assertIn("## 第 1 题 【单选题】", markdown)
        self.assertNotIn("题干 A", markdown)
        self.assertIn("### **答案：A**", markdown)

    def test_all_fields_off_still_writes_metadata(self):
        writer = ResultWriter(self.paths, META, output_fields=[])
        writer.finalize(self.questions)
        record = self._records()[0]
        self.assertEqual(record["textbook"], "药理学")
        self.assertNotIn("id", record)
        self.assertNotIn("answer", record)

    def test_existing_lines_without_id_are_kept(self):
        """关掉题号后再跑同一章节：旧内容原样保留，新结果追加在后面，绝不冲掉。"""
        with open(self.paths.jsonl, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"textbook": "药理学", "stem": "上一次的题"}, ensure_ascii=False) + "\n")
        writer = ResultWriter(self.paths, META, output_fields=[])
        writer.finalize(self.questions)
        records = self._records()
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["stem"], "上一次的题")
        self.assertEqual(records[1]["textbook"], "药理学")

    def test_module_in_json_off_only_skips_jsonl(self):
        from drpilot.modules import parse_modules_text

        specs = parse_modules_text("考点还原")
        specs[0].in_json = False
        writer = ResultWriter(self.paths, META, modules=specs)
        writer.finalize(self.questions)
        self.assertNotIn("modules", self._records()[0])
        with open(self.paths.markdown, encoding="utf-8") as handle:
            self.assertIn("### **考点还原**", handle.read())


if __name__ == "__main__":
    unittest.main()
