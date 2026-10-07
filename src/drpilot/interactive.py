# -*- coding: utf-8 -*-
"""交互式补参：缺参数时用自然语言逐项问用户，而不是甩一句 argparse 报错。

三类用户都能受益：
    * 人在终端里跑 drpilot -i（TTY 下缺参数会自动进入）；
    * 想让 CLI 自己问清楚再跑，而不是「参数错 -> 人肉改」；
    * Agent 不直接用本模块，而是读 drpilot ask --json 的 questions 去问用户。

ask / say 可注入，单元测试不需要真终端。
"""

from __future__ import annotations

import sys
from typing import Any, Callable, Mapping, Sequence

AskFn = Callable[[str], str]
SayFn = Callable[[str], None]

# 顺序就是提问顺序：先问必填，再问选填
PROMPT_SPECS: tuple[dict[str, Any], ...] = (
    {
        "dest": "page_from",
        "prompt": "从第几题开始？（以截图右上角显示的数字为准）",
        "kind": "int",
        "required": True,
    },
    {
        "dest": "page_to",
        "prompt": "到第几题结束？（包含这一题）",
        "kind": "int",
        "required": True,
    },
    {
        "dest": "output_dir",
        "prompt": "提取结果存到哪个文件夹？（例如 D:\\题库）",
        "kind": "str",
        "required": True,
    },
    {
        "dest": "textbook",
        "prompt": "教材名？（影响文件名与每题元数据，可留空跳过）",
        "kind": "str",
        "required": False,
    },
    {
        "dest": "chapter",
        "prompt": "章节名？（例如 第二章 药物代谢动力学，可留空跳过）",
        "kind": "str",
        "required": False,
    },
    {
        "dest": "device_address",
        "prompt": "手机无线调试地址？（USB 连接直接回车跳过）",
        "kind": "str",
        "required": False,
    },
    # 追加在最后：不影响既有提问顺序
    {
        "dest": "modules_text",
        "prompt": "还要额外提取哪些模块？（如 考点还原、标准解析，逗号分隔；不需要直接回车）",
        "kind": "str",
        "required": False,
    },
)

MAX_ATTEMPTS = 3


def is_tty() -> bool:
    """当前是不是「人能回答」的交互终端。"""
    try:
        return bool(sys.stdin and sys.stdin.isatty() and sys.stdout and sys.stdout.isatty())
    except Exception:
        return False


def _validate(kind: str, raw: str) -> tuple[bool, Any, str]:
    """返回 (是否合法, 解析后的值, 错误提示)。"""
    text = raw.strip()
    if not text:
        return True, "", ""
    if kind == "int":
        try:
            value = int(text)
        except ValueError:
            return False, None, "请输入数字，例如 47"
        if value < 1:
            return False, None, "题号必须 >= 1"
        return True, value, ""
    return True, text, ""


def prompts_for(dests: Sequence[str]) -> list[dict[str, Any]]:
    wanted = set(dests)
    return [spec for spec in PROMPT_SPECS if spec["dest"] in wanted]


def collect(
    dests: Sequence[str],
    *,
    ask: AskFn = input,
    say: SayFn = print,
    initial: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """按顺序提问；返回 {dest: value}。必填项被连续留空时返回 None（表示放弃）。

    initial 里已经有值的项不再问（例如命令行已经给了教材名）。
    """
    known = dict(initial or {})
    answers: dict[str, Any] = {}
    for spec in prompts_for(dests):
        dest = str(spec["dest"])
        if known.get(dest) not in (None, ""):
            continue
        prompt = str(spec["prompt"])
        for _attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                raw = ask(f"{prompt} ")
            except EOFError:
                say("（输入结束，取消）")
                return None
            except KeyboardInterrupt:
                say("（已取消）")
                return None
            ok, value, problem = _validate(str(spec["kind"]), raw)
            if ok:
                break
            say(f"  {problem}")
        else:
            say("  多次输入无效，已取消。")
            return None
        if value == "" or value is None:
            if spec["required"]:
                say(f"  「{prompt}」是必填项，不能跳过。")
                return None
            continue
        answers[dest] = value
    return answers


def confirm(question: str, *, ask: AskFn = input, say: SayFn = print, default: bool = True) -> bool:
    """跑之前确认一次；默认回车＝继续。"""
    suffix = "[Y/n] " if default else "[y/N] "
    try:
        raw = ask(f"{question} {suffix}").strip().lower()
    except (EOFError, KeyboardInterrupt):
        say("（已取消）")
        return False
    if not raw:
        return default
    return raw in ("y", "yes", "是", "好", "继续", "1")


__all__ = ["MAX_ATTEMPTS", "PROMPT_SPECS", "collect", "confirm", "is_tty", "prompts_for"]
