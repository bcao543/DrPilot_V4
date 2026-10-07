# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import json
import tempfile
import unittest

from drpilot.config import (
    AppConfig,
    build_swipe_plan,
    chapter_slug,
    load_api_keys,
    load_config_file,
    load_config_with_fallback,
    parse_chapter_no,
    parse_size,
    resolve_output_paths,
    sanitize_filename,
    save_config_file,
    strip_chapter_prefix,
)
from drpilot.errors import ConfigError


class PageActionTests(unittest.TestCase):
    """翻页动作的三选一 + 手动点按坐标。"""

    def test_defaults_are_backward_compatible(self):
        cfg = AppConfig()
        self.assertEqual(cfg.page_action, "auto")
        self.assertEqual((cfg.tap_x, cfg.tap_y, cfg.tap_ms), (0, 0, 80))

    def test_unknown_value_falls_back_to_auto(self):
        self.assertEqual(AppConfig.from_dict({"page_action": "乱写"}).page_action, "auto")
        self.assertEqual(AppConfig.from_dict({"page_action": " TAP "}).page_action, "tap")

    def test_tap_coordinates_normalized(self):
        cfg = AppConfig.from_dict({"tap_x": "-5", "tap_y": "12", "tap_ms": "-1"})
        self.assertEqual((cfg.tap_x, cfg.tap_y, cfg.tap_ms), (0, 12, 0))


class OutputFieldConfigTests(unittest.TestCase):
    """配置文件里的本体字段开关。"""

    def test_default_is_all_three(self):
        self.assertEqual([item["key"] for item in AppConfig().output_fields], ["id", "stem", "answer"])
        self.assertEqual(set(AppConfig().output_field_map), {"id", "stem", "answer"})

    def test_empty_list_survives_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "drpilot_config.json")
            save_config_file(path, AppConfig.from_dict({"output_fields": []}))
            loaded = AppConfig.from_dict(load_config_file(path))
            self.assertEqual(loaded.output_fields, [])

    def test_missing_key_uses_default(self):
        self.assertEqual(
            [item["key"] for item in AppConfig.from_dict({}).output_fields],
            ["id", "stem", "answer"],
        )


class DefaultModelMigrationTests(unittest.TestCase):
    """老配置里从没改过的默认模型值，跟着新版默认走。"""

    def test_legacy_defaults_are_migrated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "drpilot_config.json")
            save_config_file(path, AppConfig(model="Qwen/Qwen3.5-4B",
                                             base_url="https://api.siliconflow.cn/v1"))
            config, used = load_config_with_fallback(path)
            self.assertEqual(used, path)
            self.assertEqual(config.model, "deepseek-flash")
            self.assertEqual(config.base_url, "https://api.deepseek.com")

    def test_user_chosen_model_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "drpilot_config.json")
            save_config_file(path, AppConfig(model="my-vision-model",
                                             base_url="https://api.example.com/v1"))
            config, _ = load_config_with_fallback(path)
            self.assertEqual(config.model, "my-vision-model")
            self.assertEqual(config.base_url, "https://api.example.com/v1")


class ConfigTests(unittest.TestCase):
    def test_normalize(self):
        cfg = AppConfig.from_dict(
            {"page_from": "3", "page_to": "9", "batch_size": "0", "workers": -1, "base_url": "https://x/v1/"}
        )
        self.assertEqual(cfg.page_from, 3)
        self.assertEqual(cfg.batch_size, 1)
        self.assertEqual(cfg.workers, 1)
        self.assertEqual(cfg.base_url, "https://x/v1")
        self.assertEqual(cfg.total, 7)

    def test_unknown_keys_ignored(self):
        cfg = AppConfig.from_dict({"nope": 1, "page_from": 2, "page_to": 5})
        self.assertEqual(cfg.page_from, 2)

    def test_device_and_model_check_fields(self):
        cfg = AppConfig.from_dict({"device_address": " 192.168.1.5:5555 ", "check_model": False})
        self.assertEqual(cfg.device_address, "192.168.1.5:5555")
        self.assertFalse(cfg.check_model)
        self.assertEqual(AppConfig().device_address, "")
        self.assertTrue(AppConfig().check_model)

    def test_effective_request_deadline(self):
        cfg = AppConfig(request_timeout=60.0)
        self.assertEqual(cfg.effective_request_deadline, 130.0)   # 60*2+10
        cfg = AppConfig(request_timeout=30.0, request_deadline=200.0)
        self.assertEqual(cfg.effective_request_deadline, 200.0)   # 显式指定优先
        cfg = AppConfig.from_dict({"request_deadline": "-5"})
        self.assertEqual(cfg.request_deadline, 0.0)

    def test_to_dict_excludes_keys(self):
        cfg = AppConfig(api_keys=["sk-1"])
        self.assertNotIn("api_keys", cfg.to_dict())
        self.assertIn("api_keys", cfg.to_dict(include_api_keys=True))

    def test_validate_errors(self):
        cfg = AppConfig(page_from=5, page_to=1, output_dir="")
        with self.assertRaises(ConfigError):
            cfg.validate(require_keys=False)

    def test_load_api_keys_order_and_dedupe(self):
        env = {
            "SILICONFLOW_API_KEY_1": "a",
            "SILICONFLOW_API_KEY_2": "b",
            "DRPILOT_API_KEYS": "b, c",
        }
        self.assertEqual(load_api_keys(env), ["b", "c", "a"])

    def test_parse_chapter_no(self):
        self.assertEqual(parse_chapter_no("第二章 药物代谢动力学"), 2)
        self.assertEqual(parse_chapter_no("第12章 抗菌药"), 12)
        self.assertEqual(parse_chapter_no("第十一章 抗菌药"), 11)
        self.assertEqual(parse_chapter_no("药物代谢动力学"), 0)

    def test_strip_chapter_prefix(self):
        self.assertEqual(strip_chapter_prefix("第二章 药物代谢动力学"), "药物代谢动力学")
        self.assertEqual(strip_chapter_prefix("药物代谢动力学"), "药物代谢动力学")

    def test_chapter_slug(self):
        self.assertEqual(chapter_slug("药理学", "第二章 药物代谢动力学"), "药理学_02_药物代谢动力学")
        self.assertEqual(chapter_slug("药理学", "药物代谢动力学", 2), "药理学_02_药物代谢动力学")
        self.assertEqual(chapter_slug("药理学", "药物代谢动力学"), "药理学_药物代谢动力学")

    def test_sanitize_filename(self):
        self.assertEqual(sanitize_filename('a/b:c*?"<>|d'), "a_b_c_d")
        self.assertEqual(sanitize_filename("第二章 药物代谢动力学"), "第二章_药物代谢动力学")
        self.assertEqual(sanitize_filename("   "), "未命名")

    def test_resolve_output_paths_by_chapter(self):
        cfg = AppConfig(
            page_from=1,
            page_to=20,
            textbook="药理学",
            chapter="第二章 药物代谢动力学",
            output_dir="/tmp/out",
        )
        paths = resolve_output_paths(cfg)
        self.assertEqual(os.path.basename(paths.jsonl), "药理学_02_药物代谢动力学.jsonl")
        self.assertTrue(paths.markdown.endswith("药理学_02_药物代谢动力学.md"))
        self.assertEqual(os.path.basename(paths.index), "index.json")

    def test_resolve_output_paths_prefix_override(self):
        cfg = AppConfig(output_prefix=os.path.join("/tmp/x", "ch6"))
        paths = resolve_output_paths(cfg)
        self.assertEqual(paths.jsonl, os.path.join("/tmp/x", "ch6.jsonl"))

    def test_resolve_output_paths_fallback_name(self):
        cfg = AppConfig(page_from=1, page_to=20, output_dir="/tmp/out")
        paths = resolve_output_paths(cfg)
        self.assertEqual(os.path.basename(paths.jsonl), "题目_1-20.jsonl")

    def test_metadata(self):
        cfg = AppConfig(textbook="药理学", chapter="第二章 药物代谢动力学", chapter_total=53)
        meta = cfg.metadata()
        self.assertEqual(meta["chapter_no"], 2)
        self.assertEqual(meta["total"], 53)
        self.assertEqual(meta["textbook"], "药理学")

    def test_parse_size(self):
        self.assertEqual(parse_size("1080x2340"), (1080, 2340))
        self.assertEqual(parse_size("1080*2340"), (1080, 2340))
        self.assertEqual(parse_size("1080×2340"), (1080, 2340))
        self.assertIsNone(parse_size("abc"))
        self.assertIsNone(parse_size(""))

    def test_swipe_plan_without_reference(self):
        cfg = AppConfig(
            swipe_start_x=1060, swipe_start_y=553, swipe_end_x=270, swipe_end_y=551, swipe_duration_ms=150
        )
        plan = build_swipe_plan(cfg, (1080, 2340))
        self.assertEqual((plan.start_x, plan.start_y, plan.end_x, plan.end_y), (1060, 553, 270, 551))
        self.assertFalse(plan.scaled)

    def test_swipe_plan_scales_with_reference(self):
        cfg = AppConfig(
            swipe_start_x=1000,
            swipe_start_y=500,
            swipe_end_x=200,
            swipe_end_y=500,
            swipe_reference_width=1000,
            swipe_reference_height=2000,
        )
        plan = build_swipe_plan(cfg, (500, 1000))
        # 1000 缩放到 500 后正好等于屏宽，会被夹到合法范围 0~499
        self.assertEqual((plan.start_x, plan.start_y, plan.end_x, plan.end_y), (499, 250, 100, 250))
        self.assertTrue(plan.scaled)
        self.assertEqual(plan.reference, (1000, 2000))
        self.assertEqual(plan.actual, (500, 1000))

    def test_swipe_plan_clamps_to_screen(self):
        cfg = AppConfig(
            swipe_start_x=1500,
            swipe_start_y=1500,
            swipe_end_x=-50,
            swipe_end_y=1500,
            swipe_reference_width=1000,
            swipe_reference_height=1000,
        )
        plan = build_swipe_plan(cfg, (100, 100))
        self.assertEqual(plan.start_x, 99)
        self.assertEqual(plan.start_y, 99)
        self.assertEqual(plan.end_x, 0)
        self.assertEqual(plan.end_y, 99)

    def test_next_action_is_normalized(self):
        cfg = AppConfig.from_dict(
            {
                "next_action": [
                    {"kind": "tap", "x": 1, "y": 2},
                    {"kind": "bogus"},
                    {"kind": "wait", "wait_ms": 0},
                    {"kind": "key", "keycode": "rm -rf /"},
                    {"kind": "swipe", "x": 3, "y": 4, "x2": 5, "y2": 6, "duration_ms": 0},
                ]
            }
        )
        self.assertEqual([step["kind"] for step in cfg.next_action], ["tap", "swipe"])
        self.assertEqual(cfg.next_action[1]["duration_ms"], 1)
        self.assertIn("next_action", cfg.to_dict())

    def test_next_action_defaults_to_empty(self):
        self.assertEqual(AppConfig().next_action, [])
        self.assertEqual(AppConfig().replace({}).next_action, [])

    def test_config_file_roundtrip_without_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cfg.json")
            cfg = AppConfig(page_from=10, page_to=12, textbook="药理学", api_keys=["secret"])
            save_config_file(path, cfg)
            with open(path, encoding="utf-8") as handle:
                raw = json.load(handle)
            self.assertNotIn("api_keys", raw)
            loaded = AppConfig.from_dict(load_config_file(path))
            self.assertEqual(loaded.page_from, 10)
            self.assertEqual(loaded.textbook, "药理学")


class DisplayConfigTests(unittest.TestCase):
    """显示设置：加长 / 加宽 / 只改密度都算「要动手机显示」，三者互不依赖。"""

    def test_uses_display_override_for_each_knob(self):
        self.assertFalse(AppConfig().uses_display_override)                     # 默认 off
        self.assertFalse(
            AppConfig(display_mode="wide", display_scale=1.0).uses_display_override  # 什么都没改
        )
        self.assertTrue(
            AppConfig(display_mode="wide", display_scale=2.0).uses_display_override
        )
        self.assertTrue(
            AppConfig(display_mode="wide", display_scale=1.0, display_width_scale=1.3)
            .uses_display_override
        )
        self.assertTrue(
            AppConfig(display_mode="wide", display_scale=1.0, display_density=420)
            .uses_display_override
        )
        # mode=off 时一律不动手机（安全开关优先）
        self.assertFalse(
            AppConfig(display_mode="off", display_scale=2.0, display_width_scale=1.5)
            .uses_display_override
        )

    def test_width_scale_is_clamped(self):
        self.assertEqual(AppConfig(display_width_scale=9.0).normalize().display_width_scale, 2.5)
        self.assertEqual(AppConfig(display_width_scale=0.2).normalize().display_width_scale, 1.0)

    def test_width_only_passes_validation(self):
        config = AppConfig(
            display_mode="wide", display_scale=1.0, display_width_scale=1.3,
            page_from=1, page_to=2, output_dir="/tmp/x",
        )
        config.api_keys = ["sk-test"]
        config.validate()      # 不该抛：只加宽也是有效的显示设置

    def test_nothing_configured_is_rejected_for_wide(self):
        config = AppConfig(
            display_mode="wide", display_scale=1.0, display_width_scale=1.0,
            display_density=0, page_from=1, page_to=2, output_dir="/tmp/x",
        )
        config.api_keys = ["sk-test"]
        with self.assertRaises(ConfigError) as ctx:
            config.validate()
        self.assertIn("显示设置", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
