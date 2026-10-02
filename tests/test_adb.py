# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import unittest

from drpilot.adb import (
    AdbClient,
    format_devices,
    normalize_address,
    parse_connect_output,
)
from drpilot.errors import AdbError

DEVICES_OUTPUT = """List of devices attached
ABC123\tdevice product:x1 model:Pixel_7 device:panther transport_id:1
DEF456\tunauthorized usb:1-1
XYZ789\toffline
"""


class AdbParseTests(unittest.TestCase):
    def test_parse_devices(self):
        devices = AdbClient.parse_devices(DEVICES_OUTPUT)
        self.assertEqual(len(devices), 3)
        self.assertEqual(devices[0].serial, "ABC123")
        self.assertTrue(devices[0].ready)
        self.assertEqual(devices[0].model, "Pixel_7")
        self.assertFalse(devices[1].ready)
        self.assertIn("ABC123", format_devices(devices))

    def test_parse_devices_empty(self):
        self.assertEqual(AdbClient.parse_devices("List of devices attached\n"), [])
        self.assertEqual(format_devices([]), "未检测到任何 ADB 设备")

    def test_parse_wm_size(self):
        self.assertEqual(AdbClient.parse_wm_size("Physical size: 1080x2340"), (1080, 2340))
        text = "Physical size: 1080x2340\nOverride size: 720x1560"
        self.assertEqual(AdbClient.parse_wm_size(text), (720, 1560))
        self.assertIsNone(AdbClient.parse_wm_size("no size here"))


class AdbCommandTests(unittest.TestCase):
    def test_screenshot_accepts_png(self):
        client = AdbClient(adb_path="/bin/true")
        client.run = lambda *args, **kwargs: b"\x89PNG\r\n\x1a\nDATA"
        self.assertTrue(client.screenshot().startswith(b"\x89PNG"))

    def test_screenshot_rejects_non_image(self):
        client = AdbClient(adb_path="/bin/true")
        client.run = lambda *args, **kwargs: b"not an image"
        with self.assertRaises(AdbError):
            client.screenshot()

    def test_swipe_command(self):
        client = AdbClient(adb_path="/bin/true")
        captured = {}

        def fake_run(args, **kwargs):
            captured["args"] = list(args)
            return ""

        client.run = fake_run
        client.swipe(1060, 553, 270, 551, 150)
        self.assertEqual(
            captured["args"],
            ["shell", "input", "swipe", 1060, 553, 270, 551, 150],
        )

    def test_tap_keyevent_motionevent_commands(self):
        client = AdbClient(adb_path="/bin/true")
        captured = []

        def fake_run(args, **kwargs):
            captured.append(list(args))
            return ""

        client.run = fake_run
        client.tap(10, 20)
        client.keyevent("KEYCODE_DPAD_RIGHT")
        client.motionevent("down", 1, 2)
        self.assertEqual(
            captured,
            [
                ["shell", "input", "tap", 10, 20],
                ["shell", "input", "keyevent", "KEYCODE_DPAD_RIGHT"],
                ["shell", "input", "motionevent", "DOWN", 1, 2],
            ],
        )

    def test_input_supports_caches_help(self):
        client = AdbClient(adb_path="/bin/true")
        calls = []

        def fake_run(args, **kwargs):
            calls.append(list(args))
            return "Usage: input [<source>] motionevent DOWN x y"

        client.run = fake_run
        self.assertTrue(client.input_supports("motionevent"))
        self.assertFalse(client.input_supports("no-such-feature"))
        self.assertTrue(client.input_supports("motionevent"))
        self.assertEqual(len(calls), 1)

    def test_open_stream_command(self):
        import drpilot.adb as adb_module

        captured = {}

        class FakePopen:
            def __init__(self, cmd, **kwargs):
                captured["cmd"] = cmd
                captured["kwargs"] = kwargs
                self.stdout = None

        original = adb_module.subprocess.Popen
        adb_module.subprocess.Popen = FakePopen
        try:
            client = AdbClient(adb_path="/bin/true", serial="ABC")
            client.open_stream(["shell", "getevent", "-lt", "/dev/input/event3"])
        finally:
            adb_module.subprocess.Popen = original
        self.assertEqual(
            captured["cmd"],
            ["/bin/true", "-s", "ABC", "shell", "getevent", "-lt", "/dev/input/event3"],
        )
        self.assertIs(captured["kwargs"]["stderr"], adb_module.subprocess.STDOUT)
        self.assertTrue(captured["kwargs"]["text"])



class AdbWirelessTests(unittest.TestCase):
    """无线调试：地址解析与 adb connect 结果判断。"""

    def test_normalize_address(self):
        self.assertEqual(normalize_address("192.168.1.5"), "192.168.1.5:5555")
        self.assertEqual(normalize_address(" 192.168.1.5:6000 "), "192.168.1.5:6000")
        self.assertEqual(normalize_address("adb://192.168.1.5"), "192.168.1.5:5555")
        self.assertEqual(normalize_address("192.168.1.5", default_port=4444), "192.168.1.5:4444")
        self.assertEqual(
            normalize_address("adb-xxx._adb-tls-connect._tcp."),
            "adb-xxx._adb-tls-connect._tcp.",
        )
        with self.assertRaises(AdbError):
            normalize_address("")
        with self.assertRaises(AdbError):
            normalize_address("192.168.1.5:abc")
        with self.assertRaises(AdbError):
            normalize_address("192.168.1.5:70000")

    def test_parse_connect_output(self):
        self.assertTrue(parse_connect_output("already connected to 1.2.3.4:5555")[0])
        self.assertTrue(parse_connect_output("connected to 1.2.3.4:5555")[0])
        ok, message = parse_connect_output("failed to connect to '1.2.3.4:5555': Connection refused")
        self.assertFalse(ok)
        self.assertIn("无线调试", message)
        self.assertFalse(parse_connect_output("cannot connect to 1.2.3.4:5555: Connection refused")[0])
        ok, message = parse_connect_output("missing port in specification: 1.2.3.4")
        self.assertFalse(ok)
        self.assertIn("IP:端口", message)
        self.assertFalse(parse_connect_output("")[0])

    def test_connect_command(self):
        client = AdbClient(adb_path="/bin/true")
        captured = {}

        def fake_run(args, **kwargs):
            captured["args"] = list(args)
            captured["kwargs"] = dict(kwargs)
            return "connected to 192.168.1.5:5555"

        client.run = fake_run
        message = client.connect("192.168.1.5")
        self.assertIn("connected", message)
        self.assertEqual(captured["args"], ["connect", "192.168.1.5:5555"])
        self.assertFalse(captured["kwargs"]["check"])

    def test_connect_failure_raises(self):
        client = AdbClient(adb_path="/bin/true")
        client.run = lambda *args, **kwargs: "failed to connect to '1.2.3.4:5555': Connection refused"
        with self.assertRaises(AdbError):
            client.connect("1.2.3.4:5555")

    def test_disconnect_command(self):
        client = AdbClient(adb_path="/bin/true")
        captured = {}
        client.run = lambda args, **kwargs: captured.update(args=list(args)) or "disconnected everything"

        client.disconnect()
        self.assertEqual(captured["args"], ["disconnect"])
        client.disconnect("1.2.3.4:5555")
        self.assertEqual(captured["args"], ["disconnect", "1.2.3.4:5555"])

    def test_is_wireless(self):
        self.assertTrue(AdbClient.is_wireless("192.168.1.5:5555"))
        self.assertFalse(AdbClient.is_wireless("ABC123"))


if __name__ == "__main__":
    unittest.main()
