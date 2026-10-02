# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import unittest

from drpilot import interactive


def scripted(answers):
    """把输入答案变成可注入的 ask 函数；用完后抛 EOFError。"""
    queue = list(answers)

    def ask(prompt: str) -> str:
        if not queue:
            raise EOFError
        return queue.pop(0)

    return ask


class InteractiveTests(unittest.TestCase):
    def setUp(self):
        self.said: list[str] = []

    def say(self, message: str) -> None:
        self.said.append(str(message))

    def test_collect_fills_required_answers(self):
        answers = scripted(["47", "81", "D:\\题库", "", "", ""])
        result = interactive.collect(
            ["page_from", "page_to", "output_dir", "textbook", "chapter", "device_address"],
            ask=answers,
            say=self.say,
        )
        self.assertEqual(result["page_from"], 47)
        self.assertEqual(result["page_to"], 81)
        self.assertEqual(result["output_dir"], "D:\\题库")
        self.assertNotIn("textbook", result)

    def test_invalid_number_is_reasked(self):
        answers = scripted(["abc", "0", "47"])
        result = interactive.collect(["page_from"], ask=answers, say=self.say)
        self.assertEqual(result, {"page_from": 47})
        self.assertTrue(any("请输入数字" in line for line in self.said))
        self.assertTrue(any(">= 1" in line for line in self.said))

    def test_too_many_invalid_answers_cancel(self):
        answers = scripted(["x", "y", "z"])
        self.assertIsNone(interactive.collect(["page_from"], ask=answers, say=self.say))
        self.assertTrue(any("多次输入无效" in line for line in self.said))

    def test_skipping_required_answer_cancels(self):
        answers = scripted([""])
        self.assertIsNone(interactive.collect(["page_from"], ask=answers, say=self.say))
        self.assertTrue(any("必填" in line for line in self.said))

    def test_known_values_are_not_asked_again(self):
        answers = scripted(["81", "D:\\题库"])
        result = interactive.collect(
            ["page_from", "page_to", "output_dir"],
            ask=answers,
            say=self.say,
            initial={"page_from": 47},
        )
        self.assertEqual(result["page_to"], 81)
        self.assertEqual(result["output_dir"], "D:\\题库")
        self.assertNotIn("page_from", result)

    def test_eof_cancels(self):
        answers = scripted([])
        self.assertIsNone(interactive.collect(["page_from"], ask=answers, say=self.say))
        self.assertTrue(any("取消" in line for line in self.said))

    def test_confirm_defaults_and_answers(self):
        self.assertTrue(interactive.confirm("开始？", ask=lambda prompt: "", say=self.say))
        self.assertTrue(interactive.confirm("开始？", ask=lambda prompt: "y", say=self.say))
        self.assertFalse(interactive.confirm("开始？", ask=lambda prompt: "n", say=self.say))
        self.assertFalse(
            interactive.confirm("开始？", ask=lambda prompt: "", say=self.say, default=False)
        )

    def test_prompts_for_keeps_order(self):
        dests = [spec["dest"] for spec in interactive.prompts_for(["output_dir", "page_from"])]
        self.assertEqual(dests, ["page_from", "output_dir"])


if __name__ == "__main__":
    unittest.main()
