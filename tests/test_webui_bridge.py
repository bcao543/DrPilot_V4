# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from drpilot.actions import RecordResult, Step
from drpilot.adb import DeviceInfo
from drpilot.ai import ModelCheckResult
from drpilot.errors import AdbError
from drpilot.keys import env_file_path
from drpilot.webui.bridge import (
    UiBridge,
    address_from_payload,
    config_from_payload,
    log_tone,
)


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

    def test_manual_tap_fallback_from_payload(self):
        config = config_from_payload(
            make_payload(next_action="", action_tap_x="12", action_tap_y="34")
        )
        self.assertEqual(
            [step["kind"] for step in config.next_action],
            ["tap"],
        )
        self.assertEqual((config.next_action[0]["x"], config.next_action[0]["y"]), (12, 34))
        self.assertTrue(config.next_action[0]["absolute"])   # 手动坐标是当前设备像素，不缩放

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
        self.bridge.clear_action()
        self.assertEqual(self.bridge._state["action"]["steps"], [])
        result = self.bridge.use_tap(
            make_payload(next_action="", action_tap_x="12", action_tap_y="34")
        )
        self.assertTrue(result["ok"])
        steps = self.bridge._state["action"]["steps"]
        self.assertEqual((steps[0]["x"], steps[0]["y"]), (12, 34))
        self.assertTrue(steps[0]["absolute"])
        patch = self.bridge.poll()["state"]["form_patch"]
        self.assertIn("next_action", patch)

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


class FrontendContractTests(unittest.TestCase):
    """前端 app.js 依赖的字段与方法，必须真的存在（防止改名后前端悄悄失效）。"""

    FRONTEND_STATE_KEYS = [
        "device", "model", "keys", "key_masked", "key_count", "key_items",
        "progress", "running", "stopping", "model_checking",
        "workers", "question", "toasts", "form_patch", "total_hint", "output_preview",
        "recording", "action",
    ]
    FRONTEND_API = [
        "ready", "poll", "preview", "start", "stop", "test_model",
        "connect_device", "disconnect_device", "refresh_devices",
        "pick_output_dir", "open_output_dir", "set_reference", "test_swipe",
        "start_record", "stop_record", "test_action", "clear_action", "use_tap",
        "answer_question", "exit",
        "save_keys", "reveal_keys", "clear_keys", "add_keys", "delete_key",
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
