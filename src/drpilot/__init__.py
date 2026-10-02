# -*- coding: utf-8 -*-
"""DrPilot v4 —— ADB 滑动截图 + AI 识别 + JSON/Markdown 导出。

模块划分：
    actions    翻页动作：录制（getevent）、缩放与回放（tap / swipe / 多步）
    adb        ADB 设备与操作封装
    ai         AI 客户端与重试
    config     配置模型与 .env / 配置文件读写
    images     截图 -> JPEG base64
    models     题目数据结构
    output     JSON / Markdown 输出
    parser     AI 返回内容解析
    pipeline   截图与识别的编排
    cli        命令行入口
    webui      HTML/CSS 图形界面（pywebview；新拟物蓝白配色）
"""

__version__ = "4.0.0"
__all__ = ["__version__"]
