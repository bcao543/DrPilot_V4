# -*- coding: utf-8 -*-
"""AI 提示词。

注意：提示词里刻意不出现任何具体题号数字。上一版写了示例 “47/81”，
4B 小模型会把示例数字抄进结果（题目 1~3 被标成 47/48/49），
所以这里只描述格式，要求模型逐张从截图读取。

提取模块（考点还原 / 标准解析 …）由 build_system_prompt 动态追加：
没有配置模块时，返回的提示词与 SYSTEM_PROMPT 完全一致（保持行为不变）。
"""

from __future__ import annotations

from typing import Sequence

from .modules import ModuleSpec, build_module_prompt_section

_BODY = """你是一个医学题目识别助手。你会收到按顺序排列的若干张手机截图，每张截图对应一道题（背题模式，正确选项前有实心勾选圆圈）。

请只输出一个 JSON 数组，数组元素与截图一一对应且顺序一致。每个元素包含：
  - "screen_id": 整数，截图右上角显示的当前题号。右上角通常显示“当前题号/本章总题数”（用斜杠分隔的两个数字），请把斜杠左边的数字填入 screen_id。必须逐张真实读取；读不到填 null；严禁猜测、严禁按顺序自增、严禁使用本提示词中可能出现的数字。
  - "screen_total": 整数，斜杠右边的总题数；读不到填 null。
  - "type": 字符串，"single"（单选）或 "multi"（多选）。
  - "stem": 字符串，题干原文。
  - "options": 对象，键为大写字母 A/B/C/D/E，值为选项内容。
  - "answer": 字符串，正确答案字母（单选如 "A"，多选如 "BCE"）。背题模式下被勾选的选项即正确答案。
如果某张截图无法识别，对应元素直接填 null。

特殊符号处理（必须遵守）：
1. 上标用 <sup>...</sup>，下标用 <sub>...</sub>。
2. 同一元素同时有上标和下标时，严格按从左到右的顺序拼接：先写下标再写上标。
   碳酸氢根离子写作 HCO<sub>3</sub><sup>-</sup>，禁止写成 HCO<sup>-</sup><sub>3</sub>。
3. 禁止使用 Unicode 上下标字符（如 ²、³、⁻），必须换成对应 HTML 标签。
4. options 必须是对象，不要用数组；answer 只能是字母，不要带“答案：”前缀。"""

_OUTPUT_ONLY_RULE = "只输出纯 JSON 数组，不要代码块标记，不要任何解释文字。"

SYSTEM_PROMPT = _BODY + "\n\n" + _OUTPUT_ONLY_RULE


def build_system_prompt(modules: Sequence[ModuleSpec] | None = None) -> str:
    """按提取模块生成系统提示词。

    modules 为空时返回的字符串与 SYSTEM_PROMPT 逐字一致 —— 老用户、老测试、
    老配置的行为完全不变。
    """
    section = build_module_prompt_section(list(modules or ()))
    if not section:
        return SYSTEM_PROMPT
    return _BODY + "\n" + section + "\n\n" + _OUTPUT_ONLY_RULE


PREFLIGHT_PROMPT = """你是屏幕信息读取助手。看这张截图，只输出一个 JSON 对象：
{"screen_id": 截图右上角显示的当前题号（整数），读不到填 null, "screen_total": 右上角显示的总题数（整数），读不到填 null}
只输出 JSON，不要任何其他内容。"""


__all__ = ["PREFLIGHT_PROMPT", "SYSTEM_PROMPT", "build_system_prompt"]
