# -*- coding: utf-8 -*-
"""统一的异常类型。"""

from __future__ import annotations


class DrPilotError(Exception):
    """本项目所有可预期错误的基类。"""


class ConfigError(DrPilotError):
    """配置非法或缺少必要配置。"""


class AdbError(DrPilotError):
    """ADB 相关错误（未找到 adb、无设备、命令失败等）。"""


class AiError(DrPilotError):
    """调用 AI 接口失败。"""


class ParseError(DrPilotError):
    """无法解析 AI 返回内容。"""


class OutputError(DrPilotError):
    """写输出文件失败。"""
