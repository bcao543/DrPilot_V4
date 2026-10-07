# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import copy
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from drpilot import display as display_module
from drpilot.actions import RecordResult, Step, build_next_action
from drpilot.adb import DeviceInfo
from drpilot.ai import ModelCheckResult
from drpilot.config import AppConfig
from drpilot.errors import AdbError, ConfigError
from drpilot.keys import env_file_path
from drpilot.webui.bridge import (
    UiBridge,
    address_from_payload,
    config_from_payload,
    log_tone,
)
from drpilot.webui.tuner import DisplayTunerApi


def wait_until(predicate, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class FakeAdb:
    """假的 AdbClient：只记录调用，不碰真机。"""

    connected: list[str] = []
    swipes: list[tuple] = []
    devices_error: Exception | None = None
    connect_error: Exception | None = None

    def __init__(self, adb_path=None, serial=None, timeout=30.0, log=None):
        self.adb_path = adb_path or "/fake/adb"
        self.serial = serial
        self.timeout = timeout

    def devices(self):
        if FakeAdb.devices_error:
            raise FakeAdb.devices_error
        devices = [DeviceInfo(serial="USB123", state="device", model="Pixel_7")]
        for address in FakeAdb.connected:
            devices.append(DeviceInfo(serial=address, state="device", model="Pixel_7"))
        devices.append(DeviceInfo(serial="OFFLINE", state="offline"))
        return devices

    def wm_size(self):
        return (1260, 2720)

    def connect(self, address, timeout=None):
        if FakeAdb.connect_error:
            raise FakeAdb.connect_error
        FakeAdb.connected.append(address)
        return f"already connected to {address}"

    def disconnect(self, address=None, timeout=None):
        return "disconnected everything"

    def ensure_device(self):
        return DeviceInfo(serial="USB123", state="device")

    def screenshot(self):
        return b"\x89PNG\r\n\x1a\nDATA"

    def swipe(self, *args):
        FakeAdb.swipes.append(args)


def make_payload(**overrides):
    payload = {
        "page_from": "1",
        "page_to": "20",
        "textbook": "诊断学",
        "chapter": "第三章 胸部检查",
        "chapter_no": "3",
        "chapter_total": "279",
        "output_dir": "D:/题库",
        "model": "Qwen/Qwen3.5-4B",
        "base_url": "https://api.siliconflow.cn/v1",
        "batch_size": "3",
        "workers": "2",
        "wait_ms": "1000",
        "generate_markdown": True,
        "generate_index": True,
        "preflight": True,
        "check_model": True,
        "device_ip": "192.168.0.105",
        "device_port": "5555",
        "swipe_x1": "1060",
        "swipe_y1": "553",
        "swipe_x2": "270",
        "swipe_y2": "551",
        "swipe_duration": "30",
        "swipe_retries": "5",
        "swipe_retry_wait": "1.2",
        "swipe_reference": "1260x2720",
        "next_action": "",
        "action_tap_x": "",
        "action_tap_y": "",
        "action_tap_ms": "80",
        # 提取模块 / 加长主屏（display_mode / display_scaling 是勾选框状态）
        "extract_modules": "",
        "display_scale": "2.0",
        "display_density": "0",
        "display_scaling": False,
        "display_mode": False,
        "max_tokens": "4096",
    }
    payload.update(overrides)
    return payload


def temp_env_with_key(directory: str, key: str = "sk-bridge-test-only-0001") -> str:
    """给桥造一个只有测试 Key 的 .env，避免测试依赖开发者本机的 .env / 环境变量。"""
    path = os.path.join(directory, ".env")
    Path(path).write_text(f"SILICONFLOW_API_KEY_1={key}\n", encoding="utf-8")
    return path


class PayloadTests(unittest.TestCase):
    def test_payload_to_config(self):
        config = config_from_payload(make_payload())
        self.assertEqual(config.page_from, 1)
        self.assertEqual(config.page_to, 20)
        self.assertEqual(config.total, 20)
        self.assertEqual(config.device_address, "192.168.0.105:5555")
        self.assertAlmostEqual(config.wait, 1.0)
        self.assertEqual((config.swipe_reference_width, config.swipe_reference_height), (1260, 2720))
        self.assertTrue(config.check_model)
        self.assertEqual(config.swipe_duration_ms, 30)

    def test_payload_defaults_port_and_blank_reference(self):
        config = config_from_payload(make_payload(device_port="", swipe_reference=""))
        self.assertEqual(config.device_address, "192.168.0.105:5555")
        self.assertEqual((config.swipe_reference_width, config.swipe_reference_height), (0, 0))

    def test_payload_without_ip_has_no_address(self):
        config = config_from_payload(make_payload(device_ip=""))
        self.assertEqual(config.device_address, "")

    def test_payload_errors(self):
        from drpilot.errors import ConfigError

        with self.assertRaises(ConfigError):
            config_from_payload(make_payload(output_dir=""))
        with self.assertRaises(ConfigError):
            config_from_payload(make_payload(swipe_reference="abc"))
        with self.assertRaises(ConfigError):
            config_from_payload(make_payload(batch_size="三"))

    def test_next_action_roundtrip_from_payload(self):
        text = json.dumps([{"kind": "tap", "x": 10, "y": 20, "duration_ms": 40}])
        config = config_from_payload(make_payload(next_action=text))
        self.assertEqual(config.next_action[0]["kind"], "tap")
        self.assertEqual((config.next_action[0]["x"], config.next_action[0]["y"]), (10, 20))

    def test_manual_tap_coordinates_are_kept_apart_from_recorded_steps(self):
        """点按坐标单独存 tap_*，不再塞进 next_action（那是录制动作专用）。"""
        config = config_from_payload(
            make_payload(next_action="", action_tap_x="12", action_tap_y="34")
        )
        self.assertEqual(config.next_action, [])
        self.assertEqual((config.tap_x, config.tap_y), (12, 34))
        self.assertEqual(config.page_action, "auto")
        # auto 的优先级：没有录制动作时退回手动点按（坐标是当前设备像素，不缩放）
        steps = build_next_action(config)
        self.assertEqual([step.kind for step in steps], ["tap"])
        self.assertEqual((steps[0].x, steps[0].y), (12, 34))
        self.assertTrue(steps[0].absolute)

    def test_page_action_chooses_one_kind(self):
        """三选一：选了哪一种就只用它，不再回头看其它参数。"""
        recorded = json.dumps([{"kind": "swipe", "x": 9, "y": 9, "x2": 1, "y2": 1, "duration_ms": 100}])
        base = dict(next_action=recorded, action_tap_x="12", action_tap_y="34")

        config = config_from_payload(make_payload(**base, page_action="swipe"))
        self.assertEqual(config.page_action, "swipe")
        self.assertEqual([step.kind for step in build_next_action(config)], ["swipe"])

        config = config_from_payload(make_payload(**base, page_action="tap"))
        steps = build_next_action(config)
        self.assertEqual([step.kind for step in steps], ["tap"])
        self.assertEqual((steps[0].x, steps[0].y), (12, 34))

        config = config_from_payload(make_payload(**base, page_action="record"))
        steps = build_next_action(config)
        self.assertEqual([step.kind for step in steps], ["swipe"])
        self.assertEqual((steps[0].x, steps[0].y), (9, 9))

        # 选了点按却没填坐标：退回固定滑动，至少不会原地卡死
        config = config_from_payload(make_payload(page_action="tap", action_tap_x="", action_tap_y=""))
        self.assertEqual([step.kind for step in build_next_action(config)], ["swipe"])

    def test_output_fields_from_payload(self):
        """本体字段开关：JSON 字符串 / 缺席都用默认三件套，空数组 = 都不要。"""
        default = config_from_payload(make_payload())
        self.assertEqual([item["key"] for item in default.output_fields], ["id", "stem", "answer"])

        text = json.dumps([{"key": "answer", "in_json": True, "in_markdown": False}])
        config = config_from_payload(make_payload(output_fields=text))
        self.assertEqual([item["key"] for item in config.output_fields], ["answer"])
        self.assertFalse(config.output_fields[0]["in_markdown"])

        empty = config_from_payload(make_payload(output_fields="[]"))
        self.assertEqual(empty.output_fields, [])

    def test_config_dict_emits_next_action(self):
        bridge = UiBridge(adb_factory=FakeAdb)
        bridge._config.next_action = [{"kind": "tap", "x": 1, "y": 2, "duration_ms": 0}]
        fields = bridge._config_dict()
        self.assertIn("next_action", fields)
        self.assertIn('"kind": "tap"', fields["next_action"])

    def test_model_check_does_not_need_output_dir(self):
        config = config_from_payload(make_payload(output_dir=""), require_output=False)
        self.assertEqual(config.model, "Qwen/Qwen3.5-4B")
        with tempfile.TemporaryDirectory() as tmp:
            bridge = UiBridge(
                env_path=temp_env_with_key(tmp),
                model_checker=lambda *a, **k: ModelCheckResult(True, "m", "u", "可以调用", latency_ms=1),
                adb_factory=FakeAdb,
            )
            result = bridge.test_model(make_payload(output_dir=""))
            self.assertTrue(result["ok"])
            self.assertTrue(wait_until(lambda: bridge._state["model"]["tone"] == "success"))

    def test_address_from_payload(self):
        self.assertEqual(address_from_payload(make_payload()), "192.168.0.105:5555")
        self.assertEqual(address_from_payload(make_payload(device_port="6000")), "192.168.0.105:6000")
        with self.assertRaises(AdbError):
            address_from_payload(make_payload(device_ip=""))

    def test_log_tone(self):
        self.assertEqual(log_tone("运行失败：boom"), "err")
        self.assertEqual(log_tone("模型不可用：x"), "err")
        self.assertEqual(log_tone("警告：题号不一致"), "warn")
        self.assertEqual(log_tone("模型连通性正常：x"), "ok")
        self.assertEqual(log_tone("截图（1/20），预期第 1 题"), "")


class BridgeDeviceTests(unittest.TestCase):
    def setUp(self):
        FakeAdb.connected = []
        FakeAdb.swipes = []
        FakeAdb.devices_error = None
        FakeAdb.connect_error = None
        self.bridge = UiBridge(
            config_path="/tmp/drpilot_bridge_test_config.json",
            model_checker=lambda *a, **k: ModelCheckResult(True, "m", "u", "可以调用", latency_ms=5),
            adb_factory=FakeAdb,
        )

    def test_connect_success_never_sticks_on_connecting(self):
        self.bridge.connect_device(make_payload())
        self.assertTrue(wait_until(lambda: not self.bridge._state["device"]["busy"]))
        state = self.bridge._state["device"]
        self.assertNotIn("连接中", state["text"])
        self.assertEqual(state["tone"], "success")
        self.assertIn("192.168.0.105:5555", state["detail"])
        self.assertEqual(FakeAdb.connected, ["192.168.0.105:5555"])
        logs = "\n".join(line["text"] for line in self.bridge._logs)
        self.assertIn("连接成功", logs)

    def test_connect_failure_marks_danger_and_clears_busy(self):
        FakeAdb.connect_error = AdbError("failed to connect to '192.168.0.105:5555': Connection refused")
        self.bridge.connect_device(make_payload())
        self.assertTrue(wait_until(lambda: not self.bridge._state["device"]["busy"]))
        state = self.bridge._state["device"]
        self.assertEqual(state["tone"], "danger")
        self.assertIn("连接失败", state["text"])
        self.assertIn("Connection refused", state["detail"])

    def test_bad_address_reports_toast(self):
        result = self.bridge.connect_device(make_payload(device_ip=""))
        self.assertFalse(result["ok"])
        self.assertTrue(self.bridge._state["toasts"])

    def test_probe_devices_reports_resolution(self):
        self.bridge.refresh_devices()
        self.assertTrue(wait_until(lambda: not self.bridge._state["device"]["busy"]))
        state = self.bridge._state["device"]
        self.assertEqual(state["tone"], "success")
        self.assertIn("台在线", state["text"])
        self.assertIn("1260x2720", state["detail"])

    def test_probe_devices_failure_is_not_stuck(self):
        FakeAdb.devices_error = AdbError("未找到 adb 可执行文件")
        self.bridge.refresh_devices()
        self.assertTrue(wait_until(lambda: not self.bridge._state["device"]["busy"]))
        state = self.bridge._state["device"]
        self.assertEqual(state["tone"], "danger")
        self.assertIn("未找到 adb", state["detail"])

    def test_disconnect_probes_again(self):
        self.bridge.disconnect_device(make_payload())
        self.assertTrue(wait_until(lambda: "已断开" in "\n".join(l["text"] for l in self.bridge._logs)))


class BridgeModelTests(unittest.TestCase):
    def setUp(self):
        # 模型自检要能读到 Key：显式喂一个测试 Key，不依赖本机 .env
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env_path = temp_env_with_key(self.tmp.name)

    def test_model_check_success(self):
        bridge = UiBridge(
            env_path=self.env_path,
            model_checker=lambda *a, **k: ModelCheckResult(True, "m", "u", "可以调用", latency_ms=66),
            adb_factory=FakeAdb,
        )
        bridge.test_model(make_payload())
        self.assertTrue(wait_until(lambda: bridge._state["model"]["tone"] == "success"))
        self.assertFalse(bridge._state["model_checking"])
        self.assertIn("66 ms", bridge._state["model"]["note"])

    def test_model_check_failure_and_dialog(self):
        result = ModelCheckResult(
            ok=False, model="Qwen/Qwen3.5-4B", base_url="u",
            detail="平台模型列表里已经没有该模型", suggestions=("Qwen/Qwen3.5-397B-A17B",),
        )
        bridge = UiBridge(env_path=self.env_path, model_checker=lambda *a, **k: result, adb_factory=FakeAdb)
        bridge.test_model(make_payload())
        self.assertTrue(wait_until(lambda: bridge._state["model"]["tone"] == "danger"))
        self.assertIn("相似模型", bridge._state["model"]["note"])

        answers = {}

        def worker():
            answers["value"] = bridge._confirm_model(result)

        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(wait_until(lambda: bridge._state["question"] is not None))
        question = bridge._state["question"]
        self.assertIn("模型连通性测试未通过", question["title"])
        bridge.answer_question(question["id"], False)
        thread.join(timeout=3)
        self.assertFalse(answers["value"])

    def test_question_flow_yes(self):
        bridge = UiBridge(adb_factory=FakeAdb)
        box = {}
        thread = threading.Thread(target=lambda: box.update(v=bridge._ask("标题", "内容")))
        thread.start()
        self.assertTrue(wait_until(lambda: bridge._state["question"] is not None))
        bridge.answer_question(bridge._state["question"]["id"], True)
        thread.join(timeout=3)
        self.assertTrue(box["v"])
        self.assertIsNone(bridge._state["question"])


class BridgeRunTests(unittest.TestCase):
    def setUp(self):
        self.bridge = UiBridge(config_path="/tmp/drpilot_bridge_run.json", adb_factory=FakeAdb)

    def test_preview_lists_outputs(self):
        preview = self.bridge.preview(make_payload())
        self.assertEqual(preview["total_hint"], "共 20 题")
        self.assertIn("诊断学_03_胸部检查.jsonl", preview["output_preview"])
        self.assertIn("index.json", preview["output_preview"])

    def test_preview_follows_markdown_and_index_switches(self):
        full = self.bridge.preview(make_payload())["output_preview"]
        self.assertIn(".md", full)
        self.assertIn("index.json", full)

        no_md = self.bridge.preview(make_payload(generate_markdown=False))["output_preview"]
        self.assertNotIn(".md", no_md)
        self.assertIn("index.json", no_md)

        no_index = self.bridge.preview(make_payload(generate_index=False))["output_preview"]
        self.assertIn(".md", no_index)
        self.assertNotIn("index.json", no_index)

        neither = self.bridge.preview(
            make_payload(generate_markdown=False, generate_index=False)
        )["output_preview"]
        self.assertNotIn(".md", neither)
        self.assertNotIn("index.json", neither)

    def test_preview_reports_config_error(self):
        preview = self.bridge.preview(make_payload(output_dir=""))
        self.assertEqual(preview["total_hint"], "")
        self.assertIn("输出文件夹", preview["output_preview"])

    def test_start_without_api_key_is_rejected(self):
        from unittest import mock

        with mock.patch("drpilot.webui.bridge.load_api_keys", return_value=[]):
            result = self.bridge.start(make_payload())
        self.assertFalse(result["ok"])
        self.assertIn("API Key", result["error"])
        self.assertTrue(self.bridge._state["toasts"])
        self.assertIsNone(self.bridge._run_thread)

    def test_stop_is_idempotent(self):
        class FakeSession:
            def __init__(self):
                self.calls = 0

            def stop(self):
                self.calls += 1

        self.bridge._session = FakeSession()
        self.bridge._set_flag("running", True)
        self.bridge.stop()
        self.bridge.stop()
        self.assertEqual(self.bridge._session.calls, 1)
        self.assertTrue(self.bridge._state["stopping"])
        self.assertEqual(self.bridge._state["progress"]["status"], "正在停止…")
        self.assertEqual(
            [line["text"] for line in self.bridge._logs].count("用户请求停止"), 1
        )
        # 没有在跑的时候点停止，不应有任何动静
        self.bridge._set_flag("running", False)
        self.bridge.stop()
        self.assertEqual(self.bridge._session.calls, 1)

    def test_worker_and_progress_state(self):
        self.bridge._on_progress(7, 20, 7)
        self.bridge._on_worker(1, "处理批次 2")
        self.bridge._on_worker_progress(1, 2)
        self.assertEqual(self.bridge._state["progress"]["percent"], 35)
        self.assertEqual(self.bridge._state["progress"]["current"], "第 7 题（7/20）")
        self.assertEqual(self.bridge._state["workers"], [{"id": 1, "status": "处理批次 2", "done": 2}])

    def test_form_patch_is_delivered_once(self):
        self.bridge._patch_form(swipe_reference="1260x2720")
        first = self.bridge.poll()["state"]["form_patch"]
        second = self.bridge.poll()["state"]["form_patch"]
        self.assertEqual(first, {"swipe_reference": "1260x2720"})
        self.assertEqual(second, {})

    def test_logs_are_drained_by_poll(self):
        self.bridge._log("第一行")
        self.bridge._log("运行失败：第二行")
        logs = self.bridge.poll()["logs"]
        self.assertEqual([line["tone"] for line in logs], ["", "err"])
        self.assertEqual(self.bridge.poll()["logs"], [])


class BridgeActionTests(unittest.TestCase):
    def setUp(self):
        FakeAdb.connected = []
        self.bridge = UiBridge(config_path="/tmp/drpilot_bridge_action.json", adb_factory=FakeAdb)

    @staticmethod
    def _result():
        return RecordResult(
            steps=[Step(kind="tap", x=100, y=200, duration_ms=40)],
            reference=(1260, 2720),
            device="/dev/input/event3（touch）",
        )

    def test_start_record_updates_state_and_form(self):
        with mock.patch("drpilot.webui.bridge.record_once", return_value=self._result()):
            self.assertTrue(self.bridge.start_record(make_payload())["ok"])
            self.assertTrue(wait_until(lambda: bool(self.bridge._state["action"]["steps"])))
        self.assertFalse(self.bridge._state["recording"])
        state = self.bridge._state["action"]
        self.assertEqual(state["steps"][0]["kind"], "tap")
        self.assertEqual(state["reference"], "1260x2720")
        patch = self.bridge.poll()["state"]["form_patch"]
        self.assertIn("next_action", patch)
        self.assertEqual(patch["swipe_reference"], "1260x2720")

    def test_record_failure_reports_note(self):
        result = RecordResult(error="没有录到触摸事件，请重试")
        with mock.patch("drpilot.webui.bridge.record_once", return_value=result):
            self.bridge.start_record(make_payload())
            self.assertTrue(
                wait_until(lambda: "没有录到触摸事件" in self.bridge._state["action"]["note"])
            )
        self.assertFalse(self.bridge._state["recording"])
        self.assertTrue(self.bridge._state["toasts"])

    def test_stop_record_is_idempotent(self):
        def blocking(adb, *, stop_event=None, log=None, **kwargs):
            stop_event.wait(timeout=5)
            return RecordResult(error="已停止")

        with mock.patch("drpilot.webui.bridge.record_once", side_effect=blocking):
            self.bridge.start_record(make_payload())
            self.assertTrue(wait_until(lambda: self.bridge._state["recording"]))
            self.bridge.stop_record()
            self.bridge.stop_record()
            self.assertTrue(wait_until(lambda: not self.bridge._state["recording"]))
        logs = "\n".join(line["text"] for line in self.bridge._logs)
        self.assertEqual(logs.count("正在结束录制"), 1)

    def test_start_rejected_while_recording(self):
        self.bridge._set_recording(True)
        result = self.bridge.start(make_payload())
        self.assertFalse(result["ok"])
        self.assertTrue(self.bridge._state["recording"])

    def test_clear_action_and_manual_tap(self):
        self.bridge._config.next_action = [{"kind": "tap", "x": 1, "y": 2, "duration_ms": 0}]
        self.bridge.clear_action()
        self.assertEqual(self.bridge._state["action"]["steps"], [])
        result = self.bridge.use_tap(
            make_payload(next_action="", action_tap_x="12", action_tap_y="34")
        )
        self.assertTrue(result["ok"])
        # 手动点按存进 tap_*，翻页方式切成「点按」；录制动作（next_action）不受影响
        patch = self.bridge.poll()["state"]["form_patch"]
        self.assertEqual(patch.get("page_action"), "tap")
        self.assertEqual((patch.get("action_tap_x"), patch.get("action_tap_y")), (12, 34))
        self.assertEqual(self.bridge.action_state()["kind"], "tap")
        self.assertIn("12", self.bridge.action_summary())

    def test_use_tap_requires_coordinates(self):
        result = self.bridge.use_tap(make_payload(next_action=""))
        self.assertFalse(result["ok"])
        self.assertTrue(self.bridge._state["toasts"])

    def test_sync_action_from_config(self):
        self.bridge._config.next_action = [{"kind": "tap", "x": 7, "y": 8, "duration_ms": 0}]
        self.bridge._config.swipe_reference_width = 1080
        self.bridge._config.swipe_reference_height = 2340
        self.bridge._sync_action_from_config()
        state = self.bridge._state["action"]
        self.assertEqual(state["steps"][0]["x"], 7)
        self.assertEqual(state["reference"], "1080x2340")
        self.assertIn("已加载", state["note"])

    def test_test_action_without_steps(self):
        result = self.bridge.test_action(make_payload(next_action=""))
        self.assertFalse(result["ok"])


class GuiEntryTests(unittest.TestCase):
    """cli → webui.run_web_ui → app.run_web_ui 这条转发链的参数必须对得上。

    真实事故：webui/__init__.py 只转发 config_path，cli 一传 logger 就
    「run_web_ui() got an unexpected keyword argument 'logger'」，界面起不来。
    """

    def test_gui_entry_forwards_arguments_to_app(self):
        from drpilot import webui
        from drpilot.webui import app as app_module

        seen: dict[str, object] = {}

        def fake(config_path=None, width=1180, height=920, logger=None):
            seen.update(config_path=config_path, width=width, height=height, logger=logger)
            return 0

        original = app_module.run_web_ui
        app_module.run_web_ui = fake          # webui 里是调用时 import，替换有效
        try:
            code = webui.run_web_ui(config_path="cfg.json", logger="LOGGER")
        finally:
            app_module.run_web_ui = original
        self.assertEqual(code, 0)
        self.assertEqual(seen["config_path"], "cfg.json")
        self.assertEqual(seen["logger"], "LOGGER")

    def test_signatures_match(self):
        import inspect

        from drpilot import webui
        from drpilot.webui import app as app_module

        entry = set(inspect.signature(webui.run_web_ui).parameters)
        real = set(inspect.signature(app_module.run_web_ui).parameters)
        self.assertTrue(
            real <= entry, f"webui.run_web_ui 少转发参数：{sorted(real - entry)}"
        )


class FrontendContractTests(unittest.TestCase):
    """前端 app.js 依赖的字段与方法，必须真的存在（防止改名后前端悄悄失效）。"""

    FRONTEND_STATE_KEYS = [
        "device", "model", "keys", "key_masked", "key_count", "key_items",
        "progress", "running", "stopping", "model_checking",
        "workers", "question", "toasts", "form_patch", "total_hint", "output_preview",
        "recording", "action", "display", "modules",
    ]
    FRONTEND_API = [
        "ready", "poll", "preview", "start", "stop", "test_model",
        "connect_device", "disconnect_device", "refresh_devices",
        "pick_output_dir", "open_output_dir", "set_reference", "test_swipe",
        "start_record", "stop_record", "test_action", "clear_action", "use_tap",
        "answer_question", "exit",
        "save_keys", "reveal_keys", "clear_keys", "add_keys", "delete_key",
        "list_module_presets", "display_status", "reset_display", "preview_capture",
        # 实时滑块：应用显示设置、取一次缩略图、点缩略图看全尺寸
        "apply_display", "pop_display_thumb", "open_display_capture",
    ]

    def test_state_shape(self):
        bridge = UiBridge(adb_factory=FakeAdb)
        state = bridge.poll()["state"]
        for key in self.FRONTEND_STATE_KEYS:
            self.assertIn(key, state, key)
        for key in ("text", "tone", "detail", "busy"):
            self.assertIn(key, state["device"], key)
        for key in ("text", "tone", "note"):
            self.assertIn(key, state["model"], key)
        for key in ("percent", "current", "status", "tone"):
            self.assertIn(key, state["progress"], key)
        for key in ("recording", "steps", "summary", "reference", "note"):
            self.assertIn(key, state["action"], key)
        for key in ("text", "tone", "detail", "thumb_seq"):
            self.assertIn(key, state["display"], key)
        self.assertIsInstance(state["modules"], list)

    def test_gui_log_is_mirrored_to_the_run_logger(self):
        """图形界面的日志要同时进 logs/drpilot-*.log，否则关掉窗口就丢了。"""
        seen: list[tuple[str, str]] = []

        class Recorder:
            def log(self, message, level="INFO", **_kwargs):
                seen.append((str(level), str(message)))

        bridge = UiBridge(adb_factory=FakeAdb, run_logger=Recorder())
        bridge._log("正常一行")
        bridge._log("出错了", "err")
        bridge._log("小心点", "warn")
        self.assertEqual(seen[0], ("INFO", "正常一行"))
        self.assertEqual(seen[1][0], "ERROR")
        self.assertEqual(seen[2][0], "WARN")

    def test_gui_runs_without_a_run_logger(self):
        bridge = UiBridge(adb_factory=FakeAdb)
        bridge._log("没有日志文件时也不能炸")

    def test_api_methods_exist(self):
        bridge = UiBridge(adb_factory=FakeAdb)
        for name in self.FRONTEND_API:
            self.assertTrue(callable(getattr(bridge, name, None)), name)
        # app.py 关窗时要用的
        self.assertTrue(callable(getattr(bridge, "shutdown", None)))

    def test_poll_result_is_json_serializable(self):
        import json

        bridge = UiBridge(adb_factory=FakeAdb)
        bridge._log("错误：x")
        bridge._toast("提示", "ok")
        bridge._patch_form(swipe_reference="1x2")
        payload = bridge.poll()
        json.dumps(payload, ensure_ascii=False)  # pywebview 就是这么传的



class FakeDisplayAdb:
    """模拟一台支持 wm size / density / scaling 的手机。

    真机行为照抄 tests/test_display.py：超过 max_height 的尺寸被静默忽略、
    覆盖值要回读才有、截图要等画面稳定。状态放类属性上：桥每次都 new 一个客户端，
    测试要能看到「同一台手机」的前后变化。
    """

    physical = (1260, 2720)
    density = 520
    max_height = 5440
    override: tuple[int, int] | None = None
    density_override: int | None = None
    scaling_off = False
    calls: list[tuple] = []

    @classmethod
    def reset(cls) -> None:
        cls.override = None
        cls.density_override = None
        cls.scaling_off = False
        cls.calls = []

    def __init__(self, adb_path=None, serial=None, timeout=30.0, log=None):
        self.adb_path = adb_path or "/fake/adb"
        self.serial = serial
        self.timeout = timeout
        self.shots = 0

    # ---- DisplayController 用到的命令 ----
    def run(self, args, check=True, **kwargs):
        FakeDisplayAdb.calls.append(tuple(args))
        key = tuple(args)
        if key == ("shell", "wm", "size"):
            lines = [f"Physical size: {self.physical[0]}x{self.physical[1]}"]
            if FakeDisplayAdb.override:
                lines.append(
                    f"Override size: {FakeDisplayAdb.override[0]}x{FakeDisplayAdb.override[1]}"
                )
            return "\n".join(lines)
        if key == ("shell", "wm", "density"):
            lines = [f"Physical density: {self.density}"]
            if FakeDisplayAdb.density_override:
                lines.append(f"Override density: {FakeDisplayAdb.density_override}")
            return "\n".join(lines)
        if key == ("shell", "dumpsys", "window", "displays"):
            return "noscale" if FakeDisplayAdb.scaling_off else "init=1260x2720 520dpi"
        return ""

    def set_wm_size(self, width, height):
        FakeDisplayAdb.calls.append(("set_wm_size", int(width), int(height)))
        if int(height) <= self.max_height:
            FakeDisplayAdb.override = (int(width), int(height))

    def reset_wm_size(self):
        FakeDisplayAdb.calls.append(("reset_wm_size",))
        FakeDisplayAdb.override = None

    def set_wm_density(self, density):
        FakeDisplayAdb.density_override = int(density)

    def reset_wm_density(self):
        FakeDisplayAdb.density_override = None

    def set_wm_scaling(self, mode):
        FakeDisplayAdb.scaling_off = str(mode) == "off"

    # ---- 连接与截图 ----
    def connect(self, address, timeout=None):
        return f"already connected to {address}"

    def ensure_device(self):
        return DeviceInfo(serial="USB123", state="device")

    def wm_size(self):
        return self.physical

    def screenshot(self):
        self.shots += 1
        stage = 0 if self.shots < 3 else 1     # 前两张模拟 relayout，之后稳定
        return b"\x89PNG\r\n\x1a\nWIDE-" + str(stage).encode("ascii")


_PNG_CACHE: dict[tuple[int, int], bytes] = {}


def png_bytes(width: int = 1260, height: int = 2720) -> bytes:
    """真的 PNG：实时缩略图那条路要能解码（现生成一张 680 万像素的图太慢，缓存一份）。"""
    key = (int(width), int(height))
    if key not in _PNG_CACHE:
        from PIL import Image

        buffer = io.BytesIO()
        Image.new("RGB", key, (248, 248, 248)).save(buffer, format="PNG")
        _PNG_CACHE[key] = buffer.getvalue()
    return _PNG_CACHE[key]


class PngScreenshotAdb(FakeDisplayAdb):
    """截图返回一张真 PNG，尺寸跟着当前覆盖走（和真机一致）。"""

    def screenshot(self) -> bytes:
        width, height = FakeDisplayAdb.override or self.physical
        return png_bytes(width, height)


class FakeWindowEvent:
    """冒充 pywebview 的 window.events.closed（支持 += 注册）。"""

    def __init__(self) -> None:
        self._handlers: list = []

    def __iadd__(self, handler):
        self._handlers.append(handler)
        return self

    def fire(self) -> None:
        for handler in list(self._handlers):
            handler()


class FakeWindow:
    """冒充 pywebview 的子窗口：只记「开出来 / 关掉 / 拉到前面」。"""

    def __init__(self, title, page, api, width, height) -> None:
        self.title = title
        self.page = page
        self.api = api
        self.width = width
        self.height = height
        self.events = SimpleNamespace(closed=FakeWindowEvent())
        self.destroyed = False
        self.restored = False
        self.shown = False

    def destroy(self) -> None:
        self.destroyed = True

    def restore(self) -> None:
        self.restored = True

    def show(self) -> None:
        self.shown = True


class FakeHolder:
    """冒充 DisplayController：只记「restore 有没有被调到」。"""

    def __init__(self, device=None) -> None:
        self.device = device
        self.restored = False

    def restore(self) -> bool:
        self.restored = True
        if self.device is not None:
            self.device.override = None
        return True


class BridgeModulesDisplayTests(unittest.TestCase):
    """提取模块 + 加长主屏：payload 解析、回填、状态与三个新桥方法。"""

    MODULES_JSON = json.dumps(
        [
            {
                "name": "考点还原",
                "aliases": ["考点"],
                "below_fold": True,
                "required": False,
                "in_markdown": True,
                "hint": "",
            },
            {"name": "难度", "below_fold": False, "in_markdown": False},
        ],
        ensure_ascii=False,
    )

    def setUp(self):
        FakeDisplayAdb.reset()
        # 真机上要等 ~3s 画面稳定，单测里把下限调成 0，避免每次跑测试都睡觉
        self._min_settle = display_module.MIN_SETTLE_SECONDS
        display_module.MIN_SETTLE_SECONDS = 0.0
        self.addCleanup(self._restore_settle)
        self.bridge = UiBridge(
            config_path="/tmp/drpilot_bridge_modules.json",
            adb_factory=FakeDisplayAdb,
        )

    def _restore_settle(self):
        display_module.MIN_SETTLE_SECONDS = self._min_settle

    def _state(self):
        """直接读状态：poll() 会把日志和 toast 排空，测试后面还要看它们。"""
        with self.bridge._lock:
            return copy.deepcopy(self.bridge._state)

    # ---- payload / 回填 ----
    def test_payload_parses_modules_and_display(self):
        config = config_from_payload(
            make_payload(
                extract_modules=self.MODULES_JSON,
                display_mode=True,
                display_scaling=True,
                display_scale="2.5",
                display_density="420",
                max_tokens="8192",
            )
        )
        self.assertEqual([item["name"] for item in config.modules], ["考点还原", "难度"])
        self.assertTrue(config.modules[0]["below_fold"])
        self.assertEqual(config.modules[0]["aliases"][0], "考点")
        self.assertFalse(config.modules[1]["below_fold"])
        self.assertFalse(config.modules[1]["in_markdown"])
        self.assertEqual(config.display_mode, "wide")
        self.assertEqual(config.display_scaling, "off")
        self.assertAlmostEqual(config.display_scale, 2.5)
        self.assertEqual(config.display_density, 420)
        self.assertEqual(config.max_tokens, 8192)

    def test_payload_defaults_modules_and_display(self):
        config = config_from_payload(make_payload())
        self.assertEqual(config.modules, [])
        self.assertEqual(config.display_mode, "off")
        self.assertEqual(config.display_scaling, "auto")
        self.assertAlmostEqual(config.display_scale, 2.0)
        self.assertEqual(config.display_density, 0)
        self.assertEqual(config.max_tokens, 4096)

    def test_payload_without_modules_keeps_field_usable(self):
        # 逗号清单也认（兼容手写），不是只有 JSON 字符串能过
        config = config_from_payload(make_payload(extract_modules="考点还原,标准解析"))
        self.assertEqual([item["name"] for item in config.modules], ["考点还原", "标准解析"])

    def test_payload_rejects_bad_modules(self):
        with self.assertRaises(ConfigError):
            config_from_payload(make_payload(extract_modules='[{"name": "stem"}]'))
        with self.assertRaises(ConfigError):
            config_from_payload(make_payload(extract_modules='[{"name": "标准解析"}, {"name": "解析"}]'))
        with self.assertRaises(ConfigError):
            config_from_payload(make_payload(extract_modules="[{名字]"))
        with self.assertRaises(ConfigError):
            config_from_payload(make_payload(max_tokens="很多"))

    def test_config_dict_fills_back_six_fields(self):
        self.bridge._config = config_from_payload(
            make_payload(extract_modules=self.MODULES_JSON, display_mode=True, display_scaling=True)
        )
        fields = self.bridge._config_dict()
        # 勾选框回填布尔（app.js 的 FLAGS 直接写 .checked），模块回填 JSON 字符串
        self.assertIs(fields["display_mode"], True)
        self.assertIs(fields["display_scaling"], True)
        self.assertEqual(json.loads(fields["extract_modules"])[0]["name"], "考点还原")
        self.assertEqual(fields["display_scale"], 2.0)
        self.assertEqual(fields["display_density"], 0)
        self.assertEqual(fields["max_tokens"], 4096)
        self.assertEqual(fields["next_action"], "")

    def test_config_dict_roundtrip(self):
        self.bridge._config = config_from_payload(
            make_payload(extract_modules=self.MODULES_JSON, display_mode=True, display_scaling=True)
        )
        fields = self.bridge._config_dict()
        merged = make_payload()
        for key in (
            "extract_modules", "display_mode", "display_scaling",
            "display_scale", "display_density", "max_tokens",
        ):
            merged[key] = fields[key]
        round_trip = config_from_payload(merged)
        self.assertEqual(round_trip.display_mode, "wide")
        self.assertEqual(round_trip.display_scaling, "off")
        self.assertEqual([item["name"] for item in round_trip.modules], ["考点还原", "难度"])

    # ---- 状态 ----
    def test_state_exposes_modules_and_display(self):
        state = self._state()
        self.assertEqual(state["modules"], [])
        self.assertIn("显示", state["display"]["text"])
        self.assertIn("tone", state["display"])
        json.dumps(state, ensure_ascii=False)

    def test_sync_modules_state_mirrors_config(self):
        self.bridge._config = config_from_payload(make_payload(extract_modules=self.MODULES_JSON))
        self.bridge._sync_modules_state()
        state = self._state()
        self.assertEqual([item["name"] for item in state["modules"]], ["考点还原", "难度"])
        self.assertTrue(state["modules"][0]["below_fold"])

    def test_list_module_presets(self):
        self.bridge._config = config_from_payload(make_payload(extract_modules=self.MODULES_JSON))
        self.bridge._sync_modules_state()
        result = self.bridge.list_module_presets()
        names = [spec["name"] for spec in result["presets"]]
        self.assertIn("标准解析", names)
        self.assertIn("考点还原", names)
        self.assertGreaterEqual(len(names), 5)
        self.assertIn("aliases", result["presets"][0])
        self.assertEqual([item["name"] for item in result["current"]], ["考点还原", "难度"])
        json.dumps(result, ensure_ascii=False)      # 必须能过 pywebview 的 JSON 序列化

    # ---- display_status ----
    def test_display_status_reads_device(self):
        immediate = self.bridge.display_status()
        self.assertTrue(immediate["ok"])
        self.assertIn("text", immediate)
        self.assertIn("state", immediate)
        self.assertTrue(wait_until(lambda: bool(self._state()["display"]["state"])))
        display = self._state()["display"]
        self.assertIn("1260x2720", display["text"])
        self.assertEqual(display["state"]["physical"], "1260x2720")
        self.assertFalse(display["has_override"])
        self.assertIn("手机当前显示", "\n".join(line["text"] for line in self.bridge._logs))
        json.dumps(self.bridge.poll(), ensure_ascii=False)

    def test_display_status_reports_existing_override(self):
        FakeDisplayAdb.override = (1260, 5440)
        self.bridge.display_status()
        self.assertTrue(wait_until(lambda: self._state()["display"]["has_override"]))
        display = self._state()["display"]
        self.assertIn("已有覆盖", display["text"])
        self.assertEqual(display["state"]["override"], "1260x5440")
        self.assertTrue(self.bridge._state["toasts"])

    def test_display_status_failure_is_soft(self):
        class BoomAdb(FakeDisplayAdb):
            def ensure_device(self):
                raise AdbError("未找到 adb 可执行文件")

        bridge = UiBridge(config_path="/tmp/drpilot_bridge_boom.json", adb_factory=BoomAdb)
        result = bridge.display_status()
        self.assertTrue(result["ok"])               # 立刻返回缓存值，不抛
        self.assertTrue(wait_until(lambda: "失败" in bridge._state["display"]["text"]))
        self.assertTrue(bridge._state["toasts"])

    # ---- reset_display ----
    def test_reset_display_clears_override(self):
        FakeDisplayAdb.override = (1260, 5440)
        FakeDisplayAdb.density_override = 420
        FakeDisplayAdb.scaling_off = True
        result = self.bridge.reset_display()
        self.assertTrue(result["ok"])
        self.assertIn("text", result)
        self.assertTrue(wait_until(lambda: "已复位" in self._state()["display"]["text"]))
        self.assertIsNone(FakeDisplayAdb.override)
        self.assertIsNone(FakeDisplayAdb.density_override)
        self.assertFalse(FakeDisplayAdb.scaling_off)
        self.assertFalse(self._state()["display"]["has_override"])

    def test_reset_display_failure_is_soft(self):
        class BoomAdb(FakeDisplayAdb):
            def ensure_device(self):
                raise AdbError("设备未连接")

        bridge = UiBridge(config_path="/tmp/drpilot_bridge_boom.json", adb_factory=BoomAdb)
        self.assertTrue(bridge.reset_display()["ok"])
        self.assertTrue(wait_until(lambda: "复位失败" in bridge._state["display"]["text"]))
        self.assertTrue(bridge._state["toasts"])

    # ---- preview_capture ----
    def test_apply_display_density_only(self):
        """倍数 1.0 + 密度 420：不加长，但密度确实改了（以前这条是空转的）。"""
        self.bridge.apply_display(
            make_payload(display_mode=True, display_scale="1.0", display_density="420")
        )
        self.assertTrue(wait_until(lambda: FakeDisplayAdb.density_override == 420))
        self.assertIsNone(FakeDisplayAdb.override)          # 没有加长
        self.assertTrue(wait_until(lambda: "密度已改为 420" in self._state()["display"]["text"]))
        self.bridge.shutdown()
        self.assertIsNone(FakeDisplayAdb.density_override)

    def test_preview_capture_keeps_the_screen(self):
        payload = make_payload(
            display_scale="2.0", display_density="420", display_scaling=True, display_mode=True
        )
        with mock.patch.object(UiBridge, "_open_path", return_value=True) as opener:
            result = self.bridge.preview_capture(payload)
            self.assertTrue(result["ok"])
            self.assertTrue(
                wait_until(
                    lambda: bool((self._state()["display"].get("capture") or {}).get("image"))
                )
            )
        capture = self._state()["display"]["capture"]
        self.assertTrue(os.path.isfile(capture["image"]))
        with open(capture["image"], "rb") as handle:
            self.assertTrue(handle.read().startswith(b"\x89PNG"))
        self.assertEqual(capture["size"], "1260x5440")
        self.assertTrue(capture["display"]["verified"])
        self.assertTrue(capture["opened"])
        opener.assert_called_once_with(capture["image"])
        # 抓图不动屏幕：设置留着给用户继续调，复位交给「复位显示」/退出
        self.assertEqual(FakeDisplayAdb.override, (1260, 5440))
        self.assertEqual(FakeDisplayAdb.density_override, 420)
        self.assertTrue(self._state()["display"]["has_override"])
        logs = "\n".join(line["text"] for line in self.bridge._logs)
        self.assertIn("加长主屏已生效", logs)
        self.assertIn("全尺寸截图已保存并打开", logs)
        json.dumps(self.bridge.poll(), ensure_ascii=False)
        # 退出程序必须还原（不然就是把用户手机丢在加长状态）
        self.bridge.shutdown()
        self.assertIsNone(FakeDisplayAdb.override)
        self.assertIsNone(FakeDisplayAdb.density_override)
        self.assertFalse(FakeDisplayAdb.scaling_off)
        self.assertFalse(self._state()["display"]["has_override"])

    def test_preview_capture_without_display_settings_only_shoots(self):
        """倍数 1.0 + 密度 0：抓图就是截当前屏幕，一个 set/reset 都不该发。"""
        with mock.patch.object(UiBridge, "_open_path", return_value=True) as opener:
            result = self.bridge.preview_capture(make_payload(display_mode=False, display_scale="1.0"))
            self.assertTrue(result["ok"])
            self.assertTrue(
                wait_until(
                    lambda: bool((self._state()["display"].get("capture") or {}).get("image"))
                )
            )
        opener.assert_called_once()
        self.assertEqual(
            [call for call in FakeDisplayAdb.calls if call[0].startswith(("set_", "reset_"))], []
        )

    def test_preview_capture_rejects_bad_payload(self):
        result = self.bridge.preview_capture(make_payload(display_scale="abc"))
        self.assertFalse(result["ok"])
        self.assertTrue(self.bridge._state["toasts"])
        self.assertEqual(self._state()["display"]["capture"], {})

    # ---- 实时滑块：apply_display / 缩略图 / 退出复位 ----
    def test_apply_display_stretches_and_keeps_it(self):
        """滑块模式要留着加长状态给用户看（不像运行那样跑完自动复位）。"""
        self.bridge.apply_display(make_payload(display_mode=True, display_density="0"))
        self.assertTrue(wait_until(lambda: FakeDisplayAdb.override == (1260, 5440)))
        self.assertTrue(wait_until(lambda: "已加长" in self._state()["display"]["text"]))
        self.assertTrue(self._state()["display"]["has_override"])
        # 留着的状态必须还能被复位（否则就是把用户手机丢在加长状态）
        self.bridge.shutdown()
        self.assertIsNone(FakeDisplayAdb.override)
        self.assertIsNone(FakeDisplayAdb.density_override)

    def test_apply_display_with_density(self):
        self.bridge.apply_display(
            make_payload(display_mode=True, display_density="420", display_preview=False)
        )
        self.assertTrue(wait_until(lambda: FakeDisplayAdb.density_override == 420))
        self.assertTrue(wait_until(lambda: "密度 420" in self._state()["display"]["text"]))
        self.bridge.shutdown()
        self.assertIsNone(FakeDisplayAdb.density_override)

    def test_apply_display_scale_one_resets(self):
        FakeDisplayAdb.override = (1260, 5440)
        FakeDisplayAdb.density_override = 420
        self.bridge.apply_display(make_payload(display_mode=False, display_scale="1.0"))
        self.assertTrue(wait_until(lambda: FakeDisplayAdb.override is None))
        self.assertTrue(wait_until(lambda: "未加长" in self._state()["display"]["text"]))
        self.assertFalse(self._state()["display"]["has_override"])

    def test_apply_display_rejects_bad_payload(self):
        result = self.bridge.apply_display(make_payload(display_mode=True, display_scale="abc"))
        self.assertFalse(result["ok"])
        self.assertTrue(self.bridge._state["toasts"])
        self.assertIsNone(FakeDisplayAdb.override)

    def test_apply_display_thumbnail_is_popped_once(self):
        """缩略图只在 thumb_seq 变化时取一次，不跟着 150ms 轮询搬大图。"""
        bridge = UiBridge(config_path="/tmp/drpilot_bridge_thumb.json", adb_factory=PngScreenshotAdb)
        bridge.apply_display(make_payload(display_mode=True))

        def thumb_seq() -> int:
            with bridge._lock:
                return int(bridge._state["display"].get("thumb_seq") or 0)

        self.assertTrue(wait_until(lambda: thumb_seq() > 0))
        first = bridge.pop_display_thumb()
        self.assertTrue(first["thumb"].startswith("data:image/jpeg;base64,"))
        self.assertEqual(bridge.pop_display_thumb()["thumb"], "")     # 取过就没了
        with bridge._lock:
            capture = dict(bridge._state["display"]["capture"])
        self.assertEqual(capture["size"], "1260x5440")
        self.assertTrue(os.path.isfile(capture["image"]))
        with open(capture["image"], "rb") as handle:
            self.assertTrue(handle.read().startswith(b"\x89PNG"))
        # 缩略图不能把整张 1260x2720 的图塞进状态里
        self.assertLess(len(first["thumb"]), 200_000)
        bridge.shutdown()

    def test_apply_display_thumbnail_can_be_turned_off(self):
        bridge = UiBridge(config_path="/tmp/drpilot_bridge_thumb_off.json", adb_factory=PngScreenshotAdb)
        bridge.apply_display(make_payload(display_mode=True, display_preview=False))

        def text() -> str:
            with bridge._lock:
                return str(bridge._state["display"].get("text") or "")

        # 等后台线程整个跑完再收尾，否则它会晚一步去复位，把下一个用例的屏幕也擦掉
        self.assertTrue(wait_until(lambda: "已加长" in text()))
        with bridge._lock:
            self.assertEqual(int(bridge._state["display"].get("thumb_seq") or 0), 0)
        bridge.shutdown()
        self.assertIsNone(FakeDisplayAdb.override)

    def test_open_display_capture_without_shot_is_soft(self):
        self.assertFalse(self.bridge.open_display_capture()["ok"])
        self.assertTrue(self.bridge._state["toasts"])

    def test_run_session_releases_live_stretch(self):
        """开跑前要把滑块留下的加长交给管线/复位，不能让两套状态打架。"""
        FakeDisplayAdb.override = (1260, 5440)
        held = FakeHolder(FakeDisplayAdb)
        self.bridge._display_holder = held
        self.bridge._config = AppConfig(display_mode="off")
        ran: list[bool] = []

        class FakeSession:
            def run(self):
                ran.append(True)

        self.bridge._run_session(FakeSession())
        self.assertEqual(ran, [True])
        self.assertTrue(held.restored)
        self.assertIsNone(FakeDisplayAdb.override)
        self.assertIsNone(self.bridge._display_holder)

    def test_run_session_hands_stretch_to_pipeline(self):
        """这次运行本来就要加长：句柄交给管线，别在开跑前复位一遍。"""
        FakeDisplayAdb.override = (1260, 5440)
        held = FakeHolder(FakeDisplayAdb)
        self.bridge._display_holder = held
        self.bridge._config = AppConfig(display_mode="wide", display_scale=2.0)

        class FakeSession:
            def run(self):
                pass

        self.bridge._run_session(FakeSession())
        self.assertFalse(held.restored)                    # 交给管线了，跑完由管线复位
        self.assertEqual(FakeDisplayAdb.override, (1260, 5440))
        self.assertIsNone(self.bridge._display_holder)

    def test_preview_capture_failure_is_soft_and_leaves_no_override(self):
        class BoomAdb(FakeDisplayAdb):
            def ensure_device(self):
                raise AdbError("设备未连接")

        bridge = UiBridge(config_path="/tmp/drpilot_bridge_boom.json", adb_factory=BoomAdb)
        with mock.patch.object(UiBridge, "_open_path", return_value=True):
            result = bridge.preview_capture(make_payload())
        self.assertTrue(result["ok"])
        self.assertTrue(wait_until(lambda: "抓图失败" in bridge._state["display"]["text"]))
        self.assertTrue(bridge._state["toasts"])
        self.assertIsNone(FakeDisplayAdb.override)


class PanelWindowTests(unittest.TestCase):
    """主界面四个入口窗口：开窗 / 各窗口的设置读写 / 回填主界面。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.windows: list[tuple] = []

        def factory(title, page, api, width, height):
            window = FakeWindow(title, page, api, width, height)
            self.windows.append((title, page, api, width, height))
            return window

        self.bridge = UiBridge(
            config_path=os.path.join(self.tmp.name, "drpilot_config.json"),
            adb_factory=FakeAdb,
            env_path=os.path.join(self.tmp.name, ".env"),
            open_window=factory,
        )

    def _patch(self):
        return self.bridge.poll()["state"]["form_patch"]

    def test_open_each_panel_uses_its_own_api(self):
        from drpilot.webui.panels import ActionPanelApi, ModelPanelApi, ModulePanelApi
        from drpilot.webui.tuner import DisplayTunerApi

        expected = {
            "action": ("actions", ActionPanelApi),
            "modules": ("modules", ModulePanelApi),
            "model": ("model", ModelPanelApi),
            "tuner": ("tuner", DisplayTunerApi),
        }
        for name, (page, api_type) in expected.items():
            result = self.bridge.open_panel(name, make_payload())
            self.assertTrue(result["ok"], name)
            title, got_page, api, width, height = self.windows[-1]
            self.assertEqual(got_page, page)
            self.assertIsInstance(api, api_type)
            self.assertGreaterEqual(width, 800)
        self.assertEqual(len(self.windows), 4)

    def test_open_panel_twice_reuses_window(self):
        self.bridge.open_panel("modules", make_payload())
        result = self.bridge.open_panel("modules", make_payload())
        self.assertTrue(result.get("existing"))
        self.assertEqual(len(self.windows), 1)

    def test_unknown_panel_is_rejected(self):
        self.assertFalse(self.bridge.open_panel("nope", make_payload())["ok"])

    # ---- 翻页动作 ----
    def test_action_settings_roundtrip_patches_form(self):
        self.bridge.open_panel("action", make_payload())
        api = self.windows[0][2]
        result = api.apply({"kind": "tap", "tap": {"x": 540, "y": 2280, "ms": 90}})
        self.assertTrue(result["ok"])
        patch = self._patch()
        self.assertEqual(patch["page_action"], "tap")
        self.assertEqual((patch["action_tap_x"], patch["action_tap_y"]), (540, 2280))
        self.assertEqual(patch["action_tap_ms"], 90)
        state = self.bridge.action_state()
        self.assertEqual(state["kind"], "tap")
        self.assertIn("点按 (540, 2280)", state["summary"])

    def test_action_settings_rejects_tap_without_coordinates(self):
        result = self.bridge.save_action_settings({"kind": "tap", "tap": {"x": 0, "y": 0}})
        self.assertFalse(result["ok"])
        self.assertIn("X", result["error"])

    def test_action_settings_swipe_patches_coordinates(self):
        self.bridge.save_action_settings({
            "kind": "swipe",
            "swipe": {"x1": 100, "y1": 200, "x2": 300, "y2": 400, "duration": 250,
                      "retries": 3, "retry_wait": 0.8},
            "reference": "1080x2340",
        })
        patch = self._patch()
        self.assertEqual(patch["page_action"], "swipe")
        self.assertEqual((patch["swipe_x1"], patch["swipe_y1"]), (100, 200))
        self.assertEqual((patch["swipe_x2"], patch["swipe_y2"]), (300, 400))
        self.assertEqual(patch["swipe_duration"], 250)
        self.assertEqual(patch["swipe_reference"], "1080x2340")
        self.assertIn("滑动", self.bridge.action_summary())

    def test_clear_recorded_keeps_tap_and_swipe(self):
        self.bridge.save_action_settings({"kind": "tap", "tap": {"x": 5, "y": 6}})
        self.bridge._patch_form(next_action=json.dumps([{"kind": "tap", "x": 1, "y": 2}]))
        self.bridge._update("action", steps=[{"kind": "tap", "x": 1, "y": 2, "duration_ms": 0}])
        self._patch()                              # 清掉前面累积的回填，只看这一步
        self.bridge.clear_recorded()
        patch = self._patch()
        self.assertEqual(patch["next_action"], "")
        self.assertNotIn("page_action", patch)     # 只是清了录制，没改选择
        self.assertEqual(self.bridge.action_state()["record"]["steps"], [])

    # ---- 提取模块 ----
    def test_module_settings_lists_base_fields_and_modules(self):
        state = self.bridge.module_settings()
        names = [item["name"] for item in state["fields"]]
        self.assertEqual(names, ["题号", "题目", "答案"])
        self.assertEqual(state["fields"][0]["kind"], "base")
        self.assertTrue(state["presets"])
        self.assertIn("题号", state["summary"])

    def test_save_module_settings_patches_both_hidden_inputs(self):
        result = self.bridge.save_module_settings({"fields": [
            {"kind": "base", "key": "stem", "in_json": True, "in_markdown": True},
            {"kind": "module", "name": "考点还原", "aliases": ["考点"], "below_fold": True},
            {"kind": "module", "name": "标准解析", "in_json": False},
        ]})
        self.assertTrue(result["ok"])
        patch = self._patch()
        output_fields = json.loads(patch["output_fields"])
        self.assertEqual([item["key"] for item in output_fields], ["stem"])
        modules = json.loads(patch["extract_modules"])
        self.assertEqual([item["name"] for item in modules], ["考点还原", "标准解析"])
        self.assertFalse(modules[1]["in_json"])
        # 主界面入口卡片上的摘要也要跟着变
        self.assertIn("考点还原", self.bridge.panel_summaries()["modules"])

    def test_save_module_settings_rejects_reserved_and_duplicate(self):
        self.assertFalse(self.bridge.save_module_settings({"fields": [
            {"kind": "module", "name": "answer"},
        ]})["ok"])
        self.assertFalse(self.bridge.save_module_settings({"fields": [
            {"kind": "module", "name": "考点还原"},
            {"kind": "module", "name": "考点"},
        ]})["ok"])

    def test_save_module_settings_can_drop_all_base_fields(self):
        result = self.bridge.save_module_settings({"fields": [
            {"kind": "module", "name": "我的模块"},
        ]})
        self.assertTrue(result["ok"])
        self.assertEqual(json.loads(self._patch()["output_fields"]), [])
        self.assertEqual([item["name"] for item in self.bridge.module_settings()["fields"]], ["我的模块"])

    # ---- 模型服务 ----
    def test_model_settings_defaults_to_deepseek(self):
        state = self.bridge.model_settings()
        self.assertEqual(state["model"], "deepseek-flash")
        self.assertEqual(state["base_url"], "https://api.deepseek.com")
        self.assertEqual(state["default_model"], "deepseek-flash")

    def test_save_model_settings_patches_form(self):
        result = self.bridge.save_model_settings({
            "model": "deepseek-flash", "base_url": "https://api.deepseek.com/",
        })
        self.assertTrue(result["ok"])
        patch = self._patch()
        self.assertEqual(patch["model"], "deepseek-flash")
        self.assertEqual(patch["base_url"], "https://api.deepseek.com")
        self.assertIn("deepseek-flash", self.bridge.panel_summaries()["model"])

    def test_save_model_settings_validates(self):
        self.assertFalse(self.bridge.save_model_settings({"model": "", "base_url": "https://x"})["ok"])
        self.assertFalse(self.bridge.save_model_settings({"model": "m", "base_url": "ftp://x"})["ok"])

    def test_display_tuner_state_reports_requested_vs_accepted(self):
        state = self.bridge.tuner_state()
        self.assertIn("requested", state)
        self.assertFalse(state["requested"]["has"])
        self.assertIn("limits", state)


class TunerWindowTests(unittest.TestCase):
    """「显示调节」独立窗口：开窗 / 三个参数 / 复位 / 退出清理。"""

    def setUp(self):
        FakeDisplayAdb.reset()
        self._min_settle = display_module.MIN_SETTLE_SECONDS
        display_module.MIN_SETTLE_SECONDS = 0.0
        self.addCleanup(self._restore_settle)
        self.windows: list[tuple] = []

        def factory(title, page, api, width, height):
            window = FakeWindow(title, page, api, width, height)
            self.windows.append((title, page, api, width, height))
            return window

        self.bridge = UiBridge(
            config_path="/tmp/drpilot_bridge_tuner.json",
            adb_factory=FakeDisplayAdb,
            open_window=factory,
        )

    def _restore_settle(self):
        display_module.MIN_SETTLE_SECONDS = self._min_settle

    def _state(self):
        with self.bridge._lock:
            return copy.deepcopy(self.bridge._state)

    def test_open_creates_tuner_window(self):
        result = self.bridge.open_display_tuner(make_payload())
        self.assertTrue(result["ok"])
        self.assertEqual(len(self.windows), 1)
        title, page, api, width, height = self.windows[0]
        self.assertIn("屏幕调节", title)
        self.assertEqual(page, "tuner")
        self.assertIsInstance(api, DisplayTunerApi)
        self.assertGreaterEqual(width, 800)

    def test_open_twice_reuses_the_window(self):
        self.bridge.open_display_tuner(make_payload())
        result = self.bridge.open_display_tuner(make_payload())
        self.assertTrue(result.get("existing"))
        self.assertEqual(len(self.windows), 1)
        self.assertTrue(self.windows[0][2].bridge is self.bridge)

    def test_open_without_window_factory_is_soft(self):
        bridge = UiBridge(config_path="/tmp/drpilot_bridge_nowin.json", adb_factory=FakeDisplayAdb)
        result = bridge.open_display_tuner(make_payload())
        self.assertFalse(result["ok"])
        self.assertTrue(bridge._state["toasts"])

    def test_close_event_clears_reference(self):
        self.bridge.open_display_tuner(make_payload())
        window = self.bridge._windows["tuner"]
        window.events.closed.fire()
        self.assertNotIn("tuner", self.bridge._windows)

    def test_shutdown_destroys_tuner_window(self):
        self.bridge.open_display_tuner(make_payload())
        window = self.bridge._windows["tuner"]
        self.bridge.shutdown()
        self.assertTrue(window.destroyed)
        self.assertEqual(self.bridge._windows, {})

    # ---- 三个参数 ----
    def test_apply_sets_width_height_density(self):
        self.bridge.open_display_tuner(make_payload())
        api = self.windows[0][2]
        result = api.apply({"scale": 2.0, "width_scale": 1.5, "density": 420})
        self.assertTrue(result["ok"])
        self.assertTrue(wait_until(lambda: FakeDisplayAdb.override == (1890, 5440)))
        self.assertTrue(wait_until(lambda: FakeDisplayAdb.density_override == 420))
        # 主界面那三个隐藏字段要跟着变：开始提取才会用同一套设置
        patch = self.bridge.poll()["state"]["form_patch"]
        self.assertEqual(patch.get("display_scale"), 2.0)
        self.assertEqual(patch.get("display_width_scale"), 1.5)
        self.assertEqual(patch.get("display_density"), 420)

    def test_apply_rejects_garbage(self):
        self.bridge.open_display_tuner(make_payload())
        api = self.windows[0][2]
        result = api.apply({"scale": "abc"})
        self.assertFalse(result["ok"])
        self.assertIsNone(FakeDisplayAdb.override)

    def test_state_shape_for_the_page(self):
        self.bridge.open_display_tuner(make_payload(display_scale="2.0", display_density="420"))
        api = self.windows[0][2]
        state = api.ready()
        for key in ("physical", "override", "settings", "limits", "thumb_seq", "original_seq"):
            self.assertIn(key, state, key)
        self.assertEqual(state["settings"]["density"], 420)
        self.assertGreaterEqual(state["limits"]["max_scale"], 2.0)
        json.dumps(state, ensure_ascii=False)      # pywebview 就是这么传的

    def test_after_apply_state_reports_the_real_override(self):
        self.bridge.open_display_tuner(make_payload())
        api = self.windows[0][2]
        api.apply({"scale": 2.0, "width_scale": 1.5, "density": 0})
        self.assertTrue(wait_until(lambda: FakeDisplayAdb.override == (1890, 5440)))
        self.assertTrue(wait_until(lambda: bool(api.poll()["override"]["has_override"])))
        state = api.poll()
        self.assertEqual(state["override"]["width"], 1890)
        self.assertEqual(state["override"]["height"], 5440)

    def test_reset_from_tuner_clears_override(self):
        self.bridge.open_display_tuner(make_payload())
        api = self.windows[0][2]
        api.apply({"scale": 2.0, "density": 420})
        self.assertTrue(wait_until(lambda: FakeDisplayAdb.override == (1260, 5440)))
        api.reset()
        self.assertTrue(wait_until(lambda: FakeDisplayAdb.override is None))
        self.assertIsNone(FakeDisplayAdb.density_override)

    # ---- 「原本」截图 ----
    def test_original_is_popped_once(self):
        bridge = UiBridge(
            config_path="/tmp/drpilot_bridge_original.json",
            adb_factory=PngScreenshotAdb,
            open_window=lambda *args: FakeWindow(*args),
        )
        bridge.open_display_tuner(make_payload(display_mode=False, display_scale="1.0"))

        def original_seq() -> int:
            with bridge._lock:
                return int(bridge._state["display"].get("original_seq") or 0)

        self.assertTrue(wait_until(lambda: original_seq() > 0))
        first = bridge.pop_original()
        self.assertTrue(first["thumb"].startswith("data:image/jpeg;base64,"))
        self.assertEqual(first["size"], "1260x2720")
        self.assertEqual(bridge.pop_original()["thumb"], "")


KEY_ENV_NAMES = tuple(
    [f"SILICONFLOW_API_KEY_{i}" for i in range(1, 17)]
    + [f"DRPILOT_API_KEY_{i}" for i in range(1, 17)]
    + ["SILICONFLOW_API_KEY", "DRPILOT_API_KEY", "SILICONFLOW_API_KEYS", "DRPILOT_API_KEYS"]
)


class BridgeKeyTests(unittest.TestCase):
    """API Key 桥方法：只写临时目录里的 .env，绝不动仓库根 .env。"""

    KEY_A = "sk-aaaaaaaabbbbcccc1234"
    KEY_B = "sk-ddddddddeeeeffff5678"
    KEY_C = "sk-gggggggghhhhiiii9012"

    @classmethod
    def setUpClass(cls):
        cls.repo_env = env_file_path()
        cls.repo_env_before = cls.repo_env.read_bytes() if cls.repo_env.is_file() else None

    @classmethod
    def tearDownClass(cls):
        after = cls.repo_env.read_bytes() if cls.repo_env.is_file() else None
        if after != cls.repo_env_before:
            raise AssertionError("测试改写了仓库根 .env！")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env_path = os.path.join(self.tmp.name, ".env")
        self.bridge = UiBridge(
            config_path=os.path.join(self.tmp.name, "drpilot_config.json"),
            env_path=self.env_path,
            adb_factory=FakeAdb,
        )
        snapshot = {name: os.environ.get(name) for name in KEY_ENV_NAMES}
        self.addCleanup(self._restore_env, snapshot)

    @staticmethod
    def _restore_env(snapshot):
        for name, value in snapshot.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def tearDown(self):
        after = self.repo_env.read_bytes() if self.repo_env.is_file() else None
        self.assertEqual(after, self.repo_env_before, "测试改写了仓库根 .env！")

    # ---- 保存 ----
    def test_save_writes_env_file_and_masks_output(self):
        result = self.bridge.save_keys({"text": f"{self.KEY_A}\n{self.KEY_B}"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["path"], self.env_path)
        self.assertIn("****", result["masked"])
        for key in (self.KEY_A, self.KEY_B):
            self.assertNotIn(key, json.dumps(result, ensure_ascii=False))

        body = Path(self.env_path).read_text(encoding="utf-8")
        self.assertIn(f"SILICONFLOW_API_KEY_1={self.KEY_A}", body)
        self.assertIn(f"SILICONFLOW_API_KEY_2={self.KEY_B}", body)

    def test_save_updates_state_and_logs_only_masked(self):
        self.bridge.save_keys({"text": self.KEY_A})
        state = self.bridge._snapshot()          # 不走 poll()，避免把日志排空
        self.assertEqual(state["key_count"], 1)
        self.assertIn("已加载 1 个 API Key", state["keys"])
        self.assertIn("****", state["key_masked"])
        self.assertEqual([item["index"] for item in state["key_items"]], [1])
        self.assertIn("****", state["key_items"][0]["masked"])
        self.assertNotIn(self.KEY_A, json.dumps(state, ensure_ascii=False))
        self.assertNotIn(self.KEY_A, json.dumps(state["toasts"], ensure_ascii=False))
        logs = json.dumps(list(self.bridge._logs), ensure_ascii=False)
        self.assertIn("已保存 1 个 API Key", logs)
        self.assertNotIn(self.KEY_A, logs)

    def test_save_parses_multiple_separators_and_dedupes(self):
        text = f"{self.KEY_A}, {self.KEY_B};{self.KEY_C}\n{self.KEY_A}"
        result = self.bridge.save_keys({"text": text})
        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 3)
        body = Path(self.env_path).read_text(encoding="utf-8")
        self.assertEqual(body.count("SILICONFLOW_API_KEY_"), 3)

    def test_save_replaces_previous_keys(self):
        self.bridge.save_keys({"text": f"{self.KEY_A}\n{self.KEY_B}"})
        result = self.bridge.save_keys({"text": self.KEY_C})
        self.assertEqual(result["count"], 1)
        body = Path(self.env_path).read_text(encoding="utf-8")
        self.assertIn(self.KEY_C, body)
        self.assertNotIn(self.KEY_A, body)
        self.assertNotIn(self.KEY_B, body)

    def test_save_rejects_empty_and_invalid_without_writing(self):
        empty = self.bridge.save_keys({"text": "   \n , ; "})
        self.assertFalse(empty["ok"])
        self.assertIn("至少", empty["error"])
        self.assertFalse(Path(self.env_path).exists())

        too_short = self.bridge.save_keys({"text": "abc"})
        self.assertFalse(too_short["ok"])
        self.assertIn("无效", too_short["error"])
        self.assertNotIn("abc", json.dumps(list(self.bridge._logs), ensure_ascii=False))
        self.assertFalse(Path(self.env_path).exists())

        spaced = self.bridge.save_keys({"text": "sk-abcd efgh123456"})
        self.assertFalse(spaced["ok"])

    # ---- 新增（GUI「新增 API Key」按钮） ----
    def test_add_appends_without_touching_existing(self):
        self.bridge.save_keys({"text": self.KEY_A})
        result = self.bridge.add_keys({"text": self.KEY_B})
        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["added"], 1)
        self.assertEqual([item["index"] for item in result["items"]], [1, 2])
        body = Path(self.env_path).read_text(encoding="utf-8")
        self.assertIn(self.KEY_A, body)
        self.assertIn(self.KEY_B, body)

    def test_add_dedupes_existing_key(self):
        self.bridge.save_keys({"text": f"{self.KEY_A}\n{self.KEY_B}"})
        result = self.bridge.add_keys({"text": f"{self.KEY_A}\n{self.KEY_C}"})
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["count"], 3)

    def test_add_accepts_assignment_and_cjk_separators(self):
        result = self.bridge.add_keys({"text": f"SILICONFLOW_API_KEY_9={self.KEY_A}，{self.KEY_B}"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 2)

    def test_add_rejects_empty_and_invalid_without_writing(self):
        empty = self.bridge.add_keys({"text": "  "})
        self.assertFalse(empty["ok"])
        self.assertIn("填写", empty["error"])
        self.assertFalse(Path(self.env_path).exists())

        short = self.bridge.add_keys({"text": "abc"})
        self.assertFalse(short["ok"])
        self.assertNotIn("abc", json.dumps(list(self.bridge._logs), ensure_ascii=False))
        self.assertFalse(Path(self.env_path).exists())

    # ---- 删除（GUI 每个 Key 上的 ✕ 按钮） ----
    def test_delete_removes_only_target_key(self):
        self.bridge.save_keys({"text": f"{self.KEY_A}\n{self.KEY_B}\n{self.KEY_C}"})
        result = self.bridge.delete_key({"index": 2})
        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 2)
        self.assertIn("****", result["removed_key"])
        body = Path(self.env_path).read_text(encoding="utf-8")
        self.assertNotIn(self.KEY_B, body)
        self.assertIn(self.KEY_A, body)
        self.assertIn(self.KEY_C, body)

    def test_delete_last_key_clears_block_but_keeps_other_vars(self):
        Path(self.env_path).write_text(
            f"OTHER=keep\nSILICONFLOW_API_KEY_1={self.KEY_A}\n", encoding="utf-8"
        )
        result = self.bridge.delete_key({"index": 1})
        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["items"], [])
        body = Path(self.env_path).read_text(encoding="utf-8")
        self.assertIn("OTHER=keep", body)
        self.assertNotIn(self.KEY_A, body)

    def test_delete_never_returns_plaintext(self):
        self.bridge.save_keys({"text": self.KEY_A})
        payload = self.bridge.delete_key({"index": 1})
        self.assertNotIn(self.KEY_A, json.dumps(payload, ensure_ascii=False))
        self.assertNotIn(self.KEY_A, json.dumps(list(self.bridge._logs), ensure_ascii=False))
        state = self.bridge._snapshot()
        self.assertNotIn(self.KEY_A, json.dumps(state, ensure_ascii=False))

    def test_delete_out_of_range_is_soft_failure(self):
        self.bridge.save_keys({"text": self.KEY_A})
        result = self.bridge.delete_key({"index": 9})
        self.assertFalse(result["ok"])
        self.assertIn("不存在", result["error"])
        self.assertEqual(self.bridge._current_keys(), [self.KEY_A])

    def test_delete_without_index_is_soft_failure(self):
        self.assertFalse(self.bridge.delete_key({})["ok"])

    # ---- 显示 ----
    def test_reveal_returns_plaintext_only_when_asked(self):
        self.bridge.save_keys({"text": f"{self.KEY_A}\n{self.KEY_B}"})
        result = self.bridge.reveal_keys()
        self.assertTrue(result["ok"])
        self.assertEqual(result["keys"], [self.KEY_A, self.KEY_B])
        self.assertEqual(result["text"], f"{self.KEY_A}\n{self.KEY_B}")
        self.assertNotIn(self.KEY_A, json.dumps(self.bridge.poll()["state"], ensure_ascii=False))

    def test_reveal_without_keys_is_soft_failure(self):
        result = self.bridge.reveal_keys()
        self.assertFalse(result["ok"])
        self.assertEqual(result["keys"], [])
        self.assertEqual(result["text"], "")

    def test_env_path_isolates_from_repo_env(self):
        # 指定 env_path 时只读该文件，不会把仓库里真实的 Key 读进来
        self.assertEqual(self.bridge._current_keys(), [])
        self.bridge.save_keys({"text": self.KEY_A})
        self.assertEqual(self.bridge._current_keys(), [self.KEY_A])

    # ---- 清除 ----
    def test_clear_removes_keys_from_file_and_state(self):
        self.bridge.save_keys({"text": f"{self.KEY_A}\n{self.KEY_B}"})
        result = self.bridge.clear_keys()
        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 0)
        self.assertGreaterEqual(result["removed"], 2)
        body = Path(self.env_path).read_text(encoding="utf-8")
        self.assertNotIn(self.KEY_A, body)
        self.assertNotIn(self.KEY_B, body)
        state = self.bridge.poll()["state"]
        self.assertEqual(state["key_count"], 0)
        self.assertEqual(state["key_masked"], "")
        self.assertIn("未配置 API Key", state["keys"])

    def test_clear_keeps_other_env_variables(self):
        Path(self.env_path).write_text(
            f"OTHER_SETTING=keep-me\nSILICONFLOW_API_KEY_1={self.KEY_A}\n", encoding="utf-8"
        )
        self.assertTrue(self.bridge.clear_keys()["ok"])
        body = Path(self.env_path).read_text(encoding="utf-8")
        self.assertIn("OTHER_SETTING=keep-me", body)
        self.assertNotIn(self.KEY_A, body)

    def test_worker_hint_sees_new_key_count(self):
        """并发提示依赖 key_count：保存 / 清除后必须同步。"""
        self.assertEqual(self.bridge.poll()["state"]["key_count"], 0)
        self.bridge.save_keys({"text": f"{self.KEY_A}\n{self.KEY_B}"})
        self.assertEqual(self.bridge.poll()["state"]["key_count"], 2)
        self.bridge.clear_keys()
        self.assertEqual(self.bridge.poll()["state"]["key_count"], 0)

    def test_key_state_is_json_serializable(self):
        self.bridge.save_keys({"text": self.KEY_A})
        json.dumps(self.bridge.poll(), ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
