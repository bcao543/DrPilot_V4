# -*- coding: utf-8 -*-
"""drpilot doctor：一条命令把「跑之前必须成立的事」全查一遍。

给两类人用：
    * 人：直接看彩色/分行摘要，照着「修复建议」做；
    * Agent：--json 拿到结构化结果 + 退出码，判断该改参数、修环境还是直接跑。

设计原则：
    * 所有探测都走可注入的 adapter，测试里不碰真机、不联网、不读真实 .env；
    * 默认在第一个 fail 处停（附 remaining 列表），--full 才查全部，避免开局慢；
    * 任何日志/报告都只出现脱敏后的 Key。
"""

from __future__ import annotations

import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import exitcodes
from .ai import ModelCheckResult, check_model_connection
from .config import (
    AppConfig,
    load_api_keys,
    load_config_with_fallback,
    resolve_output_paths,
)
from .errors import ConfigError
from .keys import mask_keys

REQUIRED_PYTHON = (3, 13)
# 运行必需的第三方依赖（缺哪个都会在跑起来之后才炸，所以提前查）
REQUIRED_MODULES = (
    ("httpx", "httpx"),
    ("openai", "openai"),
    ("dotenv", "python-dotenv"),
    ("PIL", "pillow"),
)
# 只有 GUI 需要，缺失不算环境不可用
OPTIONAL_MODULES = (("webview", "pywebview"),)

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"
STATUS_SKIP = "skip"

_TONE_MARK = {STATUS_OK: "✓", STATUS_WARN: "!", STATUS_FAIL: "✗", STATUS_SKIP: "-"}

CHECK_TITLES = {
    "python": "Python 版本",
    "dependencies": "依赖包",
    "keys": "API Key",
    "config": "配置校验",
    "adb": "ADB / 设备",
    "output": "输出目录",
    "model": "模型连通性",
}

AdbProbe = Callable[[AppConfig], tuple[str, str, str]]
ModelProbe = Callable[[AppConfig, str], tuple[str, str, str]]


@dataclass
class CheckResult:
    """单项检查结果。"""

    name: str
    status: str
    detail: str
    hint: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    skipped_reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status in (STATUS_OK, STATUS_WARN, STATUS_SKIP)

    @property
    def title(self) -> str:
        return CHECK_TITLES.get(self.name, self.name)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
        }
        if self.hint:
            payload["hint"] = self.hint
        if self.data:
            payload["data"] = self.data
        if self.skipped_reason:
            payload["skipped_reason"] = self.skipped_reason
        return payload


@dataclass
class DoctorReport:
    """一次 doctor 运行的全部结果。"""

    checks: list[CheckResult]
    config_path: str | None = None
    full: bool = False

    @property
    def failed(self) -> list[CheckResult]:
        return [c for c in self.checks if c.status == STATUS_FAIL]

    @property
    def warnings(self) -> list[CheckResult]:
        return [c for c in self.checks if c.status == STATUS_WARN]

    @property
    def remaining(self) -> list[str]:
        """因为提前停下而没跑的检查名。"""
        done = {c.name for c in self.checks}
        return [name for name in CHECK_TITLES if name not in done]

    @property
    def ok(self) -> bool:
        return not self.failed

    @property
    def exit_code(self) -> int:
        """按重要性把失败映射到退出码：先配置错误，再环境问题。"""
        by_name = {c.name: c for c in self.checks}
        if by_name.get("config") and by_name["config"].status == STATUS_FAIL:
            return exitcodes.CONFIG_INVALID
        for name in ("python", "dependencies", "keys", "adb", "model"):
            item = by_name.get(name)
            if item and item.status == STATUS_FAIL:
                return exitcodes.ENV_UNAVAILABLE
        if by_name.get("output") and by_name["output"].status == STATUS_FAIL:
            return exitcodes.RUN_FAILED
        return exitcodes.SUCCESS

    def counts(self) -> dict[str, int]:
        counts = {STATUS_OK: 0, STATUS_WARN: 0, STATUS_FAIL: 0, STATUS_SKIP: 0}
        for check in self.checks:
            counts[check.status] = counts.get(check.status, 0) + 1
        return counts

    def summary_text(self) -> str:
        counts = self.counts()
        if self.ok:
            head = "环境自检通过"
            if counts[STATUS_WARN]:
                head += f"（{counts[STATUS_WARN]} 项提醒）"
        else:
            head = f"环境自检未通过（{counts[STATUS_FAIL]} 项失败）"
        return head + f" · 退出码 {self.exit_code}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "exit_code_name": exitcodes.name(self.exit_code),
            "full": self.full,
            "config_path": self.config_path,
            "counts": self.counts(),
            "checks": [check.as_dict() for check in self.checks],
            "remaining": self.remaining,
        }

    def render(self) -> str:
        """人读报告：每个检查一行，失败项附修复建议。"""
        lines: list[str] = []
        for check in self.checks:
            mark = _TONE_MARK.get(check.status, "?")
            lines.append(f"[{mark}] {check.title}：{check.detail}")
            if check.skipped_reason:
                lines.append(f"      跳过：{check.skipped_reason}")
            if check.hint and check.status in (STATUS_FAIL, STATUS_WARN):
                for tip_line in check.hint.splitlines():
                    lines.append(f"      建议：{tip_line}")
        if self.remaining:
            lines.append(f"（已跳过后续检查：{', '.join(CHECK_TITLES[n] for n in self.remaining)}；加 --full 可全部检查）")
        lines.append(self.summary_text())
        return "\n".join(lines)


# ---------------- 各检查的默认实现 ----------------
def _python_check() -> CheckResult:
    version = sys.version_info
    detail = f"Python {version.major}.{version.minor}.{version.micro}（{sys.executable}）"
    if (version.major, version.minor) < REQUIRED_PYTHON:
        need = ".".join(str(part) for part in REQUIRED_PYTHON)
        return CheckResult(
            "python",
            STATUS_FAIL,
            detail,
            hint=f"需要 Python >= {need}。用 uv 管理：uv python install {need}，然后 uv sync。",
            data={"version": f"{version.major}.{version.minor}.{version.micro}", "executable": sys.executable},
        )
    return CheckResult("python", STATUS_OK, detail, data={"version": f"{version.major}.{version.minor}.{version.micro}"})


def _import_module(name: str) -> tuple[bool, str]:
    try:
        module = __import__(name)
    except Exception as exc:  # ImportError 或依赖内部炸掉
        return False, str(exc)
    return True, str(getattr(module, "__version__", "") or "")


def _dependencies_check() -> CheckResult:
    missing: list[str] = []
    found: dict[str, str] = {}
    for module_name, package_name in REQUIRED_MODULES:
        ok, version = _import_module(module_name)
        if ok:
            found[package_name] = version
        else:
            missing.append(package_name)
    if missing:
        return CheckResult(
            "dependencies",
            STATUS_FAIL,
            "缺少依赖：" + "、".join(missing),
            hint="运行 uv sync（会自动按 uv.lock 安装全部依赖）。",
            data={"missing": missing, "found": found},
        )
    gui_missing = [pkg for module, pkg in OPTIONAL_MODULES if not _import_module(module)[0]]
    if gui_missing:
        return CheckResult(
            "dependencies",
            STATUS_WARN,
            "核心依赖齐全；图形界面依赖缺失：" + "、".join(gui_missing),
            hint="命令行可正常使用；要用 GUI 请运行 uv sync（pywebview 已在依赖里）。",
            data={"found": found, "gui_missing": gui_missing},
        )
    return CheckResult(
        "dependencies",
        STATUS_OK,
        "依赖齐全：" + "、".join(f"{pkg} {ver}".strip() for pkg, ver in found.items()),
        data={"found": found},
    )


def _keys_check(config: AppConfig) -> CheckResult:
    keys = load_api_keys()
    if not keys:
        return CheckResult(
            "keys",
            STATUS_FAIL,
            "未找到任何 API Key",
            hint=(
                "三种填法任选其一：\n"
                "1) GUI「模型服务 → API Key」里粘贴后点「＋ 新增 API Key」（写入项目根目录 .env）；\n"
                "2) 复制 .env.example 为 .env，填 SILICONFLOW_API_KEY_1=sk-xxx；\n"
                "3) 设置环境变量 SILICONFLOW_API_KEY_1。\n"
                "并发数不能超过 Key 数量，否则多出的 worker 会共用同一个 Key。"
            ),
            data={"count": 0},
        )
    note = ""
    if config.workers > len(keys):
        note = f"；并发 {config.workers} > Key 数 {len(keys)}，多出的 worker 会共用 Key"
    return CheckResult(
        "keys",
        STATUS_WARN if note else STATUS_OK,
        f"已加载 {len(keys)} 个 API Key（{mask_keys(keys)}）{note}",
        hint="把并发数调到不超过 Key 数量，识别速度最稳。" if note else "",
        data={"count": len(keys), "masked": mask_keys(keys)},
    )


def _config_check(
    config: AppConfig, explicit_path: str | None, used_path: str | None
) -> CheckResult:
    try:
        config.validate(require_keys=False)
    except ConfigError as exc:
        return CheckResult(
            "config",
            STATUS_FAIL,
            f"配置不合法：{exc}",
            hint=(
                "常见修法：\n"
                "- 缺题号范围：加 --from 47 --to 81（题号以截图右上角为准）；\n"
                "- 缺输出目录：加 --output-dir D:\\题库；\n"
                "- 模型/地址为空：加 --model Qwen/Qwen3.5-4B --base-url https://api.siliconflow.cn/v1。"
            ),
            data={"path": used_path},
        )
    warning = ""
    if not config.textbook and not config.chapter:
        warning = "；未设置教材/章节，文件名会退化为 题目_起-止，且缺少元数据"
    if explicit_path and not used_path:
        warning = f"；指定的配置文件不存在：{explicit_path}"
    detail = f"题号 {config.page_from}-{config.page_to}（共 {config.total} 题），模型 {config.model}"
    if used_path:
        detail += f"，配置来自 {used_path}"
    detail += warning
    status = STATUS_WARN if warning else STATUS_OK
    hint = "建议补上 --textbook / --chapter，导出的 JSONL 每行都会带上教材章节信息。" if warning else ""
    return CheckResult(
        "config",
        status,
        detail,
        hint=hint,
        data={
            "page_from": config.page_from,
            "page_to": config.page_to,
            "model": config.model,
            "base_url": config.base_url,
            "path": used_path,
        },
    )


def default_adb_probe(config: AppConfig) -> tuple[str, str, str]:
    """返回 (status, detail, hint)；不抛异常。"""
    from .adb import AdbClient, format_devices

    try:
        client = AdbClient(adb_path=config.adb_path, serial=config.serial, timeout=15.0)
    except Exception as exc:
        return (
            STATUS_FAIL,
            f"ADB 不可用：{exc}",
            "安装 Android Platform Tools 并把 adb 放进 PATH，或用 --adb 指定完整路径。",
        )
    lines: list[str] = []
    if config.device_address:
        try:
            lines.append(client.connect(config.device_address, timeout=20.0))
        except Exception as exc:
            lines.append(f"adb connect 失败：{exc}")
    try:
        devices = client.devices()
    except Exception as exc:
        return (
            STATUS_FAIL,
            f"读取设备列表失败：{exc}",
            "确认 adb 能正常执行：adb devices；USB 连接时手机上要点「允许 USB 调试」。",
        )
    ready = [device for device in devices if device.ready]
    lines.append(format_devices(devices))
    if not ready:
        return (
            STATUS_FAIL,
            "；".join(item for item in lines if item),
            (
                "没有在线设备：\n"
                "- 无线调试：手机开发者选项里开启「无线调试」，用 --connect 192.168.1.5:5555（IP 以手机显示为准）；\n"
                "- USB：换数据线/换口，授权弹窗要选允许。"
            ),
        )
    for device in ready:
        try:
            size = AdbClient(
                adb_path=client.adb_path, serial=device.serial, timeout=15.0
            ).wm_size()
        except Exception:
            size = None
        if size:
            lines.append(f"{device.serial} 分辨率：{size[0]}x{size[1]}")
    return STATUS_OK, "；".join(item for item in lines if item), ""


def _adb_check(config: AppConfig, probe: AdbProbe) -> CheckResult:
    try:
        status, detail, hint = probe(config)
    except Exception as exc:  # 探针自己炸了也不能让 doctor 崩
        status, detail, hint = STATUS_FAIL, f"设备检测异常：{exc}", "手动执行 adb devices 排查。"
    status = status if status in (STATUS_OK, STATUS_WARN, STATUS_FAIL, STATUS_SKIP) else STATUS_FAIL
    return CheckResult("adb", status, detail, hint=hint)


def default_model_probe(config: AppConfig, key: str) -> tuple[str, str, str]:
    result: ModelCheckResult = check_model_connection(
        key,
        config.base_url,
        config.model,
        timeout=min(float(config.request_timeout), 30.0),
    )
    if result.ok:
        return STATUS_OK, result.summary(), ""
    hint = result.hint()
    if result.suggestions:
        hint = "平台上的相似模型：" + "、".join(result.suggestions) + "；" + hint
    return STATUS_FAIL, result.summary(), hint


def _model_check(config: AppConfig, key: str, probe: ModelProbe) -> CheckResult:
    try:
        status, detail, hint = probe(config, key)
    except Exception as exc:
        status, detail, hint = STATUS_FAIL, f"模型测试异常：{exc}", "检查网络/代理，或先用 --no-model-check 跳过。"
    if status not in (STATUS_OK, STATUS_WARN, STATUS_FAIL, STATUS_SKIP):
        status = STATUS_FAIL
    return CheckResult("model", status, detail, hint=hint)


def _output_check(config: AppConfig) -> CheckResult:
    try:
        paths = resolve_output_paths(config)
    except Exception as exc:  # AppConfig 不含 output_dir 时理论不会抛，兜底
        return CheckResult(
            "output", STATUS_FAIL, f"无法计算输出路径：{exc}", hint="检查 --output-dir 是否是合法路径。"
        )
    target_raw = paths.directory or "."
    target = Path(target_raw)
    probe_path: Path | None = None
    try:
        target.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=target, prefix=".drpilot-doctor-", suffix=".tmp", delete=False
        ) as handle:
            probe_path = Path(handle.name)
            handle.write("probe")
    except OSError as exc:
        return CheckResult(
            "output",
            STATUS_FAIL,
            f"输出目录不可写：{target_raw}（{exc}）",
            hint="换一个可写目录：--output-dir D:\\题库；或先手动创建该文件夹。",
            data={"directory": target_raw},
        )
    finally:
        if probe_path is not None:
            try:
                probe_path.unlink()
            except OSError:
                pass
    return CheckResult(
        "output",
        STATUS_OK,
        f"输出目录可写：{target}（将生成 {Path(paths.jsonl).name} 等文件）",
        data={"directory": target_raw, "jsonl": paths.jsonl},
    )


def _config_key_hint(config: AppConfig) -> CheckResult:
    """给 keys 检查补上 config 上下文（并发数提示）。"""
    return CheckResult("keys", STATUS_SKIP, "未检查", data={"workers": config.workers})


# ---------------- 编排 ----------------
def run_doctor(
    config: AppConfig | None = None,
    config_path: str | None = None,
    full: bool = False,
    *,
    adb_probe: AdbProbe | None = None,
    model_probe: ModelProbe | None = None,
    check_model: bool = True,
) -> DoctorReport:
    """按顺序跑检查；默认在第一个 fail 处停下（附 remaining），full=True 跑完。

    Args:
        config: 已有配置；None 时按 config_path 加载（含默认与旧版配置回落）。
        config_path: 显式配置路径。
        full: 是否执行全部检查。
        adb_probe / model_probe: 便于测试注入的探针。
        check_model: 是否真的联网测模型（--no-model-check 时传 False）。
    """
    used_path: str | None = None
    if config is None:
        config, used_path = load_config_with_fallback(config_path)
    elif config_path and Path(config_path).is_file():
        used_path = config_path

    adb_probe = adb_probe or default_adb_probe
    model_probe = model_probe or default_model_probe

    checks: list[CheckResult] = []
    report = DoctorReport(checks=checks, config_path=used_path, full=full)

    def stop_after(check: CheckResult, more: bool = True) -> bool:
        """返回 True 表示应停止后续检查。"""
        if full or not more:
            return False
        return check.status == STATUS_FAIL

    check = _python_check()
    checks.append(check)
    if stop_after(check):
        return report

    check = _dependencies_check()
    checks.append(check)
    if stop_after(check):
        return report

    keys_check = _keys_check(config)
    checks.append(keys_check)
    if stop_after(keys_check):
        return report

    check = _config_check(config, config_path, used_path)
    checks.append(check)
    if stop_after(check):
        return report

    check = _adb_check(config, adb_probe)
    checks.append(check)
    if stop_after(check):
        return report

    check = _output_check(config)
    checks.append(check)
    if stop_after(check):
        return report

    if not check_model:
        checks.append(
            CheckResult(
                "model",
                STATUS_SKIP,
                "已按 --no-model-check 跳过",
                skipped_reason="调用方显式跳过",
            )
        )
        return report

    key = (load_api_keys() or [""])[0]
    check = _model_check(config, key, model_probe)
    if check.status == STATUS_FAIL and not config.check_model:
        check = CheckResult(
            "model",
            STATUS_WARN,
            check.detail + "（配置里已关闭运行前模型检查，不阻塞运行）",
            hint=check.hint,
        )
    checks.append(check)
    return report


__all__ = [
    "CHECK_TITLES",
    "CheckResult",
    "DoctorReport",
    "default_adb_probe",
    "default_model_probe",
    "run_doctor",
]
