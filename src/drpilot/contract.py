# -*- coding: utf-8 -*-
"""CLI 契约：把「Agent 该怎么调 DrPilot」变成可机读的 JSON。

解决的问题：
    * Agent 不需要读源码/README 就知道每个参数是什么、哪些必填、示例是什么；
    * 缺参数时能拿到「该问用户什么」，而不是一句 argparse 报错；
    * 危险参数单独标注，避免 Agent 顺手加 --force-start。

    drpilot schema --json     # 完整契约
    drpilot ask --json        # 只返回「还缺什么 + 该问什么 + 凑齐后的命令」
"""

from __future__ import annotations

import argparse
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = 4
PROGRAM = "drpilot"
RUN_PREFIX = "uv run drpilot"

# 退出码约定（与 exitcodes.py 一一对应，改动要同步更新）
EXIT_CODES: list[dict[str, Any]] = [
    {"code": 0, "name": "ok", "meaning": "成功", "agent_action": "读取输出文件"},
    {
        "code": 1,
        "name": "run_failed",
        "meaning": "跑起来之后失败（批量失败、写盘失败、掉线）",
        "agent_action": "读 error_report.context.question_numbers，修复后从失败题号续跑",
    },
    {
        "code": 2,
        "name": "config_invalid",
        "meaning": "参数或配置错误",
        "agent_action": "按 missing / questions 补齐参数后重跑，不要原样重试",
    },
    {
        "code": 3,
        "name": "env_unavailable",
        "meaning": "环境不可用（缺依赖 / 缺 adb / 没有 Key / 模型下架）",
        "agent_action": "跑 drpilot doctor --json，按 hint 修复；需要用户提供信息时停下来问用户",
    },
    {
        "code": 4,
        "name": "precheck_failed",
        "meaning": "预检未通过：屏幕题号 != 起始题号",
        "agent_action": "让用户把手机停在正确题目，或确认后再用 --force-start",
    },
]

# 参数角色元数据：key 是 argparse 的 dest。
# required_for_run：真正跑一次提取时必须有的（ask / 交互式提问都用它）。
PARAM_META: dict[str, dict[str, Any]] = {
    "page_from": {
        "label": "起始题号",
        "required_for_run": True,
        "type": "int",
        "example": "47",
        "question": "从第几题开始？（以截图右上角显示的数字为准）",
        "keywords": ["从第几题", "起始题号", "开始", "start", "起"],
    },
    "page_to": {
        "label": "结束题号",
        "required_for_run": True,
        "type": "int",
        "example": "81",
        "question": "到第几题结束？（包含这一题）",
        "keywords": ["到第几题", "结束", "末题", "end", "止"],
    },
    "output_dir": {
        "label": "输出文件夹",
        "required_for_run": True,
        "type": "path",
        "example": "D:\\题库",
        "question": "提取结果存到哪个文件夹？",
        "keywords": ["输出", "存到", "保存到", "文件夹", "目录", "output"],
    },
    "textbook": {
        "label": "教材名",
        "required_for_run": False,
        "type": "str",
        "example": "药理学",
        "question": "哪本教材？（影响文件名与每题元数据）",
        "keywords": ["教材", "书", "textbook"],
    },
    "chapter": {
        "label": "章节名",
        "required_for_run": False,
        "type": "str",
        "example": "第二章 药物代谢动力学",
        "question": "哪一章？（例如：第二章 药物代谢动力学）",
        "keywords": ["章节", "第几章", "chapter"],
    },
    "device_address": {
        "label": "手机无线调试地址",
        "required_for_run": False,
        "type": "ip:port",
        "example": "192.168.1.5:5555",
        "question": "手机怎么连？USB 直接插着，还是无线调试的 IP:端口？",
        "keywords": ["手机", "无线", "connect", "ip", "端口"],
    },
}

# 危险参数：Agent 用之前必须先跟用户确认
DANGEROUS_PARAMS: dict[str, str] = {
    "force_start": "跳过题号一致性预检，可能整批编号错位",
    "preflight": "关掉开始前的题号预检，起始屏幕不对也不会提醒",
    "check_model": "关掉运行前的模型连通性测试，模型下架时会整批失败",
    "swipe_start_x": "改滑动坐标：换机型需要重录，错了会原地重复截图",
    "swipe_start_y": "改滑动坐标：换机型需要重录，错了会原地重复截图",
    "swipe_end_x": "改滑动坐标：换机型需要重录，错了会原地重复截图",
    "swipe_end_y": "改滑动坐标：换机型需要重录，错了会原地重复截图",
}

# 自然语言 -> 命令的常用套路（供 Agent 直接照抄）
WORKFLOWS: list[dict[str, str]] = [
    {
        "intent": "把某章第 X~Y 题提取到某个文件夹",
        "command": RUN_PREFIX
        + ' --from X --to Y --output-dir "D:\\题库" --textbook 药理学 --chapter "第二章 药物代谢动力学"',
        "note": "X/Y 是截图右上角的题号，不是页码；先跑 drpilot ask --json 确认参数齐了",
    },
    {
        "intent": "缺啥问啥：拿到要补的参数清单",
        "command": RUN_PREFIX + " ask --from 47 --output-dir D:\\题库 --json",
        "note": "返回 missing / questions / command，Agent 照 questions 问用户即可",
    },
    {
        "intent": "用一句自然语言生成参数",
        "command": RUN_PREFIX + ' ask --text "药理学第二章 47到81题，存到 D:\\题库" --json',
        "note": "解析有把握的部分，其余仍以 missing 的形式返回",
    },
    {
        "intent": "先看会怎么跑、会不会写错文件",
        "command": RUN_PREFIX
        + " --from 1 --to 20 --output-dir DIR --textbook 药理学 --chapter 第二章 --dry-run --json",
        "note": "不连手机、不调 AI，校验配置并算出输出路径",
    },
    {
        "intent": "环境自检（Agent 第一步永远先跑它）",
        "command": RUN_PREFIX + " doctor --json",
        "note": "7 项检查：python/依赖/Key/配置/adb/输出/模型",
    },
    {
        "intent": "补录漏掉的第 17 题",
        "command": RUN_PREFIX
        + " --from 17 --to 17 --output-dir 同一目录 --textbook 同一教材 --chapter 同一章节",
        "note": "按题号合并写回，不影响其它题",
    },
    {
        "intent": "看手机连上没有",
        "command": RUN_PREFIX + " devices --json",
        "note": "无线调试可以加 --connect 192.168.1.5:5555",
    },
    {
        "intent": "测模型还能不能调用",
        "command": RUN_PREFIX + " check-model --json",
        "note": "模型下架时返回 env_unavailable 并列出相似模型",
    },
    {
        "intent": "打开图形界面",
        "command": RUN_PREFIX + " --gui",
        "note": "给人用的；API Key 在「模型服务」卡片里新增/删除",
    },
]

JSON_CONTRACT: dict[str, Any] = {
    "stdout": "只有一份 JSON（--json 时）；所有日志/进度走 stderr",
    "success": {
        "ok": True,
        "exit_code": 0,
        "exit_code_name": "ok",
        "questions": "识别到的题目数",
        "jsonl": "主产物路径",
        "log_file": "本次运行的日志文件路径（Agent 定位问题时读它）",
    },
    "failure": {
        "ok": False,
        "exit_code": "1~4",
        "exit_code_name": "run_failed / config_invalid / env_unavailable / precheck_failed",
        "error_code": "稳定的机器可读错误码，例如 missing_required_arguments",
        "error": "人类可读的错误描述",
        "phase": "出错阶段：args / config / env / preflight / connect / capture / batch / write",
        "missing": "缺哪些参数（数组，仅参数类错误）",
        "questions": "该向用户追问什么（数组，仅参数类错误）",
        "suggestion": "下一步怎么办",
        "context": "出错上下文，如 question_numbers / worker / batch",
        "retry_command": "可以直接续跑的命令（批量失败时给出）",
        "log_file": "日志文件路径",
        "traceback": "仅 --debug 时出现",
    },
}


def _type_name(action: argparse.Action) -> str:
    if action.type is int:
        return "int"
    if action.type is float:
        return "float"
    if isinstance(action, argparse._StoreTrueAction):
        return "bool"
    if isinstance(action, argparse._StoreFalseAction):
        return "bool"
    return "str"


def param_specs(parser: argparse.ArgumentParser) -> list[dict[str, Any]]:
    """从真实 parser 生成参数清单；角色信息来自 PARAM_META，避免两处定义走样。"""
    specs: list[dict[str, Any]] = []
    for action in parser._actions:  # noqa: SLF001 - argparse 没有公开的遍历接口
        if not action.option_strings or action.dest in ("help", "version"):
            continue
        meta = PARAM_META.get(action.dest, {})
        default = action.default
        if default is None and isinstance(action, argparse._StoreTrueAction):
            default = False
        spec: dict[str, Any] = {
            "flags": list(action.option_strings),
            "name": action.option_strings[0],
            "dest": action.dest,
            "type": meta.get("type", _type_name(action)),
            "takes_value": not isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction)),
            "default": default if default is not None else "",
            "help": (action.help or "").strip(),
            "required_for_run": bool(meta.get("required_for_run")),
            "label": meta.get("label", ""),
            "question": meta.get("question", ""),
            "example": meta.get("example", ""),
            "keywords": list(meta.get("keywords", [])),
        }
        if action.dest in DANGEROUS_PARAMS:
            spec["danger"] = DANGEROUS_PARAMS[action.dest]
        specs.append(spec)
    return specs


def build_schema(parser: argparse.ArgumentParser) -> dict[str, Any]:
    """完整契约：子命令、参数、退出码、常用套路、JSON 结构。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "program": PROGRAM,
        "version": "4.0.0",
        "synopsis": "ADB 滑动截图 -> AI 识别题目 -> 导出 JSONL / Markdown / index.json",
        "run_prefix": RUN_PREFIX,
        "subcommands": {
            "doctor": "环境自检（python/依赖/Key/配置/adb/输出/模型）",
            "devices": "列出 ADB 设备",
            "check-model": "测试模型连通性",
            "schema": "打印这份契约（给 Agent 读）",
            "ask": "只回答「还缺什么参数、该问用户什么、凑齐后跑什么命令」",
        },
        "params": param_specs(parser),
        "required_for_run": [spec["name"] for spec in param_specs(parser) if spec["required_for_run"]],
        "dangerous_params": DANGEROUS_PARAMS,
        "exit_codes": EXIT_CODES,
        "workflows": WORKFLOWS,
        "json_contract": JSON_CONTRACT,
        "notes": [
            "--from / --to 是截图右上角的题号，不是页码；",
            "只跑一次建议加 --dry-run 先确认输出路径；",
            "缺参数时不会瞎猜：drpilot ask --json 会告诉 Agent 该问用户什么。",
        ],
    }


def quote_arg(value: Any) -> str:
    """命令回显用的引号处理：含空格 / 特殊字符就加双引号。"""
    text = str(value)
    if not text:
        return '""'
    if any(ch in text for ch in ' "&|<>^'):
        return '"' + text.replace('"', '\\"') + '"'
    return text


def format_command(values: Mapping[str, Any], extra: Sequence[str] = ()) -> str:
    """按顺序拼一条可以直接复制运行的命令。"""
    parts = [RUN_PREFIX]
    for flag, value in values.items():
        if value is None or value == "" or value is False:
            continue
        if value is True:
            parts.append(str(flag))
        else:
            parts.extend([str(flag), quote_arg(value)])
    parts.extend(str(item) for item in extra)
    return " ".join(parts)


def question_for(dest: str, fallback: str = "") -> str:
    meta = PARAM_META.get(dest)
    if meta and meta.get("question"):
        return str(meta["question"])
    return fallback or f"请提供 {dest}"


def missing_param(dest: str, *, reason: str = "") -> dict[str, Any]:
    """缺参描述：给 Agent 的「该问什么」+ 给人看的 flag。"""
    meta = PARAM_META.get(dest, {})
    item: dict[str, Any] = {
        "param": meta.get("label", dest),
        "dest": dest,
        "flag": {
            "page_from": "--from",
            "page_to": "--to",
            "output_dir": "--output-dir",
            "textbook": "--textbook",
            "chapter": "--chapter",
            "device_address": "--connect",
        }.get(dest, f"--{dest.replace('_', '-')}"),
        "required": bool(meta.get("required_for_run")),
        "question": question_for(dest),
        "example": meta.get("example", ""),
    }
    if reason:
        item["reason"] = reason
    return item


__all__ = [
    "DANGEROUS_PARAMS",
    "EXIT_CODES",
    "JSON_CONTRACT",
    "PARAM_META",
    "PROGRAM",
    "RUN_PREFIX",
    "SCHEMA_VERSION",
    "WORKFLOWS",
    "build_schema",
    "format_command",
    "missing_param",
    "param_specs",
    "question_for",
    "quote_arg",
]
