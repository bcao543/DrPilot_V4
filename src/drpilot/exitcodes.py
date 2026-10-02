# -*- coding: utf-8 -*-
"""统一退出码：让 Agent / CI 能凭退出码判断「下一步该修什么」。

约定（不要随意改动数值，AGENTS.md 里对 Agent 有明确承诺）：
    0  成功
    1  运行期失败（跑起来之后失败：ADB 中断连、AI 批量失败、写文件失败…）
    2  参数或配置错误（用户/Agent 给错了参数，改参数即可）
    3  环境不可用（缺依赖、缺 adb、没有 API Key、模型不可用…）
    4  前置检查未通过（预检题号不一致且没有 --force-start 等）
"""

from __future__ import annotations

SUCCESS = 0
RUN_FAILED = 1
CONFIG_INVALID = 2
ENV_UNAVAILABLE = 3
PRECHECK_FAILED = 4

# JSON 输出里使用的稳定名字，便于日志/看板归类
_NAMES = {
    SUCCESS: "ok",
    RUN_FAILED: "run_failed",
    CONFIG_INVALID: "config_invalid",
    ENV_UNAVAILABLE: "env_unavailable",
    PRECHECK_FAILED: "precheck_failed",
}


def name(code: int) -> str:
    """把退出码翻成稳定的英文标识。"""
    return _NAMES.get(int(code), f"unknown_{code}")


__all__ = [
    "CONFIG_INVALID",
    "ENV_UNAVAILABLE",
    "PRECHECK_FAILED",
    "RUN_FAILED",
    "SUCCESS",
    "name",
]
