# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import json
import unittest

from drpilot.actions import (
    RawGesture,
    Step,
    TouchDevice,
    TouchRecorder,
    TouchSample,
    build_action_plan,
    build_next_action,
    classify_gesture,
    normalize_steps,
    parse_getevent_devices,
    parse_getevent_line,
    payload_to_steps,
    pick_touch_device,
    record_once,
    replay_next_action,
    steps_to_payload,
)
from drpilot.config import AppConfig

DEVICE_LISTING = """add device 1: /dev/input/event3
  name: "synaptics_dsx"
  events:
    ABS (0003): ABS_MT_SLOT  : value 0, min 0, max 9, fuzz 0, flat 0, resolution 0
                ABS_MT_POSITION_X : value 0, min 0, max 1079, fuzz 0, flat 0, resolution 0
                ABS_MT_POSITION_Y : value 0, min 0, max 2399, fuzz 0, flat 0, resolution 0
                ABS_MT_TRACKING_ID : value 0, min 0, max 65535, fuzz 0, flat 0, resolution 0
  input props:
    INPUT_PROP_DIRECT
add device 2: /dev/input/event0
  name: "gpio-keys"
"""

TAP_LINES = [
    "[  1000.000000] EV_ABS       ABS_MT_POSITION_X    0000021c\n",
    "[  1000.000000] EV_ABS       ABS_MT_POSITION_Y    000004b0\n",
    "[  1000.000000] EV_ABS       ABS_MT_TRACKING_ID   00000001\n",
    "[  1000.000000] EV_SYN       SYN_REPORT           00000000\n",
    "[  1000.060000] EV_ABS       ABS_MT_TRACKING_ID   ffffffff\n",
    "[  1000.060000] EV_SYN       SYN_REPORT           00000000\n",
]

SWIPE_LINES = [
    "[  2000.000000] EV_ABS       ABS_MT_POSITION_X    000003e8\n",
    "[  2000.000000] EV_ABS       ABS_MT_POSITION_Y    000004b0\n",
    "[  2000.000000] EV_ABS       ABS_MT_TRACKING_ID   00000001\n",
    "[  2000.000000] EV_SYN       SYN_REPORT           00000000\n",
    "[  2000.100000] EV_ABS       ABS_MT_POSITION_X    00000258\n",
    "[  2000.100000] EV_SYN       SYN_REPORT           00000000\n",
    "[  2000.200000] EV_ABS       ABS_MT_POSITION_X    000000c8\n",
    "[  2000.200000] EV_SYN       SYN_REPORT           00000000\n",
    "[  2000.260000] EV_ABS       ABS_MT_TRACKING_ID   ffffffff\n",
    "[  2000.260000] EV_SYN       SYN_REPORT           00000000\n",
]


class FakeStream:
    def __init__(self, lines):
        self.stdout = iter(lines)
        self.terminated = False

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


class FakeRecordAdb:
    def __init__(self, lines, listing=DEVICE_LISTING, size=(1080, 2340), motionevent=False):
        self.lines = list(lines)
        self.listing = listing
        self.size = size
        self.motionevent = motionevent
        self.stream_args = None
        self.proc = None

    def wm_size(self):
        return self.size

    def run(self, args, **kwargs):
        return self.listing

    def input_supports(self, feature):
        return bool(self.motionevent) and feature == "motionevent"

    def open_stream(self, args):
        self.stream_args = list(args)
        self.proc = FakeStream(self.lines)
        return self.proc


class FakeReplayAdb:
    def __init__(self, motionevent=False):
        self.calls = []
        self.motionevent_supported = motionevent

    def tap(self, x, y):
        self.calls.append(("tap", x, y))

    def swipe(self, x1, y1, x2, y2, duration):
        self.calls.append(("swipe", x1, y1, x2, y2, duration))

    def keyevent(self, code):
        self.calls.append(("key", code))

    def motionevent(self, phase, x, y):
        self.calls.append(("motion", phase, x, y))

    def input_supports(self, feature):
        return self.motionevent_supported and feature == "motionevent"


class StepTests(unittest.TestCase):
    def test_roundtrip(self):
        steps = normalize_steps(
            [
                {"kind": "tap", "x": 1, "y": 2, "duration_ms": 90},
                {"kind": "swipe", "x": 10, "y": 20, "x2": 30, "y2": 40, "duration_ms": 150},
                {"kind": "key", "keycode": "keycode_dpad_right"},
                {"kind": "wait", "wait_ms": 300},
                {"kind": "path", "points": [[1, 2], [3, 4]], "point_ms": [0, 50], "duration_ms": 50},
            ]
        )
        self.assertEqual([step.kind for step in steps], ["tap", "swipe", "key", "wait", "path"])
        again = normalize_steps(steps_to_payload(steps))
        self.assertEqual([step.to_dict() for step in again], [step.to_dict() for step in steps])
        self.assertEqual(again[2].keycode, "KEYCODE_DPAD_RIGHT")

    def test_invalid_steps_dropped(self):
        steps = normalize_steps(
            [
                {"kind": "bogus"},
                {"kind": "wait", "wait_ms": 0},
                {"kind": "key", "keycode": "rm -rf /"},
                {"kind": "path", "points": [[1, 2]]},
                "not-a-dict",
                {"kind": "tap", "x": "3", "y": "4", "duration_ms": 999999},
            ]
        )
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].kind, "tap")
        self.assertEqual((steps[0].x, steps[0].y), (3, 4))
        self.assertEqual(steps[0].duration_ms, 5000)

    def test_payload_accepts_json_string_and_dict(self):
        text = json.dumps([{"kind": "tap", "x": 5, "y": 6}])
        self.assertEqual(payload_to_steps(text)[0].x, 5)
        self.assertEqual(payload_to_steps({"steps": [{"kind": "tap", "x": 7, "y": 8}]})[0].y, 8)
        self.assertEqual(payload_to_steps(""), [])
        self.assertEqual(payload_to_steps("{bad json"), [])

    def test_step_limit(self):
        many = [{"kind": "tap", "x": 1, "y": 1} for _ in range(50)]
        self.assertEqual(len(normalize_steps(many)), 32)


class PlanTests(unittest.TestCase):
    def _config(self, **overrides):
        base = dict(
            swipe_start_x=1060,
            swipe_start_y=553,
            swipe_end_x=270,
            swipe_end_y=551,
            swipe_duration_ms=150,
        )
        base.update(overrides)
        return AppConfig(**base)

    def test_legacy_fallback_is_single_swipe(self):
        steps = build_next_action(self._config())
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].kind, "swipe")
        self.assertEqual((steps[0].x, steps[0].y, steps[0].x2, steps[0].y2), (1060, 553, 270, 551))

    def test_recorded_action_wins(self):
        config = self._config(next_action=[{"kind": "tap", "x": 10, "y": 20, "duration_ms": 30}])
        steps = build_next_action(config)
        self.assertEqual([step.kind for step in steps], ["tap"])

    def test_plan_scales_and_clamps(self):
        steps = [Step(kind="swipe", x=530, y=276, x2=135, y2=275, duration_ms=150)]
        plan = build_action_plan(steps, (540, 1170), (1080, 2340))
        self.assertTrue(plan.scaled)
        self.assertFalse(plan.orientation_mismatch)
        self.assertEqual(
            (plan.steps[0].x, plan.steps[0].y, plan.steps[0].x2, plan.steps[0].y2),
            (1060, 552, 270, 550),
        )

    def test_plan_without_reference_keeps_coordinates(self):
        steps = [Step(kind="tap", x=1060, y=553)]
        plan = build_action_plan(steps, None, (1080, 2340))
        self.assertFalse(plan.scaled)
        self.assertEqual((plan.steps[0].x, plan.steps[0].y), (1060, 553))

    def test_absolute_steps_are_not_scaled(self):
        steps = [
            Step(kind="tap", x=630, y=2380, duration_ms=80, absolute=True),
            Step(kind="tap", x=100, y=100, duration_ms=80),
        ]
        plan = build_action_plan(steps, (1260, 2720), (1080, 2340))
        self.assertEqual((plan.steps[0].x, plan.steps[0].y), (630, 2340 - 1))
        self.assertNotEqual((plan.steps[1].x, plan.steps[1].y), (100, 100))

    def test_orientation_mismatch_is_flagged(self):
        steps = [Step(kind="tap", x=10, y=10)]
        plan = build_action_plan(steps, (1080, 2340), (2340, 1080))
        self.assertTrue(plan.orientation_mismatch)
        self.assertIn("方向", plan.describe())


class ReplayTests(unittest.TestCase):
    def test_tap_short_and_long(self):
        adb = FakeReplayAdb()
        plan = build_action_plan(
            [Step(kind="tap", x=1, y=2, duration_ms=30), Step(kind="tap", x=3, y=4, duration_ms=200)],
            None,
            None,
        )
        replay_next_action(adb, plan)
        self.assertEqual(adb.calls[0], ("tap", 1, 2))
        self.assertEqual(adb.calls[1], ("swipe", 3, 4, 3, 4, 200))

    def test_swipe_key_wait(self):
        adb = FakeReplayAdb()
        plan = build_action_plan(
            [
                Step(kind="swipe", x=1, y=2, x2=3, y2=4, duration_ms=120),
                Step(kind="key", keycode="KEYCODE_DPAD_RIGHT"),
                Step(kind="wait", wait_ms=1),
            ],
            None,
            None,
        )
        replay_next_action(adb, plan)
        self.assertEqual(
            adb.calls,
            [
                ("swipe", 1, 2, 3, 4, 120),
                ("key", "KEYCODE_DPAD_RIGHT"),
            ],
        )

    def test_path_falls_back_without_motionevent(self):
        adb = FakeReplayAdb(motionevent=False)
        plan = build_action_plan(
            [Step(kind="path", points=[(10, 20), (30, 40), (50, 60)], point_ms=[0, 50, 100], duration_ms=100)],
            None,
            None,
        )
        replay_next_action(adb, plan)
        self.assertEqual(adb.calls, [("swipe", 10, 20, 50, 60, 100)])

    def test_path_uses_motionevent_when_supported(self):
        adb = FakeReplayAdb(motionevent=True)
        plan = build_action_plan(
            [Step(kind="path", points=[(10, 20), (50, 60)], point_ms=[0, 100], duration_ms=100)],
            None,
            None,
        )
        replay_next_action(adb, plan)
        self.assertEqual(adb.calls[0], ("motion", "DOWN", 10, 20))
        self.assertEqual(adb.calls[1], ("motion", "MOVE", 50, 60))
        self.assertEqual(adb.calls[2], ("motion", "UP", 50, 60))


class GeteventTests(unittest.TestCase):
    def test_parse_line_labeled_and_raw(self):
        labeled = parse_getevent_line("[  1000.000000] EV_ABS       ABS_MT_POSITION_X    0000021c\n")
        self.assertEqual(labeled, (1000.0, "ABS_MT_POSITION_X", 0x21C))
        raw = parse_getevent_line("[  1000.000000] 0003 0035 0000021c\n")
        self.assertEqual(raw, (1000.0, "ABS_MT_POSITION_X", 0x21C))
        syn = parse_getevent_line("[  1000.000000] 0000 0000 00000000\n")
        self.assertEqual(syn[1], "SYN_REPORT")
        self.assertIsNone(parse_getevent_line("garbage"))

    def test_parse_devices_and_pick(self):
        devices = parse_getevent_devices(DEVICE_LISTING)
        self.assertEqual(len(devices), 1)
        device = pick_touch_device(devices)
        self.assertEqual(device.path, "/dev/input/event3")
        self.assertEqual((device.max_x, device.max_y), (1079, 2399))
        self.assertTrue(device.direct)

    def test_recorder_tap(self):
        recorder = TouchRecorder()
        for line in TAP_LINES:
            recorder.feed(line)
        gestures = recorder.finish()
        self.assertEqual(len(gestures), 1)
        self.assertEqual(len(gestures[0].samples), 1)
        self.assertFalse(recorder.multi_touch)

    def test_recorder_swipe(self):
        recorder = TouchRecorder()
        for line in SWIPE_LINES:
            recorder.feed(line)
        gestures = recorder.finish()
        self.assertEqual(len(gestures), 1)
        self.assertEqual([sample.x for sample in gestures[0].samples], [1000, 600, 200])

    def test_recorder_ignores_secondary_finger(self):
        recorder = TouchRecorder()
        lines = [
            "[  1.000000] EV_ABS ABS_MT_SLOT 00000000\n",
            "[  1.000000] EV_ABS ABS_MT_POSITION_X 00000064\n",
            "[  1.000000] EV_ABS ABS_MT_POSITION_Y 00000064\n",
            "[  1.000000] EV_ABS ABS_MT_TRACKING_ID 00000001\n",
            "[  1.000000] EV_SYN SYN_REPORT 00000000\n",
            "[  1.010000] EV_ABS ABS_MT_SLOT 00000001\n",
            "[  1.010000] EV_ABS ABS_MT_POSITION_X 00000200\n",
            "[  1.010000] EV_ABS ABS_MT_POSITION_Y 00000200\n",
            "[  1.010000] EV_ABS ABS_MT_TRACKING_ID 00000002\n",
            "[  1.010000] EV_SYN SYN_REPORT 00000000\n",
            "[  1.020000] EV_ABS ABS_MT_TRACKING_ID ffffffff\n",
            "[  1.020000] EV_SYN SYN_REPORT 00000000\n",
            "[  1.030000] EV_ABS ABS_MT_TRACKING_ID ffffffff\n",
            "[  1.030000] EV_SYN SYN_REPORT 00000000\n",
        ]
        for line in lines:
            recorder.feed(line)
        self.assertTrue(recorder.multi_touch)


class ClassifyTests(unittest.TestCase):
    def setUp(self):
        self.device = TouchDevice(path="/dev/input/event3", min_x=0, max_x=1079, min_y=0, max_y=2399)

    def test_tap_detection_and_mapping(self):
        gesture = RawGesture([TouchSample(0.0, 540, 1200), TouchSample(0.05, 542, 1202)])
        step = classify_gesture(gesture, (1080, 2340), self.device)
        self.assertEqual(step.kind, "tap")
        self.assertEqual((step.x, step.y), (540, 1170))

    def test_swipe_detection(self):
        gesture = RawGesture(
            [
                TouchSample(0.0, 1000, 1200),
                TouchSample(0.1, 600, 1200),
                TouchSample(0.2, 200, 1200),
            ]
        )
        step = classify_gesture(gesture, (1080, 2340), self.device)
        self.assertEqual(step.kind, "swipe")
        self.assertEqual((step.x, step.y, step.x2, step.y2), (1000, 1170, 200, 1170))
        self.assertEqual(step.duration_ms, 200)   # 直接构造的手势没有 release_t，用最后一个采样点


class RecordOnceTests(unittest.TestCase):
    def test_records_swipe(self):
        adb = FakeRecordAdb(SWIPE_LINES)
        result = record_once(adb, max_seconds=2.0, quiet_seconds=0.05)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.steps[0].kind, "swipe")
        self.assertEqual(result.reference, (1080, 2340))
        self.assertEqual(adb.stream_args, ["shell", "getevent", "-lt", "/dev/input/event3"])
        self.assertTrue(adb.proc.terminated)

    def test_records_tap(self):
        adb = FakeRecordAdb(TAP_LINES)
        result = record_once(adb, max_seconds=2.0, quiet_seconds=0.05)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.steps[0].kind, "tap")
        self.assertEqual(result.steps[0].duration_ms, 60)

    def test_permission_denied(self):
        adb = FakeRecordAdb(SWIPE_LINES, listing="Permission denied")
        result = record_once(adb, max_seconds=1.0, quiet_seconds=0.05)
        self.assertFalse(result.ok)
        self.assertIn("权限", result.error)

    def test_no_touch_device(self):
        adb = FakeRecordAdb(SWIPE_LINES, listing="add device 1: /dev/input/event0\n  name: \"gpio-keys\"\n")
        result = record_once(adb, max_seconds=1.0, quiet_seconds=0.05)
        self.assertFalse(result.ok)
        self.assertIn("没有找到触摸屏", result.error)

    def test_no_events(self):
        adb = FakeRecordAdb([])
        result = record_once(adb, max_seconds=2.0, quiet_seconds=0.05)
        self.assertFalse(result.ok)
        self.assertIn("没有录到触摸事件", result.error)


if __name__ == "__main__":
    unittest.main()
