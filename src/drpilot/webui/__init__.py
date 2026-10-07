# -*- coding: utf-8 -*-
"""HTML/CSS（pywebview）图形界面。

    bridge.py  前端与业务之间的桥（不依赖 pywebview，可单测）
    app.py     创建窗口、启动事件循环
    assets/    index.html / style.css / app.js
"""

from __future__ import annotations

from typing import Any

__all__ = ["run_web_ui"]


def run_web_ui(
    config_path: str | None = None,
    width: int = 1180,
    height: int = 920,
    logger: Any = None,
) -> int:
    """GUI 入口（cli 只认这一层）。

    注意这里是**转发层**：cli 调的是 webui.run_web_ui，真正的实现在 app.py。
    以前这里只转发 config_path，结果 cli 传 logger / 尺寸时直接 TypeError
    （「run_web_ui() got an unexpected keyword argument 'logger'」）——
    所以参数要和 app.run_web_ui 保持一致，改了 app 那边记得同步这里。
    """
    from .app import run_web_ui as _run

    return _run(config_path, width=width, height=height, logger=logger)
