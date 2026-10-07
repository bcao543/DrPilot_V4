# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import base64
import json
import os
import tempfile
import threading
import time
import unittest

from drpilot.adb import DeviceInfo
from drpilot.ai import ModelCheckResult
from drpilot.config import AppConfig, resolve_output_paths
from drpilot.models import Question
from drpilot.pipeline import PilotSession


class FakeAdb:
    def __init__(self, duplicate_first: bool = False):
        self.counter = 0
        self.duplicate_first = duplicate_first
        self.swipes = 0
        self.last_swipe = None
        self.taps: list[tuple[int, int]] = []
        self._first = None

    def ensure_device(self):
        return DeviceInfo(serial="FAKE", state="device", model="FakePhone")

    def wm_size(self):
        return (1080, 2340)

    def screenshot(self):
        self.counter += 1
        if self.duplicate_first and self.counter == 2:
            return self._first
        data = b"IMG-%03d" % self.counter
        if self._first is None:
            self._first = data
        return data

    def swipe(self, x1, y1, x2, y2, duration):
        self.swipes += 1
        self.last_swipe = (x1, y1, x2, y2, duration)

    def tap(self, x, y):
        self.taps.append((x, y))


class FakeAi:
    """按顺序返回递增的 screen_id，可模拟预检和失败。"""

    def __init__(self, start: int = 1, fail: bool = False, ai_preflight=(1, 53)):
        self.next_id = start
        self.fail = fail
        self.ai_preflight = ai_preflight

    def complete(self, messages, meta=""):
        if meta == "preflight":
            if self.ai_preflight is None:
                raise RuntimeError("预检失败")
            screen_id, screen_total = self.ai_preflight
            return json.dumps({"screen_id": screen_id, "screen_total": screen_total})
        if self.fail:
            raise RuntimeError("模拟 AI 失败")
        count = sum(1 for part in messages[1]["content"] if part.get("type") == "image_url")
        ids = [self.next_id + index for index in range(count)]
        self.next_id += count
        items = [
            {
                "screen_id": number,
                "screen_total": 53,
                "type": "single",
                "stem": f"题干{number}",
                "options": {"A": "甲"},
                "answer": "A",
            }
            for number in ids
        ]
        return json.dumps(items, ensure_ascii=False)


class PipelineTests(unittest.TestCase):
    def _run(
        self,
        tmp,
        total=5,
        batch_size=2,
        page_from=1,
        fail=False,
        ai_start=None,
        ai_preflight=(1, 53),
        on_preflight=None,
        on_model_check=None,
        duplicate_first=False,
        config_overrides=None,
        model_ok=True,
    ):
        overrides = dict(config_overrides or {})
        config = AppConfig(
            page_from=page_from,
            page_to=page_from + total - 1,
            textbook="药理学",
            chapter="第二章 药物代谢动力学",
            chapter_total=53,
            output_dir=tmp,
            batch_size=batch_size,
            workers=1,
            wait=0.0,
            retry_wait=0.0,
            api_keys=["test-key"],
            **overrides,
        )
        config.normalize()
        adb = FakeAdb(duplicate_first=duplicate_first)
        start = ai_start if ai_start is not None else page_from
        session = PilotSession(
            config,
            adb=adb,
            ai_factory=lambda key, cfg: FakeAi(start=start, fail=fail, ai_preflight=ai_preflight),
            image_encoder=lambda data: base64.b64encode(data).decode("ascii"),
            on_log=lambda message: None,
            on_preflight=on_preflight,
            on_model_check=on_model_check,
            model_checker=lambda api_key, base_url, model, timeout=30.0: ModelCheckResult(
                ok=model_ok,
                model=model,
                base_url=base_url,
                detail="可以调用" if model_ok else "平台模型列表里已经没有该模型",
                latency_ms=12,
                model_listed=model_ok,
            ),
        )
        return session, session.run(), adb

    def _records(self, session):
        with open(session.output_paths["jsonl"], encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def test_ordered_output_metadata_and_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            session, ok, _ = self._run(tmp)
            self.assertTrue(ok)
            records = self._records(session)
            self.assertEqual([record["id"] for record in records], [1, 2, 3, 4, 5])
            self.assertEqual(records[0]["textbook"], "药理学")
            self.assertEqual(records[0]["chapter"], "第二章 药物代谢动力学")
            self.assertEqual(records[0]["chapter_no"], 2)
            self.assertEqual(records[0]["total"], 53)
            self.assertTrue(os.path.isfile(session.output_paths["markdown"]))
            self.assertTrue(os.path.isfile(session.output_paths["index"]))
            self.assertEqual(
                os.path.basename(session.output_paths["jsonl"]),
                "药理学_02_药物代谢动力学.jsonl",
            )

    def test_screen_id_is_authoritative(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 手机实际停在第 16 题，用户把起始题号填成 1，但用户选择继续
            session, ok, _ = self._run(
                tmp,
                total=3,
                page_from=1,
                ai_start=16,
                ai_preflight=(16, 53),
                on_preflight=lambda screen_id, expected: True,
            )
            self.assertTrue(ok)
            self.assertEqual([record["id"] for record in self._records(session)], [16, 17, 18])

    def test_preflight_mismatch_cancels(self):
        with tempfile.TemporaryDirectory() as tmp:
            session, ok, _ = self._run(
                tmp,
                ai_preflight=(47, 53),
                on_preflight=lambda screen_id, expected: False,
            )
            self.assertFalse(ok)
            self.assertIsNotNone(session.error)

    def test_preflight_disabled_still_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            session, ok, _ = self._run(
                tmp,
                total=3,
                ai_preflight=(47, 53),
                config_overrides={"preflight": False},
            )
            self.assertTrue(ok)
            self.assertEqual([record["id"] for record in self._records(session)], [1, 2, 3])

    def test_ai_failure_keeps_alignment(self):
        with tempfile.TemporaryDirectory() as tmp:
            session, ok, _ = self._run(tmp, fail=True)
            self.assertTrue(ok)
            self.assertEqual([question.id for question in session.questions], [1, 2, 3, 4, 5])
            self.assertTrue(all(question.is_placeholder for question in session.questions))

    def test_failures_are_reported_with_question_numbers(self):
        """批量失败后，Agent 必须能从 diagnostics 里拿到失败题号去续跑。"""
        with tempfile.TemporaryDirectory() as tmp:
            session, ok, _ = self._run(tmp, total=5, batch_size=2, fail=True)
            self.assertTrue(ok)                       # 占位题写出，不算整体失败
            diagnostics = session.diagnostics()
            self.assertEqual(diagnostics["failed_question_numbers"], [1, 2, 3, 4, 5])
            self.assertEqual(len(diagnostics["batch_failures"]), 3)
            self.assertEqual(diagnostics["error_context"]["phase"], "batch")
            self.assertIn("question_numbers", diagnostics["error_context"])
            self.assertEqual(diagnostics["error_context"]["error"], "模拟 AI 失败")
            self.assertEqual(diagnostics["phase"], "done")

    def test_phase_tracks_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            session, ok, _ = self._run(tmp, total=2)
            self.assertTrue(ok)
            self.assertEqual(session.phase, "done")
            self.assertEqual(session.diagnostics()["failed_question_numbers"], [])

    def test_duplicate_screenshot_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            session, ok, adb = self._run(tmp, duplicate_first=True)
            self.assertTrue(ok)
            self.assertEqual(len(session.questions), 5)
            self.assertGreaterEqual(adb.swipes, 5)

    def test_merge_re_extract_one_question(self):
        with tempfile.TemporaryDirectory() as tmp:
            session, ok, _ = self._run(tmp, total=3, page_from=1, ai_start=1)
            self.assertTrue(ok)
            session2, ok2, _ = self._run(tmp, total=1, page_from=2, ai_start=2, ai_preflight=(2, 53))
            self.assertTrue(ok2)
            records = self._records(session2)
            self.assertEqual([record["id"] for record in records], [1, 2, 3])
            self.assertEqual(records[1]["stem"], "题干2")

    def test_reference_auto_recorded_on_first_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            session, ok, _ = self._run(tmp, total=2)
            self.assertTrue(ok)
            self.assertTrue(session.reference_recorded)
            self.assertEqual(session.config.swipe_reference_width, 1080)
            self.assertEqual(session.config.swipe_reference_height, 2340)

    def test_swipe_scaled_to_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            session, ok, adb = self._run(
                tmp,
                total=2,
                config_overrides={
                    "swipe_reference_width": 540,
                    "swipe_reference_height": 1170,
                    "swipe_start_x": 530,
                    "swipe_start_y": 276,
                    "swipe_end_x": 135,
                    "swipe_end_y": 275,
                },
            )
            self.assertTrue(ok)
            self.assertFalse(session.reference_recorded)
            self.assertEqual(adb.last_swipe, (1060, 552, 270, 550, 150))

    def test_recorded_tap_action_is_replayed(self):
        with tempfile.TemporaryDirectory() as tmp:
            session, ok, adb = self._run(
                tmp,
                total=2,
                config_overrides={
                    "next_action": [{"kind": "tap", "x": 100, "y": 200, "duration_ms": 40}]
                },
            )
            self.assertTrue(ok)
            self.assertEqual(len(session.questions), 2)
            self.assertTrue(adb.taps, "应回放录制的点按动作")
            self.assertEqual(adb.last_swipe, None)

    def test_recorded_swipe_scales_to_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            session, ok, adb = self._run(
                tmp,
                total=2,
                config_overrides={
                    "next_action": [
                        {"kind": "swipe", "x": 530, "y": 276, "x2": 135, "y2": 275, "duration_ms": 150}
                    ],
                    "swipe_reference_width": 540,
                    "swipe_reference_height": 1170,
                },
            )
            self.assertTrue(ok)
            self.assertFalse(session.reference_recorded)
            self.assertEqual(adb.last_swipe, (1060, 552, 270, 550, 150))

    def test_duplicate_retry_replays_recorded_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            session, ok, adb = self._run(
                tmp,
                duplicate_first=True,
                config_overrides={
                    "next_action": [{"kind": "tap", "x": 10, "y": 20, "duration_ms": 30}]
                },
            )
            self.assertTrue(ok)
            self.assertEqual(len(session.questions), 5)
            self.assertGreaterEqual(len(adb.taps), 2)

    def test_no_markdown_no_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            session, ok, _ = self._run(
                tmp,
                config_overrides={"generate_markdown": False, "generate_index": False},
            )
            self.assertTrue(ok)
            self.assertIsNone(session.output_paths["markdown"])
            self.assertIsNone(session.output_paths["index"])

    def test_model_check_failure_aborts_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            session, ok, adb = self._run(tmp, model_ok=False)
            self.assertFalse(ok)
            self.assertIsNotNone(session.error)
            self.assertIn("模型", str(session.error))
            self.assertEqual(adb.swipes, 0)
            self.assertIsNotNone(session.model_check)
            self.assertFalse(session.model_check.ok)

    def test_model_check_failure_can_continue(self):
        with tempfile.TemporaryDirectory() as tmp:
            session, ok, _ = self._run(tmp, model_ok=False, on_model_check=lambda result: True)
            self.assertTrue(ok)
            self.assertEqual(len(session.questions), 5)
            self.assertFalse(session.model_check.ok)

    def test_model_check_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            session, ok, _ = self._run(
                tmp, config_overrides={"check_model": False}, model_ok=False
            )
            self.assertTrue(ok)
            self.assertIsNone(session.model_check)

    def test_device_address_triggers_connect(self):
        class WirelessAdb(FakeAdb):
            def __init__(self):
                super().__init__()
                self.connected = []

            def connect(self, address, timeout=None):
                self.connected.append(address)
                return "connected to " + address

        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig(
                page_from=1,
                page_to=1,
                output_dir=tmp,
                workers=1,
                wait=0.0,
                api_keys=["test-key"],
                device_address="192.168.1.5:5555",
                check_model=False,
            )
            config.normalize()
            adb = WirelessAdb()
            session = PilotSession(
                config,
                adb=adb,
                ai_factory=lambda key, cfg: FakeAi(),
                image_encoder=lambda data: base64.b64encode(data).decode("ascii"),
                model_checker=lambda *a, **k: ModelCheckResult(True, "m", "u", "ok"),
            )
            self.assertTrue(session.run())
            self.assertEqual(adb.connected, ["192.168.1.5:5555"])

    def test_workers_can_exceed_api_keys(self):
        """并发按用户设置来，Key 不够时轮换共用（以前会被静默压到 Key 数量）。"""
        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig(
                page_from=1,
                page_to=6,
                output_dir=tmp,
                workers=3,
                batch_size=1,
                wait=0.0,
                api_keys=["key-1", "key-2"],
                check_model=False,
            )
            config.normalize()
            logs: list[str] = []
            started: list[int] = []
            session = PilotSession(
                config,
                adb=FakeAdb(),
                ai_factory=lambda key, cfg: FakeAi(),
                image_encoder=lambda data: base64.b64encode(data).decode("ascii"),
                on_log=logs.append,
                on_worker=lambda worker_id, status: started.append(worker_id),
            )
            self.assertTrue(session.run())
            self.assertEqual(sorted(set(started)), [1, 2, 3])
            self.assertTrue(any("并发 3" in line for line in logs))
            self.assertTrue(any("共用同一个 Key" in line for line in logs))

    def test_later_batches_are_written_while_first_is_stuck(self):
        """0 号批次卡住（重试中）时，后面已完成的批次也必须落盘。

        曾经这里会因为「批次顺序出现缺口」而一条都不写，用户看到 worker 在干活、
        输出文件夹却是空的。这里按 meta 里的批次号卡住 0 号批次，
        保证无论哪个 worker 抢到它，缺口都稳定复现。
        """
        release = threading.Event()
        in_flight = threading.Event()

        class BlockFirstBatchAi(FakeAi):
            def complete(self, messages, meta=""):
                if meta.endswith("batch0"):
                    in_flight.set()
                    # 故意等很久：模拟“0 号批次一直在重试”，由测试在 finally 里放行
                    release.wait(timeout=30)
                return super().complete(messages, meta)

        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig(
                page_from=1,
                page_to=6,
                output_dir=tmp,
                batch_size=1,
                workers=2,
                wait=0.0,
                api_keys=["k1", "k2"],
                check_model=False,
                preflight=False,
            )
            config.normalize()
            logs: list[str] = []
            session = PilotSession(
                config,
                adb=FakeAdb(),
                ai_factory=lambda key, cfg: BlockFirstBatchAi(),
                image_encoder=lambda data: base64.b64encode(data).decode("ascii"),
                on_log=logs.append,
            )
            thread = threading.Thread(target=session.run)
            thread.start()
            try:
                self.assertTrue(in_flight.wait(5), "0 号批次没有进入处理")
                jsonl = resolve_output_paths(config).jsonl
                deadline = time.time() + 3
                while time.time() < deadline and not os.path.isfile(jsonl):
                    time.sleep(0.05)
                self.assertTrue(
                    os.path.isfile(jsonl),
                    "0 号批次卡住时，后面已完成的批次也应该写入磁盘",
                )
                with open(jsonl, encoding="utf-8") as handle:
                    written = [json.loads(line) for line in handle if line.strip()]
                self.assertTrue(written)
            finally:
                release.set()
                thread.join(timeout=10)
            self.assertFalse(thread.is_alive())

    def test_stop_drops_pending_batches(self):
        """点停止后：不再处理排队中的批次，只等已开工的收尾，运行要能很快结束。"""
        release = threading.Event()
        started = threading.Event()

        class SlowAi(FakeAi):
            def complete(self, messages, meta=""):
                if meta != "preflight":
                    started.set()
                    release.wait(timeout=5)
                return super().complete(messages, meta)

        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig(
                page_from=1,
                page_to=10,
                output_dir=tmp,
                batch_size=1,
                workers=1,
                wait=0.0,
                api_keys=["test-key"],
                check_model=False,
                preflight=False,
            )
            config.normalize()
            logs: list[str] = []
            session = PilotSession(
                config,
                adb=FakeAdb(),
                ai_factory=lambda key, cfg: SlowAi(),
                image_encoder=lambda data: base64.b64encode(data).decode("ascii"),
                on_log=logs.append,
            )
            thread = threading.Thread(target=session.run)
            thread.start()
            self.assertTrue(started.wait(5), "第一个批次没有开跑")
            time.sleep(0.3)          # 让截图循环把剩余批次排进队列
            session.stop()
            session.stop()           # 重复停止不应有副作用
            release.set()
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive(), "停止后运行没有结束")
            self.assertTrue(any("丢弃" in line for line in logs), logs)
            self.assertLess(len(session.questions), 10)
            self.assertIsNone(session.error)

    def test_validation_requires_keys(self):
        config = AppConfig(page_from=1, page_to=2, output_dir=tempfile.gettempdir())
        session = PilotSession(config, adb=FakeAdb(), ai_factory=lambda key, cfg: FakeAi())
        self.assertFalse(session.run())
        self.assertIsNotNone(session.error)


class ModuleFakeAi(FakeAi):
    """会按模块返回正文的假 AI，并记录收到的系统提示词。"""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.system_prompts: list[str] = []

    def complete(self, messages, meta=""):
        text = super().complete(messages, meta)
        self.system_prompts.append(str(messages[0]["content"]))
        if meta == "preflight":
            return text
        items = json.loads(text)
        for item in items:
            item["考点还原"] = f"考点还原正文{item['screen_id']}"
            item["标准解析"] = f"解析正文{item['screen_id']}"
        return json.dumps(items, ensure_ascii=False)


class DisplayFakeAdb(FakeAdb):
    """FakeAdb + 加长主屏需要的 wm 命令，行为照抄真机（过大尺寸静默忽略）。"""

    def __init__(self, *, max_height: int = 4680, **kwargs) -> None:
        super().__init__(**kwargs)
        self.physical = (1080, 2340)
        self.override: tuple[int, int] | None = None
        self.density_override: int | None = None
        self.scaling_off = False
        self.max_height = max_height
        self.display_calls: list[tuple] = []
        self.shots = 0

    # ---- 给 DisplayController 用 ----
    def run(self, args, check: bool = True, **kwargs):
        args = tuple(args)
        if args == ("shell", "wm", "size"):
            lines = [f"Physical size: {self.physical[0]}x{self.physical[1]}"]
            if self.override:
                lines.append(f"Override size: {self.override[0]}x{self.override[1]}")
            return "\n".join(lines)
        if args == ("shell", "wm", "density"):
            lines = ["Physical density: 520"]
            if self.density_override:
                lines.append(f"Override density: {self.density_override}")
            return "\n".join(lines)
        if args == ("shell", "dumpsys", "window", "displays"):
            return "noscale" if self.scaling_off else ""
        return ""

    def wm_size(self):
        return self.override or self.physical

    def set_wm_size(self, width, height):
        self.display_calls.append(("set_wm_size", int(width), int(height)))
        if int(height) <= self.max_height:
            self.override = (int(width), int(height))
        return ""

    def reset_wm_size(self):
        self.display_calls.append(("reset_wm_size",))
        self.override = None

    def set_wm_density(self, density):
        self.display_calls.append(("set_wm_density", int(density)))
        self.density_override = int(density)

    def reset_wm_density(self):
        self.display_calls.append(("reset_wm_density",))
        self.density_override = None

    def set_wm_scaling(self, mode):
        self.display_calls.append(("set_wm_scaling", str(mode)))
        self.scaling_off = str(mode) == "off"

    def screenshot(self):
        # 第 2~6 张给「加长屏等稳定」用：连续相同即视为稳定，测试不用真等 3 秒；
        # 之后恢复成每张都不同的画面，避免触发「重复截图」重试逻辑
        self.shots += 1
        if 2 <= self.shots <= 3:
            return b"SETTLE"
        self.counter += 1
        return b"IMG-%03d" % self.counter

    def named(self, name: str) -> list[tuple]:
        return [call for call in self.display_calls if call[0] == name]


class ModuleAndDisplayTests(unittest.TestCase):
    """提取模块 + 加长主屏的端到端行为。"""

    def setUp(self):
        # 真机上加长屏要等约 3s 画面稳定；单测把等待下限与轮询间隔压到近乎 0
        from drpilot import display as display_module

        self._patched = (
            display_module.MIN_SETTLE_SECONDS,
            display_module.SETTLE_INTERVAL,
            display_module.DEFAULT_SETTLE_TIMEOUT,
        )
        display_module.MIN_SETTLE_SECONDS = 0.0
        display_module.SETTLE_INTERVAL = 0.01
        # 设备拒绝某个高度时不会 relayout，画面永远不「稳定」：上限必须压小
        display_module.DEFAULT_SETTLE_TIMEOUT = 0.05

    def tearDown(self):
        from drpilot import display as display_module

        (
            display_module.MIN_SETTLE_SECONDS,
            display_module.SETTLE_INTERVAL,
            display_module.DEFAULT_SETTLE_TIMEOUT,
        ) = self._patched

    def _run(self, tmp, *, total=3, config_overrides=None, ai_factory=None, adb=None):
        config = AppConfig(
            page_from=1,
            page_to=total,
            textbook="生物化学与分子生物学",
            chapter="第三章 核酸的结构与功能",
            chapter_total=211,
            output_dir=tmp,
            batch_size=2,
            workers=1,
            wait=0.0,
            retry_wait=0.0,
            api_keys=["test-key"],
            **(config_overrides or {}),
        )
        config.normalize()
        prompts: list[str] = []
        factory = ai_factory or (lambda key, cfg: ModuleFakeAi(start=1))

        session = PilotSession(
            config,
            adb=adb if adb is not None else FakeAdb(),
            ai_factory=factory,
            image_encoder=lambda data: base64.b64encode(data).decode("ascii"),
            on_log=lambda message: None,
            model_checker=lambda *a, **k: ModelCheckResult(True, "m", "u", "ok"),
        )
        return session, session.run(), prompts

    def _records(self, session):
        with open(session.output_paths["jsonl"], encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def test_modules_land_in_prompt_jsonl_and_markdown(self):
        from drpilot.modules import modules_to_payload, parse_modules_text

        specs = parse_modules_text("考点还原,标准解析")
        with tempfile.TemporaryDirectory() as tmp:
            session, ok, _ = self._run(
                tmp,
                config_overrides={"modules": modules_to_payload(specs)},
            )
            self.assertTrue(ok)
            records = self._records(session)
            self.assertEqual(len(records), 3)
            for record in records:
                self.assertEqual(
                    record["modules"],
                    {
                        "考点还原": f"考点还原正文{record['id']}",
                        "标准解析": f"解析正文{record['id']}",
                    },
                )
            with open(session.output_paths["markdown"], encoding="utf-8") as handle:
                markdown = handle.read()
            self.assertIn("### **考点还原**", markdown)
            self.assertIn("### **标准解析**", markdown)
            self.assertNotIn("---", markdown)
            prompt = session.system_prompt
            self.assertIn("考点还原", prompt)
            self.assertIn("严禁编造", prompt)
            self.assertEqual(session.diagnostics()["modules"], ["考点还原", "标准解析"])

    def test_without_modules_output_has_no_modules_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            session, ok, _ = self._run(tmp)
            self.assertTrue(ok)
            for record in self._records(session):
                self.assertNotIn("modules", record)
            self.assertNotIn("考点还原", session.system_prompt)

    def test_required_module_missing_is_reported(self):
        from drpilot.modules import modules_to_payload, parse_modules_text

        specs = parse_modules_text("考点还原")
        payload = modules_to_payload(specs)
        payload[0]["required"] = True

        class NoModuleAi(FakeAi):
            def complete(self, messages, meta=""):
                text = super().complete(messages, meta)
                if meta == "preflight":
                    return text
                items = json.loads(text)
                for item in items:
                    item.pop("考点还原", None)
                return json.dumps(items, ensure_ascii=False)

        with tempfile.TemporaryDirectory() as tmp:
            session, ok, _ = self._run(
                tmp,
                config_overrides={"modules": payload},
                ai_factory=lambda key, cfg: NoModuleAi(start=1),
            )
            self.assertTrue(ok)
            self.assertEqual(session.diagnostics()["missing_modules"], ["考点还原"])

    def test_display_wide_applies_and_restores_and_keeps_swipe_scale(self):
        with tempfile.TemporaryDirectory() as tmp:
            adb = DisplayFakeAdb()
            session, ok, _ = self._run(
                tmp,
                config_overrides={"display_mode": "wide", "display_scale": 2.0},
                adb=adb,
            )
            self.assertTrue(ok)
            self.assertEqual(adb.named("set_wm_size"), [("set_wm_size", 1080, 4680)])
            self.assertEqual(len(adb.named("reset_wm_size")), 1)
            self.assertIsNone(adb.override)
            result = session.diagnostics()["display"]
            self.assertTrue(result["verified"])
            self.assertEqual(result["actual"], "1080x4680")
            # 关键：加长屏下翻页坐标仍按物理分辨率换算，y 不会被放大 2 倍
            self.assertEqual(adb.last_swipe, (1060, 553, 270, 551, 150))

    def test_display_wide_restores_even_when_run_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            adb = DisplayFakeAdb()
            session, ok, _ = self._run(
                tmp,
                config_overrides={"display_mode": "wide", "display_scale": 2.0},
                adb=adb,
            )
            self.assertTrue(ok)
            self.assertIsNone(adb.override)

    def test_display_density_only_without_stretching(self):
        """倍数 1.0 + 密度 420：只改密度，wm size 一动不动，跑完照样复位。"""
        with tempfile.TemporaryDirectory() as tmp:
            adb = DisplayFakeAdb()
            session, ok, _ = self._run(
                tmp,
                config_overrides={
                    "display_mode": "wide", "display_scale": 1.0, "display_density": 420
                },
                adb=adb,
            )
            self.assertTrue(ok)
            self.assertEqual(adb.named("set_wm_size"), [])
            self.assertEqual(adb.named("set_wm_density"), [("set_wm_density", 420)])
            self.assertEqual(len(adb.named("reset_wm_density")), 1)
            result = session.diagnostics()["display"]
            self.assertTrue(result["verified"])
            self.assertTrue(result["density_only"])

    def test_display_off_never_touches_device(self):
        with tempfile.TemporaryDirectory() as tmp:
            adb = DisplayFakeAdb()
            _, ok, _ = self._run(tmp, adb=adb)
            self.assertTrue(ok)
            self.assertEqual(adb.display_calls, [])

    def test_display_falls_back_when_device_rejects(self):
        with tempfile.TemporaryDirectory() as tmp:
            adb = DisplayFakeAdb(max_height=3510)      # 2.0 被拒，1.5 通过
            session, ok, _ = self._run(
                tmp,
                config_overrides={"display_mode": "wide", "display_scale": 2.0},
                adb=adb,
            )
            self.assertTrue(ok)
            result = session.diagnostics()["display"]
            self.assertTrue(result["verified"])
            self.assertEqual(result["actual"], "1080x3510")


class RecoverMissingTests(unittest.TestCase):
    """模型回复不完整（多为 max_tokens 截断）时逐张重问，别把整批记成失败。"""

    def _session(self, logs: list[str]) -> PilotSession:
        config = AppConfig(
            page_from=1, page_to=3, output_dir=".", batch_size=3, workers=1,
            api_keys=["test-key"],
        )
        config.normalize()
        return PilotSession(config, on_log=logs.append)

    def test_missing_slots_are_reasked_one_by_one(self):
        logs: list[str] = []
        session = self._session(logs)
        asked: list[str] = []

        class OneShot:
            def complete(self, messages, meta=""):
                asked.append(meta)
                number = 2 if len(asked) == 1 else 3
                return json.dumps(
                    [{"screen_id": number, "type": "single", "stem": f"题干{number}",
                      "options": {"A": "甲"}, "answer": "A"}],
                    ensure_ascii=False,
                )

        items = [("enc1", 1), ("enc2", 2), ("enc3", 3)]
        parsed: list[Question | None] = [Question(id=1, stem="题干1", answer="A"), None, None]
        out = session._recover_missing(parsed, items, [1, 2, 3], 1, 0, OneShot())
        # 题号仍由 _assign_questions 按「截图题号优先」落定（和 worker 里的顺序一致）
        assigned = session._assign_questions(out, [1, 2, 3], 1, 0)
        self.assertEqual([q.id for q in assigned], [1, 2, 3])
        self.assertEqual(len(asked), 2)                      # 缺的两题各重问一次
        self.assertEqual(session.diagnostics()["failed_question_numbers"], [])
        self.assertTrue(any("逐张重问" in line for line in logs))

    def test_still_missing_is_recorded_as_failure(self):
        logs: list[str] = []
        session = self._session(logs)

        class Broken:
            def complete(self, messages, meta=""):
                raise RuntimeError("模拟调用失败")

        out = session._recover_missing([None, None], [("e1", 1), ("e2", 2)], [1, 2], 1, 0, Broken())
        self.assertEqual(out, [None, None])
        diag = session.diagnostics()
        self.assertEqual(diag["failed_question_numbers"], [1, 2])
        self.assertTrue(diag["batch_failures"])
        self.assertTrue(any("仍未识别" in line for line in logs))


class StalledCaptureTests(unittest.TestCase):
    """翻页失效（画面一直不变）时必须停下来，不能把同一张图当成下一题写进去。"""

    class StuckAdb(FakeAdb):
        def screenshot(self):
            return b"SAME-FRAME"

    def test_stalled_paging_stops_capture_and_records_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs: list[str] = []
            config = AppConfig(
                page_from=1, page_to=5, textbook="书", chapter="球菌", chapter_total=9,
                output_dir=tmp, batch_size=2, workers=1, wait=0.0, retry_wait=0.0,
                max_swipe_retries=2, api_keys=["test-key"], preflight=False,
                check_model=False,
            )
            config.normalize()
            session = PilotSession(
                config,
                adb=self.StuckAdb(),
                ai_factory=lambda key, cfg: FakeAi(start=1, ai_preflight=(1, 9)),
                image_encoder=lambda data: base64.b64encode(data).decode("ascii"),
                on_log=logs.append,
            )
            self.assertTrue(session.run())
            self.assertTrue(session.capture_stalled)
            with open(session.output_paths["jsonl"], encoding="utf-8") as handle:
                records = [json.loads(line) for line in handle if line.strip()]
            self.assertEqual([record["id"] for record in records], [1])   # 没有伪造第 2 题
            diag = session.diagnostics()
            self.assertTrue(diag["capture_stalled"])
            self.assertEqual(diag["failed_question_numbers"], [2])
            self.assertTrue(any("翻页失败" in line for line in logs))


if __name__ == "__main__":
    unittest.main()
