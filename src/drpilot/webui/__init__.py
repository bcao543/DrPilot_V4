# -*- coding: utf-8 -*-
"""HTML/CSS（pywebview）图形界面。

    bridge.py  前端与业务之间的桥（不依赖 pywebview，可单测）
    app.py     创建窗口、启动事件循环
    assets/    index.html / style.css / app.js
"""

from __future__ import annotations

__all__ = ["run_web_ui"]


def run_web_ui(config_path: str | None = None) -> int:
    from .app import run_web_ui as _run

    return _run(config_path)
