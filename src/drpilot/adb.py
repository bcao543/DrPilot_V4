# -*- coding: utf-8 -*-
"""ADB 设备与操作封装。

只依赖标准库，方便在没有安装第三方依赖时也能做设备检测。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from .errors import AdbError

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JPEG_SIGNATURE = b"\xff\xd8\xff"
_SIZE_RE = re.compile(r"(\d+)\s*x\s*(\d+)", re.IGNORECASE)

DEFAULT_ADB_PORT = 5555
_CONNECT_FAIL_RE = re.compile(
    r"failed to connect|unable to connect|cannot connect|connection refused|"
    r"no route to host|timed out|missing port|actively refused",
    re.IGNORECASE,
)


def normalize_address(text: Any, default_port: int = DEFAULT_ADB_PORT) -> str:
    """把用户填的「手机 IP / IP:端口」规整成 adb 需要的 host:port。

    支持 192.168.1.5、192.168.1.5:5555、[fe80::1]:5555 以及 mDNS 服务名。
    """
    raw = str(text or "").strip()
    if not raw:
        raise AdbError("请填写手机 IP 和端口，例如 192.168.1.5:5555")
    lowered = raw.lower()
    for prefix in ("adb://", "tcp://", "http://", "https://"):
        if lowered.startswith(prefix):
            raw = raw[len(prefix):]
            break
    raw = raw.strip().strip("/")
    if not raw:
        raise AdbError("请填写手机 IP 和端口，例如 192.168.1.5:5555")

    has_port = False
    if raw.startswith("["):  # [fe80::1]:5555
        host, _, rest = raw[1:].partition("]")
        port_text = rest.lstrip(":").strip()
        has_port = bool(port_text)
    elif raw.count(":") > 1:  # 裸 IPv6
        host, port_text = raw, ""
    else:
        host, _, port_text = raw.partition(":")
        host = host.strip()
        port_text = port_text.strip()
        has_port = bool(port_text)

    if not host:
        raise AdbError(f"无法解析手机地址：{text!r}")
    # mDNS 服务名（adb-xxx._adb-tls-connect._tcp）不带端口，交给 adb 自己解析
    if not has_port and ("_tcp" in host or host.endswith(".local")):
        return host
    port_text = port_text or str(default_port)
    if not port_text.isdigit():
        raise AdbError(f"端口必须是数字：{port_text!r}")
    port = int(port_text)
    if not 0 < port < 65536:
        raise AdbError(f"端口超出范围（1-65535）：{port}")
    return f"{host}:{port}"


def parse_connect_output(output: Any, address: str = "") -> tuple[bool, str]:
    """判断 adb connect 的输出是成功还是失败，返回 (是否成功, 可读信息)。"""
    text = " ".join(str(output or "").split())
    if not text:
        return False, "adb connect 没有任何输出（adb 可能没启动）"
    lowered = text.lower()
    if _CONNECT_FAIL_RE.search(lowered):
        if "missing port" in lowered:
            return False, f"{text}（地址需要写成 IP:端口，例如 192.168.1.5:5555）"
        if "refused" in lowered or "actively refused" in lowered:
            return False, f"{text}（手机上的无线调试可能没开，或 IP 变了）"
        return False, text
    if "already connected" in lowered or "connected to" in lowered:
        return True, text
    return False, text


@dataclass
class DeviceInfo:
    """一台 ADB 设备。"""

    serial: str
    state: str
    model: str = ""
    product: str = ""
    device: str = ""
    transport_id: str = ""

    @property
    def ready(self) -> bool:
        return self.state == "device"

    def describe(self) -> str:
        parts = [self.serial, self.state]
        if self.model:
            parts.append(self.model)
        if self.product:
            parts.append(self.product)
        return " | ".join(parts)


def format_devices(devices: Sequence[DeviceInfo]) -> str:
    """给 CLI / GUI 用的可读设备列表。"""
    if not devices:
        return "未检测到任何 ADB 设备"
    lines = []
    for index, device in enumerate(devices, 1):
        mark = "✓" if device.ready else "✗"
        lines.append(f"{mark} [{index}] {device.describe()}")
    return "\n".join(lines)


class AdbClient:
    """封装 adb 命令；可注入 serial 以支持多设备。"""

    def __init__(
        self,
        adb_path: str | None = None,
        serial: str | None = None,
        timeout: float = 30.0,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self._adb_path = adb_path
        self.serial = serial
        self.timeout = float(timeout)
        self.log = log or (lambda message: None)
        self._resolved: str | None = None
        self._input_help: str | None = None

    # ---- adb 可执行文件定位 ----
    @property
    def adb_path(self) -> str:
        if self._resolved is None:
            self._resolved = self._resolve_adb()
        return self._resolved

    @staticmethod
    def _probe(candidate: str | None) -> str | None:
        if not candidate:
            return None
        expanded = os.path.expanduser(candidate)
        if os.path.dirname(expanded):
            return expanded if os.path.isfile(expanded) else None
        return shutil.which(expanded)

    def _resolve_adb(self) -> str:
        candidates: list[str] = []
        if self._adb_path:
            candidates.append(self._adb_path)
        for name in ("ADB", "ADB_PATH"):
            value = os.environ.get(name)
            if value:
                candidates.append(value)
        for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
            base = os.environ.get(var)
            if base:
                candidates.append(os.path.join(base, "platform-tools", "adb"))
                candidates.append(os.path.join(base, "platform-tools", "adb.exe"))
        candidates.extend(["adb", "adb.exe"])
        local = Path(__file__).resolve().parents[2] / "adb"
        candidates.extend([str(local), str(local) + ".exe"])
        for candidate in candidates:
            found = self._probe(candidate)
            if found:
                self.log(f"使用 adb：{found}")
                return found
        raise AdbError(
            "未找到 adb 可执行文件。请安装 platform-tools 并加入 PATH，"
            "或用 --adb 指定路径，或设置 ADB 环境变量。"
        )

    def _base_cmd(self) -> list[str]:
        cmd = [self.adb_path]
        if self.serial:
            cmd.extend(["-s", self.serial])
        return cmd

    # ---- 基础命令 ----
    def run(
        self,
        args: Sequence[Any],
        check: bool = True,
        text: bool = True,
        timeout: float | None = None,
        merge_stderr: bool = False,
    ) -> Any:
        cmd = self._base_cmd() + [str(item) for item in args]
        effective_timeout = timeout if timeout is not None else self.timeout
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=effective_timeout)
        except FileNotFoundError as exc:
            raise AdbError(f"无法执行 adb：{cmd[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise AdbError(f"ADB 命令超时（{effective_timeout}s）：{' '.join(cmd)}") from exc
        except OSError as exc:
            raise AdbError(f"执行 ADB 命令失败：{exc}") from exc

        if check and proc.returncode != 0:
            stderr = _decode(proc.stderr)
            stdout = _decode(proc.stdout)
            detail = (stderr or stdout).strip()
            raise AdbError(f"ADB 命令失败（{proc.returncode}）：{' '.join(cmd)}\n{detail}")

        if text:
            stdout_text = _decode(proc.stdout)
            if merge_stderr:
                stderr_text = _decode(proc.stderr)
                if stderr_text.strip():
                    return (stdout_text + "\n" + stderr_text).strip()
            return stdout_text
        return proc.stdout if isinstance(proc.stdout, bytes) else (proc.stdout or b"")

    # ---- 设备 ----
    def devices(self) -> list[DeviceInfo]:
        return self.parse_devices(self.run(["devices", "-l"]))

    @staticmethod
    def parse_devices(output: str) -> list[DeviceInfo]:
        devices: list[DeviceInfo] = []
        for raw in (output or "").splitlines():
            line = raw.strip()
            if not line or line.startswith("List of devices") or line.startswith("*"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            serial, state = parts[0], parts[1]
            extra: dict[str, str] = {}
            for token in parts[2:]:
                key, sep, value = token.partition(":")
                if sep:
                    extra[key] = value
            devices.append(
                DeviceInfo(
                    serial=serial,
                    state=state,
                    model=extra.get("model", ""),
                    product=extra.get("product", ""),
                    device=extra.get("device", ""),
                    transport_id=extra.get("transport_id", ""),
                )
            )
        return devices

    # ---- 无线调试 ----
    def connect(self, address: Any, timeout: float | None = None) -> str:
        """adb connect IP:端口；失败抛 AdbError。"""
        target = normalize_address(address)
        output = self.run(
            ["connect", target],
            check=False,
            timeout=timeout if timeout is not None else 20.0,
            merge_stderr=True,
        )
        ok, message = parse_connect_output(output, target)
        if not ok:
            raise AdbError(f"连接 {target} 失败：{message}")
        return message or f"已连接 {target}"

    def disconnect(self, address: Any | None = None, timeout: float | None = None) -> str:
        """adb disconnect（不填地址则断开全部）。"""
        args: list[Any] = ["disconnect"]
        if address:
            args.append(normalize_address(address))
        return self.run(
            args, check=False, timeout=timeout if timeout is not None else 15.0, merge_stderr=True
        )

    @staticmethod
    def is_wireless(serial: Any) -> bool:
        """无线设备（IP:端口）判定。"""
        return ":" in str(serial or "")

    def ensure_device(self) -> DeviceInfo:
        """确认目标设备在线，并返回设备信息。"""
        devices = self.devices()
        if self.serial:
            for device in devices:
                if device.serial == self.serial:
                    if device.ready:
                        return device
                    raise AdbError(f"设备 {self.serial} 当前状态为 {device.state}，不可用")
            raise AdbError(f"未找到 serial 为 {self.serial} 的设备")
        ready = [device for device in devices if device.ready]
        if not ready:
            raise AdbError("未检测到已连接的 ADB 设备，请检查 USB / 无线调试授权。")
        if len(ready) > 1:
            self.log(f"检测到 {len(ready)} 台设备，默认使用 {ready[0].serial}（可用 --serial 指定）")
        self.serial = ready[0].serial
        return ready[0]

    # ---- 截图与滑动 ----
    def screenshot(self) -> bytes:
        data = self.run(
            ["exec-out", "screencap", "-p"],
            text=False,
            timeout=max(self.timeout, 30.0),
        )
        if not isinstance(data, bytes):
            raise AdbError("截图返回了非二进制数据")
        if not _is_image(data):
            stripped = data.lstrip(b"\r\n")
            if _is_image(stripped):
                data = stripped
        if not data:
            raise AdbError("截图数据为空，可能是设备未授权或屏幕关闭")
        if not _is_image(data):
            raise AdbError("截图数据格式异常（不是 PNG/JPEG）")
        return data

    def swipe(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        duration_ms: int = 150,
    ) -> None:
        self.run(
            ["shell", "input", "swipe", int(x1), int(y1), int(x2), int(y2), int(duration_ms)]
        )

    def tap(self, x: int, y: int) -> None:
        """点按屏幕坐标。"""
        self.run(["shell", "input", "tap", int(x), int(y)])

    def keyevent(self, keycode: Any) -> None:
        """发送一个按键（如 KEYCODE_DPAD_RIGHT / KEYCODE_VOLUME_DOWN）。"""
        self.run(["shell", "input", "keyevent", str(keycode)])

    def motionevent(self, phase: Any, x: int, y: int) -> None:
        """精确路径回放用：input motionevent DOWN|MOVE|UP x y（部分机型支持）。"""
        self.run(["shell", "input", "motionevent", str(phase).upper(), int(x), int(y)])

    def input_supports(self, feature: Any) -> bool:
        """探测 adb shell input 是否支持某个子命令（结果缓存）。"""
        name = str(feature or "").strip().lower()
        if not name:
            return False
        if self._input_help is None:
            try:
                self._input_help = str(
                    self.run(["shell", "input"], check=False, timeout=10.0, merge_stderr=True) or ""
                )
            except AdbError:
                self._input_help = ""
        return name in self._input_help.lower()

    def open_stream(self, args: Sequence[Any]) -> "subprocess.Popen[str]":
        """启动一个持续输出的 adb 命令（getevent 录制用），stderr 合并进 stdout。"""
        cmd = self._base_cmd() + [str(item) for item in args]
        try:
            return subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                bufsize=1,
            )
        except OSError as exc:
            raise AdbError(f"无法启动 adb 命令：{' '.join(cmd)}（{exc}）") from exc

    def wm_size(self) -> tuple[int, int] | None:
        """读取设备分辨率；失败返回 None。"""
        try:
            output = self.run(["shell", "wm", "size"])
        except AdbError:
            return None
        return self.parse_wm_size(output)

    # ---- 显示覆盖（加长主屏）----
    # 这些命令故意 check=False：设备可能静默忽略过大的尺寸，
    # 成败一律以「回读 wm size」为准（见 display.py）。
    def wm_size_output(self) -> str:
        """`wm size` 的原始输出（含 Physical size / Override size 两行）。"""
        return str(self.run(["shell", "wm", "size"], check=False) or "")

    def wm_density_output(self) -> str:
        """`wm density` 的原始输出。"""
        return str(self.run(["shell", "wm", "density"], check=False) or "")

    def set_wm_size(self, width: int, height: int) -> str:
        """临时覆盖逻辑分辨率（加长主屏的核心命令）。"""
        return str(self.run(["shell", "wm", "size", f"{int(width)}x{int(height)}"], check=False) or "")

    def reset_wm_size(self) -> str:
        return str(self.run(["shell", "wm", "size", "reset"], check=False) or "")

    def set_wm_density(self, density: int) -> str:
        return str(self.run(["shell", "wm", "density", str(int(density))], check=False) or "")

    def reset_wm_density(self) -> str:
        return str(self.run(["shell", "wm", "density", "reset"], check=False) or "")

    def set_wm_scaling(self, mode: Any) -> str:
        """`wm scaling off|auto`：off = 物理屏 1:1 裁剪，不再压扁显示。"""
        return str(self.run(["shell", "wm", "scaling", str(mode)], check=False) or "")

    def window_displays_dump(self) -> str:
        """`dumpsys window displays`：用来读 noscale 等显示状态。"""
        return str(self.run(["shell", "dumpsys", "window", "displays"], check=False) or "")

    @staticmethod
    def parse_wm_size(output: str) -> tuple[int, int] | None:
        override: tuple[int, int] | None = None
        physical: tuple[int, int] | None = None
        for line in (output or "").splitlines():
            match = _SIZE_RE.search(line)
            if not match:
                continue
            size = (int(match.group(1)), int(match.group(2)))
            lowered = line.lower()
            if "override" in lowered:
                override = size
            elif "physical" in lowered and physical is None:
                physical = size
        return override or physical


def _decode(raw: Any) -> str:
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return raw or ""


def _is_image(data: bytes) -> bool:
    return data.startswith(PNG_SIGNATURE) or data.startswith(JPEG_SIGNATURE)
