# -*- coding: utf-8 -*-
"""命令行入口（v4：为 Agent 与自然语言而优化）。

面向 Agent 的约定（AGENTS.md 有完整说明）：
    * 子命令：devices / doctor / check-model / schema / ask；也兼容旧的扁平参数写法；
    * --json：stdout 只输出一份 JSON 结果，进度与日志走 stderr，便于程序解析；
    * --text：一句自然语言 -> 参数（显式命令行参数优先）；
    * -i/--interactive：缺参数时逐项向用户提问；TTY 下缺参数也会自动进入提问；
    * 每次运行写一份日志文件（logs/drpilot-*.log），JSON 里回传 log_file；
    * 出错一定有 error_code / phase / context / suggestion，Agent 不用猜；
    * --dry-run：只校验配置、算出输出路径，不碰手机；
    * 退出码见 exitcodes.py：0 成功 / 2 参数错 / 3 环境不可用 / 4 前置检查未通过 / 1 运行失败。
"""

from __future__ import annotations

import argparse
import json as jsonlib
import sys
import traceback
from typing import Any, Sequence

from . import __version__, contract, exitcodes, interactive
from .actions import Step, payload_to_steps, steps_to_payload
from .config import (
    AppConfig,
    default_config_path,
    load_api_keys,
    load_config_file,
    load_config_with_fallback,
    load_dotenv_candidates,
    parse_chapter_no,
    parse_size,
    resolve_output_paths,
    save_config_file,
)
from .errors import AdbError, AiError, ConfigError, OutputError
from .nlparse import describe_params, parse_request
from .runlog import RunLogger, mask_secrets

SUBCOMMANDS = ("devices", "doctor", "check-model", "schema", "ask")
# 允许把后续参数原样透传给主 parser 的子命令（ask 需要完整的运行参数）
PASSTHROUGH_SUBCOMMANDS = ("ask",)


def _configure_console() -> None:
    """Windows 控制台下尽量使用 UTF-8，避免中文输出乱码。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def build_parser(parser: argparse.ArgumentParser | None = None) -> argparse.ArgumentParser:
    parser = parser or argparse.ArgumentParser(
        prog="drpilot",
        description="ADB 滑动截图 -> AI 识别 -> 导出 JSONL（可选 Markdown / index.json）",
        epilog=(
            "子命令：drpilot doctor | devices | check-model | schema | ask。"
            "Agent 调用建议加 --json；缺参数用 drpilot ask --json 拿追问清单。"
        ),
    )
    parser.add_argument("--gui", action="store_true", help="启动图形界面")
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="环境自检：Python/依赖/Key/配置/设备/输出目录/模型，并给出修复建议",
    )
    parser.add_argument("--devices", action="store_true", help="列出已连接的 ADB 设备并退出")
    parser.add_argument(
        "--check-model",
        dest="check_model_only",
        action="store_true",
        help="测试模型连通性（模型是否已下架 / Key 是否可用）并退出",
    )
    parser.add_argument(
        "--schema",
        action="store_true",
        help="打印机器可读的 CLI 契约（参数 / 退出码 / 常用命令）并退出，给 Agent 用",
    )
    parser.add_argument(
        "--ask",
        action="store_true",
        help="只回答「还缺哪些参数、该问用户什么、凑齐后跑什么命令」，不运行",
    )
    parser.add_argument(
        "--text",
        metavar="TEXT",
        help="一句自然语言需求，自动解析成参数（如「药理学第二章 47到81题，存到 D:\\题库」）",
    )
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="stdout 只输出 JSON 结果（日志走 stderr），方便 Agent/脚本解析",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="配合 --doctor：不在第一个失败处停下，所有检查都跑一遍",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只校验配置并计算输出路径，不连接手机、不调用 AI",
    )
    parser.add_argument("--config", metavar="PATH", help="配置文件路径（JSON）")

    parser.add_argument(
        "-i",
        "--interactive",
        dest="interactive",
        action="store_true",
        help="缺参数时逐项向用户提问补齐（TTY 下缺参数也会自动问）",
    )
    parser.add_argument(
        "-y",
        "--yes",
        dest="assume_yes",
        action="store_true",
        help="交互模式不再二次确认，补齐参数后直接开始",
    )
    parser.add_argument(
        "--log-file",
        metavar="PATH",
        help="运行日志写到哪里（默认 logs/drpilot-<时间>.log，JSON 里会回传 log_file）",
    )
    parser.add_argument("--no-log-file", action="store_true", help="不写运行日志文件")
    parser.add_argument("--debug", action="store_true", help="日志记录 DEBUG 与堆栈；JSON 里附带 traceback")

    parser.add_argument("--from", dest="page_from", type=int, help="起始题号（章节内题号）")
    parser.add_argument("--to", dest="page_to", type=int, help="结束题号（章节内题号）")

    parser.add_argument("--textbook", help="教材名称，如 药理学")
    parser.add_argument("--chapter", help="章节名称，如 第二章 药物代谢动力学")
    parser.add_argument("--chapter-no", type=int, help="章号（不填则从章节名自动解析）")
    parser.add_argument("--chapter-total", type=int, help="本章总题数（不填则用预检读到的值）")

    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument(
        "--output",
        dest="output_prefix",
        metavar="PREFIX",
        help="输出文件前缀（不含扩展名），覆盖教材/章节自动命名",
    )
    output_group.add_argument(
        "--output-dir",
        dest="output_dir",
        metavar="DIR",
        help="输出文件夹，文件名由教材/章节自动生成",
    )
    parser.add_argument(
        "--no-md",
        dest="generate_markdown",
        action="store_false",
        default=None,
        help="不生成 Markdown（默认生成）",
    )
    parser.add_argument(
        "--no-index",
        dest="generate_index",
        action="store_false",
        default=None,
        help="不生成 index.json 索引（默认生成）",
    )

    parser.add_argument(
        "--no-preflight",
        dest="preflight",
        action="store_false",
        default=None,
        help="跳过“先截图确认起始题号”的预检",
    )
    parser.add_argument(
        "--force-start",
        dest="force_start",
        action="store_true",
        default=None,
        help="屏幕题号与起始题号不一致时仍然继续",
    )

    parser.add_argument("--model", help="模型 ID")
    parser.add_argument("--base-url", help="API 地址")
    parser.add_argument(
        "--no-model-check",
        dest="check_model",
        action="store_false",
        default=None,
        help="运行前不测试模型连通性（默认会测试）",
    )
    parser.add_argument("--batch-size", type=int, help="每次请求发送的截图数量")
    parser.add_argument("--workers", type=int, help="并发 worker 数（不超过 API Key 数量）")
    wait_group = parser.add_mutually_exclusive_group()
    wait_group.add_argument("--wait", type=float, help="滑动后等待秒数")
    wait_group.add_argument("--wait-ms", type=float, help="滑动后等待毫秒数（1000 = 1 秒）")
    parser.add_argument("--request-timeout", type=float, help="单次 API 请求超时秒数")
    parser.add_argument(
        "--request-deadline",
        type=float,
        help="单次 AI 调用的总时间预算秒数（含重试），0=自动（请求超时 x2 + 10）",
    )
    parser.add_argument("--jpeg-quality", type=int, help="截图转 JPEG 的质量（1-100）")

    parser.add_argument("--max-retries", type=int, help="API 最大重试次数")
    parser.add_argument("--retry-delay", type=float, help="API 重试初始间隔秒数")
    parser.add_argument("--retry-backoff", type=float, help="API 重试退避倍数")

    parser.add_argument("--max-swipe-retries", type=int, help="截图重复时最多重试滑动次数")
    parser.add_argument("--retry-wait", type=float, help="重复截图重试前的等待秒数")
    parser.add_argument("--swipe-start-x", type=int, help="滑动起点 X")
    parser.add_argument("--swipe-start-y", type=int, help="滑动起点 Y")
    parser.add_argument("--swipe-end-x", type=int, help="滑动终点 X")
    parser.add_argument("--swipe-end-y", type=int, help="滑动终点 Y")
    parser.add_argument("--swipe-duration", type=int, dest="swipe_duration_ms", help="滑动时长（毫秒）")
    parser.add_argument(
        "--swipe-reference",
        metavar="WxH",
        help="滑动坐标的基准分辨率，如 1080x2340；不填或 auto 表示首次运行自动记录",
    )
    parser.add_argument(
        "--next-action-file",
        metavar="PATH",
        help='翻页动作 JSON（GUI 录制导出：{"reference":"WxH","steps":[...]} 或裸步骤数组）',
    )
    parser.add_argument(
        "--tap",
        action="append",
        metavar="X,Y",
        help="把点按坐标加入翻页动作（可重复），例如 --tap 630,2380",
    )

    parser.add_argument("--serial", help="ADB 设备 serial（多设备时指定）")
    parser.add_argument(
        "--connect",
        dest="device_address",
        metavar="IP[:PORT]",
        help="运行前先 adb connect 到手机的无线调试地址，例如 192.168.1.5:5555",
    )
    parser.add_argument("--adb", dest="adb_path", help="adb 可执行文件路径")
    parser.add_argument("--version", action="version", version=f"drpilot {__version__}")
    return parser


# 子命令名 -> 等价的顶层开关；这样一套参数定义同时支持
# 「drpilot doctor」和「drpilot --doctor」两种写法。
SUBCOMMAND_FLAGS = {
    "devices": "--devices",
    "doctor": "--doctor",
    "check-model": "--check-model",
    "schema": "--schema",
    "ask": "--ask",
}
# 子命令里允许透传给顶层 parser 的开关（带值的单独列出来）
_SUBCOMMAND_GLOBAL_FLAGS = ("--json", "--full", "--debug", "--no-log-file", "--yes", "-y")
_SUBCOMMAND_VALUE_FLAGS = ("--config", "--log-file")


def _expand_subcommand(argv: Sequence[str]) -> list[str]:
    """把「drpilot <子命令> [...]」翻译成等价的扁平参数。

    只在第一个参数（跳过全局开关后）就是子命令名时才翻译，避免误伤
    --config 后面的路径之类的内容。不认识的参数直接报错退出，
    而不是丢给主 parser 产生莫名其妙的 "unrecognized arguments"。
    ask 是例外：它需要完整的运行参数，一律原样透传。
    """
    items = list(argv)
    if not items:
        return items
    first = next((index for index, token in enumerate(items) if not token.startswith("-")), None)
    if first is None or items[first] not in SUBCOMMAND_FLAGS:
        return items
    command = items[first]
    rest = items[first + 1:]
    forwarded = [SUBCOMMAND_FLAGS[command]]
    if command in PASSTHROUGH_SUBCOMMANDS:
        return forwarded + list(rest)
    index = 0
    while index < len(rest):
        token = rest[index]
        if token in _SUBCOMMAND_VALUE_FLAGS:         # 带值开关要先处理，否则会把值当未知参数
            if index + 1 >= len(rest):
                raise ConfigError(f"{token} 后面缺少值")
            forwarded.extend([token, rest[index + 1]])
            index += 1
        elif token in _SUBCOMMAND_GLOBAL_FLAGS:
            forwarded.append(token)
        else:
            allowed = " / ".join(_SUBCOMMAND_GLOBAL_FLAGS + _SUBCOMMAND_VALUE_FLAGS)
            raise ConfigError(f"{command} 不接受参数 {token!r}（只支持 {allowed}）")
        index += 1
    return forwarded


# ---------------- 输出助手 ----------------
class _Output:
    """统一的输出口：人读模式走 stdout，--json 模式把过程写 stderr。

    同时把每条消息写进运行日志（RunLogger），并负责组织结构化错误。
    """

    def __init__(self, json_mode: bool = False, logger: RunLogger | None = None, debug: bool = False) -> None:
        self.json_mode = json_mode
        self.logger = logger
        self.debug = bool(debug)
        self.result: dict[str, Any] | None = None

    def _log(self, message: str, level: str) -> None:
        if self.logger is not None:
            self.logger.log(message, level)
            if level == "ERROR":
                self.logger.set_phase("error")

    def info(self, message: str = "") -> None:
        self._log(message, "INFO")
        print(message, file=sys.stderr if self.json_mode else sys.stdout)

    def warn(self, message: str) -> None:
        self._log(message, "WARN")
        print(message, file=sys.stderr)

    def error(self, message: str) -> None:
        self._log(message, "ERROR")
        print(message, file=sys.stderr)

    def set_result(self, payload: dict[str, Any]) -> None:
        self.result = payload

    def fail(
        self,
        exit_code: int,
        error_code: str,
        message: str,
        *,
        phase: str | None = None,
        suggestion: str | None = None,
        context: dict[str, Any] | None = None,
        missing: list[Any] | None = None,
        questions: list[Any] | None = None,
        hint: str | None = None,
        extra: dict[str, Any] | None = None,
        exc: BaseException | None = None,
    ) -> int:
        """统一的失败出口：人读 + 结构化（error_code / phase / context / suggestion）。"""
        if self.logger is not None and phase:
            self.logger.set_phase(phase)
        self.error(f"出错（{error_code}）：{message}")
        if hint:
            self.error(hint)
        if suggestion:
            self.error(f"建议：{suggestion}")
        payload: dict[str, Any] = {"ok": False, "error": message, "error_code": error_code}
        if phase:
            payload["phase"] = phase
        if missing:
            payload["missing"] = missing
        if questions:
            payload["questions"] = questions
        if hint:
            payload["hint"] = hint
        if suggestion:
            payload["suggestion"] = suggestion
        if context:
            payload["context"] = context
        if extra:
            payload.update(extra)
        if exc is not None and self.debug:
            payload["traceback"] = "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            )
        self.set_result(payload)
        return self.emit(exit_code)

    def emit(self, exit_code: int) -> int:
        """输出最终结果（JSON 模式），返回退出码。"""
        if self.json_mode and self.result is not None:
            payload = dict(self.result)
            payload.setdefault("ok", exit_code == exitcodes.SUCCESS)
            payload["exit_code"] = exit_code
            payload["exit_code_name"] = exitcodes.name(exit_code)
            if self.logger is not None and self.logger.path is not None:
                payload.setdefault("log_file", str(self.logger.path))
            print(jsonlib.dumps(payload, ensure_ascii=False, indent=2))
        elif self.logger is not None and self.logger.path is not None:
            if exit_code != exitcodes.SUCCESS or self.debug:
                print(f"日志文件：{self.logger.path}", file=sys.stderr)
        return exit_code


def _print_progress(current: int, total: int, number: int) -> None:
    percent = int(current / total * 100) if total else 0
    print(f"[进度] {current}/{total}（{percent}%）预期第 {number} 题", file=sys.stderr)


# ---------------- 参数缺口分析 ----------------
def _missing_params(args: argparse.Namespace, config_data: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """列出还没凑齐的参数（必填在前），供 --ask / 交互式提问 / 报错复用。

    config_data：配置文件里的已有值，有值就不算缺。
    """
    provided = {key for key, value in (config_data or {}).items() if value not in (None, "", [], {})}

    def supplied(dest: str) -> bool:
        value = getattr(args, dest, None)
        return value not in (None, "") or dest in provided

    missing: list[dict[str, Any]] = []
    if not supplied("page_from"):
        missing.append(contract.missing_param("page_from"))
    if not supplied("page_to"):
        missing.append(contract.missing_param("page_to"))
    if not supplied("output_dir") and not supplied("output_prefix"):
        missing.append(contract.missing_param("output_dir"))
    for dest in ("textbook", "chapter"):
        if not supplied(dest):
            missing.append(contract.missing_param(dest))
    return missing


_CIRCLED = "①②③④⑤⑥⑦⑧⑨"

def _ask_user_sentence(missing: Sequence[dict[str, Any]]) -> str:
    """把缺口拼成一句可以直接发给用户的话（带参数名，Agent 好照抄）。"""
    required = [item for item in missing if item.get("required")]
    if not required:
        return "参数已齐全，可以直接运行。"
    parts = []
    for index, item in enumerate(required):
        mark = _CIRCLED[index] if index < len(_CIRCLED) else f"({index + 1})"
        parts.append(f"{mark} {item['param']}（{item['flag']}）：{item['question']}")
    return "请补充：" + " ".join(parts)


def _run_command_line(args: argparse.Namespace, *, template: bool = False) -> str:
    """按当前参数拼一条可复制运行的命令。

    template=True 用占位符（并带 --dry-run），方便 Agent 先把命令给用户看；
    template=False 用真实值，缺的项省略。
    """
    def value(dest: str, placeholder: str) -> Any:
        current = getattr(args, dest, None)
        if template:
            return placeholder
        return current if current not in (None, "") else None

    values = {
        "--from": value("page_from", "<起始题号>"),
        "--to": value("page_to", "<结束题号>"),
        "--output-dir": value("output_dir", "<输出文件夹>"),
        "--textbook": value("textbook", "<教材>"),
        "--chapter": value("chapter", "<章节>"),
        "--connect": value("device_address", "") if template else value("device_address", ""),
    }
    return contract.format_command(values, extra=["--dry-run"] if template else [])


def _retry_command(config: AppConfig, numbers: Sequence[int]) -> str | None:
    """批量失败后的补录命令（同一输出目录，按题号合并写回）。"""
    valid = sorted({int(n) for n in numbers if int(n) > 0})
    if not valid:
        return None
    values = {
        "--from": valid[0],
        "--to": valid[-1],
        "--output-dir": config.output_dir or None,
        "--textbook": config.textbook or None,
        "--chapter": config.chapter or None,
    }
    return contract.format_command(values)


# ---------------- 子命令实现 ----------------
def _list_devices(args: argparse.Namespace, out: _Output) -> int:
    from .adb import AdbClient, format_devices

    try:
        client = AdbClient(adb_path=args.adb_path, serial=args.serial)
        if args.device_address:
            out.info(f"正在连接 {args.device_address} …")
            out.info(client.connect(args.device_address))
        devices = client.devices()
    except AdbError as exc:
        return out.fail(
            exitcodes.ENV_UNAVAILABLE,
            "adb_unavailable",
            f"ADB 错误：{exc}",
            phase="connect",
            suggestion="确认 adb 已安装并加入 PATH；USB 连接检查手机授权；无线调试用 --connect IP:5555",
            extra={"devices": [], "ready": 0},
            exc=exc,
        )

    out.info(format_devices(devices))
    ready = [device for device in devices if device.ready]
    sizes: dict[str, str] = {}
    for device in ready:
        try:
            size = AdbClient(adb_path=client.adb_path, serial=device.serial).wm_size()
        except Exception:
            size = None
        if size:
            sizes[device.serial] = f"{size[0]}x{size[1]}"
            out.info(f"    {device.serial} 分辨率：{sizes[device.serial]}")
    if not ready:
        out.warn("提示：USB 连接请检查授权；无线调试可用 --connect 192.168.1.5:5555 先连接。")
    out.set_result(
        {
            "devices": [
                {"serial": d.serial, "state": d.state, "model": d.model, "ready": d.ready}
                for d in devices
            ],
            "ready": len(ready),
            "resolution": sizes,
            "adb": client.adb_path,
        }
    )
    return out.emit(exitcodes.SUCCESS if ready else exitcodes.ENV_UNAVAILABLE)


def _doctor_command(args: argparse.Namespace, out: _Output) -> int:
    from .doctor import run_doctor

    full = bool(getattr(args, "full", False))
    probe_config: AppConfig | None = None
    used_path = args.config
    if args.config:
        try:
            probe_config, used_path = load_config_with_fallback(args.config)
        except ConfigError as exc:
            return out.fail(
                exitcodes.CONFIG_INVALID,
                "config_file_invalid",
                f"配置错误：{exc}",
                phase="config",
                suggestion="检查 --config 指向的 JSON 文件是否存在、能否解析",
                exc=exc,
            )

    report = run_doctor(
        config=probe_config,
        config_path=used_path,
        full=full,
        check_model=args.check_model is not False,
    )
    out.info(report.render())
    payload = report.as_dict()
    payload["summary"] = report.summary_text()
    out.set_result(payload)
    return out.emit(report.exit_code)


def _check_model_command(config: AppConfig, out: _Output) -> int:
    from .ai import check_model_connection

    if not config.api_keys:
        return out.fail(
            exitcodes.ENV_UNAVAILABLE,
            "missing_api_key",
            "未找到 API Key",
            phase="env",
            suggestion="在 GUI 的「模型服务」卡片点「＋ 新增 API Key」，或把 Key 写进 .env",
            extra={"model": config.model, "base_url": config.base_url},
        )
    out.info(f"正在测试模型：{config.model}")
    out.info(f"API 地址：{config.base_url}")
    try:
        result = check_model_connection(
            config.api_keys[0],
            config.base_url,
            config.model,
            timeout=min(float(config.request_timeout), 30.0),
        )
    except Exception as exc:
        return out.fail(
            exitcodes.ENV_UNAVAILABLE,
            "model_check_failed",
            f"模型连通性测试出错：{exc}",
            phase="model",
            suggestion="检查网络 / API 地址；也可以跑 drpilot doctor --json 看完整自检",
            extra={"model": config.model, "base_url": config.base_url},
            exc=exc,
        )
    out.info(("OK  " if result.ok else "FAIL ") + result.summary())
    if result.suggestions:
        out.info("  平台上的相似模型：" + "、".join(result.suggestions))
    if not result.ok:
        out.warn("  建议：" + result.hint())
    out.set_result(
        {
            "ok": result.ok,
            "model": result.model,
            "base_url": result.base_url,
            "detail": result.detail,
            "latency_ms": result.latency_ms,
            "model_listed": result.model_listed,
            "suggestions": list(result.suggestions),
        }
    )
    if not result.ok:
        return out.fail(
            exitcodes.ENV_UNAVAILABLE,
            "model_unavailable",
            f"模型不可用：{result.detail}",
            phase="model",
            suggestion=result.hint() or "换一个平台上的可用模型，或检查 API Key",
        )
    return out.emit(exitcodes.SUCCESS)


def _schema_command(args: argparse.Namespace, out: _Output) -> int:
    """打印机器可读的 CLI 契约（给 Agent 用）。"""
    schema = contract.build_schema(build_parser())
    out.set_result(schema)
    if args.json_output:
        return out.emit(exitcodes.SUCCESS)
    out.info(f"DrPilot {__version__} CLI 契约（schema_version={schema['schema_version']}）")
    out.info("用法：" + contract.RUN_PREFIX + " [参数] --json")
    out.info("")
    out.info("必填参数（跑一次提取所需）：")
    for spec in schema["params"]:
        if not spec["required_for_run"]:
            continue
        out.info(f"  {spec['name']:<16} {spec['help']}    例：{spec['example']}")
    out.info("")
    out.info("常用套路：")
    for flow in schema["workflows"]:
        out.info(f"  · {flow['intent']}")
        out.info(f"    {flow['command']}")
    out.info("")
    out.info("危险参数（用之前先跟用户确认）：")
    for name, danger in schema["dangerous_params"].items():
        out.info(f"  --{name.replace('_', '-')}：{danger}")
    out.info("")
    out.info("退出码：" + "，".join(f"{item['code']}={item['name']}（{item['meaning']}）" for item in contract.EXIT_CODES))
    return out.emit(exitcodes.SUCCESS)


def _ask_command(args: argparse.Namespace, out: _Output) -> int:
    """只回答：还缺什么、该问用户什么、凑齐后跑什么命令。"""
    config_data: dict[str, Any] = {}
    if args.config:
        try:
            config_data = load_config_file(args.config)
        except ConfigError as exc:
            return out.fail(
                exitcodes.CONFIG_INVALID,
                "config_file_invalid",
                f"配置错误：{exc}",
                phase="config",
                suggestion="检查 --config 指向的 JSON 文件",
                exc=exc,
            )
    missing = _missing_params(args, config_data)
    required_missing = [item for item in missing if item.get("required")]
    ready = not required_missing
    key_count = len(load_api_keys())
    notes: list[str] = []
    if not ready:
        notes.append("补齐 --from / --to / --output-dir 之后才能跑；不要用默认值猜题号范围。")
    if not (args.textbook or config_data.get("textbook")):
        notes.append("未提供教材名：文件名会退化成「题目_起-止」，且缺少教材元数据。")
    if not (args.chapter or config_data.get("chapter")):
        notes.append("未提供章节名：缺少章节元数据，补录时容易和目标文件对不上。")
    if key_count == 0:
        notes.append("当前没有可用的 API Key：先在 GUI「模型服务」里新增，或写进 .env。")
    payload: dict[str, Any] = {
        "mode": "ask",
        "ready": ready,
        "missing": missing,
        "required_missing": required_missing,
        "questions": [item["question"] for item in required_missing],
        "recommended_questions": [item["question"] for item in missing if not item.get("required")],
        "ask_user": _ask_user_sentence(missing),
        "missing_flags": [item["flag"] for item in required_missing],
        "command": _run_command_line(args) if ready else None,
        "command_template": _run_command_line(args, template=True),
        "key_count": key_count,
        "notes": notes,
    }
    out.set_result(payload)
    if not args.json_output:
        out.info(payload["ask_user"])
        for note in notes:
            out.info("  · " + note)
        if ready and payload["command"]:
            out.info("可直接运行：" + str(payload["command"]))
    return out.emit(exitcodes.SUCCESS if ready else exitcodes.CONFIG_INVALID)


# ---------------- 参数装配 ----------------
def _load_next_action_file(path: str, parser: argparse.ArgumentParser) -> tuple[list[Step], tuple[int, int] | None]:
    """读取 GUI 录制导出的翻页动作文件，返回 (步骤, 基准分辨率)。"""
    from .fileio import read_json_file

    try:
        data = read_json_file(path)
    except Exception as exc:
        parser.error(f"读取翻页动作文件失败：{path}（{exc}）")
        raise  # parser.error 已经退出；这里只是让类型检查安心
    reference = parse_size(data.get("reference")) if isinstance(data, dict) and data.get("reference") else None
    steps = payload_to_steps(data)
    if not steps:
        parser.error(f"翻页动作文件里没有可用步骤：{path}")
    return steps, reference


def _tap_steps(values: Sequence[str], parser: argparse.ArgumentParser) -> list[Step]:
    steps: list[Step] = []
    for item in values:
        parts = str(item).replace("，", ",").split(",")
        numbers = [part.strip() for part in parts]
        if len(numbers) != 2 or not all(number.lstrip("-").isdigit() for number in numbers):
            parser.error(f"--tap 格式应为 X,Y，例如 630,2380（收到 {item!r}）")
        steps.append(
            Step(kind="tap", x=int(numbers[0]), y=int(numbers[1]), duration_ms=80, absolute=True)
        )
    return steps


def _build_config(args: argparse.Namespace, parser: argparse.ArgumentParser) -> tuple[AppConfig, str | None]:
    """把命令行参数叠加到配置文件上，并载入 API Key。"""
    base, used_path = load_config_with_fallback(args.config)
    overrides = {
        key: value
        for key, value in vars(args).items()
        if value is not None and key in AppConfig.field_names()
    }
    action_steps: list[Step] = []
    action_reference: tuple[int, int] | None = None
    if args.next_action_file:
        action_steps, action_reference = _load_next_action_file(args.next_action_file, parser)
    if args.tap:
        action_steps = action_steps + _tap_steps(args.tap, parser)
    if action_steps:
        overrides["next_action"] = steps_to_payload(action_steps[:32])
    if action_reference:
        overrides["swipe_reference_width"], overrides["swipe_reference_height"] = action_reference
    # 只给了 --chapter 没给 --chapter-no 时，章号必须从新的章节名重新解析。
    # 否则配置文件里残留的 chapter_no 会继续生效，文件名悄悄变成「第三章 细菌的遗传与变异」
    # 却配上旧章号（例如 05），用户看到的文件名和实际章节对不上。
    if args.chapter is not None and args.chapter_no is None:
        derived = parse_chapter_no(args.chapter)
        if derived > 0:
            overrides["chapter_no"] = derived
    if args.wait_ms is not None:
        overrides["wait"] = float(args.wait_ms) / 1000.0
    if args.swipe_reference:
        text = args.swipe_reference.strip().lower()
        if text in ("auto", "0", "0x0"):
            overrides["swipe_reference_width"] = 0
            overrides["swipe_reference_height"] = 0
        else:
            size = parse_size(args.swipe_reference)
            if not size:
                parser.error("--swipe-reference 格式应为 宽x高（如 1080x2340），或 auto")
            overrides["swipe_reference_width"], overrides["swipe_reference_height"] = size
    # --output 与 --output-dir 互斥：显式指定的那个要清掉配置文件里的另一个，
    # 否则旧配置里的 output_prefix 会悄悄覆盖命令行的 --output-dir。
    if args.output_dir:
        overrides["output_prefix"] = ""
    elif args.output_prefix:
        overrides["output_dir"] = ""
    config = base.replace(overrides)
    config.api_keys = load_api_keys()
    return config, used_path


def _missing_scope_error(missing: Sequence[dict[str, Any]]) -> str:
    """缺少必要参数时给出「照抄就能跑」的命令示例。"""
    flags = "、".join(item["flag"] for item in missing if item.get("required"))
    return (
        "缺少必要参数：" + flags + "\n"
        "示例：drpilot --from 47 --to 81 --output-dir D:\\题库 "
        "--textbook 药理学 --chapter \"第二章 药物代谢动力学\"\n"
        "也可以直接说人话：drpilot --text \"药理学第二章 47到81题，存到 D:\\题库\"\n"
        "或指定已有配置文件：drpilot --config drpilot_config.json"
    )


def _apply_natural_language(args: argparse.Namespace, out: _Output) -> None:
    """--text：一句自然语言 -> 参数；显式命令行参数优先，绝不覆盖。"""
    text = getattr(args, "text", None)
    if not text:
        return
    parsed = parse_request(text)
    applied: dict[str, Any] = {}
    for dest, value in parsed["params"].items():
        if dest in ("dry_run", "force_start"):
            if not bool(getattr(args, dest, False)):
                setattr(args, dest, value)
                applied[dest] = value
            continue
        if getattr(args, dest, None) in (None, ""):
            setattr(args, dest, value)
            applied[dest] = value
    if getattr(out, "logger", None) is not None:
        out.logger.log(f"自然语言输入：{mask_secrets(text)}", "DEBUG")
        out.logger.log(f"自然语言解析：{describe_params(applied)}", "DEBUG")
    out.info(f"[自然语言] 解析出：{describe_params(applied)}")
    if parsed["evidence"]:
        out.info("[自然语言] 依据：" + "；".join(f"{key}←{value}" for key, value in parsed["evidence"].items()))


def _interactive_fill(args: argparse.Namespace, missing: Sequence[dict[str, Any]], out: _Output) -> bool:
    """缺参数时逐项问用户；返回是否补齐。"""
    dests = [item["dest"] for item in missing]
    # 必填的问完再顺带问教材/章节，避免来回跑两趟
    for dest in ("textbook", "chapter"):
        if dest not in dests:
            dests.append(dest)
    out.info("参数还不齐，下面几个问题填一下就能开始（Ctrl+C 取消）：")
    answers = interactive.collect(
        dests,
        ask=input,
        say=lambda message: out.info(message),
        initial={
            "page_from": args.page_from,
            "page_to": args.page_to,
            "output_dir": args.output_dir,
            "textbook": args.textbook,
            "chapter": args.chapter,
            "device_address": args.device_address,
        },
    )
    if answers is None:
        out.error("交互式补参被取消，没有运行。")
        return False
    for dest, value in answers.items():
        setattr(args, dest, value)
    return True


def _run_command(args: argparse.Namespace, parser: argparse.ArgumentParser, out: _Output) -> int:
    config_data: dict[str, Any] = {}
    if args.config:
        try:
            config_data = load_config_file(args.config)
        except ConfigError as exc:
            return out.fail(
                exitcodes.CONFIG_INVALID,
                "config_file_invalid",
                f"读取配置文件失败：{exc}",
                phase="config",
                suggestion="检查 --config 指向的 JSON 文件；不确定就删掉 --config 改用命令行参数",
                exc=exc,
            )

    missing = _missing_params(args, config_data)
    required_missing = [item for item in missing if item.get("required")]
    if required_missing:
        want_interactive = bool(args.interactive) or (interactive.is_tty() and not args.json_output)
        if want_interactive:
            if not _interactive_fill(args, required_missing, out):
                return out.fail(
                    exitcodes.CONFIG_INVALID,
                    "cancelled_by_user",
                    "用户取消了参数补全",
                    phase="args",
                    suggestion="想一次问完：drpilot ask --json；想直接改参数：看 drpilot schema --json",
                )
            missing = _missing_params(args, config_data)
            required_missing = [item for item in missing if item.get("required")]
        if required_missing:
            problem = _missing_scope_error(missing)
            return out.fail(
                exitcodes.CONFIG_INVALID,
                "missing_required_arguments",
                "缺少必要参数：" + "、".join(item["flag"] for item in required_missing),
                phase="args",
                missing=[item["flag"] for item in required_missing],
                questions=[item["question"] for item in required_missing],
                hint=problem,
                suggestion="按 questions 补齐后重跑；或跑 drpilot ask --json 让 Agent 生成追问。",
            )

    try:
        config, used_path = _build_config(args, parser)
    except ConfigError as exc:
        return out.fail(
            exitcodes.CONFIG_INVALID,
            "config_invalid",
            f"配置错误：{exc}",
            phase="config",
            suggestion="检查参数组合；drpilot schema --json 有完整参数表",
            exc=exc,
        )

    try:
        config.validate(require_keys=True)
    except ConfigError as exc:
        message = str(exc)
        code = "missing_api_key" if not config.api_keys else "config_invalid"
        suggestion = (
            "在 GUI「模型服务」卡片点「＋ 新增 API Key」，或把 Key 写进 .env；再跑 drpilot doctor --json"
            if code == "missing_api_key"
            else "按提示修正参数后重跑；先跑 drpilot doctor --json 看环境"
        )
        return out.fail(
            exitcodes.CONFIG_INVALID if code != "missing_api_key" else exitcodes.ENV_UNAVAILABLE,
            code,
            message,
            phase="env" if code == "missing_api_key" else "config",
            suggestion=suggestion,
            exc=exc,
        )

    if used_path:
        out.info(f"已加载配置：{used_path}")
    if not config.textbook and not config.chapter:
        out.warn("[提示] 未设置 --textbook/--chapter，文件名将使用 题目_起-止，且缺少教材元数据。")
    if args.chapter and args.chapter_total is None and config.chapter_total > 0 and used_path:
        out.warn(
            f"[提示] 本次指定了新的章节，但「本章总题数」仍是配置文件里的 {config.chapter_total}；"
            "如与实际不符，请加 --chapter-total N 覆盖。"
        )

    paths = resolve_output_paths(config)
    if args.dry_run:
        out.info("[dry-run] 配置校验通过，未连接手机、未调用 AI。")
        out.info(f"[dry-run] 输出目录：{paths.directory}")
        out.info(f"[dry-run] 将生成：{paths.jsonl}" + (f" / {paths.markdown}" if paths.markdown else ""))
        out.set_result(
            {
                "dry_run": True,
                "page_from": config.page_from,
                "page_to": config.page_to,
                "total": config.total,
                "model": config.model,
                "base_url": config.base_url,
                "output_dir": paths.directory,
                "jsonl": paths.jsonl,
                "markdown": paths.markdown,
                "index": paths.index,
                "key_count": len(config.api_keys),
                "questions": 0,
            }
        )
        return out.emit(exitcodes.SUCCESS)

    if args.interactive and not args.assume_yes:
        command = _run_command_line(args) or contract.format_command(
            {"--from": config.page_from, "--to": config.page_to, "--output-dir": config.output_dir}
        )
        if not interactive.confirm(f"将执行：{command}\n现在开始？", ask=input, say=lambda message: out.info(message)):
            return out.fail(
                exitcodes.CONFIG_INVALID,
                "cancelled_by_user",
                "用户在确认时取消",
                phase="args",
                suggestion="确认无误后重跑，或加 -y 跳过确认",
            )

    from .pipeline import PilotSession

    preflight_rejected = False
    preflight_detail = ""

    def confirm_preflight(screen_id: int, expected: int) -> bool:
        nonlocal preflight_rejected, preflight_detail
        if config.force_start:
            out.warn(f"[警告] 屏幕显示第 {screen_id} 题，设置为 {expected} 题；已指定 --force-start，继续执行。")
            return True
        preflight_rejected = True
        preflight_detail = f"屏幕显示第 {screen_id} 题，起始题号设置为 {expected} 题"
        out.error(f"[警告] {preflight_detail}。")
        out.error("请把手机停在正确的题目后重跑，或加 --force-start 强制继续。")
        return False

    session = PilotSession(
        config,
        on_log=out.info,
        on_progress=_print_progress,
        on_preflight=confirm_preflight,
        on_error=lambda exc: out.error(f"错误：{exc}"),
    )
    ok = session.run()
    if session.reference_recorded:
        try:
            saved = save_config_file(args.config or default_config_path(), config)
            out.info(f"滑动基准分辨率已写入配置：{saved}")
        except Exception as exc:
            out.warn(f"[提示] 滑动基准分辨率保存失败：{exc}")

    diagnostics = session.diagnostics()
    failed_numbers = diagnostics.get("failed_question_numbers") or []
    payload: dict[str, Any] = {
        "page_from": config.page_from,
        "page_to": config.page_to,
        "questions": len(session.questions),
        "jsonl": paths.jsonl,
        "markdown": paths.markdown,
        "index": paths.index,
        "reference_recorded": bool(session.reference_recorded),
        "stopped": bool(session.stop_event.is_set()),
        "error": str(session.error) if session.error else None,
        "diagnostics": diagnostics,
    }
    if failed_numbers:
        payload["failed_question_numbers"] = failed_numbers
        payload["retry_command"] = _retry_command(config, failed_numbers)
    if diagnostics.get("missing_question_numbers"):
        payload["missing_question_numbers"] = diagnostics["missing_question_numbers"]
    out.set_result(payload)

    if preflight_rejected and not ok:
        return out.fail(
            exitcodes.PRECHECK_FAILED,
            "precheck_failed",
            preflight_detail or "起始题号预检未通过",
            phase="preflight",
            context={"expected": config.page_from, "screen_id": preflight_detail},
            suggestion="把手机停在起始题那页再重跑；确认无误才加 --force-start",
            extra=payload,
        )
    if ok:
        return out.emit(exitcodes.SUCCESS)

    exc = session.error
    code, phase = _error_code_for(exc, diagnostics)
    return out.fail(
        exitcodes.RUN_FAILED,
        code,
        f"运行失败：{exc}" if exc else "运行失败",
        phase=phase,
        context=diagnostics.get("error_context") or None,
        suggestion=_run_failure_suggestion(code, payload),
        extra=payload,
        exc=exc,
    )


def _error_code_for(exc: BaseException | None, diagnostics: dict[str, Any]) -> tuple[str, str]:
    """异常 -> (机器可读错误码, 阶段)。"""
    phase = str(diagnostics.get("phase") or "run")
    if isinstance(exc, AdbError):
        return "adb_error", phase
    if isinstance(exc, AiError):
        return "ai_error", phase
    if isinstance(exc, OutputError):
        return "output_error", phase
    if isinstance(exc, ConfigError):
        return "config_invalid", "config"
    return "run_failed", phase


def _run_failure_suggestion(code: str, payload: dict[str, Any]) -> str:
    if payload.get("retry_command"):
        return f"先修问题，再用这条命令补录失败题号：{payload['retry_command']}"
    if code == "adb_error":
        return "检查手机连接（drpilot devices --json），确认屏幕没锁、无线调试没掉线"
    if code == "ai_error":
        return "跑 drpilot check-model --json 确认模型可用；看 diagnostics.error_context 里的题号"
    if code == "output_error":
        return "检查输出目录是否存在、是否有写权限、磁盘是否满"
    return "读 log_file 日志定位；失败题号在 diagnostics.error_context.question_numbers 里"


# ---------------- 入口 ----------------
def main(argv: Sequence[str] | None = None) -> int:
    _configure_console()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    try:
        items = _expand_subcommand(raw_argv)
    except ConfigError as exc:
        print(f"参数错误：{exc}", file=sys.stderr)
        return exitcodes.CONFIG_INVALID

    parser = build_parser()
    json_requested = "--json" in items
    try:
        args = parser.parse_args(items)
    except SystemExit as exc:
        # argparse 自己报的错（拼写错误 / 互斥参数冲突）也给一份 JSON，Agent 好处理
        code = int(exc.code or 0)
        if code != 0 and json_requested:
            print(
                jsonlib.dumps(
                    {
                        "ok": False,
                        "error_code": "invalid_arguments",
                        "error": "命令行参数不合法（详情见 stderr 的用法说明）",
                        "phase": "args",
                        "suggestion": "跑 drpilot schema --json 查看完整参数表与示例",
                        "exit_code": exitcodes.CONFIG_INVALID,
                        "exit_code_name": exitcodes.name(exitcodes.CONFIG_INVALID),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return exitcodes.CONFIG_INVALID
        return code

    json_mode = bool(args.json_output)
    logger = RunLogger(
        path=args.log_file,
        enabled=not args.no_log_file,
        verbose=bool(args.debug),
        echo=False,
    )
    out = _Output(json_mode=json_mode, logger=logger, debug=bool(args.debug))
    logger.log(f"命令行：{mask_secrets(' '.join(raw_argv)) or '(无参数)'}", "DEBUG")
    try:
        loaded = load_dotenv_candidates()
        if loaded:
            logger.log("已加载环境变量文件：" + "、".join(loaded), "DEBUG")
            if not json_mode:
                out.info("已加载环境变量：" + "、".join(loaded))

        if args.text:
            _apply_natural_language(args, out)

        if args.schema:
            return _schema_command(args, out)

        if args.ask:
            return _ask_command(args, out)

        if args.doctor:
            return _doctor_command(args, out)

        if args.devices:
            return _list_devices(args, out)

        if args.check_model_only:
            try:
                config, used_path = _build_config(args, parser)
            except ConfigError as exc:
                return out.fail(
                    exitcodes.CONFIG_INVALID,
                    "config_invalid",
                    f"配置错误：{exc}",
                    phase="config",
                    suggestion="检查参数组合；drpilot schema --json 有完整参数表",
                    exc=exc,
                )
            if used_path:
                out.info(f"已加载配置：{used_path}")
            return _check_model_command(config, out)

        if args.gui:
            if json_mode:
                return out.fail(
                    exitcodes.CONFIG_INVALID,
                    "gui_conflicts_with_json",
                    "--gui 不能和 --json 一起用：图形界面是给人用的",
                    phase="args",
                    suggestion="去掉 --json 再启动图形界面",
                )
            try:
                from .webui import run_web_ui

                return run_web_ui(config_path=args.config)
            except Exception as exc:  # 缺少 pywebview、WebView2 运行时缺失等
                return out.fail(
                    exitcodes.ENV_UNAVAILABLE,
                    "gui_unavailable",
                    f"启动图形界面失败：{exc}",
                    phase="gui",
                    suggestion="确认已 uv sync（安装 pywebview）；Windows 需要 Edge WebView2 运行时",
                    exc=exc,
                )

        return _run_command(args, parser, out)
    finally:
        logger.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
