# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import json
import unittest

from drpilot.errors import ConfigError
from drpilot.modules import (
    PRESETS,
    ModuleSpec,
    build_module_prompt_section,
    default_output_fields,
    has_below_fold,
    match_module_name,
    module_names,
    modules_summary,
    modules_to_payload,
    normalize_modules,
    normalize_output_fields,
    output_field_enabled,
    output_fields_summary,
    parse_modules_text,
    preset_for,
    split_module_names,
)
from drpilot.prompts import SYSTEM_PROMPT, build_system_prompt


class SplitTests(unittest.TestCase):
    def test_splits_on_every_common_separator(self):
        text = "考点还原,标准解析、总结分析;技巧点拨|难度，统计、来源"
        self.assertEqual(
            split_module_names(text),
            ["考点还原", "标准解析", "总结分析", "技巧点拨", "难度", "统计", "来源"],
        )

    def test_empty(self):
        self.assertEqual(split_module_names(""), [])
        self.assertEqual(split_module_names(None), [])


class PresetTests(unittest.TestCase):
    def test_preset_lookup_by_name_and_alias(self):
        self.assertIsNotNone(preset_for("考点还原"))
        self.assertIsNotNone(preset_for("考点"))
        self.assertIsNone(preset_for("不存在的模块"))

    def test_parse_text_inherits_preset_metadata(self):
        specs = parse_modules_text("考点还原,标准解析")
        self.assertEqual(module_names(specs), ["考点还原", "标准解析"])
        self.assertTrue(all(spec.below_fold for spec in specs))
        self.assertIn("考点", specs[0].aliases)
        self.assertTrue(specs[0].hint)

    def test_parse_alias_keeps_user_name(self):
        spec = parse_modules_text("解析")[0]
        self.assertEqual(spec.name, "解析")
        self.assertIn("标准解析", spec.aliases)

    def test_unknown_module_defaults_to_below_fold(self):
        spec = parse_modules_text("我的自定义模块")[0]
        self.assertTrue(spec.below_fold)
        self.assertEqual(spec.aliases, [])

    def test_presets_unique(self):
        names = [spec.name for spec in PRESETS]
        self.assertEqual(len(names), len(set(names)))
        for spec in PRESETS:
            self.assertTrue(spec.name)
            self.assertTrue(spec.hint)


class NormalizeTests(unittest.TestCase):
    def test_accepts_payload_dicts(self):
        raw = [spec.to_dict() for spec in parse_modules_text("考点还原")]
        specs = normalize_modules(raw)
        self.assertEqual(module_names(specs), ["考点还原"])

    def test_accepts_plain_string_and_mapping_wrapper(self):
        self.assertEqual(module_names(normalize_modules("考点还原")), ["考点还原"])
        self.assertEqual(
            module_names(normalize_modules({"modules": ["标准解析"]})), ["标准解析"]
        )

    def test_drops_duplicates_and_empty_in_lenient_mode(self):
        specs = normalize_modules(["考点还原", "考点还原", "", "  "])
        self.assertEqual(module_names(specs), ["考点还原"])

    def test_strict_mode_raises_on_duplicate(self):
        with self.assertRaises(ConfigError):
            normalize_modules(["考点还原", "考点"], strict=True)

    def test_strict_mode_raises_on_reserved_name(self):
        with self.assertRaises(ConfigError):
            normalize_modules(["answer"], strict=True)

    def test_lenient_mode_skips_reserved_name(self):
        self.assertEqual(module_names(normalize_modules(["answer", "考点还原"])), ["考点还原"])

    def test_limit(self):
        specs = normalize_modules([f"模块{index}" for index in range(30)])
        self.assertEqual(len(specs), 12)

    def test_round_trip_payload(self):
        specs = parse_modules_text("考点还原,难度")
        again = normalize_modules(modules_to_payload(specs))
        self.assertEqual([s.to_dict() for s in again], [s.to_dict() for s in specs])


class MatchTests(unittest.TestCase):
    def test_matches_name_and_alias(self):
        specs = parse_modules_text("考点还原")
        self.assertEqual(match_module_name(specs, "考点还原"), "考点还原")
        self.assertEqual(match_module_name(specs, "考点"), "考点还原")
        self.assertIsNone(match_module_name(specs, "标准解析"))

    def test_ascii_case_insensitive(self):
        specs = normalize_modules([ModuleSpec(name="explanation")])
        self.assertEqual(match_module_name(specs, "Explanation"), "explanation")


class PromptTests(unittest.TestCase):
    def test_prompt_unchanged_without_modules(self):
        self.assertEqual(build_system_prompt(), SYSTEM_PROMPT)
        self.assertEqual(build_system_prompt([]), SYSTEM_PROMPT)

    def test_prompt_lists_modules_and_aliases(self):
        prompt = build_system_prompt(parse_modules_text("考点还原"))
        self.assertIn("考点还原", prompt)
        self.assertIn("考点", prompt)
        self.assertIn("严禁编造", prompt)
        self.assertTrue(prompt.startswith(SYSTEM_PROMPT.split("只输出纯 JSON")[0].strip()[:20]))

    def test_prompt_section_empty_without_modules(self):
        self.assertEqual(build_module_prompt_section([]), "")

    def test_summary_and_below_fold(self):
        specs = parse_modules_text("考点还原,难度")
        self.assertTrue(has_below_fold(specs))
        self.assertIn("考点还原", modules_summary(specs))
        self.assertIn("未配置", modules_summary([]))

    def test_json_serializable(self):
        payload = modules_to_payload(parse_modules_text("考点还原"))
        self.assertEqual(json.loads(json.dumps(payload))[0]["name"], "考点还原")


class OutputFieldTests(unittest.TestCase):
    """题目本体字段（题号 / 题目 / 答案）的开关模型。"""

    def test_default_is_all_three(self):
        fields = default_output_fields()
        self.assertEqual([item["key"] for item in fields], ["id", "stem", "answer"])
        self.assertTrue(all(item["in_json"] and item["in_markdown"] for item in fields))

    def test_none_and_blank_fall_back_to_default(self):
        self.assertEqual(normalize_output_fields(None), default_output_fields())
        self.assertEqual(normalize_output_fields(""), default_output_fields())

    def test_explicit_empty_list_is_kept(self):
        self.assertEqual(normalize_output_fields([]), [])

    def test_accepts_chinese_names_and_json_string(self):
        text = json.dumps([{"name": "答案", "json": False, "markdown": True}])
        fields = normalize_output_fields(text)
        self.assertEqual([item["key"] for item in fields], ["answer"])
        self.assertFalse(fields[0]["in_json"])
        self.assertTrue(fields[0]["in_markdown"])

    def test_order_is_canonical_and_duplicates_dropped(self):
        fields = normalize_output_fields(["answer", "id", "answer", "stem"])
        self.assertEqual([item["key"] for item in fields], ["id", "stem", "answer"])

    def test_unknown_keys_dropped(self):
        self.assertEqual(normalize_output_fields(["modules", "id"]), [
            {"key": "id", "in_json": True, "in_markdown": True}
        ])

    def test_enabled_helper(self):
        fields = normalize_output_fields([{"key": "id", "in_markdown": False}])
        self.assertTrue(output_field_enabled(fields, "id", "json"))
        self.assertFalse(output_field_enabled(fields, "id", "markdown"))
        self.assertFalse(output_field_enabled(fields, "stem", "json"))    # 没保留 = 不写

    def test_summary_text(self):
        text = output_fields_summary(default_output_fields())
        self.assertIn("题号→JSONL/Markdown", text)
        self.assertIn("答案→JSONL/Markdown", text)
        self.assertIn("全部不写入", output_fields_summary([]))

    def test_module_spec_in_json_flag_roundtrip(self):
        spec = ModuleSpec(name="考点还原", in_json=False)
        again = normalize_modules([spec.to_dict()])[0]
        self.assertFalse(again.in_json)
        self.assertTrue(again.in_markdown)


if __name__ == "__main__":
    unittest.main()
