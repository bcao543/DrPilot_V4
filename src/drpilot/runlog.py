# -*- coding: utf-8 -*-
"""运行日志：给 Agent 留一条能定位问题的完整轨迹。

设计目标（对应「报错时方便 Agent 定位问题」）：
    * 每次运行都写一份日志文件（默认 logs/drpilot-<时间>-<pid>.log），
      JSON 结果里回传 log_file，Agent 拿到路径就能读到全过程；
    * 每行带时间、级别、阶段（phase），并按阶段聚合；
    * 任何写进日志的文本都先过 mask_secrets()，Key 永远不会落盘；
    * 结构化错误（error_report）带 error_code / phase / context / suggestion，
      Agent 不用解析中文句子猜下一步。
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOG_DIRNAME = "logs"

# 形如 sk-xxxx / AIza... / 32 位以上无空格串的疑似密钥；日志里一律脱敏
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}\b"),
    re.compile(r"\b(sk|rk|pk)-[A-Za-z0-9]{8,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{10,}\b"),
)

LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")


def mask_secrets(text: Any) -> str:
    """把文本里疑似 API Key 的片段替换成脱敏形式。"""
    result = "" if text is None else str(text)
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(_mask_match, result)
    return result


def _mask_match(match: re.Match[str]) -> str:
    value = match.group(0)
    if len(value) <= 8:
        return "****"
    return f"{value[:3]}****{value[-4:]}"


def default_log_path(now: float | None = None) -> Path:
    """默认日志文件：logs/drpilot-YYYYmmdd-HHMMSS-<pid>.log（已被 .gitignore 忽略）。"""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now if now is not None else time.time()))
    return PROJECT_ROOT / LOG_DIRNAME / f"drpilot-{stamp}-{os.getpid()}.log"


class RunLogger:
    """同时写日志文件与 stderr 的运行日志。

    * json_mode=True 时 stderr 也照常输出（stdout 只留给 JSON 结果）；
    * quiet=True 时只写文件，不打扰人；
    * 线程安全：pipeline 的 worker 线程会并发写日志。
    """

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        stream: Any = None,
        verbose: bool = False,
        enabled: bool = True,
        echo: bool = True,
    ) -> None:
        self.verbose = bool(verbose)
        self.echo = bool(echo)
        self._stream = stream if stream is not None else sys.stderr
        self._lock = threading.RLock()
        self._handle = None
        self.path: Path | None = None
        self.entries: list[dict[str, Any]] = []
        self.error_count = 0
        self.warning_count = 0
        self._phase = "start"
        if enabled:
            target = Path(path) if path else default_log_path()
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                self._handle = open(target, "a", encoding="utf-8", newline="\n")
                self.path = target
            except OSError:
                self._handle = None   # 写不了日志文件也不能让主流程挂掉
        self.log(f"DrPilot 启动：pid={os.getpid()}，日志文件={self.path or '（未启用）'}", level="DEBUG")

    # ---- 基础 ----
    @property
    def phase(self) -> str:
        return self._phase

    def set_phase(self, phase: str) -> None:
        self._phase = str(phase)

    def log(
        self,
        message: Any,
        level: str = "INFO",
        *,
        phase: str | None = None,
        detail: str | None = None,
    ) -> None:
        level_name = str(level).upper()
        if level_name not in LEVELS:
            level_name = "INFO"
        current_phase = phase or self._phase
        text = mask_secrets(message)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()) + f".{int(time.time() * 1000) % 1000:03d}"
        line = f"{stamp} | {level_name:<5} | {current_phase} | {text}"
        if detail:
            line += f" | {mask_secrets(detail)}"
        entry = {"ts": stamp, "level": level_name, "phase": current_phase, "text": text}
        with self._lock:
            self.entries.append(entry)
            if self._handle is not None:
                try:
                    self._handle.write(line + "\n")
                    self._handle.flush()
                except OSError:
                    self._handle = None
            if level_name == "ERROR":
                self.error_count += 1
            elif level_name == "WARN":
                self.warning_count += 1
        if self.echo and (level_name != "DEBUG" or self.verbose):
            prefix = {"ERROR": "[错误] ", "WARN": "[警告] ", "DEBUG": "[调试] "}.get(level_name, "")
            print(mask_secrets(prefix + text) if not detail else mask_secrets(f"{prefix}{text}\n        {detail}"), file=self._stream)

    def debug(self, message: Any, **kwargs: Any) -> None:
        self.log(message, "DEBUG", **kwargs)

    def warn(self, message: Any, **kwargs: Any) -> None:
        self.log(message, "WARN", **kwargs)

    def error(self, message: Any, **kwargs: Any) -> None:
        self.log(message, "ERROR", **kwargs)

    def exception(self, exc: BaseException, *, phase: str | None = None, context: Mapping[str, Any] | None = None) -> None:
        """记录异常：级别 ERROR，带类型、消息与上下文（--debug 时附加堆栈）。"""
        import traceback

        detail = f"{type(exc).__name__}: {exc}"
        if self.verbose:
            detail += "\n" + mask_secrets(traceback.format_exc())
        if context:
            detail += "  context=" + mask_secrets(json.dumps(dict(context), ensure_ascii=False))
        self.error(detail, phase=phase)

    # ---- 收尾 ----
    def close(self) -> None:
        with self._lock:
            if self._handle is not None:
                try:
                    self._handle.close()
                except OSError:
                    pass
                self._handle = None

    def __enter__(self) -> "RunLogger":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


__all__ = [
    "LOG_DIRNAME",
    "LEVELS",
    "PROJECT_ROOT",
    "RunLogger",
    "default_log_path",
    "mask_secrets",
]
