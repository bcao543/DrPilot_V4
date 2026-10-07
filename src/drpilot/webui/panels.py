# -*- coding: utf-8 -*-
"""主界面三个「入口」窗口的瘦 API（翻页动作 / 提取模块 / 模型服务）。

和 tuner.py（屏幕调节窗口）一样：每个窗口只暴露自己那几个方法，
避免窗口之间互相误触；所有方法都立刻返回，重活（录制 / 试一次 / 测模型）
交给 UiBridge 的后台线程，前端靠 poll() 拿状态。
"""

from __future__ import annotations

from typing import Any, Mapping


class ActionPanelApi:
    """「翻页动作」窗口：点按 / 滑动 / 录制动作 三选一。"""

    def __init__(self, bridge: Any) -> None:
        self.bridge = bridge

    # ---- 页面初始化与轮询 ----
    def ready(self) -> dict[str, Any]:
        return self.bridge.action_state()

    def poll(self) -> dict[str, Any]:
        return self.bridge.action_state()

    # ---- 选择与参数 ----
    def apply(self, values: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """保存「用哪一种 + 它的参数」，并回填主界面。"""
        return self.bridge.save_action_settings(values or {})

    # ---- 验证 ----
    def test(self, values: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """按当前选择试一次翻页（对比前后截图）。"""
        return self.bridge.test_panel_action(values or {})

    def set_reference(self, values: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """把当前设备分辨率记为滑动坐标的基准分辨率。"""
        return self.bridge.set_reference(self.bridge.panel_payload(values or {}))

    def start_record(self, values: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self.bridge.start_record(self.bridge.panel_payload(values or {}))

    def stop_record(self) -> dict[str, Any]:
        return self.bridge.stop_record()

    def clear_recorded(self) -> dict[str, Any]:
        """清除已录制的动作（不影响点按 / 滑动参数）。"""
        return self.bridge.clear_recorded()


class ModulePanelApi:
    """「提取模块」窗口：题号/题目/答案 + 自定义模块的统一清单。"""

    def __init__(self, bridge: Any) -> None:
        self.bridge = bridge

    def ready(self) -> dict[str, Any]:
        return self.bridge.module_settings()

    def poll(self) -> dict[str, Any]:
        return self.bridge.module_settings()

    def save(self, values: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """保存整份清单（本体字段 + 自定义模块），并回填主界面隐藏字段。"""
        return self.bridge.save_module_settings(values or {})

    def open_display(self) -> dict[str, Any]:
        """顺手打开「屏幕调节」窗口：首屏之外的模块要靠加长屏幕才截得全。"""
        return self.bridge.open_panel("tuner")


class ModelPanelApi:
    """「模型服务」窗口：模型 ID / API 地址 / API Key / 连通性。"""

    def __init__(self, bridge: Any) -> None:
        self.bridge = bridge

    def ready(self) -> dict[str, Any]:
        return self.bridge.model_settings()

    def poll(self) -> dict[str, Any]:
        return self.bridge.model_settings()

    def save(self, values: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """保存模型 ID 与 API 地址，并回填主界面隐藏字段。"""
        return self.bridge.save_model_settings(values or {})

    def test(self, values: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """测试模型连通性（用当前填的模型 / 地址 / Key）。"""
        return self.bridge.test_model(self.bridge.panel_payload(values or {}))

    # ---- API Key ----
    def add_keys(self, values: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self.bridge.add_keys(values or {})

    def delete_key(self, values: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self.bridge.delete_key(values or {})

    def clear_keys(self) -> dict[str, Any]:
        return self.bridge.clear_keys()

    def reveal_keys(self) -> dict[str, Any]:
        return self.bridge.reveal_keys()


__all__ = ["ActionPanelApi", "ModelPanelApi", "ModulePanelApi"]
