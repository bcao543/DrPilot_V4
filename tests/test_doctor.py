# -*- coding: utf-8 -*-
"""drpilot doctor 的测试：全部走注入的探针，不碰真机、不联网、不改真实 .env。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from drpilot import exitcodes
from drpilot.config import AppConfig
from drpilot.doctor import (
    STATUS_FAIL,
    STATUS_OK,
    STATUS_SKIP,
    STATUS_WARN,
    CheckResult,
    run_doctor,
)

FAKE_KEYS = ["sk-aaaaaaaaaaaaaaaa", "sk-bbbbbbbbbbbbbbbb"]
FAKE_ADB_OK = ("ok", "192.168.0.5:5555 device model:Pixel_7；分辨率 1260x2720", "")
FAKE_ADB_FAIL = ("fail", "未检测到任何 ADB 设备", "开启无线调试后用 --connect 连接")
FAKE_MODEL_OK = ("ok", "模型连通性正常：Qwen/Qwen3.5-4B（800 ms）", "")
FAKE_MODEL_FAIL = ("fail", "模型不可用：已下架", "换一个模型 ID")


def _config(**overrides) -> AppConfig:
    data = {
        "page_from": 1,
        "page_to": 5,
        "textbook": "药理学",
        "chapter": "第二章 药物代谢动力学",
        "output_dir": tempfile.mkdtemp(prefix="drpilot-doctor-"),
        "model": "Qwen/Qwen3.5-4B",
        "base_url": "https://api.siliconflow.cn/v1",
    }
    data.update(overrides)
    return AppConfig.from_dict(data)


class DoctorTests(unittest.TestCase):
    def _run(self, config=None, **kwargs):
        kwargs.setdefault("adb_probe", lambda c: FAKE_ADB_OK)
        kwargs.setdefault("model_probe", lambda c, k: FAKE_MODEL_OK)
        return run_doctor(config or _config(), **kwargs)

    def test_all_green(self):
        with mock.patch("drpilot.doctor._dependencies_check", return_value=CheckResult("dependencies", STATUS_OK, "ok")), \
             mock.patch("drpilot.doctor.load_api_keys", return_value=FAKE_KEYS):
            report = self._run(full=True)
        self.assertTrue(report.ok)
        self.assertEqual(report.exit_code, exitcodes.SUCCESS)
        self.assertEqual([c.name for c in report.checks], list(report.as_dict()["checks"]) and
                         ["python", "dependencies", "keys", "config", "adb", "output", "model"])

    def test_missing_keys_is_env_unavailable(self):
        with mock.patch("drpilot.doctor._dependencies_check", return_value=CheckResult("dependencies", STATUS_OK, "ok")), \
             mock.patch("drpilot.doctor.load_api_keys", return_value=[]):
            report = self._run()
        self.assertFalse(report.ok)
        self.assertEqual(report.exit_code, exitcodes.ENV_UNAVAILABLE)
        keys = next(c for c in report.checks if c.name == "keys")
        self.assertEqual(keys.status, STATUS_FAIL)
        self.assertIn(".env", keys.hint)          # 修复建议里必须告诉用户去哪儿填

    def test_keys_masked_never_leaks_plaintext(self):
        with mock.patch("drpilot.doctor._dependencies_check", return_value=CheckResult("dependencies", STATUS_OK, "ok")), \
             mock.patch("drpilot.doctor.load_api_keys", return_value=FAKE_KEYS):
            report = self._run(full=True)
        blob = json.dumps(report.as_dict(), ensure_ascii=False) + report.render()
        for key in FAKE_KEYS:
            self.assertNotIn(key, blob)
        self.assertIn("sk-********", blob)

    def test_stops_at_first_failure_by_default(self):
        with mock.patch("drpilot.doctor._dependencies_check", return_value=CheckResult("dependencies", STATUS_FAIL, "缺少依赖")), \
             mock.patch("drpilot.doctor.load_api_keys", return_value=FAKE_KEYS):
            report = self._run()
        self.assertEqual([c.name for c in report.checks], ["python", "dependencies"])
        self.assertEqual(report.remaining, ["keys", "config", "adb", "output", "model"])
        self.assertEqual(report.exit_code, exitcodes.ENV_UNAVAILABLE)
        self.assertIn("--full", report.render())

    def test_full_ignores_early_failure(self):
        with mock.patch("drpilot.doctor._dependencies_check", return_value=CheckResult("dependencies", STATUS_FAIL, "缺少依赖")), \
             mock.patch("drpilot.doctor.load_api_keys", return_value=FAKE_KEYS):
            report = self._run(full=True)
        self.assertIn("model", [c.name for c in report.checks])
        self.assertEqual(report.remaining, [])
        self.assertEqual(report.exit_code, exitcodes.ENV_UNAVAILABLE)

    def test_config_invalid_wins_exit_code_over_env(self):
        config = _config(page_to=0, output_dir="")
        with mock.patch("drpilot.doctor._dependencies_check", return_value=CheckResult("dependencies", STATUS_OK, "ok")), \
             mock.patch("drpilot.doctor.load_api_keys", return_value=FAKE_KEYS):
            report = self._run(config=config, full=True)
        self.assertEqual(report.exit_code, exitcodes.CONFIG_INVALID)

    def test_workers_over_key_count_warns(self):
        with mock.patch("drpilot.doctor._dependencies_check", return_value=CheckResult("dependencies", STATUS_OK, "ok")), \
             mock.patch("drpilot.doctor.load_api_keys", return_value=FAKE_KEYS):
            report = self._run(config=_config(workers=3), full=True)
        keys = next(c for c in report.checks if c.name == "keys")
        self.assertEqual(keys.status, STATUS_WARN)
        self.assertIn("并发", keys.detail)
        self.assertIn("2 个 API Key", keys.detail)

    def test_adb_failure_gives_connect_hint(self):
        with mock.patch("drpilot.doctor._dependencies_check", return_value=CheckResult("dependencies", STATUS_OK, "ok")), \
             mock.patch("drpilot.doctor.load_api_keys", return_value=FAKE_KEYS):
            report = self._run(adb_probe=lambda c: FAKE_ADB_FAIL)
        adb = next(c for c in report.checks if c.name == "adb")
        self.assertEqual(adb.status, STATUS_FAIL)
        self.assertIn("--connect", adb.hint)
        self.assertEqual(report.exit_code, exitcodes.ENV_UNAVAILABLE)

    def test_probe_exception_does_not_crash(self):
        def boom(_config):
            raise RuntimeError("adb 进程炸了")

        with mock.patch("drpilot.doctor._dependencies_check", return_value=CheckResult("dependencies", STATUS_OK, "ok")), \
             mock.patch("drpilot.doctor.load_api_keys", return_value=FAKE_KEYS):
            report = self._run(adb_probe=boom)
        adb = next(c for c in report.checks if c.name == "adb")
        self.assertEqual(adb.status, STATUS_FAIL)
        self.assertIn("adb 进程炸了", adb.detail)

    def test_model_failure_after_adb(self):
        with mock.patch("drpilot.doctor._dependencies_check", return_value=CheckResult("dependencies", STATUS_OK, "ok")), \
             mock.patch("drpilot.doctor.load_api_keys", return_value=FAKE_KEYS):
            report = self._run(model_probe=lambda c, k: FAKE_MODEL_FAIL)
        self.assertEqual(next(c for c in report.checks if c.name == "model").status, STATUS_FAIL)
        self.assertEqual(report.exit_code, exitcodes.ENV_UNAVAILABLE)

    def test_model_skipped_when_disabled(self):
        with mock.patch("drpilot.doctor._dependencies_check", return_value=CheckResult("dependencies", STATUS_OK, "ok")), \
             mock.patch("drpilot.doctor.load_api_keys", return_value=FAKE_KEYS):
            report = self._run(check_model=False)
        model = next(c for c in report.checks if c.name == "model")
        self.assertEqual(model.status, STATUS_SKIP)
        self.assertTrue(report.ok)

    def test_output_dir_not_writable(self):
        with mock.patch("drpilot.doctor._dependencies_check", return_value=CheckResult("dependencies", STATUS_OK, "ok")), \
             mock.patch("drpilot.doctor.load_api_keys", return_value=FAKE_KEYS):
            with mock.patch("drpilot.doctor.Path.mkdir", side_effect=PermissionError("拒绝访问")):
                report = self._run(full=True)
        output = next(c for c in report.checks if c.name == "output")
        self.assertEqual(output.status, STATUS_FAIL)
        self.assertIn("--output-dir", output.hint)

    def test_output_probe_file_is_cleaned_up(self):
        target = tempfile.mkdtemp(prefix="drpilot-doctor-out-")
        with mock.patch("drpilot.doctor._dependencies_check", return_value=CheckResult("dependencies", STATUS_OK, "ok")), \
             mock.patch("drpilot.doctor.load_api_keys", return_value=FAKE_KEYS):
            report = self._run(config=_config(output_dir=target), check_model=False)
        self.assertEqual(next(c for c in report.checks if c.name == "output").status, STATUS_OK)
        self.assertEqual([p for p in os.listdir(target) if p.startswith(".drpilot-doctor-")], [])

    def test_as_dict_is_json_serializable(self):
        with mock.patch("drpilot.doctor._dependencies_check", return_value=CheckResult("dependencies", STATUS_OK, "ok")), \
             mock.patch("drpilot.doctor.load_api_keys", return_value=FAKE_KEYS):
            payload = self._run(full=True).as_dict()
        text = json.dumps(payload, ensure_ascii=False)
        self.assertIn("exit_code_name", json.loads(text))
        self.assertEqual(payload["counts"]["fail"], 0)

    def test_render_marks_every_status(self):
        report = self._run(full=True)
        text = report.render()
        self.assertIn("[✓]", text)
        self.assertIn("环境自检", text)


class DoctorConfigPathTests(unittest.TestCase):
    def test_explicit_missing_config_is_warning_not_crash(self):
        with mock.patch("drpilot.doctor._dependencies_check", return_value=CheckResult("dependencies", STATUS_OK, "ok")), \
             mock.patch("drpilot.doctor.load_api_keys", return_value=FAKE_KEYS):
            report = run_doctor(
                config=_config(),
                config_path=str(Path(tempfile.mkdtemp()) / "none.json"),
                full=True,
                adb_probe=lambda c: FAKE_ADB_OK,
                model_probe=lambda c, k: FAKE_MODEL_OK,
            )
        self.assertTrue(all(c.status != STATUS_FAIL for c in report.checks))


if __name__ == "__main__":
    unittest.main()
