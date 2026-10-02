# -*- coding: utf-8 -*-
"""pywebview 窗口：HTML/CSS 界面 + UiBridge。"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from .bridge import UiBridge

ASSETS = Path(__file__).resolve().parent / "assets"
INDEX = ASSETS / "index.html"


def _folder_dialog_type(webview: Any) -> Any:
    """兼容 pywebview 各版本：新的用 FileDialog.FOLDER，旧的用 FOLDER_DIALOG。"""
    file_dialog = getattr(webview, "FileDialog", None)
    folder = getattr(file_dialog, "FOLDER", None)
    if folder is not None:
        return folder
    return getattr(webview, "FOLDER_DIALOG", 20)


def run_web_ui(config_path: str | None = None, width: int = 1180, height: int = 920) -> int:
    """打开图形界面；返回进程退出码。"""
    try:
        import webview
    except ImportError as exc:  # pragma: no cover - 依赖缺失
        print("启动图形界面需要 pywebview：请先执行 uv sync（或 pip install pywebview）", file=sys.stderr)
        print(f"（{exc}）", file=sys.stderr)
        return 1

    if not INDEX.is_file():  # pragma: no cover - 安装损坏
        print(f"界面文件缺失：{INDEX}", file=sys.stderr)
        return 1

    holder: dict[str, Any] = {}

    def pick_directory(initial: str) -> str | None:
        window = holder.get("window")
        if window is None:
            return None
        dialog_type = _folder_dialog_type(webview)
        try:
            result = window.create_file_dialog(dialog_type, directory=initial or None)
        except TypeError:  # 老版本没有 directory 参数
            result = window.create_file_dialog(dialog_type)
        if not result:
            return None
        if isinstance(result, (list, tuple)):
            return str(result[0]) if result else None
        return str(result)

    def request_exit() -> None:
        window = holder.get("window")
        if window is not None:
            window.destroy()

    bridge = UiBridge(config_path=config_path, pick_directory=pick_directory, on_exit=request_exit)
    # 注意：这里传「本地路径」而不是 file:// —— pywebview 会自动起一个本地 http 服务
    # （http://127.0.0.1:port/…）。同源页面才能用 canvas 读像素，点阵马背景才画得出来；
    # 用 file:// 的话 getImageData 会被判定为跨域而报 SecurityError。
    window = webview.create_window(
        "DrPilot v4 · ADB 题目识别",
        url=str(INDEX),
        js_api=bridge,
        width=width,
        height=height,
        min_size=(980, 660),
        background_color="#e7eaf1",
        text_select=False,
    )
    holder["window"] = window

    def on_closing() -> bool:
        bridge.shutdown()
        return True

    try:
        window.events.closing += on_closing
    except Exception:  # pragma: no cover - 事件对象因版本而异
        pass

    try:
        webview.start(debug=os.environ.get("DRPILOT_WEBVIEW_DEBUG") == "1")
    except Exception as exc:  # pragma: no cover - 运行时缺失（WebView2 / GTK / Qt）
        print(f"启动界面失败：{exc}", file=sys.stderr)
        if os.name == "nt":
            print(
                "Windows 需要 Edge WebView2 运行时（Windows 11 自带）。"
                "如提示缺失，请安装 Microsoft Edge WebView2 Runtime 后重试。",
                file=sys.stderr,
            )
        else:
            print(
                "Linux 需要 GTK（python3-gi + gir1.2-webkit2-4.1）或 Qt（PySide6）后端。",
                file=sys.stderr,
            )
        return 1
    return 0
