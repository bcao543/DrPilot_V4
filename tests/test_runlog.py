# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import tempfile
import unittest
from pathlib import Path

from drpilot.runlog import RunLogger, default_log_path, mask_secrets


class MaskSecretsTests(unittest.TestCase):
    def test_key_like_tokens_are_masked(self):
        text = mask_secrets("key=sk-abcdefghijklmnop1234 结束")
        self.assertNotIn("sk-abcdefghijklmnop1234", text)
        self.assertIn("****", text)
        self.assertIn("结束", text)

    def test_google_style_key_is_masked(self):
        self.assertNotIn("AIzaSyABCDEFGH1234567890", mask_secrets("AIzaSyABCDEFGH1234567890"))

    def test_normal_text_is_untouched(self):
        self.assertEqual(mask_secrets("普通日志 47 -> 81"), "普通日志 47 -> 81")
        self.assertEqual(mask_secrets(None), "")


class RunLoggerTests(unittest.TestCase):
    def test_default_log_path_is_in_logs_dir(self):
        path = default_log_path()
        self.assertEqual(path.parent.name, "logs")
        self.assertTrue(path.name.startswith("drpilot-"))
        self.assertTrue(path.name.endswith(".log"))

    def test_writes_timestamped_phased_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "run.log"
            logger = RunLogger(target, echo=False)
            logger.set_phase("capture")
            logger.log("截图 1/5")
            logger.warn("重试一次")
            logger.error("批次失败")
            logger.close()
            body = target.read_text(encoding="utf-8")
            self.assertIn("| INFO  | capture | 截图 1/5", body)
            self.assertIn("| WARN  | capture | 重试一次", body)
            self.assertIn("ERROR", body)
            self.assertEqual(logger.error_count, 1)
            self.assertEqual(logger.warning_count, 1)

    def test_secrets_never_hit_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "run.log"
            logger = RunLogger(target, echo=False)
            logger.log("使用 Key sk-abcdefghijklmnop1234 调用")
            logger.close()
            body = target.read_text(encoding="utf-8")
            self.assertNotIn("sk-abcdefghijklmnop1234", body)
            self.assertIn("****", body)

    def test_echo_goes_to_stream(self):
        import io as _io

        buffer = _io.StringIO()
        logger = RunLogger(None, enabled=False, stream=buffer, echo=True)
        logger.log("你好")
        self.assertIn("你好", buffer.getvalue())
        self.assertIsNone(logger.path)

    def test_exception_records_type_and_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "run.log"
            logger = RunLogger(target, echo=False)
            try:
                raise ValueError("模拟失败")
            except ValueError as exc:
                logger.exception(exc, phase="batch", context={"question_numbers": [17]})
            logger.close()
            body = target.read_text(encoding="utf-8")
            self.assertIn("ValueError: 模拟失败", body)
            self.assertIn("question_numbers", body)

    def test_unwritable_path_does_not_crash(self):
        logger = RunLogger("/proc/definitely/not/writable/x.log", echo=False)
        logger.log("照常工作")
        self.assertIsNone(logger.path)


if __name__ == "__main__":
    unittest.main()
