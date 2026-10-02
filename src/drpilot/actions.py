# -*- coding: utf-8 -*-
"""翻页（下一题）动作：录制、归一化、缩放与回放。

设计约定：
    * 动作是一串纯数据步骤（tap / swipe / path / key / wait），配置与前端都用 JSON 传递；
    * 录制只解析 adb shell getevent 的输出，解析与手势识别都是纯函数，方便单测；
    * 回放只走 adb 的白名单命令（input tap / swipe / keyevent / motionevent），
      绝不把配置里的字符串当 shell 命令执行；
    * 没有录制动作时退回到旧的 swipe_* 坐标（build_next_action），行为与旧版一致。
"""

from __future__ import annotations

import json
import math
import queue
import re
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Sequence

STEP_KINDS = ("tap", "swipe", "path", "key", "wait")
MAX_STEPS = 32
MAX_WAIT_MS = 5000
MAX_DURATION_MS = 5000
MAX_PATH_POINTS = 24
MIN_TAP_HOLD_MS = 80            # 按住超过这个时长：用同位 swipe 回放（长按）
MIN_GAP_MS = 120                # 手势之间超过这个间隔，插一个等待步骤
MAX_GAP_MS = 3000
KEY_RE = re.compile(r"^KEYCODE_[A-Z0-9_]+$")

_DEVICE_HEAD_RE = re.compile(r"add device \d+:\s*(/dev/input/event\d+)")
_DEVICE_NAME_RE = re.compile(r'name:\s*"([^"]*)"')
_ABS_RANGE_RE = re.compile(
    r"ABS_MT_POSITION_([XY])\s*:\s*value\s*(-?\d+),\s*min\s*(-?\d+),\s*max\s*(-?\d+)"
)
_GETEVENT_RE = re.compile(
    r"^\s*\[\s*([0-9.]+)\]\s+(?:(\S+)\s+)?(\S+)\s+([0-9a-fA-F]+)\s*$"
)

# getevent 不开 -l 时输出的是十六进制码，这里把我们要的码补上名字
_RAW_CODE_NAMES = {
    "002f": "ABS_MT_SLOT",
    "0035": "ABS_MT_POSITION_X",
    "0036": "ABS_MT_POSITION_Y",
    "0039": "ABS_MT_TRACKING_ID",
    "014a": "BTN_TOUCH",
    "0000": "SYN_REPORT",
}
_TRACKING_UP = 0xFFFFFFFF


def _short(exc: BaseException, limit: int = 120) -> str:
    text = " ".join(str(exc).split())
    return text[:limit] if text else type(exc).__name__


def _as_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return fallback


def _clamp(value: int, low: int, high: int) -> int:
    if high < low:
        return low
    return min(max(int(value), low), high)


# ---------------- 步骤模型 ----------------
@dataclass
class Step:
    """一个可回放的翻页步骤。"""

    kind: str
    x: int = 0
    y: int = 0
    x2: int = 0
    y2: int = 0
    duration_ms: int = 0
    wait_ms: int = 0
    keycode: str = ""
    points: list[tuple[int, int]] = field(default_factory=list)
    point_ms: list[int] = field(default_factory=list)
    note: str = ""
    # True 表示坐标就是当前设备像素（手动点按 / --tap），不做基准分辨率缩放
    absolute: bool = False

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"kind": self.kind}
        if self.absolute:
            data["absolute"] = True
        if self.kind == "tap":
            data.update({"x": self.x, "y": self.y, "duration_ms": self.duration_ms})
        elif self.kind == "swipe":
            data.update(
                {
                    "x": self.x,
                    "y": self.y,
                    "x2": self.x2,
                    "y2": self.y2,
                    "duration_ms": self.duration_ms,
                }
            )
        elif self.kind == "path":
            data.update(
                {
                    "points": [[int(px), int(py)] for px, py in self.points],
                    "point_ms": [int(t) for t in self.point_ms],
                    "duration_ms": self.duration_ms,
                }
            )
        elif self.kind == "key":
            data["keycode"] = self.keycode
        elif self.kind == "wait":
            data["wait_ms"] = self.wait_ms
        if self.note:
            data["note"] = self.note
        return data

    @classmethod
    def from_dict(cls, data: Any) -> "Step | None":
        if isinstance(data, Step):
            return data
        if not isinstance(data, Mapping):
            return None
        kind = str(data.get("kind") or "").strip().lower()
        if kind not in STEP_KINDS:
            return None
        absolute = bool(data.get("absolute"))
        if kind == "tap":
            return cls(
                kind="tap",
                x=_as_int(data.get("x")),
                y=_as_int(data.get("y")),
                duration_ms=_clamp(_as_int(data.get("duration_ms")), 0, MAX_DURATION_MS),
                note=str(data.get("note") or ""),
                absolute=absolute,
            )
        if kind == "swipe":
            return cls(
                kind="swipe",
                x=_as_int(data.get("x")),
                y=_as_int(data.get("y")),
                x2=_as_int(data.get("x2")),
                y2=_as_int(data.get("y2")),
                duration_ms=_clamp(_as_int(data.get("duration_ms")), 1, MAX_DURATION_MS),
                note=str(data.get("note") or ""),
                absolute=absolute,
            )
        if kind == "path":
            raw_points = data.get("points")
            if not isinstance(raw_points, Sequence) or isinstance(raw_points, (str, bytes)):
                return None
            points: list[tuple[int, int]] = []
            for item in raw_points:
                if isinstance(item, Sequence) and not isinstance(item, (str, bytes)) and len(item) >= 2:
                    points.append((_as_int(item[0]), _as_int(item[1])))
            if len(points) < 2:
                return None
            points = points[:MAX_PATH_POINTS]
            duration_ms = _clamp(_as_int(data.get("duration_ms")), 1, MAX_DURATION_MS)
            raw_times = data.get("point_ms")
            times: list[int] = []
            if isinstance(raw_times, Sequence) and not isinstance(raw_times, (str, bytes)):
                times = [max(0, _as_int(item)) for item in raw_times][: len(points)]
            if len(times) != len(points):
                last = times[-1] if times else duration_ms
                span = max(last, 1)
                times = [round(span * i / max(len(points) - 1, 1)) for i in range(len(points))]
            for index in range(1, len(times)):
                if times[index] < times[index - 1]:
                    times[index] = times[index - 1]
            return cls(
                kind="path", points=points, point_ms=times, duration_ms=duration_ms,
                absolute=absolute,
            )
        if kind == "key":
            keycode = str(data.get("keycode") or "").strip().upper()
            if not KEY_RE.match(keycode):
                return None
            return cls(kind="key", keycode=keycode)
        if kind == "wait":
            wait_ms = _clamp(_as_int(data.get("wait_ms")), 0, MAX_WAIT_MS)
            if wait_ms <= 0:
                return None
            return cls(kind="wait", wait_ms=wait_ms)
        return None

    def describe(self) -> str:
        if self.kind == "tap":
            text = f"点击 ({self.x},{self.y})"
            if self.duration_ms >= MIN_TAP_HOLD_MS:
                text += f"，按住 {self.duration_ms}ms"
            if self.note:
                text += f"（{self.note}）"
            return text
        if self.kind == "swipe":
            text = f"滑动 ({self.x},{self.y}) -> ({self.x2},{self.y2})，{self.duration_ms}ms"
            if self.note:
                text += f"（{self.note}）"
            return text
        if self.kind == "path":
            return f"精确滑动 {len(self.points)} 个点，{self.duration_ms}ms"
        if self.kind == "key":
            return f"按键 {self.keycode}"
        if self.kind == "wait":
            return f"等待 {self.wait_ms}ms"
        return self.kind


def normalize_steps(raw: Any, limit: int = MAX_STEPS) -> list[Step]:
    """把任意来源（配置列表 / 前端 JSON 字符串 / 字典）归一成合法步骤。"""
    data: Any = raw
    if isinstance(data, str):
        text = data.strip()
        if not text:
            return []
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            return []
    if isinstance(data, Mapping):
        data = data.get("steps") or []
    if not isinstance(data, Sequence) or isinstance(data, (str, bytes)):
        return []
    steps: list[Step] = []
    for item in data:
        step = Step.from_dict(item)
        if step is None:
            continue
        steps.append(step)
        if len(steps) >= limit:
            break
    return steps


def payload_to_steps(value: Any) -> list[Step]:
    """表单里传的是 JSON 字符串，配置文件里传的是 list；两者都接受。"""
    return normalize_steps(value)


def steps_to_payload(steps: Sequence[Step]) -> list[dict[str, Any]]:
    return [step.to_dict() for step in steps]


def steps_summary(steps: Sequence[Step], limit: int = 6) -> str:
    parts = [f"{index}. {step.describe()}" for index, step in enumerate(steps[:limit], 1)]
    text = "；".join(parts)
    if len(steps) > limit:
        text += f" …（共 {len(steps)} 步）"
    return text


def default_swipe_step(config: Any) -> Step:
    """旧版固定滑动坐标 -> 单条 swipe 步骤（保证老配置行为不变）。"""
    return Step(
        kind="swipe",
        x=int(getattr(config, "swipe_start_x", 0) or 0),
        y=int(getattr(config, "swipe_start_y", 0) or 0),
        x2=int(getattr(config, "swipe_end_x", 0) or 0),
        y2=int(getattr(config, "swipe_end_y", 0) or 0),
        duration_ms=max(1, int(getattr(config, "swipe_duration_ms", 150) or 1)),
    )


def build_next_action(config: Any) -> list[Step]:
    """录制动作优先；没有录制动作时退回旧的固定滑动。"""
    steps = normalize_steps(getattr(config, "next_action", None))
    if steps:
        return steps
    return [default_swipe_step(config)]


# ---------------- 缩放与回放 ----------------
@dataclass
class ActionPlan:
    """已经按实际分辨率算好的动作。"""

    steps: list[Step]
    reference: tuple[int, int] | None = None
    actual: tuple[int, int] | None = None
    scaled: bool = False
    orientation_mismatch: bool = False

    def describe(self, limit: int = 4) -> str:
        text = steps_summary(self.steps, limit=limit)
        if self.scaled and self.reference and self.actual:
            text += (
                f"（基准 {self.reference[0]}x{self.reference[1]}"
                f" 缩放至 {self.actual[0]}x{self.actual[1]}）"
            )
        if self.orientation_mismatch:
            text += "（注意：屏幕方向与录制时不同，建议实测或重录）"
        return text


def _normalize_size(size: Any) -> tuple[int, int] | None:
    if not size:
        return None
    try:
        width, height = int(size[0]), int(size[1])
    except (TypeError, ValueError, IndexError):
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


def _scale_point(
    x: int, y: int, reference: tuple[int, int] | None, actual: tuple[int, int] | None
) -> tuple[int, int]:
    if not reference or not actual:
        return int(x), int(y)
    ref_w, ref_h = reference
    act_w, act_h = actual
    return round(x * act_w / ref_w), round(y * act_h / ref_h)


def build_action_plan(
    steps: Sequence[Step],
    reference: Any = None,
    actual: Any = None,
) -> ActionPlan:
    """按基准分辨率把动作坐标换算到实际设备分辨率，并夹到屏幕内。"""
    ref = _normalize_size(reference)
    act = _normalize_size(actual)
    scaled = bool(ref and act and ref != act)
    orientation_mismatch = bool(ref and act and (ref[0] > ref[1]) != (act[0] > act[1]))

    resolved: list[Step] = []
    for step in steps:
        if step.kind in ("tap", "swipe"):
            if step.absolute:
                x, y, x2, y2 = step.x, step.y, step.x2, step.y2
            else:
                x, y = _scale_point(step.x, step.y, ref, act)
                x2, y2 = _scale_point(step.x2, step.y2, ref, act)
            new = replace(step, x=x, y=y, x2=x2, y2=y2)
            if act:
                new.x = _clamp(new.x, 0, act[0] - 1)
                new.x2 = _clamp(new.x2, 0, act[0] - 1)
                new.y = _clamp(new.y, 0, act[1] - 1)
                new.y2 = _clamp(new.y2, 0, act[1] - 1)
            resolved.append(new)
        elif step.kind == "path":
            points = []
            for px, py in step.points:
                nx, ny = (px, py) if step.absolute else _scale_point(px, py, ref, act)
                if act:
                    nx = _clamp(nx, 0, act[0] - 1)
                    ny = _clamp(ny, 0, act[1] - 1)
                points.append((nx, ny))
            resolved.append(replace(step, points=points))
        else:
            resolved.append(replace(step))

    return ActionPlan(
        steps=resolved,
        reference=ref,
        actual=act,
        scaled=scaled,
        orientation_mismatch=orientation_mismatch,
    )


def _supports_motionevent(adb: Any) -> bool:
    checker = getattr(adb, "input_supports", None)
    if checker is None:
        return False
    try:
        return bool(checker("motionevent"))
    except Exception:
        return False


_warned: set[str] = set()


def _warn_once(log: Callable[[str], None] | None, message: str) -> None:
    if log is None or message in _warned:
        return
    _warned.add(message)
    log(message)


def _replay_path(adb: Any, step: Step) -> None:
    points = step.points
    times = step.point_ms[: len(points)]
    if len(times) < len(points):
        times = [0] * len(points)
    adb.motionevent("DOWN", points[0][0], points[0][1])
    previous = times[0]
    # times 上面已补齐到与 points 等长，这里用 strict=True 把「等长」写成硬约束：
    # 万一将来改坏补齐逻辑，会立刻报错，而不是悄悄少走几步。
    for (px, py), moment in zip(points[1:], times[1:], strict=True):
        delay = max(0, moment - previous) / 1000.0
        if delay:
            time.sleep(min(delay, 1.0))
        adb.motionevent("MOVE", px, py)
        previous = moment
    adb.motionevent("UP", points[-1][0], points[-1][1])


def replay_next_action(
    adb: Any,
    plan: ActionPlan,
    log: Callable[[str], None] | None = None,
    settle_wait: float = 0.0,
) -> None:
    """按顺序执行动作；最后统一等 settle_wait 让页面稳定。"""
    for step in plan.steps:
        if step.kind == "tap":
            if step.duration_ms >= MIN_TAP_HOLD_MS:
                adb.swipe(step.x, step.y, step.x, step.y, step.duration_ms)
            else:
                adb.tap(step.x, step.y)
        elif step.kind == "swipe":
            adb.swipe(step.x, step.y, step.x2, step.y2, step.duration_ms)
        elif step.kind == "path":
            if _supports_motionevent(adb):
                _replay_path(adb, step)
            else:
                _warn_once(
                    log,
                    "该设备不支持 input motionevent，精确路径已按起止点拉直回放",
                )
                first = step.points[0]
                last = step.points[-1]
                adb.swipe(first[0], first[1], last[0], last[1], max(1, step.duration_ms))
        elif step.kind == "key":
            adb.keyevent(step.keycode)
        elif step.kind == "wait":
            time.sleep(max(0, step.wait_ms) / 1000.0)
    if settle_wait > 0:
        time.sleep(settle_wait)


# ---------------- getevent 解析 ----------------
@dataclass
class TouchDevice:
    path: str
    name: str = ""
    min_x: int = 0
    min_y: int = 0
    max_x: int = 0
    max_y: int = 0
    direct: bool = False

    def describe(self) -> str:
        label = self.name or "未知触摸设备"
        return f"{self.path}（{label}，{self.min_x}~{self.max_x} x {self.min_y}~{self.max_y}）"


def parse_getevent_devices(text: Any) -> list[TouchDevice]:
    """解析 adb shell getevent -pl 的设备清单。"""
    devices: list[TouchDevice] = []
    current: TouchDevice | None = None
    for raw in str(text or "").splitlines():
        head = _DEVICE_HEAD_RE.search(raw)
        if head:
            if current is not None:
                devices.append(current)
            current = TouchDevice(path=head.group(1))
            continue
        if current is None:
            continue
        name = _DEVICE_NAME_RE.search(raw)
        if name and not current.name:
            current.name = name.group(1)
        if "INPUT_PROP_DIRECT" in raw:
            current.direct = True
        for match in _ABS_RANGE_RE.finditer(raw):
            axis, _value, low, high = match.groups()
            if axis == "X":
                current.min_x, current.max_x = int(low), int(high)
            else:
                current.min_y, current.max_y = int(low), int(high)
    if current is not None:
        devices.append(current)
    return [item for item in devices if item.max_x > 0 and item.max_y > 0]


def pick_touch_device(devices: Sequence[TouchDevice]) -> TouchDevice | None:
    """优先直连触摸屏，其次名字像触摸屏的，最后取量程最大的。"""
    if not devices:
        return None
    ranked = sorted(
        devices,
        key=lambda item: (
            1 if item.direct else 0,
            1 if re.search(r"touch|_ts|tp|panel", item.name, re.IGNORECASE) else 0,
            item.max_x,
        ),
        reverse=True,
    )
    return ranked[0]


def parse_getevent_line(line: Any) -> tuple[float, str, int] | None:
    """一行 getevent 输出 -> (时间戳, 事件名, 数值)；无法解析返回 None。"""
    match = _GETEVENT_RE.match(str(line or ""))
    if not match:
        return None
    moment = float(match.group(1))
    first, second, value_text = match.group(2), match.group(3), match.group(4)
    value = int(value_text, 16)
    if first and first.startswith("EV_"):
        return moment, second, value
    return moment, _RAW_CODE_NAMES.get(second.lower(), second.lower()), value


@dataclass
class TouchSample:
    t: float
    x: int
    y: int


@dataclass
class RawGesture:
    samples: list[TouchSample] = field(default_factory=list)
    release_t: float | None = None

    @property
    def start_t(self) -> float:
        return self.samples[0].t if self.samples else 0.0

    @property
    def end_t(self) -> float:
        if self.release_t is not None:
            return self.release_t
        return self.samples[-1].t if self.samples else 0.0


class TouchRecorder:
    """把 getevent 行喂进来，吐出主触点手势（多指只保留第一根手指）。"""

    def __init__(self) -> None:
        self._slot = 0
        self._state: dict[int, dict[str, Any]] = {}
        self._active: dict[int, list[TouchSample]] = {}
        self._gestures: list[RawGesture] = []
        self._primary_slot: int | None = None
        self.multi_touch = False

    @property
    def gesture_count(self) -> int:
        return len(self._gestures)

    def _slot_state(self, slot: int) -> dict[str, Any]:
        return self._state.setdefault(slot, {"x": None, "y": None})

    def _begin(self, slot: int) -> None:
        if slot in self._active:
            return
        self._active[slot] = []
        if self._primary_slot is None:
            self._primary_slot = slot
        elif slot != self._primary_slot:
            self.multi_touch = True

    def _end(self, slot: int, moment: float | None = None) -> None:
        samples = self._active.pop(slot, None)
        if samples and slot == self._primary_slot:
            self._gestures.append(RawGesture(samples=list(samples), release_t=moment))

    def _push(self, slot: int, moment: float) -> None:
        if slot not in self._active:
            return
        state = self._slot_state(slot)
        if state["x"] is None or state["y"] is None:
            return
        self._active[slot].append(TouchSample(moment, int(state["x"]), int(state["y"])))

    def feed(self, line: Any) -> None:
        parsed = parse_getevent_line(line)
        if parsed is None:
            return
        moment, code, value = parsed
        slot = self._slot
        state = self._slot_state(slot)
        if code == "ABS_MT_SLOT":
            self._slot = int(value)
            return
        if code == "ABS_MT_POSITION_X":
            state["x"] = int(value)
            state["t"] = moment
            return
        if code == "ABS_MT_POSITION_Y":
            state["y"] = int(value)
            state["t"] = moment
            return
        if code == "ABS_MT_TRACKING_ID":
            if value == _TRACKING_UP:
                self._end(slot, moment)
            else:
                self._begin(slot)
            return
        if code == "BTN_TOUCH" and value == 0:
            self._end(slot, moment)
            return
        if code == "SYN_REPORT":
            for active_slot in self._active:
                self._push(active_slot, moment)

    def finish(self) -> list[RawGesture]:
        for slot in list(self._active):
            samples = self._active.get(slot) or []
            self._end(slot, samples[-1].t if samples else None)
        return sorted(self._gestures, key=lambda item: item.start_t)


# ---------------- 手势识别 ----------------
def _raw_to_px(raw: int, low: int, high: int, size: int) -> int:
    if high <= low or size <= 1:
        return _clamp(int(raw), 0, max(size - 1, 0))
    ratio = (raw - low) / (high - low)
    return _clamp(round(ratio * (size - 1)), 0, size - 1)


def _max_deviation(
    points: Sequence[tuple[int, int]], start: tuple[int, int], end: tuple[int, int]
) -> float:
    ax, ay = start
    bx, by = end
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    worst = 0.0
    for px, py in points:
        if length_sq == 0:
            distance = math.hypot(px - ax, py - ay)
        else:
            t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
            distance = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
        worst = max(worst, distance)
    return worst


def _downsample(
    points: Sequence[tuple[int, int, float]], limit: int
) -> tuple[list[tuple[int, int]], list[int]]:
    if len(points) <= limit:
        chosen = list(points)
    else:
        stride = (len(points) - 1) / (limit - 1)
        indices = sorted({round(index * stride) for index in range(limit)})
        chosen = [points[index] for index in indices]
    base = points[0][2]
    times = [int(round((item[2] - base) * 1000)) for item in chosen]
    for index in range(1, len(times)):
        if times[index] < times[index - 1]:
            times[index] = times[index - 1]
    return [(item[0], item[1]) for item in chosen], times


def classify_gesture(
    gesture: RawGesture,
    screen: tuple[int, int],
    device: TouchDevice,
    motionevent: bool = False,
) -> Step | None:
    """一次触摸手势 -> 一个步骤（点按 / 滑动 / 精确路径）。"""
    if not gesture.samples:
        return None
    width, height = screen
    points = [
        (
            _raw_to_px(sample.x, device.min_x, device.max_x, width),
            _raw_to_px(sample.y, device.min_y, device.max_y, height),
            sample.t,
        )
        for sample in gesture.samples
    ]
    first = points[0]
    last = points[-1]
    duration_ms = max(1, int(round((gesture.end_t - first[2]) * 1000)))
    move_threshold = max(12, round(0.02 * min(width, height)))
    displacement = max(math.hypot(item[0] - first[0], item[1] - first[1]) for item in points)

    if displacement <= move_threshold:
        return Step(
            kind="tap",
            x=first[0],
            y=first[1],
            duration_ms=min(duration_ms, MAX_DURATION_MS),
        )

    duration = min(duration_ms, MAX_DURATION_MS) or 1
    deviation = _max_deviation(
        [(item[0], item[1]) for item in points],
        (first[0], first[1]),
        (last[0], last[1]),
    )
    curved = deviation > max(20, 0.04 * min(width, height))
    if curved and motionevent:
        path_points, path_times = _downsample(points, MAX_PATH_POINTS)
        return Step(
            kind="path",
            points=path_points,
            point_ms=path_times,
            duration_ms=duration,
            note="弧线",
        )
    step = Step(kind="swipe", x=first[0], y=first[1], x2=last[0], y2=last[1], duration_ms=duration)
    if curved:
        step.note = "弧线已拉直回放"
    return step


def _steps_from_gestures(
    gestures: Sequence[RawGesture],
    screen: tuple[int, int],
    device: TouchDevice,
    motionevent: bool,
) -> list[Step]:
    steps: list[Step] = []
    previous_end: float | None = None
    for gesture in gestures:
        if previous_end is not None:
            gap_ms = int(round((gesture.start_t - previous_end) * 1000))
            if gap_ms > MIN_GAP_MS:
                steps.append(Step(kind="wait", wait_ms=min(gap_ms, MAX_GAP_MS)))
        step = classify_gesture(gesture, screen, device, motionevent)
        if step is not None:
            steps.append(step)
        previous_end = gesture.end_t
        if len(steps) >= MAX_STEPS:
            break
    return steps[:MAX_STEPS]


# ---------------- 录制 ----------------
@dataclass
class RecordResult:
    steps: list[Step] = field(default_factory=list)
    reference: tuple[int, int] | None = None
    device: str = ""
    note: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.steps)


_STREAM_END = object()


def _close_stream(proc: Any) -> None:
    if proc is None:
        return
    terminate = getattr(proc, "terminate", None)
    kill = getattr(proc, "kill", None)
    wait = getattr(proc, "wait", None)
    try:
        if terminate is not None:
            terminate()
    except Exception:
        pass
    if wait is not None:
        try:
            wait(timeout=1.0)
            return
        except Exception:
            pass
    try:
        if kill is not None:
            kill()
    except Exception:
        pass


def record_once(
    adb: Any,
    *,
    stop_event: threading.Event | None = None,
    max_seconds: float = 25.0,
    quiet_seconds: float = 2.5,
    log: Callable[[str], None] | None = None,
) -> RecordResult:
    """录一次「下一题」动作；任何失败都通过 RecordResult.error 返回，不抛异常。"""
    log = log or (lambda message: None)
    stop_event = stop_event or threading.Event()

    try:
        size = adb.wm_size()
    except Exception as exc:
        return RecordResult(error=f"读取设备分辨率失败：{_short(exc)}")
    size = _normalize_size(size)
    if not size:
        return RecordResult(error="设备没有返回分辨率，无法录制")

    try:
        listing = adb.run(["shell", "getevent", "-pl"], check=False, merge_stderr=True)
    except Exception as exc:
        return RecordResult(error=f"读取触摸设备失败：{_short(exc)}")
    listing = str(listing or "")
    if "permission denied" in listing.lower():
        return RecordResult(error="这台设备不允许读取触摸事件（getevent 权限不足），请改用手动填写坐标")

    devices = parse_getevent_devices(listing)
    if not devices:
        return RecordResult(error="没有找到触摸屏设备（getevent -pl），请改用手动填写坐标")
    device = pick_touch_device(devices)
    if device is None:
        return RecordResult(error="没有找到触摸屏设备，请改用手动填写坐标")
    log(f"录制：使用触摸设备 {device.describe()}")

    checker = getattr(adb, "input_supports", None)
    motionevent = bool(checker and checker("motionevent"))

    try:
        proc = adb.open_stream(["shell", "getevent", "-lt", device.path])
    except Exception as exc:
        return RecordResult(error=f"启动 getevent 失败：{_short(exc)}")

    lines: "queue.Queue[Any]" = queue.Queue()

    def _reader() -> None:
        try:
            stream = getattr(proc, "stdout", None)
            if stream is not None:
                for line in stream:
                    lines.put(line)
        except Exception:
            pass
        lines.put(_STREAM_END)

    reader = threading.Thread(target=_reader, name="drpilot-getevent", daemon=True)
    reader.start()

    recorder = TouchRecorder()
    started = time.monotonic()
    last_event = started
    first_gesture_at: float | None = None
    stream_ended = False
    try:
        while True:
            if stop_event.is_set():
                break
            now = time.monotonic()
            if now - started >= max_seconds:
                log(f"录制达到 {max_seconds:.0f}s 上限，自动结束")
                break
            try:
                item = lines.get(timeout=0.2)
            except queue.Empty:
                if first_gesture_at is not None and now - last_event >= quiet_seconds:
                    break
                if stream_ended:
                    break
                continue
            if item is _STREAM_END:
                stream_ended = True
                if recorder.gesture_count or now - started >= 1.0:
                    break
                continue
            last_event = now
            recorder.feed(item)
            if recorder.gesture_count and first_gesture_at is None:
                first_gesture_at = now
    finally:
        _close_stream(proc)
        reader.join(timeout=1.0)

    gestures = recorder.finish()
    if not gestures:
        return RecordResult(reference=size, error="没有录到触摸事件，请重试或改用手动填写坐标")

    steps = _steps_from_gestures(gestures, size, device, motionevent)
    note = ""
    if recorder.multi_touch:
        note = "检测到多指操作，已忽略副触点"
    return RecordResult(
        steps=steps,
        reference=size,
        device=device.describe(),
        note=note,
    )


__all__ = [
    "ActionPlan",
    "RawGesture",
    "RecordResult",
    "Step",
    "TouchDevice",
    "TouchRecorder",
    "TouchSample",
    "build_action_plan",
    "build_next_action",
    "classify_gesture",
    "default_swipe_step",
    "normalize_steps",
    "parse_getevent_devices",
    "parse_getevent_line",
    "payload_to_steps",
    "pick_touch_device",
    "record_once",
    "replay_next_action",
    "steps_summary",
    "steps_to_payload",
]
