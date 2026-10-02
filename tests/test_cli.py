# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import contextlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from drpilot import exitcodes
from drpilot.cli import _build_config, _expand_subcommand, build_parser, main
from drpilot.config import AppConfig
from drpilot.doctor import CheckResult, DoctorReport
from drpilot.errors import AdbError, ConfigError

# 单元测试自带的假 Key：别人 git clone 下来没有 .env，也必须能跑全绿
TEST_KEY_ENV = "SILICONFLOW_API_KEY_1"
TEST_KEY_VALUE = "sk-unit-test-only-000000000001"


class KeyEnvMixin:
    """给需要真跑一遍的用例塞一个假 Key（用完恢复原值）。

    没有它，测试就会偷偷依赖开发者本机的 .env：新克隆的仓库里没有 Key，
    dry-run 会以「缺 API Key」（退出码 3）失败。
    """

    def setUp(self):
        super().setUp()
        self._key_before = os.environ.get(TEST_KEY_ENV)
        os.environ[TEST_KEY_ENV] = TEST_KEY_VALUE
        self.addCleanup(self._restore_key_env)

    def _restore_key_env(self):
        if self._key_before is None:
            os.environ.pop(TEST_KEY_ENV, None)
        else:
            os.environ[TEST_KEY_ENV] = self._key_before


class CliParserTests(unittest.TestCase):
    def _overrides(self, argv):
        args = build_parser().parse_args(argv)
        return {
            key: value
            for key, value in vars(args).items()
            if value is not None and key in AppConfig.field_names()
        }

    def test_output_maps_to_output_prefix(self):
        overrides = self._overrides(
            ["--from", "47", "--to", "81", "--output", "ch6", "--no-md", "--batch-size", "5", "--serial", "ABC"]
        )
        self.assertEqual(overrides["output_prefix"], "ch6")
        self.assertEqual(overrides["page_from"], 47)
        self.assertEqual(overrides["page_to"], 81)
        self.assertFalse(overrides["generate_markdown"])
        self.assertEqual(overrides["batch_size"], 5)
        self.assertEqual(overrides["serial"], "ABC")
        self.assertNotIn("gui", overrides)

    def test_output_dir_and_wait_ms(self):
        args = build_parser().parse_args(
            ["--from", "47", "--to", "81", "--output-dir", "/tmp/out", "--wait-ms", "800"]
        )
        self.assertEqual(args.output_dir, "/tmp/out")
        self.assertEqual(args.wait_ms, 800)
        self.assertIsNone(args.output_prefix)
        self.assertIsNone(args.wait)

    def test_chapter_options(self):
        overrides = self._overrides(
            [
                "--from", "1", "--to", "20",
                "--output-dir", "/tmp/out",
                "--textbook", "药理学",
                "--chapter", "第二章 药物代谢动力学",
                "--chapter-no", "2",
                "--chapter-total", "53",
                "--no-index",
                "--no-preflight",
            ]
        )
        self.assertEqual(overrides["textbook"], "药理学")
        self.assertEqual(overrides["chapter"], "第二章 药物代谢动力学")
        self.assertEqual(overrides["chapter_no"], 2)
        self.assertEqual(overrides["chapter_total"], 53)
        self.assertFalse(overrides["generate_index"])
        self.assertFalse(overrides["preflight"])

    def test_swipe_reference_option(self):
        args = build_parser().parse_args(
            ["--from", "1", "--to", "2", "--output-dir", "/tmp/x", "--swipe-reference", "1440x3200"]
        )
        self.assertEqual(args.swipe_reference, "1440x3200")

    def test_output_and_output_dir_conflict(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args(
                ["--from", "1", "--to", "2", "--output", "x", "--output-dir", "y"]
            )

    def test_wait_and_wait_ms_conflict(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["--wait", "1", "--wait-ms", "500"])

    def test_connect_and_model_check_flags(self):
        overrides = self._overrides(
            [
                "--from", "1", "--to", "2", "--output-dir", "/tmp/x",
                "--connect", "192.168.1.5:5555",
                "--no-model-check",
            ]
        )
        self.assertEqual(overrides["device_address"], "192.168.1.5:5555")
        self.assertFalse(overrides["check_model"])

    def test_check_model_only_flag_is_not_a_config_field(self):
        args = build_parser().parse_args(["--check-model"])
        self.assertTrue(args.check_model_only)
        self.assertNotIn("check_model_only", AppConfig.field_names())

    def test_next_action_file_and_tap(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "action.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "reference": "1000x2000",
                        "steps": [
                            {"kind": "swipe", "x": 1, "y": 2, "x2": 3, "y2": 4, "duration_ms": 100}
                        ],
                    },
                    handle,
                )
            parser = build_parser()
            args = parser.parse_args(
                [
                    "--from", "1", "--to", "2",
                    "--output-dir", "/tmp/x",
                    "--config", os.path.join(tmp, "none.json"),
                    "--next-action-file", path,
                    "--tap", "12,34",
                ]
            )
            config, _ = _build_config(args, parser)
            self.assertEqual([step["kind"] for step in config.next_action], ["swipe", "tap"])
            self.assertEqual((config.next_action[1]["x"], config.next_action[1]["y"]), (12, 34))
            self.assertTrue(config.next_action[1]["absolute"])   # --tap 是当前设备像素
            self.assertEqual(
                (config.swipe_reference_width, config.swipe_reference_height), (1000, 2000)
            )

    def test_tap_option_requires_xy(self):
        parser = build_parser()
        args = parser.parse_args(
            [
                "--from", "1", "--to", "2", "--output-dir", "/tmp/x",
                "--config", "/tmp/drpilot-none.json", "--tap", "abc",
            ]
        )
        with self.assertRaises(SystemExit):
            _build_config(args, parser)

    def test_defaults_are_none_so_config_file_wins(self):
        overrides = self._overrides(["--from", "1", "--to", "2", "--output", "x"])
        self.assertNotIn("generate_markdown", overrides)


class CliSubcommandTests(KeyEnvMixin, unittest.TestCase):
    """子命令展开与 Agent 友好输出（--json / --dry-run / 退出码）。"""

    def test_subcommand_expands_to_flat_flags(self):
        self.assertEqual(_expand_subcommand(["doctor", "--json"]), ["--doctor", "--json"])
        self.assertEqual(_expand_subcommand(["devices"]), ["--devices"])
        self.assertEqual(
            _expand_subcommand(["check-model", "--config", "a.json"]),
            ["--check-model", "--config", "a.json"],
        )

    def test_subcommand_expansion_leaves_normal_calls_alone(self):
        argv = ["--from", "47", "--to", "81", "--output-dir", "/tmp/x"]
        self.assertEqual(_expand_subcommand(argv), argv)
        # 第一个位置参数不是子命令名时不能误判（例如 --config 后面跟 doctor.json）
        argv2 = ["--config", "doctor.json", "--from", "1", "--to", "2", "--output-dir", "/tmp/x"]
        self.assertEqual(_expand_subcommand(argv2), argv2)

    def test_unknown_subcommand_argument_raises_config_error(self):
        with self.assertRaises(ConfigError):
            _expand_subcommand(["doctor", "--from", "1"])

    def test_config_flag_without_value_raises(self):
        with self.assertRaises(ConfigError):
            _expand_subcommand(["check-model", "--config"])

    def _capture(self, argv, log_file=None):
        """跑一次 main()；默认不写运行日志，避免单元测试在仓库里留 logs/。"""
        argv = list(argv)
        if log_file:
            argv += ["--log-file", log_file]
        elif "--no-log-file" not in argv:
            argv.append("--no-log-file")
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_missing_args_json_error_is_structured(self):
        code, stdout, stderr = self._capture(["--json"])
        self.assertEqual(code, exitcodes.CONFIG_INVALID)
        payload = json.loads(stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], "missing_required_arguments")
        self.assertEqual(payload["missing"], ["--from", "--to", "--output-dir"])
        self.assertEqual(payload["phase"], "args")
        self.assertEqual(payload["exit_code_name"], "config_invalid")
        # Agent 拿到 questions 就能直接去问用户，不用解析中文报错
        self.assertEqual(len(payload["questions"]), 3)
        self.assertIn("--from", payload["hint"])
        self.assertIn("示例", stderr)

    def test_dry_run_json_reports_planned_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, stdout, _ = self._capture(
                [
                    "--from", "47", "--to", "81", "--output-dir", tmp,
                    "--textbook", "药理学", "--chapter", "第二章 药物代谢动力学",
                    "--dry-run", "--json",
                ]
            )
        self.assertEqual(code, exitcodes.SUCCESS)
        payload = json.loads(stdout)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["total"], 35)
        # 显式 --chapter 时章号必须重新解析（不能被配置文件里残留的 chapter_no 带偏）
        self.assertTrue(payload["jsonl"].endswith("药理学_02_药物代谢动力学.jsonl"), payload["jsonl"])
        self.assertEqual(payload["exit_code"], exitcodes.SUCCESS)

    def test_doctor_json_is_valid_and_never_leaks_keys(self):
        fake = CheckResult("dependencies", "ok", "依赖齐全")
        with mock.patch("drpilot.doctor._dependencies_check", return_value=fake), \
             mock.patch("drpilot.doctor.load_api_keys", return_value=["sk-secretsecretsecret"]), \
             mock.patch("drpilot.doctor.default_adb_probe", return_value=("ok", "设备在线", "")), \
             mock.patch("drpilot.doctor.default_model_probe", return_value=("ok", "模型正常", "")):
            code, stdout, _ = self._capture(["doctor", "--json"])
        payload = json.loads(stdout)
        self.assertEqual(code, payload["exit_code"])
        self.assertNotIn("sk-secretsecretsecret", stdout)
        self.assertIn("checks", payload)

    def test_doctor_full_flag_is_accepted(self):
        report = DoctorReport(checks=[], full=True)
        with mock.patch("drpilot.doctor.run_doctor", return_value=report) as runner:
            code, _, _ = self._capture(["doctor", "--full", "--json"])
        self.assertEqual(code, exitcodes.SUCCESS)
        self.assertTrue(runner.call_args.kwargs["full"])

    def test_json_and_dry_run_flags_are_not_config_fields(self):
        args = build_parser().parse_args(["--json", "--dry-run"])
        self.assertTrue(args.json_output)
        self.assertTrue(args.dry_run)
        self.assertNotIn("json_output", AppConfig.field_names())
        self.assertNotIn("dry_run", AppConfig.field_names())

    def test_argparse_error_still_returns_json(self):
        """拼错参数也要给 Agent 一份 JSON，而不是只有一段 usage。"""
        code, stdout, stderr = self._capture(["--json", "--wait", "1", "--wait-ms", "500"])
        self.assertEqual(code, exitcodes.CONFIG_INVALID)
        payload = json.loads(stdout)
        self.assertEqual(payload["error_code"], "invalid_arguments")
        self.assertEqual(payload["phase"], "args")
        self.assertIn("usage", stderr)

    def test_log_file_is_written_and_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = os.path.join(tmp, "run.log")
            code, stdout, _ = self._capture(
                [
                    "--from", "47", "--to", "81", "--output-dir", tmp,
                    "--textbook", "药理学", "--chapter", "第二章 药物代谢动力学",
                    "--dry-run", "--json",
                ],
                log_file=log_path,
            )
            payload = json.loads(stdout)
            self.assertEqual(code, exitcodes.SUCCESS)
            self.assertEqual(payload["log_file"], log_path)
            self.assertTrue(os.path.isfile(log_path))
            body = Path(log_path).read_text(encoding="utf-8")
            self.assertIn("dry-run", body)
            # 日志每行都带时间戳、级别、阶段，Agent 能按阶段定位
            self.assertRegex(body, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} \| INFO")

    def test_keys_never_reach_the_log_file(self):
        secret = "sk-supersecret1234567890"
        with tempfile.TemporaryDirectory() as tmp:
            log_path = os.path.join(tmp, "run.log")
            self._capture(
                [
                    "--from", "1", "--to", "2", "--output-dir", tmp,
                    "--model", secret, "--dry-run", "--json",
                ],
                log_file=log_path,
            )
            body = Path(log_path).read_text(encoding="utf-8")
            self.assertNotIn(secret, body)
            self.assertIn("****", body)

    def test_no_log_file_reports_nothing(self):
        code, stdout, _ = self._capture(
            ["--from", "1", "--to", "2", "--output-dir", "/tmp/x", "--dry-run", "--json"]
        )
        payload = json.loads(stdout)
        self.assertEqual(code, exitcodes.SUCCESS)
        self.assertNotIn("log_file", payload)

    def test_debug_flag_adds_traceback_for_config_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            broken = os.path.join(tmp, "broken.json")
            with open(broken, "w", encoding="utf-8") as handle:
                handle.write("{ not json")
            code, stdout, _ = self._capture(["--json", "--debug", "--config", broken])
            payload = json.loads(stdout)
            self.assertEqual(code, exitcodes.CONFIG_INVALID)
            self.assertEqual(payload["error_code"], "config_file_invalid")
            self.assertIn("traceback", payload)
            self.assertIn("Traceback", payload["traceback"])


class AgentCliTests(KeyEnvMixin, unittest.TestCase):
    """给 Agent 用的新入口：schema / ask / --text。"""

    def _capture(self, argv):
        argv = list(argv) + ["--no-log-file"]
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_schema_lists_params_exit_codes_and_workflows(self):
        code, stdout, _ = self._capture(["schema", "--json"])
        self.assertEqual(code, exitcodes.SUCCESS)
        schema = json.loads(stdout)
        self.assertEqual(schema["schema_version"], 4)
        names = [spec["name"] for spec in schema["params"]]
        self.assertIn("--from", names)
        self.assertIn("--output-dir", names)
        self.assertIn("--text", names)
        self.assertEqual([item["name"] for item in schema["exit_codes"]], [
            "ok", "run_failed", "config_invalid", "env_unavailable", "precheck_failed",
        ])
        self.assertTrue(schema["workflows"])
        self.assertIn("force_start", schema["dangerous_params"])
        required = [spec for spec in schema["params"] if spec["required_for_run"]]
        self.assertEqual(sorted(spec["dest"] for spec in required), ["output_dir", "page_from", "page_to"])

    def test_ask_reports_missing_questions_and_template(self):
        code, stdout, _ = self._capture(["ask", "--from", "47", "--json"])
        self.assertEqual(code, exitcodes.CONFIG_INVALID)
        payload = json.loads(stdout)
        self.assertFalse(payload["ready"])
        self.assertEqual(payload["missing_flags"], ["--to", "--output-dir"])
        self.assertEqual(len(payload["questions"]), 2)
        self.assertIn("--to", payload["ask_user"])
        self.assertIsNone(payload["command"])
        self.assertIn("<结束题号>", payload["command_template"])

    def test_ask_ready_returns_copyable_command(self):
        code, stdout, _ = self._capture(
            [
                "ask", "--from", "47", "--to", "81", "--output-dir", "D:\\题库",
                "--textbook", "药理学", "--chapter", "第二章 药物代谢动力学", "--json",
            ]
        )
        self.assertEqual(code, exitcodes.SUCCESS)
        payload = json.loads(stdout)
        self.assertTrue(payload["ready"])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["missing_flags"], [])
        self.assertIn("--from 47", payload["command"])
        self.assertIn("--to 81", payload["command"])

    def test_text_natural_language_fills_everything(self):
        code, stdout, _ = self._capture(
            ["ask", "--text", "药理学第二章 47到81题，存到 D:\\题库", "--json"]
        )
        self.assertEqual(code, exitcodes.SUCCESS)
        payload = json.loads(stdout)
        self.assertTrue(payload["ready"])
        self.assertIn("--from 47", payload["command"])
        self.assertIn("--to 81", payload["command"])
        self.assertIn("药理学", payload["command"])

    def test_text_never_overrides_explicit_flags(self):
        code, stdout, _ = self._capture(
            ["ask", "--text", "47到81题 存到 D:\\题库", "--from", "1", "--json"]
        )
        payload = json.loads(stdout)
        self.assertTrue(payload["ready"])
        self.assertIn("--from 1", payload["command"])
        self.assertIn("--to 81", payload["command"])

    def test_text_dry_run_hint_reaches_dry_run_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, stdout, _ = self._capture(
                [
                    "--text", f"先别跑，看看配置 1到3题 存到 {tmp}",
                    "--json",
                ]
            )
            payload = json.loads(stdout)
            self.assertEqual(code, exitcodes.SUCCESS)
            self.assertTrue(payload["dry_run"])

    def test_ask_notes_missing_api_key(self):
        with mock.patch("drpilot.cli.load_api_keys", return_value=[]):
            code, stdout, _ = self._capture(
                ["ask", "--from", "1", "--to", "2", "--output-dir", "/tmp/x", "--json"]
            )
        payload = json.loads(stdout)
        self.assertEqual(code, exitcodes.SUCCESS)
        self.assertTrue(any("API Key" in note for note in payload["notes"]))

class RunFailureReportTests(KeyEnvMixin, unittest.TestCase):
    """批量失败时，Agent 要能从 JSON 里直接拿到失败题号与补录命令。"""

    def _capture(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(list(argv) + ["--no-log-file"])
        return code, stdout.getvalue(), stderr.getvalue()

    def _run_with_session(self, fake):
        with mock.patch("drpilot.pipeline.PilotSession", fake), \
             mock.patch("drpilot.cli.load_api_keys", return_value=["test-key-1234"]):
            return self._capture(
                [
                    "--from", "3", "--to", "6", "--output-dir", "/tmp/out",
                    "--textbook", "药理学", "--chapter", "第二章 药物代谢动力学",
                    "--no-model-check", "--no-preflight", "--json",
                ]
            )

    def test_batch_failure_reports_question_numbers_and_retry_command(self):
        class FakeSession:
            def __init__(self, config, **kwargs):
                self.config = config
                self.questions = []
                self.reference_recorded = False
                self.error = None
                self.stop_event = threading.Event()

            def run(self):
                return True

            def diagnostics(self):
                return {
                    "phase": "done",
                    "error_context": {
                        "phase": "batch", "worker": 1, "batch": 0,
                        "question_numbers": [3, 4], "error": "HTTP 429",
                    },
                    "failed_question_numbers": [3, 4],
                    "missing_question_numbers": [4],
                    "batch_failures": [{"batch": 0, "question_numbers": [3, 4]}],
                }

        code, stdout, _ = self._run_with_session(FakeSession)
        self.assertEqual(code, exitcodes.SUCCESS)
        payload = json.loads(stdout)
        self.assertEqual(payload["failed_question_numbers"], [3, 4])
        self.assertIn("--from 3 --to 4", payload["retry_command"])
        self.assertEqual(payload["diagnostics"]["error_context"]["phase"], "batch")
        self.assertEqual(payload["missing_question_numbers"], [4])

    def test_adb_error_maps_to_error_code_and_suggestion(self):
        class FakeSession:
            def __init__(self, config, **kwargs):
                self.config = config
                self.questions = []
                self.reference_recorded = False
                self.stop_event = threading.Event()
                self.error = AdbError("未检测到设备")

            def run(self):
                return False

            def diagnostics(self):
                return {
                    "phase": "connect",
                    "error_context": {"phase": "connect", "error": "未检测到设备", "error_type": "AdbError"},
                    "failed_question_numbers": [],
                    "missing_question_numbers": [],
                    "batch_failures": [],
                }

        code, stdout, stderr = self._run_with_session(FakeSession)
        self.assertEqual(code, exitcodes.RUN_FAILED)
        payload = json.loads(stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], "adb_error")
        self.assertEqual(payload["phase"], "connect")
        self.assertEqual(payload["context"]["error_type"], "AdbError")
        self.assertIn("devices --json", payload["suggestion"])
        self.assertIn("adb_error", stderr)

    def test_preflight_rejection_is_reported_separately(self):
        class FakeSession:
            def __init__(self, config, **kwargs):
                self.config = config
                self.questions = []
                self.reference_recorded = False
                self.stop_event = threading.Event()
                self.error = ConfigError("已取消：屏幕显示第 9 题")
                self._on_preflight = kwargs.get("on_preflight")

            def run(self):
                if self._on_preflight is not None:
                    self._on_preflight(9, 3)
                return False

            def diagnostics(self):
                return {
                    "phase": "preflight",
                    "error_context": {"phase": "preflight"},
                    "failed_question_numbers": [],
                    "missing_question_numbers": [],
                    "batch_failures": [],
                }

        code, stdout, _ = self._run_with_session(FakeSession)
        self.assertEqual(code, exitcodes.PRECHECK_FAILED)
        payload = json.loads(stdout)
        self.assertEqual(payload["error_code"], "precheck_failed")
        self.assertIn("--force-start", payload["suggestion"])



if __name__ == "__main__":
    unittest.main()
