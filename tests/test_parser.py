# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import json
import unittest

from drpilot.errors import ParseError
from drpilot.parser import (
    parse_ai_response,
    parse_questions,
    parse_screen_info,
    strip_code_fences,
)

SAMPLE = [
    {"id": 47, "type": "single", "stem": "题干一", "options": {"A": "甲", "B": "乙"}, "answer": "A"},
    {"id": 48, "type": "multi", "stem": "题干二", "options": ["甲", "乙", "丙"], "answer": "b, c"},
]


class ParserTests(unittest.TestCase):
    def test_clean_array(self):
        questions = parse_questions(json.dumps(SAMPLE, ensure_ascii=False), [47, 48])
        self.assertEqual([q.id for q in questions], [47, 48])
        self.assertEqual(questions[1].type, "multi")
        self.assertEqual(questions[1].options, {"A": "甲", "B": "乙", "C": "丙"})
        self.assertEqual(questions[1].answer, "BC")

    def test_code_fence(self):
        text = "```json\n" + json.dumps(SAMPLE, ensure_ascii=False) + "\n```"
        questions = parse_questions(text, [47, 48])
        self.assertEqual([q.id for q in questions], [47, 48])

    def test_prose_wrapped(self):
        text = "好的，识别结果如下：\n" + json.dumps(SAMPLE, ensure_ascii=False) + "\n以上。"
        questions = parse_questions(text, [47, 48])
        self.assertEqual([q.id for q in questions], [47, 48])

    def test_wrapper_object(self):
        text = json.dumps({"questions": SAMPLE}, ensure_ascii=False)
        questions = parse_questions(text, [47, 48])
        self.assertEqual(len(questions), 2)

    def test_jsonl(self):
        text = "\n".join(json.dumps(item, ensure_ascii=False) for item in SAMPLE)
        questions = parse_questions(text, [47, 48])
        self.assertEqual([q.id for q in questions], [47, 48])

    def test_null_alignment(self):
        raw = [SAMPLE[0], None]
        questions = parse_questions(json.dumps(raw, ensure_ascii=False), [47, 48])
        self.assertEqual(len(questions), 2)
        self.assertTrue(questions[1].is_placeholder)
        self.assertEqual(questions[1].id, 48)

    def test_missing_items_padded(self):
        text = json.dumps([SAMPLE[0]], ensure_ascii=False)
        questions = parse_questions(text, [47, 48, 49])
        self.assertEqual(len(questions), 3)
        self.assertEqual([q.id for q in questions], [47, 48, 49])
        self.assertTrue(questions[1].is_placeholder)

    def test_extra_items_truncated(self):
        text = json.dumps(SAMPLE * 2, ensure_ascii=False)
        questions = parse_questions(text, [47, 48])
        self.assertEqual(len(questions), 2)

    def test_parse_failure_raises(self):
        with self.assertRaises(ParseError):
            parse_ai_response("完全不是 JSON 的回复")

    def test_strip_fence_picks_longest(self):
        text = "```\n[]\n```\n```json\n[1,2,3]\n```"
        self.assertEqual(strip_code_fences(text), "[1,2,3]")


class ScreenInfoTests(unittest.TestCase):
    def test_json_object(self):
        self.assertEqual(
            parse_screen_info('{"screen_id": 16, "screen_total": 53}'),
            (16, 53),
        )

    def test_fenced_json(self):
        text = '```json\n{"screen_id": 7, "screen_total": 20}\n```'
        self.assertEqual(parse_screen_info(text), (7, 20))

    def test_fraction_fallback(self):
        self.assertEqual(parse_screen_info("第 16/53 题"), (16, 53))

    def test_bare_integer(self):
        self.assertEqual(parse_screen_info("16"), (16, None))

    def test_garbage(self):
        self.assertEqual(parse_screen_info("看不清"), (None, None))

    def test_screen_fields_parsed_in_questions(self):
        text = json.dumps(
            [
                {
                    "screen_id": 12,
                    "screen_total": 53,
                    "type": "single",
                    "stem": "题干",
                    "options": {"A": "甲"},
                    "answer": "A",
                }
            ],
            ensure_ascii=False,
        )
        questions = parse_questions(text, [1])
        self.assertEqual(questions[0].screen_id, 12)
        self.assertEqual(questions[0].screen_total, 53)


if __name__ == "__main__":
    unittest.main()
