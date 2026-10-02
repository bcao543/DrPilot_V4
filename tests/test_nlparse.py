# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import unittest

from drpilot.nlparse import describe_params, parse_request


class NaturalLanguageTests(unittest.TestCase):
    def parse(self, text):
        return parse_request(text)["params"]

    def test_range_and_output(self):
        params = self.parse("把这章 47 到 81 题提取出来，存到 D:\\题库")
        self.assertEqual(params["page_from"], 47)
        self.assertEqual(params["page_to"], 81)
        self.assertEqual(params["output_dir"], "D:\\题库")

    def test_dash_range_and_quoted_book(self):
        params = self.parse("《诊断学》第三章 胸部检查 5-9 题，保存到 E:\\题")
        self.assertEqual((params["page_from"], params["page_to"]), (5, 9))
        self.assertEqual(params["textbook"], "诊断学")
        self.assertEqual(params["chapter"], "第三章 胸部检查")

    def test_single_question_is_a_one_question_range(self):
        params = self.parse("补录第二章 药物代谢动力学 第 17 题")
        self.assertEqual(params["page_from"], 17)
        self.assertEqual(params["page_to"], 17)
        self.assertEqual(params["chapter"], "第二章 药物代谢动力学")

    def test_book_prefix_before_chapter(self):
        params = self.parse("药理学 第二章 药物代谢动力学 1到20题 输出到桌面题库")
        self.assertEqual(params["textbook"], "药理学")
        self.assertEqual(params["chapter"], "第二章 药物代谢动力学")
        self.assertEqual(params["output_dir"], "桌面题库")

    def test_range_after_chapter_is_not_part_of_chapter_name(self):
        params = self.parse("药理学第二章 1 到 20 题，输出到桌面题库")
        self.assertEqual(params["chapter"], "第二章")
        self.assertEqual(params["page_from"], 1)
        self.assertEqual(params["page_to"], 20)

    def test_device_address(self):
        params = self.parse("连一下手机 192.168.1.5:5555")
        self.assertEqual(params["device_address"], "192.168.1.5:5555")

    def test_dry_run_intent(self):
        params = self.parse("先别跑，看看配置对不对 47-81 D:\\题库")
        self.assertTrue(params["dry_run"])
        self.assertEqual(params["page_from"], 47)

    def test_posix_path_and_force_intent(self):
        params = self.parse("帮我从第 100 题到第 120 题提取，存到 /data/bank，强制继续")
        self.assertEqual((params["page_from"], params["page_to"]), (100, 120))
        self.assertEqual(params["output_dir"], "/data/bank")
        self.assertTrue(params["force_start"])

    def test_empty_and_garbage_input_is_silent(self):
        self.assertEqual(self.parse(""), {})
        self.assertEqual(self.parse("帮我弄一下"), {})

    def test_describe_params_is_human_readable(self):
        text = describe_params({"page_from": 47, "page_to": 81, "output_dir": "D:\\题库"})
        self.assertIn("起始题号 47", text)
        self.assertIn("输出目录 D:\\题库", text)
        self.assertEqual(describe_params({}), "没有解析出任何参数")


if __name__ == "__main__":
    unittest.main()
