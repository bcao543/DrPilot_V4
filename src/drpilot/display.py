# -*- coding: utf-8 -*-
"""加长主屏：临时放大逻辑屏高度，一屏装下「考点还原」「标准解析」等首屏之外的内容。

真机实测（HUAWEI ALN-AL00 / Android 12 / 医考帮，见 README「加长主屏」一节）：

    * `wm size 1260x5440`（物理高的 2 倍）后 `screencap` 返回 1260x5440，
      题干/选项/答案/考点还原/标准解析 全部进屏，字形大小不变；
    * 过大（例如 8000）会被系统**静默忽略** —— 所以设置完必须回读校验；
    * 应用后画面约 3.3s 才稳定 —— 所以必须等稳定再截图；
    * `wm scaling off` 不改变截图内容，只让物理屏 1:1 裁剪（不变形）；
    * 覆盖值写在 Settings.Global（display_size_forced）里，必须复位。

本模块只负责「读状态 / 应用 / 等稳定 / 复位」，不碰截图与业务。
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

# 降级阶梯：设备可能静默忽略过大的尺寸，按比例逐级退让直到回读一致
# （0.875 / 0.75 / 0.625 = 旧的「2.0 → 1.75 → 1.5 → 1.25 倍」；现在宽高一起退）
FALLBACK_RATIOS: tuple[float, ...] = (0.875, 0.75, 0.625)
MIN_SETTLE_SECONDS = 1.2
DEFAULT_SETTLE_TIMEOUT = 12.0
SETTLE_INTERVAL = 0.4

_SIZE_LINE_RE = re.compile(r"(physical|override)\s*size\s*:\s*(\d+)\s*x\s*(\d+)", re.IGNORECASE)
_DENSITY_LINE_RE = re.compile(r"(physical|override)\s*density\s*:\s*(\d+)", re.IGNORECASE)
_ZERO_VALUES = {"", "null", "none", "0", "0x0"}


def parse_size_lines(text: Any) -> dict[str, tuple[int, int]]:
    """`wm size` 输出 -> {"physical": (w,h), "override": (w,h)}。"""
    sizes: dict[str, tuple[int, int]] = {}
    for match in _SIZE_LINE_RE.finditer(str(text or "")):
        sizes[match.group(1).lower()] = (int(match.group(2)), int(match.group(3)))
    return sizes


def parse_density_lines(text: Any) -> dict[str, int]:
    """`wm density` 输出 -> {"physical": n, "override": n}。"""
    found: dict[str, int] = {}
    for match in _DENSITY_LINE_RE.finditer(str(text or "")):
        found[match.group(1).lower()] = int(match.group(2))
    return found


def parse_scaling_disabled(dump: Any) -> bool:
    """`dumpsys window displays` 里出现 noscale 表示显示缩放被关掉（1:1 裁剪）。"""
    lowered = str(dump or "").lower()
    return "noscale" in lowered or "display scaling disabled" in lowered


def _clean_setting(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text.lower() in _ZERO_VALUES else text


@dataclass
class DisplayState:
    """手机当前的显示设置快照。"""

    physical: tuple[int, int] | None = None
    override: tuple[int, int] | None = None
    density: int | None = None
    density_override: int | None = None
    scaling_disabled: bool = False
    forced_size_setting: str = ""
    forced_density_setting: str = ""

    @property
    def effective(self) -> tuple[int, int] | None:
        """App 实际看到的逻辑尺寸（有覆盖用覆盖，否则用物理）。"""
        return self.override or self.physical

    @property
    def has_override(self) -> bool:
        return bool(self.override or self.density_override)

    def describe(self) -> str:
        parts: list[str] = []
        if self.physical:
            parts.append(f"物理 {self.physical[0]}x{self.physical[1]}")
        if self.override:
            parts.append(f"覆盖 {self.override[0]}x{self.override[1]}")
        if self.density:
            parts.append(f"密度 {self.density}")
        if self.density_override:
            parts.append(f"密度覆盖 {self.density_override}")
        if self.scaling_disabled:
            parts.append("缩放已关（物理屏 1:1 裁剪）")
        return "，".join(parts) or "未读到显示信息"

    def as_dict(self) -> dict[str, Any]:
        def size(value: tuple[int, int] | None) -> str:
            return f"{value[0]}x{value[1]}" if value else ""

        return {
            "physical": size(self.physical),
            "override": size(self.override),
            "density": self.density or 0,
            "density_override": self.density_override or 0,
            "scaling_disabled": bool(self.scaling_disabled),
            "forced_size_setting": self.forced_size_setting,
            "forced_density_setting": self.forced_density_setting,
            "has_override": self.has_override,
        }


@dataclass
class ApplyResult:
    """一次「加长主屏」的结果；日志与 JSON 都用它。"""

    mode: str = "off"
    requested: tuple[int, int] | None = None
    actual: tuple[int, int] | None = None
    verified: bool = False
    attempts: list[dict[str, Any]] = field(default_factory=list)
    settled_ms: int = 0
    density: int = 0
    scaling: str = "auto"
    note: str = ""
    # 只改密度、没加长（倍数 1.0 + 密度 N）：截图尺寸不变，只是字变小、一屏装得更多
    density_only: bool = False
    # 逻辑宽 = 物理宽 × width_scale（1.0 = 不加宽）
    width_scale: float = 1.0

    @property
    def applied(self) -> bool:
        return self.verified

    def describe(self) -> str:
        if self.mode == "off":
            return "加长主屏：未启用（不改手机显示设置）"
        if self.density_only:
            if self.verified:
                return f"显示密度：已设为 {self.density}（{self.settled_ms} ms 稳定），结束后会自动复位"
            return f"显示密度：未生效（{self.note or '设备没有接受这个密度'}）"
        if not self.requested:
            return f"加长主屏：未生效（{self.note or '宽度/高度读取失败'}）"
        head = f"加长主屏：请求 {self.requested[0]}x{self.requested[1]}"
        if self.verified:
            return (
                f"{head} → 生效 {self.actual[0]}x{self.actual[1]}"
                f"（{self.settled_ms} ms 稳定），结束后会自动复位"
            )
        return f"{head} → 未生效：{self.note or '设备未接受该尺寸'}"

    def as_dict(self) -> dict[str, Any]:
        def size(value: tuple[int, int] | None) -> str:
            return f"{value[0]}x{value[1]}" if value else ""

        return {
            "mode": self.mode,
            "requested": size(self.requested),
            "actual": size(self.actual),
            "verified": bool(self.verified),
            "settled_ms": int(self.settled_ms),
            "density": int(self.density),
            "scaling": self.scaling,
            "attempts": list(self.attempts),
            "note": self.note,
            "density_only": bool(self.density_only),
            "width_scale": float(self.width_scale),
        }


class DisplayController:
    """加长主屏的应用/复位控制器。

    只在自己改过设置之后才复位（restore 幂等），不会去动用户手动设的覆盖 ——
    `display --reset` 子命令才是无条件复位。
    """

    def __init__(self, adb: Any, log: Callable[[str], None] | None = None) -> None:
        self.adb = adb
        self.log = log or (lambda message: None)
        self._applied = False
        self.result = ApplyResult()

    # ---- 读状态 ----
    def read_state(self, quick: bool = False) -> DisplayState:
        """读当前显示设置。

        quick=True 只跑一条 `wm size`（管线的默认路径用，省掉 4 次 shell 往返）；
        quick=False 连密度、缩放模式与 Settings.Global 残留一起读（display 子命令 / doctor 用）。
        """
        state = DisplayState()
        try:
            sizes = parse_size_lines(self.adb.run(["shell", "wm", "size"], check=False))
            state.physical, state.override = sizes.get("physical"), sizes.get("override")
        except Exception as exc:
            self.log(f"读取 wm size 失败：{exc}")
        if quick:
            return state
        try:
            densities = parse_density_lines(self.adb.run(["shell", "wm", "density"], check=False))
            state.density = densities.get("physical")
            state.density_override = densities.get("override")
        except Exception as exc:
            self.log(f"读取 wm density 失败：{exc}")
        try:
            dump = self.adb.run(["shell", "dumpsys", "window", "displays"], check=False)
            state.scaling_disabled = parse_scaling_disabled(dump)
        except Exception:
            pass
        for key, attr in (
            ("display_size_forced", "forced_size_setting"),
            ("display_density_forced", "forced_density_setting"),
        ):
            try:
                raw = self.adb.run(["shell", "settings", "get", "global", key], check=False)
            except Exception:
                raw = ""
            setattr(state, attr, _clean_setting(raw))
        return state

    # ---- 应用 ----
    def plan_sizes(
        self, physical: tuple[int, int], *, width_scale: float = 1.0, scale: float = 2.0
    ) -> list[tuple[int, int]]:
        """候选目标尺寸：先试用户要的宽高，设备静默拒绝过大的值时按比例逐级退让。

        比例阶梯（0.875 / 0.75 / 0.625）是旧版「2.0 → 1.75 → 1.5 → 1.25 倍」的等价写法，
        只是现在宽、高一起退让，只调宽度时也走同一套。
        """
        physical_width, physical_height = int(physical[0]), int(physical[1])
        base_width = max(physical_width, int(round(physical_width * float(width_scale))))
        base_height = max(physical_height, int(round(physical_height * float(scale))))
        sizes: list[tuple[int, int]] = [(base_width, base_height)]
        for ratio in FALLBACK_RATIOS:
            candidate = (
                max(physical_width, int(round(base_width * ratio))),
                max(physical_height, int(round(base_height * ratio))),
            )
            if candidate not in sizes:
                sizes.append(candidate)
        return sizes

    def apply(
        self,
        *,
        scale: float = 2.0,
        width_scale: float = 1.0,
        density: int = 0,
        scaling: str = "auto",
        settle_timeout: float | None = None,
        screenshot: Callable[[], bytes] | None = None,
    ) -> ApplyResult:
        """把逻辑屏改到「物理宽 × width_scale，物理高 × scale」，回读校验并等画面稳定。

        * 高度看 scale：真机实测 2.0 可行，过大值会被系统静默忽略 → 自动降级重试；
        * 宽度看 width_scale（> 1 = 逻辑屏变宽，App 按更宽的画布重排版，一屏多截一些）；
        * 倍数 1.0 + 宽度 1.0 + 密度 > 0 时**只改密度**：截图尺寸不变，字变小、一屏装更多。
        settle_timeout=None 时用 DEFAULT_SETTLE_TIMEOUT（真机实测约 3s 稳定）。
        """
        timeout = DEFAULT_SETTLE_TIMEOUT if settle_timeout is None else float(settle_timeout)
        ws = float(width_scale) if float(width_scale or 0) > 0 else 1.0
        result = ApplyResult(
            mode="wide", density=int(density), scaling=str(scaling), width_scale=ws
        )
        state = self.read_state()
        if not state.physical:
            result.note = "没读到物理分辨率，无法计算加长后的尺寸"
            self.result = result
            return result
        if state.has_override:
            self.log(
                f"[提示] 手机上已有显示覆盖（{state.describe()}）：本次会按新值设置，"
                "运行结束后一并复位"
            )

        physical = state.physical
        physical_width, physical_height = physical
        target_width = max(physical_width, int(round(physical_width * ws)))
        target_height = max(physical_height, int(round(physical_height * float(scale))))
        want_size = (target_width, target_height) != (physical_width, physical_height)
        want_density = int(density) > 0

        if not want_size and not want_density:
            result.note = "倍数 1.0、宽度 1.0 且密度 0：没有要改的显示设置"
            self.result = result
            return result
        if not want_size:
            return self._apply_density_only(
                result, density=int(density), state=state, timeout=timeout, screenshot=screenshot
            )

        target = (target_width, target_height)

        # 快路径：手机上已经是目标尺寸（例如刚在界面上拖过滑块）就直接确认。
        # 再设一遍会多触发一次重排版（App 可能跳回第 1 题），还可能白等一轮稳定。
        if state.override == target and (
            not want_density or state.density_override == int(density)
        ):
            if str(scaling) == "off" and not state.scaling_disabled:
                try:
                    self.adb.set_wm_scaling("off")
                except Exception as exc:
                    self.log(f"设置 wm scaling off 失败：{exc}")
            if not want_density and state.density_override:
                # 密度被改回「跟随系统」：把旧的密度覆盖一起清掉，别留着
                self._reset_density()
                self.log("已把显示密度复位（密度 0 = 跟随系统）")
            self._applied = True
            result.requested = target
            result.actual = state.override
            result.verified = True
            result.settled_ms = 0
            result.density = int(density) if want_density else (state.density or 0)
            result.note = "手机上已经是目标尺寸，无需重设"
            self.log(f"加长主屏已生效：{target_width}x{target_height}（已经是目标尺寸，跳过重设）")
            self.result = result
            return result

        capture = screenshot or getattr(self.adb, "screenshot", None)
        baseline = self._digest(capture) if capture is not None else ""
        started = time.monotonic()

        for attempt_width, attempt_height in self.plan_sizes(physical, width_scale=ws, scale=scale):
            factor = attempt_height / physical_height if physical_height else 1.0
            attempt: dict[str, Any] = {
                "scale": round(factor, 4),
                "width_scale": round(attempt_width / physical_width, 4) if physical_width else 1.0,
                "width": attempt_width,
                "height": attempt_height,
            }
            try:
                self.adb.set_wm_size(attempt_width, attempt_height)
                if want_density:
                    self.adb.set_wm_density(int(density))
                elif state.density_override:
                    self._reset_density()      # 密度改回 0：顺手清掉旧覆盖
                if str(scaling) == "off":
                    self.adb.set_wm_scaling("off")
            except Exception as exc:
                attempt["error"] = str(exc)
                attempt["verified"] = False
                result.attempts.append(attempt)
                self.log(f"设置 wm size {attempt_width}x{attempt_height} 失败：{exc}")
                continue

            settled_ms = self._wait_settle(capture, baseline, timeout)
            now = self.read_state()
            actual = now.override
            attempt["actual"] = f"{actual[0]}x{actual[1]}" if actual else ""
            attempt["verified"] = bool(actual == (attempt_width, attempt_height))
            result.attempts.append(attempt)
            if attempt["verified"]:
                self._applied = True
                result.requested = target
                result.actual = actual
                result.verified = True
                result.settled_ms = settled_ms
                result.density = now.density_override or now.density or 0
                if (attempt_width, attempt_height) == target:
                    result.note = ""
                elif ws == 1.0:
                    result.note = f"设备只接受了 {factor:g} 倍"
                else:
                    result.note = f"设备只接受了 {attempt_width}x{attempt_height}"
                self.log(
                    f"加长主屏已生效：{attempt_width}x{attempt_height}（{factor:g} 倍，"
                    f"{settled_ms} ms 稳定）；运行结束后会自动复位"
                )
                self.result = result
                return result
            self.log(f"[提示] 设备未接受 {attempt_width}x{attempt_height}，尝试更小的尺寸…")
            self._reset_size()

        # 全部失败：把尺寸复位，照常运行
        self._reset_size()
        if want_density:
            self._reset_density()
        if str(scaling) == "off":
            self._reset_scaling()
        result.note = result.note or "设备拒绝所有候选尺寸（已复位，按原始分辨率运行）"
        result.settled_ms = int((time.monotonic() - started) * 1000)
        self.log(f"[警告] {result.note}")
        self.result = result
        return result

    def _apply_density_only(
        self,
        result: ApplyResult,
        *,
        density: int,
        state: DisplayState,
        timeout: float,
        screenshot: Callable[[], bytes] | None,
    ) -> ApplyResult:
        """只改显示密度（倍数 1.0）：截图尺寸不变，字变小、一屏装得更多。

        真机行为同加长：设置后画面要重新排版（等稳定）才能截图，值写在
        Settings.Global（display_density_forced）里，结束必须复位。
        """
        result.density_only = True
        target = int(density)
        if state.override:
            # 从「加长 / 加宽」切回「只改密度」：先把尺寸覆盖撤掉，别把上一次的屏幕留着
            self._reset_size()
            self.log("已把逻辑屏尺寸复位（这次只改密度）")
        if state.density_override == target:
            self._applied = True
            result.requested = result.actual = state.physical
            result.verified = True
            result.settled_ms = 0
            result.density = target
            result.note = "手机上已经是这个密度，无需重设"
            self.log(f"显示密度已生效：{target}（已经是目标密度，跳过重设）")
            self.result = result
            return result

        capture = screenshot or getattr(self.adb, "screenshot", None)
        baseline = self._digest(capture) if capture is not None else ""
        try:
            self.adb.set_wm_density(target)
        except Exception as exc:
            result.note = f"设置 wm density {target} 失败：{exc}"
            self.log(f"[警告] {result.note}")
            self.result = result
            return result

        settled_ms = self._wait_settle(capture, baseline, timeout)
        now = self.read_state()
        result.attempts.append(
            {
                "density": target,
                "actual_density": now.density_override or 0,
                "verified": now.density_override == target,
            }
        )
        result.settled_ms = settled_ms
        if now.density_override == target:
            self._applied = True
            result.requested = result.actual = now.physical
            result.verified = True
            result.density = target
            self.log(
                f"显示密度已生效：{target}（{settled_ms} ms 稳定）；运行结束后会自动复位"
            )
        else:
            self._reset_density()
            result.note = "设备没有接受这个密度（已复位）"
            self.log(f"[警告] {result.note}")
        self.result = result
        return result

    # ---- 复位 ----
    def restore(self) -> bool:
        """把自己改过的设置复位；没改过就什么都不做。"""
        if not self._applied:
            return True
        self._reset_size()
        self._reset_density()
        self._reset_scaling()
        state = self.read_state()
        ok = not state.has_override
        self._applied = False
        if ok:
            self.log(f"已复位手机显示设置：{state.describe()}")
        else:
            self.log(
                f"[警告] 显示设置可能没复位干净（{state.describe()}）："
                "可执行 drpilot display --reset 手动恢复"
            )
        return ok

    def reset_all(self) -> DisplayState:
        """无条件复位（给 drpilot display --reset 用）。"""
        self._reset_size()
        self._reset_density()
        self._reset_scaling()
        self._applied = False
        return self.read_state()

    # ---- 高层：给用户看一眼 ----
    def probe(
        self,
        *,
        scale: float = 2.0,
        density: int = 0,
        scaling: str = "auto",
        settle_timeout: float | None = None,
    ) -> tuple[ApplyResult, bytes | None, bytes | None]:
        """应用 -> 前后各截一张 -> 无条件复位；返回 (结果, 之前截图, 之后截图)。"""
        before: bytes | None = None
        after: bytes | None = None
        try:
            before = self.adb.screenshot()
        except Exception as exc:
            self.log(f"探测前截图失败：{exc}")
        try:
            result = self.apply(
                scale=scale, density=density, scaling=scaling, settle_timeout=settle_timeout
            )
            try:
                after = self.adb.screenshot()
            except Exception as exc:
                self.log(f"探测后截图失败：{exc}")
        finally:
            self.restore()
        return result, before, after

    # ---- 内部 ----
    def _reset_size(self) -> None:
        try:
            self.adb.reset_wm_size()
        except Exception as exc:
            self.log(f"[警告] wm size reset 失败：{exc}")

    def _reset_density(self) -> None:
        try:
            self.adb.reset_wm_density()
        except Exception as exc:
            self.log(f"[警告] wm density reset 失败：{exc}")

    def _reset_scaling(self) -> None:
        try:
            self.adb.set_wm_scaling("auto")
        except Exception as exc:
            self.log(f"[警告] wm scaling auto 失败：{exc}")

    @staticmethod
    def _digest(screenshot: Callable[[], bytes] | None) -> str:
        """截一张图取摘要；截图失败就返回空串（调用方按「未稳定」处理）。"""
        if screenshot is None:
            return ""
        try:
            data = screenshot()
        except Exception:
            return ""
        return hashlib.md5(data).hexdigest()

    def _wait_settle(
        self,
        screenshot: Callable[[], bytes] | None,
        baseline: str,
        timeout: float,
    ) -> int:
        """等画面稳定：连续两张截图一致，且至少等了 MIN_SETTLE_SECONDS。"""
        started = time.monotonic()
        if screenshot is None:
            time.sleep(min(MIN_SETTLE_SECONDS, max(0.0, timeout)))
            return int(MIN_SETTLE_SECONDS * 1000)
        previous = baseline
        stable = 0
        while True:
            elapsed = time.monotonic() - started
            if elapsed >= timeout:
                break
            time.sleep(SETTLE_INTERVAL)
            current = self._digest(screenshot)
            if current and current == previous:
                stable += 1
                if stable >= 1 and time.monotonic() - started >= MIN_SETTLE_SECONDS:
                    break
            else:
                stable = 0
            previous = current
        return int((time.monotonic() - started) * 1000)


__all__ = [
    "DEFAULT_SETTLE_TIMEOUT",
    "FALLBACK_RATIOS",
    "ApplyResult",
    "DisplayController",
    "DisplayState",
    "parse_density_lines",
    "parse_scaling_disabled",
    "parse_size_lines",
]
