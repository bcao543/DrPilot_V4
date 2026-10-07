# -*- coding: utf-8 -*-
from __future__ import annotations

import base64
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import unittest

from drpilot import display as display_module
from drpilot.display import (
    DisplayController,
    parse_density_lines,
    parse_scaling_disabled,
    parse_size_lines,
)
from drpilot.images import image_size, to_thumb_data_url


class FakeDisplayAdb:
    """模拟一台会用 wm size/density/scaling 的手机。

    关键行为照抄真机：
        * 超过 max_height 的覆盖会被**静默忽略**（回读还是旧值）；
        * 设置成功后 wm size 会多出 Override size 一行；
        * 截图在前两次调用里有变化，之后稳定（模拟 relayout）。
    """

    def __init__(self, *, max_height: int = 5440, settle_after: int = 2) -> None:
        self.physical = (1260, 2720)
        self.density = 520
        self.override: tuple[int, int] | None = None
        self.density_override: int | None = None
        self.scaling_off = False
        self.max_height = max_height
        self.settle_after = settle_after
        self.calls: list[tuple] = []
        self.shots = 0

    # ---- 命令 ----
    def run(self, args, check: bool = True, **kwargs):
        self.calls.append(tuple(args))
        if tuple(args) == ("shell", "wm", "size"):
            return self.size_output()
        if tuple(args) == ("shell", "wm", "density"):
            return self.density_output()
        if tuple(args) == ("shell", "dumpsys", "window", "displays"):
            return "init=1260x2720 520dpi noscale" if self.scaling_off else "init=1260x2720 520dpi"
        if tuple(args)[:3] == ("shell", "settings", "get"):
            return ""
        return ""

    def size_output(self) -> str:
        lines = [f"Physical size: {self.physical[0]}x{self.physical[1]}"]
        if self.override:
            lines.append(f"Override size: {self.override[0]}x{self.override[1]}")
        return "\n".join(lines)

    def density_output(self) -> str:
        lines = [f"Physical density: {self.density}"]
        if self.density_override:
            lines.append(f"Override density: {self.density_override}")
        return "\n".join(lines)

    def set_wm_size(self, width, height):
        self.calls.append(("set_wm_size", int(width), int(height)))
        if int(height) <= self.max_height:
            self.override = (int(width), int(height))
        return ""

    def reset_wm_size(self):
        self.calls.append(("reset_wm_size",))
        self.override = None

    def set_wm_density(self, density):
        self.calls.append(("set_wm_density", int(density)))
        self.density_override = int(density)

    def reset_wm_density(self):
        self.calls.append(("reset_wm_density",))
        self.density_override = None

    def set_wm_scaling(self, mode):
        self.calls.append(("set_wm_scaling", str(mode)))
        self.scaling_off = str(mode) == "off"

    def screenshot(self) -> bytes:
        self.shots += 1
        stage = 0 if self.shots < self.settle_after else 1
        return f"IMG-{stage}".encode("ascii")

    # ---- 断言辅助 ----
    def count(self, name: str) -> int:
        return sum(1 for call in self.calls if call and call[0] == name)


class ParserTests(unittest.TestCase):
    def test_parse_size_lines(self):
        sizes = parse_size_lines("Physical size: 1260x2720\nOverride size: 1260x5440")
        self.assertEqual(sizes["physical"], (1260, 2720))
        self.assertEqual(sizes["override"], (1260, 5440))

    def test_parse_density_lines(self):
        found = parse_density_lines("Physical density: 520\nOverride density: 420")
        self.assertEqual(found, {"physical": 520, "override": 420})

    def test_parse_scaling_disabled(self):
        self.assertTrue(parse_scaling_disabled("base=1260x4080 420dpi noscale cur=1260x4080"))
        self.assertFalse(parse_scaling_disabled("base=1260x4080 520dpi cur=1260x4080"))


class ControllerTests(unittest.TestCase):
    def setUp(self):
        # 真机上要等 ~3s 画面稳定，单测里把下限调成 0，避免每次跑测试都睡觉
        self._min = display_module.MIN_SETTLE_SECONDS
        display_module.MIN_SETTLE_SECONDS = 0.0

    def tearDown(self):
        display_module.MIN_SETTLE_SECONDS = self._min

    def test_read_state(self):
        adb = FakeDisplayAdb()
        adb.override = (1260, 4080)
        state = DisplayController(adb).read_state()
        self.assertEqual(state.physical, (1260, 2720))
        self.assertEqual(state.override, (1260, 4080))
        self.assertEqual(state.effective, (1260, 4080))
        self.assertTrue(state.has_override)
        self.assertIn("覆盖", state.describe())

    def test_apply_doubles_height_and_verifies(self):
        adb = FakeDisplayAdb()
        result = DisplayController(adb).apply(scale=2.0, screenshot=adb.screenshot)
        self.assertTrue(result.verified)
        self.assertEqual(result.requested, (1260, 5440))
        self.assertEqual(result.actual, (1260, 5440))
        self.assertEqual(adb.override, (1260, 5440))

    def test_apply_falls_back_when_device_rejects(self):
        adb = FakeDisplayAdb(max_height=4080)      # 2.0/1.75 会被静默忽略
        controller = DisplayController(adb)
        result = controller.apply(scale=2.0, screenshot=adb.screenshot)
        self.assertTrue(result.verified)
        self.assertEqual(result.actual, (1260, 4080))
        self.assertIn("1.5", result.note)
        self.assertEqual([item["scale"] for item in result.attempts], [2.0, 1.75, 1.5])

    def test_apply_reports_failure_and_leaves_no_override(self):
        adb = FakeDisplayAdb(max_height=2720)      # 全部候选都拒绝
        controller = DisplayController(adb)
        result = controller.apply(scale=2.0, screenshot=adb.screenshot)
        self.assertFalse(result.verified)
        self.assertIsNone(adb.override)
        self.assertIn("拒绝", result.note)
        self.assertIn("未生效", result.describe())

    def test_apply_widens_the_logical_screen(self):
        """只加宽：高不变，App 按更宽的画布重排版（实测 1800x4080 会重排并多截内容）。"""
        adb = FakeDisplayAdb()
        result = DisplayController(adb).apply(
            scale=1.0, width_scale=1.5, screenshot=adb.screenshot
        )
        self.assertTrue(result.verified)
        self.assertEqual(result.requested, (1890, 2720))
        self.assertEqual(adb.override, (1890, 2720))
        self.assertEqual(result.width_scale, 1.5)
        self.assertAlmostEqual(result.as_dict()["width_scale"], 1.5)

    def test_apply_widens_and_stretches_together(self):
        adb = FakeDisplayAdb()
        result = DisplayController(adb).apply(
            scale=2.0, width_scale=1.5, screenshot=adb.screenshot
        )
        self.assertTrue(result.verified)
        self.assertEqual(adb.override, (1890, 5440))
        self.assertEqual(result.attempts[0]["width_scale"], 1.5)

    def test_width_only_request_has_no_ladder_when_accepted(self):
        adb = FakeDisplayAdb()
        result = DisplayController(adb).apply(scale=1.0, width_scale=1.2)
        self.assertEqual([item["width"] for item in result.attempts], [1512])

    def test_density_and_scaling_applied_together(self):
        adb = FakeDisplayAdb()
        controller = DisplayController(adb)
        controller.apply(scale=2.0, density=420, scaling="off", screenshot=adb.screenshot)
        self.assertEqual(adb.density_override, 420)
        self.assertTrue(adb.scaling_off)

    def test_restore_after_apply_resets_everything(self):
        adb = FakeDisplayAdb()
        controller = DisplayController(adb)
        controller.apply(scale=2.0, density=420, scaling="off", screenshot=adb.screenshot)
        self.assertTrue(controller.restore())
        self.assertIsNone(adb.override)
        self.assertIsNone(adb.density_override)
        self.assertFalse(adb.scaling_off)
        self.assertEqual(adb.count("reset_wm_size"), 1)

    def test_restore_without_apply_does_not_touch_device(self):
        adb = FakeDisplayAdb()
        controller = DisplayController(adb)
        self.assertTrue(controller.restore())
        self.assertEqual([call for call in adb.calls if call[0].startswith(("reset", "set_"))], [])

    def test_reset_all_is_unconditional(self):
        adb = FakeDisplayAdb()
        adb.override = (1260, 4080)
        adb.density_override = 420
        state = DisplayController(adb).reset_all()
        self.assertFalse(state.has_override)
        self.assertEqual(adb.count("reset_wm_size"), 1)
        self.assertEqual(adb.count("reset_wm_density"), 1)

    def test_apply_without_physical_size_fails_cleanly(self):
        class NoSize(FakeDisplayAdb):
            def size_output(self):
                return ""

        result = DisplayController(NoSize()).apply(scale=2.0)
        self.assertFalse(result.verified)
        self.assertIn("物理分辨率", result.note)

    def test_probe_always_restores(self):
        adb = FakeDisplayAdb()
        result, before, after = DisplayController(adb).probe(scale=2.0)
        self.assertTrue(result.verified)
        self.assertTrue(before and after)
        self.assertIsNone(adb.override)

    def test_probe_restores_even_when_apply_raises(self):
        class Boom(FakeDisplayAdb):
            def set_wm_size(self, width, height):
                self.override = (int(width), int(height))
                raise RuntimeError("adb 掉线")

        adb = Boom()
        try:
            DisplayController(adb).probe(scale=2.0)
        except RuntimeError:
            pass
        self.assertIsNone(adb.override)


class FastPathTests(unittest.TestCase):
    """手机上已经是目标尺寸时不要重设：重设会多触发一次重排版，App 可能跳回第 1 题。

    界面上拖动倍数滑块就是这条路径（先加长、再开始提取）。
    """

    def setUp(self):
        # 真机上要等约 3s 画面稳定，单测里把等待压到 0
        self._patched = (display_module.MIN_SETTLE_SECONDS, display_module.SETTLE_INTERVAL)
        display_module.MIN_SETTLE_SECONDS = 0.0
        display_module.SETTLE_INTERVAL = 0.0
        self.addCleanup(self._restore)

    def _restore(self):
        display_module.MIN_SETTLE_SECONDS, display_module.SETTLE_INTERVAL = self._patched

    def test_already_target_size_is_not_applied_again(self):
        adb = FakeDisplayAdb()
        adb.override = (1260, 5440)
        controller = DisplayController(adb)
        result = controller.apply(scale=2.0, settle_timeout=0.5)
        self.assertTrue(result.verified)
        self.assertEqual(result.settled_ms, 0)
        self.assertEqual(adb.count("set_wm_size"), 0)
        self.assertIn("已经是目标尺寸", result.note)
        # 快路径也算「我改过」：运行结束必须把它复位，不能留给用户
        self.assertTrue(controller.restore())
        self.assertIsNone(adb.override)

    def test_fast_path_requires_matching_density(self):
        adb = FakeDisplayAdb()
        adb.override = (1260, 5440)
        result = DisplayController(adb).apply(scale=2.0, density=420, settle_timeout=0.5)
        self.assertTrue(result.verified)
        self.assertEqual(adb.count("set_wm_size"), 1)     # 密度不一致 -> 老实重设
        self.assertEqual(adb.density_override, 420)

    def test_fast_path_requires_matching_size(self):
        adb = FakeDisplayAdb()
        adb.override = (1260, 4080)                        # 上次只加长到 1.5 倍
        result = DisplayController(adb).apply(scale=2.0, settle_timeout=0.5)
        self.assertTrue(result.verified)
        self.assertEqual(adb.count("set_wm_size"), 1)
        self.assertEqual(adb.override, (1260, 5440))

    def test_scale_one_without_density_changes_nothing(self):
        adb = FakeDisplayAdb()
        adb.override = (1260, 2720)                        # 和物理一样高：加长没意义
        result = DisplayController(adb).apply(scale=1.0, settle_timeout=0.5)
        self.assertFalse(result.verified)
        self.assertIn("没有要改的显示设置", result.note)
        # 没让人动手机就别动（无条件复位是 display --reset 的事）
        self.assertEqual([c for c in adb.calls if c[0].startswith(("set_", "reset_"))], [])
        self.assertEqual(adb.override, (1260, 2720))

    def test_density_works_without_stretching(self):
        """倍数 1.0 + 密度 420：截图尺寸不变，只把字变小（这条以前是空转的）。"""
        adb = FakeDisplayAdb()
        controller = DisplayController(adb)
        result = controller.apply(scale=1.0, density=420, settle_timeout=0.5)
        self.assertTrue(result.verified)
        self.assertTrue(result.density_only)
        self.assertEqual(adb.density_override, 420)
        self.assertEqual(adb.count("set_wm_size"), 0)      # 没有加长
        self.assertIn("显示密度", result.describe())
        self.assertEqual(result.as_dict()["density_only"], True)
        self.assertTrue(controller.restore())              # 结束要能还原
        self.assertIsNone(adb.density_override)

    def test_density_only_fast_path(self):
        adb = FakeDisplayAdb()
        adb.density_override = 420
        result = DisplayController(adb).apply(scale=1.0, density=420, settle_timeout=0.5)
        self.assertTrue(result.verified)
        self.assertEqual(result.settled_ms, 0)
        self.assertEqual(adb.count("set_wm_density"), 0)   # 已经是这个密度：别重设

    def test_density_only_clears_previous_size_override(self):
        """从「加长 + 加宽」切到「只改密度」时，尺寸覆盖必须撤掉。"""
        adb = FakeDisplayAdb()
        adb.override = (1764, 5440)
        controller = DisplayController(adb)
        result = controller.apply(scale=1.0, density=420, settle_timeout=0.5)
        self.assertTrue(result.verified)
        self.assertIsNone(adb.override)
        self.assertEqual(adb.density_override, 420)
        self.assertEqual(adb.count("reset_wm_size"), 1)
        self.assertTrue(controller.restore())        # 只改密度也要能复位
        self.assertIsNone(adb.density_override)

    def test_density_back_to_zero_clears_override(self):
        adb = FakeDisplayAdb()
        adb.override = (1260, 5440)
        adb.density_override = 420
        result = DisplayController(adb).apply(scale=2.0, density=0, settle_timeout=0.5)
        self.assertTrue(result.verified)
        self.assertIsNone(adb.density_override)            # 密度改回 0 要顺手清掉旧覆盖


class ThumbnailTests(unittest.TestCase):
    """GUI 实时预览用的缩略图：必须真的缩小，不能把整张截图搬进状态轮询。"""

    @staticmethod
    def _png(width: int, height: int) -> bytes:
        from PIL import Image

        buffer = io.BytesIO()
        Image.new("RGB", (width, height), (250, 250, 250)).save(buffer, format="PNG")
        return buffer.getvalue()

    def test_thumb_is_smaller_and_decodable(self):
        original = self._png(1260, 2720)
        thumb = to_thumb_data_url(original)
        self.assertTrue(thumb.startswith("data:image/jpeg;base64,"))
        raw = base64.b64decode(thumb.split(",", 1)[1])
        width, height = image_size(raw)
        self.assertLessEqual(height, 640)
        self.assertLessEqual(width, 240)
        self.assertLess(len(raw), len(original))

    def test_thumb_does_not_upscale(self):
        original = self._png(120, 200)
        thumb = to_thumb_data_url(original)
        raw = base64.b64decode(thumb.split(",", 1)[1])
        self.assertLessEqual(image_size(raw), (120, 200))

    def test_thumb_rejects_empty(self):
        with self.assertRaises(ValueError):
            to_thumb_data_url(b"")


if __name__ == "__main__":
    unittest.main()
