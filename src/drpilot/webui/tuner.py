# -*- coding: utf-8 -*-
"""「屏幕调节」独立窗口的桥（tuner.html 的 js_api）。

主界面只留一个入口按钮，真正的调参在第二个窗口里做：宽 / 长 / 密度三个数，
长宽可以直接拖矩形改，手机实时跟着变。这里只暴露显示相关的方法，
避免调节窗口误触提取 / API Key 这些东西；所有方法都立刻返回，
重活（wm size / 截图）交给 UiBridge 的后台线程。
"""

from __future__ import annotations

from typing import Any, Mapping


class DisplayTunerApi:
    """给 tuner.html 用的瘦 API：转发到 UiBridge 的显示相关方法。"""

    def __init__(self, bridge: Any) -> None:
        self.bridge = bridge

    # ---- 页面初始化与轮询 ----
    def ready(self) -> dict[str, Any]:
        """打开窗口时调一次：返回当前设置 + 真机状态（顺带触发一次真机读屏）。"""
        return self.bridge.tuner_ready()

    def poll(self) -> dict[str, Any]:
        """页面每 200ms 轮询一次：设置、真机覆盖、状态文案、缩略图版本号。"""
        return self.bridge.tuner_state()

    # ---- 三个参数（宽 / 长 / 密度） ----
    def apply(self, values: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """按当前三个数改手机显示；立刻返回，后台线程去做（拖动时的中间值会被丢掉）。"""
        return self.bridge.tuner_apply(values or {})

    def reset(self) -> dict[str, Any]:
        """无条件复位手机的 wm size / density / scaling。"""
        return self.bridge.reset_display(self.bridge.tuner_payload())

    # ---- 看效果 ----
    def pop_thumb(self) -> dict[str, Any]:
        """取走最近一张缩略图（取一次就清空，图不进轮询）。"""
        return self.bridge.pop_display_thumb()

    def capture(self) -> dict[str, Any]:
        """抓一张全尺寸截图并打开（手机保持当前设置，不复位）。"""
        return self.bridge.preview_capture(self.bridge.tuner_payload())

    def open_capture(self) -> dict[str, Any]:
        """打开最近一次的全尺寸截图。"""
        return self.bridge.open_display_capture()

    def original(self) -> dict[str, Any]:
        """重新抓一张「原本」的截图（当前手机画面，不加任何设置）。"""
        return self.bridge.tuner_original()

    def pop_original(self) -> dict[str, Any]:
        """取走「原本」的截图（取一次就清空）。"""
        return self.bridge.pop_original()


__all__ = ["DisplayTunerApi"]
