# -*- coding: utf-8 -*-
"""界面桥：把 GUI 需要的能力包成一组可被 JS 调用的方法。

设计要点：
    * 每个方法都立刻返回，耗时操作丢给后台线程；前端每 150ms 轮询 poll() 取日志和状态。
    * 状态只在 UiBridge 里维护一份，前端只负责渲染，避免两边各存一份互相打架。
    * ask() 用「事件 + 轮询」实现阻塞式提问（预检不一致、模型不可用时要不要继续），
      不依赖 pywebview 的跨线程调用，稳。
    * 本模块不 import pywebview，可以脱离窗口单独测试。
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import threading
import time
from collections import deque
from typing import Any, Callable, Mapping

from ..actions import (
    RecordResult,
    Step,
    build_action_plan,
    build_next_action,
    normalize_steps,
    payload_to_steps,
    record_once,
    replay_next_action,
    steps_summary,
    steps_to_payload,
)
from ..adb import AdbClient, format_devices, normalize_address
from ..ai import ModelCheckResult, check_model_connection
from ..config import (
    DEFAULT_SWIPE_DURATION_MS,
    DEFAULT_SWIPE_END_X,
    DEFAULT_SWIPE_END_Y,
    DEFAULT_SWIPE_START_X,
    DEFAULT_SWIPE_START_Y,
    AppConfig,
    build_swipe_plan,
    default_config_path,
    load_api_keys,
    load_config_with_fallback,
    load_dotenv_candidates,
    parse_size,
    resolve_output_paths,
    save_config_file,
)
from ..errors import AdbError, ConfigError
from ..keys import (
    append_api_keys,
    clear_api_keys,
    env_file_keys,
    env_file_path,
    mask_keys,
    parse_key_input,
    remove_api_key,
    save_api_keys,
)
from ..pipeline import PilotSession

DEFAULT_DEVICE_PORT = "5555"
MAX_LOG_BUFFER = 4000
NOT_SET_ACTION_NOTE = "未设置：将使用下方手动滑动坐标"


def log_tone(message: str) -> str:
    """给日志行上色：错误红、警告黄、成功绿。"""
    text = str(message)
    if any(key in text for key in ("错误", "失败", "不可用", "✗", "Traceback")):
        return "err"
    if any(key in text for key in ("警告", "提示", "重试", "跳过", "未通过")):
        return "warn"
    if any(key in text for key in ("完成", "成功", "正常", "✓")):
        return "ok"
    return ""


def _text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key, "")
    return str(value).strip() if value is not None else ""


def _as_int(payload: Mapping[str, Any], key: str, name: str, default: int = 0) -> int:
    raw = _text(payload, key)
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError as exc:
        raise ConfigError(f"{name} 必须是整数") from exc


def _as_float(
    payload: Mapping[str, Any], key: str, name: str, default: float | None = None
) -> float:
    raw = _text(payload, key)
    if not raw:
        if default is None:
            raise ConfigError(f"{name} 必须是数字")
        return float(default)
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} 必须是数字") from exc


def config_from_payload(
    payload: Mapping[str, Any] | None, require_output: bool = True
) -> AppConfig:
    """把前端表单（一个 dict）变成 AppConfig。

    require_output=False 用于「测试模型」这类跟输出目录无关的操作，
    免得第一次打开、还没选文件夹时自检直接报错。
    """
    data: Mapping[str, Any] = payload or {}
    output_dir = _text(data, "output_dir")
    if require_output and not output_dir:
        raise ConfigError("请先选择输出文件夹")

    reference_text = _text(data, "swipe_reference")
    reference = parse_size(reference_text) if reference_text else None
    if reference_text and not reference:
        raise ConfigError("基准分辨率格式应为 宽x高，例如 1080x2340")

    ip = _text(data, "device_ip")
    port = _text(data, "device_port") or DEFAULT_DEVICE_PORT

    steps = payload_to_steps(_text(data, "next_action"))
    if not steps:
        tap_x = _as_int(data, "action_tap_x", "手动点按 X")
        tap_y = _as_int(data, "action_tap_y", "手动点按 Y")
        if tap_x > 0 and tap_y > 0:
            steps = [
                Step(
                    kind="tap",
                    x=tap_x,
                    y=tap_y,
                    duration_ms=_as_int(data, "action_tap_ms", "手动点按按住时长", 80),
                    absolute=True,
                )
            ]

    config = AppConfig(
        page_from=_as_int(data, "page_from", "起始题号", 1),
        page_to=_as_int(data, "page_to", "结束题号", 1),
        textbook=_text(data, "textbook"),
        chapter=_text(data, "chapter"),
        chapter_no=_as_int(data, "chapter_no", "章号"),
        chapter_total=_as_int(data, "chapter_total", "本章总题数"),
        output_dir=output_dir,
        output_prefix="",
        generate_markdown=bool(data.get("generate_markdown", True)),
        generate_index=bool(data.get("generate_index", True)),
        preflight=bool(data.get("preflight", True)),
        check_model=bool(data.get("check_model", True)),
        model=_text(data, "model"),
        base_url=_text(data, "base_url"),
        batch_size=_as_int(data, "batch_size", "批大小", 3),
        workers=_as_int(data, "workers", "并发数", 2),
        wait=_as_float(data, "wait_ms", "翻页等待(毫秒)", 1000.0) / 1000.0,
        # 前端留空时一律回落到 config.py 里的默认值，不让输入框为空把参数变成 0
        swipe_start_x=_as_int(data, "swipe_x1", "滑动起点 X", DEFAULT_SWIPE_START_X),
        swipe_start_y=_as_int(data, "swipe_y1", "滑动起点 Y", DEFAULT_SWIPE_START_Y),
        swipe_end_x=_as_int(data, "swipe_x2", "滑动终点 X", DEFAULT_SWIPE_END_X),
        swipe_end_y=_as_int(data, "swipe_y2", "滑动终点 Y", DEFAULT_SWIPE_END_Y),
        swipe_duration_ms=_as_int(
            data, "swipe_duration", "滑动时长", DEFAULT_SWIPE_DURATION_MS
        ),
        max_swipe_retries=_as_int(data, "swipe_retries", "重复截图重试次数", 5),
        retry_wait=_as_float(data, "swipe_retry_wait", "重试前等待", 1.2),
        swipe_reference_width=(reference[0] if reference else 0),
        swipe_reference_height=(reference[1] if reference else 0),
        next_action=steps_to_payload(steps),
        device_address=f"{ip}:{port}" if ip else "",
    )
    return config.normalize()


def address_from_payload(payload: Mapping[str, Any] | None) -> str:
    data: Mapping[str, Any] = payload or {}
    ip = _text(data, "device_ip")
    port = _text(data, "device_port") or DEFAULT_DEVICE_PORT
    return normalize_address(f"{ip}:{port}" if port else ip)


def describe_exception(exc: BaseException, limit: int = 200) -> str:
    text = " ".join(str(exc).split())
    return text[:limit] if text else type(exc).__name__


class UiBridge:
    """前端（HTML/JS）与业务之间的唯一接口。"""

    def __init__(
        self,
        config_path: str | None = None,
        pick_directory: Callable[[str], str | None] | None = None,
        on_exit: Callable[[], None] | None = None,
        model_checker: Callable[..., ModelCheckResult] | None = None,
        adb_factory: Callable[..., Any] | None = None,
        env_path: str | None = None,
    ) -> None:
        self.config_path = config_path
        # 默认写项目根 .env；测试/多实例可注入临时路径，避免碰真实 .env
        self.env_path = str(env_path) if env_path else None
        self._pick_directory = pick_directory or (lambda initial: None)
        self._on_exit = on_exit or (lambda: None)
        self._model_checker = model_checker or check_model_connection
        self._adb_factory = adb_factory or AdbClient

        self._lock = threading.RLock()
        self._logs: deque[dict[str, str]] = deque(maxlen=MAX_LOG_BUFFER)
        self._state: dict[str, Any] = {
            "device": {"text": "设备：检测中…", "tone": "info", "detail": "尚未检测", "busy": False},
            "model": {"text": "模型：未测试", "tone": "muted", "note": "开始前会自动测试模型连通性"},
            "keys": "",
            "key_masked": "",
            "key_count": 0,
            # [{"index": 1, "masked": "sk-****1234"}, …]，供界面渲染「已保存 Key」列表
            "key_items": [],
            "progress": {"percent": 0, "current": "--", "status": "就绪", "tone": ""},
            "running": False,
            "stopping": False,
            "model_checking": False,
            "workers": [],
            "question": None,
            "toasts": [],
            "form_patch": {},
            "total_hint": "",
            "output_preview": "",
            "recording": False,
            "action": {
                "recording": False,
                "steps": [],
                "summary": "",
                "reference": "",
                "note": NOT_SET_ACTION_NOTE,
            },
        }
        self._config = AppConfig()
        self._session: PilotSession | None = None
        self._run_thread: threading.Thread | None = None
        self._worker_state: dict[int, dict[str, Any]] = {}
        self._questions: dict[int, threading.Event] = {}
        self._answers: dict[int, bool] = {}
        self._question_seq = 0
        self._record_stop = threading.Event()
        self._record_thread: threading.Thread | None = None

    # ---------------- 状态工具 ----------------
    def _log(self, message: str, tone: str | None = None) -> None:
        line = {"text": str(message), "tone": tone if tone is not None else log_tone(message)}
        with self._lock:
            self._logs.append(line)

    def _toast(self, text: str, tone: str = "") -> None:
        with self._lock:
            self._state["toasts"].append({"text": str(text), "tone": tone})

    def _take_logs(self) -> list[dict[str, str]]:
        with self._lock:
            lines = list(self._logs)
            self._logs.clear()
        return lines

    def _take_toasts(self) -> list[dict[str, str]]:
        with self._lock:
            items = list(self._state["toasts"])
            self._state["toasts"] = []
        return items

    def _update(self, section: str, **fields: Any) -> None:
        with self._lock:
            self._state[section].update(fields)

    def _set_flag(self, key: str, value: Any) -> None:
        with self._lock:
            self._state[key] = value

    def _patch_form(self, **fields: Any) -> None:
        """把 Python 侧算出来的值回填到前端输入框（下一次轮询生效）。"""
        with self._lock:
            self._state["form_patch"].update(fields)

    def _take_form_patch(self) -> dict[str, Any]:
        with self._lock:
            patch = dict(self._state["form_patch"])
            self._state["form_patch"] = {}
        return patch

    def _set_progress(self, **fields: Any) -> None:
        self._update("progress", **fields)

    def _set_device(self, text: str, tone: str, detail: str | None = None, busy: bool | None = None) -> None:
        fields: dict[str, Any] = {"text": text, "tone": tone}
        if detail is not None:
            fields["detail"] = detail
        if busy is not None:
            fields["busy"] = busy
        self._update("device", **fields)

    def _set_model(self, text: str, tone: str, note: str | None = None) -> None:
        fields: dict[str, Any] = {"text": text, "tone": tone}
        if note is not None:
            fields["note"] = note
        self._update("model", **fields)

    def _snapshot(self) -> dict[str, Any]:
        with self._lock:
            state = copy.deepcopy(self._state)
        state["toasts"] = self._take_toasts()
        state["form_patch"] = self._take_form_patch()
        return state

    # ---------------- 提问 ----------------
    def _ask(self, title: str, message: str, kind: str = "yesno") -> bool:
        """阻塞当前（后台）线程，等前端点按钮。"""
        with self._lock:
            self._question_seq += 1
            qid = self._question_seq
            self._questions[qid] = threading.Event()
            self._state["question"] = {"id": qid, "title": title, "message": message, "kind": kind}
            event = self._questions[qid]
        event.wait()
        with self._lock:
            return bool(self._answers.pop(qid, False))

    def answer_question(self, qid: Any, ok: Any) -> dict[str, Any]:
        try:
            key = int(qid)
        except (TypeError, ValueError):
            return {"ok": False}
        with self._lock:
            self._answers[key] = bool(ok)
            self._state["question"] = None
            event = self._questions.pop(key, None)
        if event is not None:
            event.set()
        return {"ok": True}

    # ---------------- 配置 ----------------
    def _config_dict(self) -> dict[str, Any]:
        config = self._config
        return {
            "page_from": config.page_from,
            "page_to": config.page_to,
            "textbook": config.textbook,
            "chapter": config.chapter,
            "chapter_no": config.chapter_no or "",
            "chapter_total": config.chapter_total or "",
            # 输出目录为空就返回空串：让前端显示「请先选择输出文件夹」，不要编造路径
            "output_dir": config.output_dir or os.path.dirname(config.output_prefix) or "",
            "model": config.model,
            "base_url": config.base_url,
            "batch_size": config.batch_size,
            "workers": config.workers,
            "wait_ms": int(round(config.wait * 1000)),
            "generate_markdown": config.generate_markdown,
            "generate_index": config.generate_index,
            "preflight": config.preflight,
            "check_model": config.check_model,
            "swipe_x1": config.swipe_start_x,
            "swipe_y1": config.swipe_start_y,
            "swipe_x2": config.swipe_end_x,
            "swipe_y2": config.swipe_end_y,
            "swipe_duration": config.swipe_duration_ms,
            "swipe_retries": config.max_swipe_retries,
            "swipe_retry_wait": config.retry_wait,
            "swipe_reference": (
                f"{config.swipe_reference_width}x{config.swipe_reference_height}"
                if config.swipe_reference_width and config.swipe_reference_height
                else ""
            ),
            "next_action": (
                json.dumps(config.next_action, ensure_ascii=False) if config.next_action else ""
            ),
            "action_tap_x": "",
            "action_tap_y": "",
            "action_tap_ms": 80,
            "device_ip": "",
            "device_port": DEFAULT_DEVICE_PORT,
        } | self._device_fields()

    def _device_fields(self) -> dict[str, str]:
        address = self._config.device_address or ""
        if ":" in address:
            host, _, port = address.rpartition(":")
            return {"device_ip": host, "device_port": port or DEFAULT_DEVICE_PORT}
        return {"device_ip": address, "device_port": DEFAULT_DEVICE_PORT}

    def _load_config(self) -> None:
        try:
            config, path = load_config_with_fallback(self.config_path)
        except ConfigError as exc:
            self._log(f"读取配置失败：{exc}", "err")
            return
        self._config = config
        if path:
            self._log(f"已加载配置：{path}")

    def _save_config(self, config: AppConfig) -> None:
        try:
            save_config_file(self.config_path or default_config_path(), config)
        except Exception as exc:
            self._log(f"保存配置失败：{exc}", "warn")

    def _env_file_keys(self) -> list[str]:
        """只从指定 .env 文件读 Key（不读进程环境；测试可重复，且不碰真实 .env）。"""
        path = env_file_path(self.env_path)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return []
        mapping: dict[str, str] = {}
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            name, _, value = stripped.partition("=")
            mapping[name.strip()] = value.strip().strip("\"'")
        return load_api_keys(mapping)

    def _current_keys(self) -> list[str]:
        """当前生效的 Key：默认 .env + 环境变量；指定 env_path 时只看该文件。"""
        if self.env_path:
            return self._env_file_keys()
        load_dotenv_candidates()
        return load_api_keys()

    @staticmethod
    def _key_items(keys: list[str]) -> list[dict[str, Any]]:
        """把 Key 列表转成 [{"index": N, "masked": "sk-****1234"}]，只含脱敏值。"""
        return [
            {"index": index, "masked": mask_keys([key])}
            for index, key in enumerate(keys, start=1)
            if key
        ]

    def _managed_keys(self) -> list[str]:
        """GUI 能管理的那部分 Key：只来自 .env，顺序与 remove_api_key() 一致。"""
        return env_file_keys(self.env_path)

    def _refresh_key_info(self) -> None:
        """刷新 Key 数量、脱敏摘要与「已保存」列表；只保存脱敏结果，绝不落明文。"""
        keys = self._current_keys()
        managed = self._managed_keys()
        masked = mask_keys(keys)
        self._set_flag("key_count", len(keys))
        self._set_flag("key_masked", masked)
        self._set_flag("key_items", self._key_items(managed))
        if keys:
            extra = len(keys) - len(managed)
            suffix = f"（另有 {extra} 个来自环境变量）" if extra > 0 else ""
            self._set_flag("keys", f"已加载 {len(keys)} 个 API Key{suffix}")
        else:
            self._set_flag("keys", "未配置 API Key：在「模型服务」里填入后点「新增 API Key」")

    # ---------------- API Key ----------------
    def save_keys(self, payload: Any = None) -> dict[str, Any]:
        """把用户输入的 Key 写入 .env。

        明文只进文件；返回值、日志、状态一律只用 mask_keys() 的脱敏摘要。
        """
        data: Mapping[str, Any] = payload if isinstance(payload, Mapping) else {"text": payload}
        keys = parse_key_input(data.get("text", ""))
        target = str(env_file_path(self.env_path))
        if not keys:
            return self._key_result(False, error="请至少填写 1 个 API Key")
        try:
            info = save_api_keys(keys, path=self.env_path)
        except Exception as exc:                    # noqa: BLE001 - 桥方法必须把错误变成提示
            detail = describe_exception(exc)
            self._log(f"保存 API Key 失败：{detail}", "err")
            self._toast(f"保存 API Key 失败：{detail}", "err")
            return self._key_result(False, error=detail)
        self._refresh_key_info()                    # 内部会 load_dotenv_candidates()，新 Key 立即生效
        masked = str(info.get("masked") or "")
        count = int(info.get("count") or 0)
        path = str(info.get("path") or target)
        self._log(f"已保存 {count} 个 API Key 到 .env：{masked}", "ok")
        self._toast(f"已保存 {count} 个 Key 到 .env", "ok")
        return self._key_result(True, count=count, masked=masked, path=path)

    def _key_result(self, ok: bool, **fields: Any) -> dict[str, Any]:
        """Key 相关方法的统一返回：只带脱敏列表，绝不带上明文。"""
        keys = self._current_keys() if ok else []
        managed = self._managed_keys() if ok else []
        masked = mask_keys(keys)
        result: dict[str, Any] = {
            "ok": ok,
            "count": fields.get("count", len(keys)),
            "masked": fields.get("masked", masked),
            "items": self._key_items(managed),
            "error": fields.get("error"),
        }
        for key, value in fields.items():
            result.setdefault(key, value)
        return result

    def add_keys(self, payload: Any = None) -> dict[str, Any]:
        """「新增 API Key」：把输入框里的 Key 追加到 .env，已有的 Key 保持不变。"""
        data: Mapping[str, Any] = payload if isinstance(payload, Mapping) else {"text": payload}
        keys = parse_key_input(data.get("text", ""))
        target = str(env_file_path(self.env_path))
        if not keys:
            return self._key_result(False, error="请先填写要新增的 API Key（可多行 / 逗号分隔）")
        try:
            info = append_api_keys(keys, path=self.env_path)
        except Exception as exc:                    # noqa: BLE001 - 桥方法必须把错误变成提示
            detail = describe_exception(exc)
            self._log(f"新增 API Key 失败：{detail}", "err")
            self._toast(f"新增 API Key 失败：{detail}", "err")
            return self._key_result(False, error=detail)
        self._refresh_key_info()
        added = int(info.get("added") or 0)
        masked = str(info.get("masked") or "")
        count = int(info.get("count") or 0)
        self._log(f"新增 {added} 个 API Key，当前共 {count} 个：{masked}", "ok")
        self._toast(f"已新增 {added} 个 Key（共 {count} 个）" if added else "这些 Key 已经在列表里了", "ok")
        return self._key_result(True, count=count, masked=masked, added=added,
                                path=str(info.get("path") or target))

    def delete_key(self, payload: Any = None) -> dict[str, Any]:
        """「删除 API Key」：删除 .env 里第 index 个 Key（1 起，顺序同界面列表）。"""
        data: Mapping[str, Any] = payload if isinstance(payload, Mapping) else {"index": payload}
        try:
            index = int(str(data.get("index", "")).strip())
        except (TypeError, ValueError):
            return self._key_result(False, error="请选择要删除的 API Key")
        target = str(env_file_path(self.env_path))
        try:
            info = remove_api_key(index, path=self.env_path)
        except Exception as exc:                    # noqa: BLE001 - 同上
            detail = describe_exception(exc)
            self._log(f"删除 API Key 失败：{detail}", "err")
            self._toast(f"删除 API Key 失败：{detail}", "err")
            return self._key_result(False, error=detail)
        self._refresh_key_info()
        removed_key = str(info.get("removed_key") or "")
        count = int(info.get("count") or 0)
        self._log(f"已删除 API Key：{removed_key}（剩余 {count} 个）", "warn")
        self._toast(f"已删除 {removed_key}" if removed_key else "已删除该 Key", "ok")
        return self._key_result(True, count=count, removed_key=removed_key,
                                path=str(info.get("path") or target))

    def reveal_keys(self) -> dict[str, Any]:
        """只有用户主动点「显示」才会调用；返回明文，但不写日志、不进状态。"""
        keys = self._current_keys()
        if not keys:
            return {"ok": False, "keys": [], "text": "", "count": 0, "error": "没有已保存的 API Key"}
        self._log(f"界面已显示 {len(keys)} 个 API Key 的明文（仅本地显示，不会写入日志 / 配置）")
        return {"ok": True, "keys": list(keys), "text": "\n".join(keys), "count": len(keys),
                "error": None}

    def clear_keys(self) -> dict[str, Any]:
        """清空 .env 里的 Key（前端必须显式确认后才调用）。"""
        target = str(env_file_path(self.env_path))
        try:
            info = clear_api_keys(path=self.env_path)
        except Exception as exc:                    # noqa: BLE001 - 同上
            detail = describe_exception(exc)
            self._log(f"清除 API Key 失败：{detail}", "err")
            self._toast(f"清除 API Key 失败：{detail}", "err")
            return self._key_result(False, error=detail)
        self._refresh_key_info()
        removed = int(info.get("removed") or 0)
        self._log(f"已清除 .env 中的 API Key（{removed} 行）", "warn")
        self._toast("已清除 .env 中的 API Key", "ok")
        return self._key_result(True, count=0, masked="", removed=removed,
                                path=str(info.get("path") or target))

    # ---------------- 前端轮询 ----------------
    def ready(self) -> dict[str, Any]:
        self._load_config()
        self._refresh_key_info()
        self._refresh_preview(self._config_dict())
        self._sync_action_from_config()
        state = self._snapshot()
        self.refresh_devices()
        if self._config.check_model:
            self.test_model(self._config_dict())
        return {"config": self._config_dict(), "state": state, "logs": self._take_logs()}

    def poll(self) -> dict[str, Any]:
        return {"logs": self._take_logs(), "state": self._snapshot()}

    def preview(self, payload: Mapping[str, Any] | None) -> dict[str, Any]:
        return self._refresh_preview(payload or {})

    def _refresh_preview(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        try:
            config = config_from_payload(payload)
        except ConfigError as exc:
            message = str(exc)
            self._set_flag("total_hint", "")
            self._set_flag("output_preview", message)
            return {"total_hint": "", "output_preview": message}
        paths = resolve_output_paths(config)
        names = [os.path.basename(paths.jsonl)]
        if paths.markdown:
            names.append(os.path.basename(paths.markdown))
        if paths.index:
            names.append(os.path.basename(paths.index))
        preview = f"将输出：{paths.directory} → " + " / ".join(names)
        total_hint = f"共 {config.total} 题"
        self._set_flag("total_hint", total_hint)
        self._set_flag("output_preview", preview)
        return {"total_hint": total_hint, "output_preview": preview}

    # ---------------- 设备 ----------------
    def refresh_devices(self) -> dict[str, Any]:
        self._set_device("设备：检测中…", "info", busy=True)
        threading.Thread(target=self._probe_devices, name="drpilot-devices", daemon=True).start()
        return {"ok": True}

    def _probe_devices(self) -> None:
        """列设备 + 读分辨率；无论成败都会收尾，绝不把按钮卡在「检测中」。"""
        try:
            client = self._adb_factory(timeout=15.0)
            devices = client.devices()
            lines = [format_devices(devices)]
            ready = [device for device in devices if device.ready]
            for device in ready:
                try:
                    size = self._adb_factory(
                        adb_path=client.adb_path, serial=device.serial, timeout=15.0
                    ).wm_size()
                except Exception:
                    size = None
                if size:
                    lines.append(f"{device.serial} 分辨率：{size[0]}x{size[1]}")
            detail = "\n".join(lines)
            if ready:
                text, tone = f"设备：{len(ready)} 台在线", "success"
            elif devices:
                text, tone = f"设备：{len(devices)} 台未授权", "warning"
            else:
                text, tone = "设备：未连接", "muted"
            self._set_device(text, tone, detail=detail or "未检测到任何 ADB 设备")
        except Exception as exc:
            self._set_device("设备：ADB 不可用", "danger", detail=f"检测失败：{describe_exception(exc)}")
        finally:
            self._update("device", busy=False)

    def connect_device(self, payload: Mapping[str, Any] | None) -> dict[str, Any]:
        try:
            address = address_from_payload(payload)
        except AdbError as exc:
            self._toast(str(exc), "err")
            return {"ok": False, "error": str(exc)}
        self._set_device("设备：连接中…", "info", busy=True)
        self._log(f"正在 adb connect {address} …")
        threading.Thread(target=self._connect, args=(address,), name="drpilot-connect", daemon=True).start()
        return {"ok": True}

    def _connect(self, address: str) -> None:
        try:
            client = self._adb_factory(timeout=15.0)
            message = client.connect(address, timeout=20.0)
        except Exception as exc:
            detail = describe_exception(exc)
            self._log(f"连接失败：{detail}", "err")
            self._set_device("设备：连接失败", "danger", detail=detail, busy=False)
            self._toast(f"连接失败：{detail}", "err")
            return
        # 先按 connect 的结果立刻更新状态，再去补设备列表，避免一直停在「连接中」
        self._log(f"连接成功：{message}")
        self._toast(f"已连接 {address}", "ok")
        self._set_device("设备：已连接", "success", detail=f"{address} · {message}", busy=True)
        self._probe_devices()

    def disconnect_device(self, payload: Mapping[str, Any] | None) -> dict[str, Any]:
        try:
            address = address_from_payload(payload)
        except AdbError:
            address = ""
        self._log(f"正在断开 {address or '全部无线设备'} …")
        threading.Thread(target=self._disconnect, args=(address,), daemon=True).start()
        return {"ok": True}

    def _disconnect(self, address: str) -> None:
        try:
            output = self._adb_factory(timeout=15.0).disconnect(address or None)
            self._log(f"已断开：{output.strip() or (address or '全部设备')}")
            self._toast("已断开连接", "ok")
        except Exception as exc:
            self._log(f"断开失败：{describe_exception(exc)}", "err")
        self._probe_devices()

    # ---------------- 模型 ----------------
    def test_model(self, payload: Mapping[str, Any] | None) -> dict[str, Any]:
        try:
            config = config_from_payload(payload, require_output=False)
        except ConfigError as exc:
            self._toast(str(exc), "err")
            return {"ok": False, "error": str(exc)}
        self._set_flag("model_checking", True)
        self._set_model("模型：测试中…", "info", note=f"正在测试 {config.model} 是否可调用…")
        threading.Thread(target=self._check_model, args=(config,), name="drpilot-model-check", daemon=True).start()
        return {"ok": True}

    def _check_model(self, config: AppConfig) -> None:
        # 和其它地方用同一个 Key 来源：指定 env_path 时只读那个文件，测试/多实例可隔离
        keys = self._current_keys()
        if not keys:
            result = ModelCheckResult(False, config.model, config.base_url, "未配置 API Key（请检查 .env）")
        else:
            try:
                result = self._model_checker(
                    keys[0], config.base_url, config.model,
                    timeout=min(float(config.request_timeout), 30.0),
                )
            except Exception as exc:
                result = ModelCheckResult(False, config.model, config.base_url, f"测试过程出错：{describe_exception(exc)}")
        self._set_flag("model_checking", False)
        self._apply_model_result(result)

    def _apply_model_result(self, result: ModelCheckResult) -> None:
        if result.ok:
            note = f"✓ {result.model} 可调用 · {result.latency_ms} ms"
            if result.model_listed is False:
                note += "（未在平台模型列表中，但可调用）"
            if result.latency_ms >= 5000:
                note += "　⚠ 响应偏慢，识别一批可能更久，建议换更小的视觉模型"
            self._set_model("模型：可用", "success", note=note)
        else:
            note = f"✗ {result.model} 不可用：{result.detail}"
            if result.suggestions:
                note += f"　相似模型：{'、'.join(result.suggestions)}"
            self._set_model("模型：不可用", "danger", note=note)
        self._log(result.summary())
        if not result.ok and result.hint():
            self._log(f"提示：{result.hint()}", "warn")

    def _confirm_model(self, result: ModelCheckResult) -> bool:
        self._apply_model_result(result)
        message = f"{result.model} 无法调用：\n{result.detail}\n\n"
        if result.suggestions:
            message += f"平台上的相似模型：{'、'.join(result.suggestions)}\n\n"
        message += "仍要开始提取吗？（大概率整批都会失败）"
        return self._ask("模型连通性测试未通过", message)

    # ---------------- 运行 ----------------
    def start(self, payload: Mapping[str, Any] | None) -> dict[str, Any]:
        if self._run_thread is not None and self._run_thread.is_alive():
            return {"ok": False, "error": "任务正在运行，请先停止"}
        if self._state["recording"]:
            self._toast("正在录制翻页动作，请先点「停止录制」", "err")
            return {"ok": False, "error": "正在录制翻页动作"}
        load_dotenv_candidates()
        try:
            config = config_from_payload(payload)
            config.api_keys = load_api_keys()
            config.validate(require_keys=True)
        except (ConfigError, ValueError) as exc:
            self._toast(str(exc), "err")
            return {"ok": False, "error": str(exc)}

        self._config = config
        self._refresh_key_info()
        self._save_config(config)
        self._reset_run_state()
        session = PilotSession(
            config,
            on_log=self._log,
            on_progress=self._on_progress,
            on_worker=self._on_worker,
            on_worker_progress=self._on_worker_progress,
            on_done=self._on_done,
            on_error=lambda exc: self._log(f"错误：{exc}", "err"),
            on_preflight=self._confirm_preflight,
            on_model_check=self._confirm_model,
        )
        self._session = session
        self._run_thread = threading.Thread(target=session.run, name="drpilot-run", daemon=True)
        self._run_thread.start()
        return {"ok": True}

    def stop(self) -> dict[str, Any]:
        """停止运行；重复点击只生效一次。"""
        if self._session is None:
            return {"ok": True}
        with self._lock:
            if self._state["stopping"] or not self._state["running"]:
                return {"ok": True}
            self._state["stopping"] = True
        self._session.stop()
        self._set_progress(status="正在停止…", tone="warn")
        self._log("用户请求停止", "warn")
        return {"ok": True}

    def _reset_run_state(self) -> None:
        with self._lock:
            self._worker_state.clear()
            self._state["workers"] = []
        self._set_flag("running", True)
        self._set_flag("stopping", False)
        self._update("progress", percent=0, current="--", status="运行中", tone="")

    def _on_progress(self, current: int, total: int, number: int) -> None:
        percent = int(current / total * 100) if total else 0
        self._update("progress", percent=percent, current=f"第 {number} 题（{current}/{total}）")

    def _on_worker(self, worker_id: int, status: str) -> None:
        with self._lock:
            item = self._worker_state.setdefault(worker_id, {"id": worker_id, "status": "空闲", "done": 0})
            item["status"] = status
            self._state["workers"] = [self._worker_state[k] for k in sorted(self._worker_state)]

    def _on_worker_progress(self, worker_id: int, done: int) -> None:
        with self._lock:
            item = self._worker_state.setdefault(worker_id, {"id": worker_id, "status": "空闲", "done": 0})
            item["done"] = done
            self._state["workers"] = [self._worker_state[k] for k in sorted(self._worker_state)]

    def _on_done(self) -> None:
        failed = self._session is not None and self._session.error is not None
        was_stopping = bool(self._state["stopping"])
        self._update(
            "progress",
            status="已停止" if was_stopping and not failed else ("失败" if failed else "已完成"),
            tone="warn" if was_stopping and not failed else ("err" if failed else "ok"),
        )
        self._set_flag("running", False)
        self._set_flag("stopping", False)
        session = self._session
        if session is None:
            return
        config = session.config
        self._config = config
        if session.reference_recorded:
            self._log(
                f"滑动基准分辨率已记录：{config.swipe_reference_width}x{config.swipe_reference_height}"
            )
            self._patch_form(
                swipe_reference=f"{config.swipe_reference_width}x{config.swipe_reference_height}"
            )
        if session.model_check is not None:
            self._apply_model_result(session.model_check)
        self._save_config(config)

    def _confirm_preflight(self, screen_id: int, expected: int) -> bool:
        return self._ask(
            "起始题号不一致",
            f"当前屏幕显示第 {screen_id} 题，\n但你设置的起始题号是 {expected}。\n\n仍要继续吗？",
        )

    # ---------------- 其他操作 ----------------
    def set_reference(self, payload: Mapping[str, Any] | None) -> dict[str, Any]:
        try:
            address = address_from_payload(payload)
        except AdbError:
            address = ""
        self._log("正在读取设备分辨率…")
        threading.Thread(target=self._read_reference, args=(address,), daemon=True).start()
        return {"ok": True}

    def _read_reference(self, address: str) -> None:
        try:
            adb = self._adb_factory(timeout=15.0)
            if address:
                try:
                    adb.connect(address, timeout=20.0)
                except Exception as exc:
                    self._log(f"无线连接失败（改用已连接设备）：{describe_exception(exc)}", "warn")
            adb.ensure_device()
            size = adb.wm_size()
        except Exception as exc:
            self._log(f"读取分辨率失败：{describe_exception(exc)}", "err")
            return
        if not size:
            self._log("设备未返回分辨率，无法设置基准", "warn")
            return
        text = f"{size[0]}x{size[1]}"
        self._log(f"滑动基准分辨率已设为当前设备：{text}")
        self._toast(f"基准分辨率：{text}", "ok")
        self._patch_form(swipe_reference=text)

    def test_swipe(self, payload: Mapping[str, Any] | None) -> dict[str, Any]:
        try:
            config = config_from_payload(payload)
        except ConfigError as exc:
            self._toast(str(exc), "err")
            return {"ok": False, "error": str(exc)}
        self._log("正在试滑（对比滑动前后的截图）…")
        threading.Thread(target=self._do_test_swipe, args=(config,), daemon=True).start()
        return {"ok": True}

    def _do_test_swipe(self, config: AppConfig) -> None:
        try:
            adb = self._adb_factory(adb_path=config.adb_path, timeout=20.0)
            if config.device_address:
                adb.connect(config.device_address, timeout=20.0)
            adb.ensure_device()
            size = adb.wm_size() or None
            before = adb.screenshot()
            plan = build_swipe_plan(config, size)
            adb.swipe(plan.start_x, plan.start_y, plan.end_x, plan.end_y, plan.duration_ms)
            time.sleep(max(config.wait, 0.8))
            after = adb.screenshot()
            changed = hashlib.md5(before).hexdigest() != hashlib.md5(after).hexdigest()
            if changed:
                self._log(f"试滑成功：画面已变化 ✓　{plan.describe()}", "ok")
                self._toast("试滑成功：画面已变化 ✓", "ok")
            else:
                self._log(f"试滑后画面没有变化 ✗（坐标可能不对，或已在最后一题）　{plan.describe()}", "warn")
                self._toast("试滑后画面没有变化 ✗", "warn")
        except Exception as exc:
            self._log(f"试滑失败：{describe_exception(exc)}", "err")
            self._toast(f"试滑失败：{describe_exception(exc, 80)}", "err")

    # ---------------- 翻页动作（录制 / 试一次 / 手动点按） ----------------
    def _sync_action_from_config(self) -> None:
        """把配置里已保存的翻页动作回填到界面状态，启动时也能看到步骤。"""
        steps = normalize_steps(self._config.next_action)
        if not steps:
            return
        reference = (
            f"{self._config.swipe_reference_width}x{self._config.swipe_reference_height}"
            if self._config.swipe_reference_width and self._config.swipe_reference_height
            else ""
        )
        self._update(
            "action",
            steps=steps_to_payload(steps),
            summary=steps_summary(steps),
            reference=reference,
            note=f"已加载 {len(steps)} 步",
        )

    def _set_recording(self, value: bool, **fields: Any) -> None:
        self._set_flag("recording", bool(value))
        self._update("action", recording=bool(value), **fields)

    def start_record(self, payload: Mapping[str, Any] | None) -> dict[str, Any]:
        """开始录制「下一题」动作：用户在手机上操作，后台读 getevent。"""
        if self._state["running"]:
            self._toast("任务正在运行，请先停止再录制翻页动作", "err")
            return {"ok": False, "error": "任务正在运行"}
        if self._record_thread is not None and self._record_thread.is_alive():
            return {"ok": True}
        try:
            config = config_from_payload(payload, require_output=False)
        except ConfigError as exc:
            self._toast(str(exc), "err")
            return {"ok": False, "error": str(exc)}
        self._record_stop.clear()
        self._set_recording(
            True,
            steps=[],
            summary="",
            note="录制中…请在手机上完成一次「下一题」动作",
        )
        self._log(
            "开始录制翻页动作：请在手机上完成一次「下一题」动作（点击或滑动；"
            "多步请连续操作，或点「停止录制」结束）"
        )
        self._record_thread = threading.Thread(
            target=self._do_record, args=(config,), name="drpilot-record", daemon=True
        )
        self._record_thread.start()
        return {"ok": True}

    def stop_record(self) -> dict[str, Any]:
        """结束录制；重复点击没有副作用。"""
        thread = self._record_thread
        if thread is None or not thread.is_alive():
            return {"ok": True}
        if self._record_stop.is_set():      # 重复点「停止录制」不再刷日志
            return {"ok": True}
        self._record_stop.set()
        self._set_recording(False, note="正在结束录制…")
        self._log("正在结束录制…")
        return {"ok": True}

    def _do_record(self, config: AppConfig) -> None:
        result: RecordResult | None = None
        try:
            adb = self._adb_factory(
                adb_path=config.adb_path, serial=config.serial, timeout=30.0
            )
            if config.device_address:
                try:
                    adb.connect(config.device_address, timeout=20.0)
                except Exception as exc:
                    self._log(
                        f"无线连接失败（改用已连接的设备）：{describe_exception(exc)}", "warn"
                    )
            adb.ensure_device()
            result = record_once(adb, stop_event=self._record_stop, log=self._log)
        except Exception as exc:
            result = RecordResult(error=describe_exception(exc))
        finally:
            self._set_recording(False)
        if result is None:
            return
        if result.error:
            self._log(f"录制失败：{result.error}", "err")
            self._toast(f"录制失败：{result.error[:80]}", "err")
            self._update("action", note=result.error)
            return
        payload_steps = steps_to_payload(result.steps)
        summary = steps_summary(result.steps)
        reference = f"{result.reference[0]}x{result.reference[1]}" if result.reference else ""
        note = result.note or f"已录制 {len(result.steps)} 步"
        self._update(
            "action", steps=payload_steps, summary=summary, reference=reference, note=note
        )
        patch: dict[str, Any] = {"next_action": json.dumps(payload_steps, ensure_ascii=False)}
        if reference:
            patch["swipe_reference"] = reference
        self._patch_form(**patch)
        self._log(f"录制完成（{len(result.steps)} 步）：{summary}", "ok")
        if result.device:
            self._log(f"录制设备：{result.device}")
        if result.note:
            self._log(result.note, "warn")
        if reference:
            self._log(f"录制分辨率已写为基准分辨率：{reference}")
        self._toast("录制完成，可以点「试一次」验证", "ok")
        self._log("提示：录制/试动作会让手机前进一题，开始提取前请把手机切回起始题")

    def test_action(self, payload: Mapping[str, Any] | None) -> dict[str, Any]:
        """按当前动作试一次，并对比前后截图。"""
        try:
            config = config_from_payload(payload)
        except ConfigError as exc:
            self._toast(str(exc), "err")
            return {"ok": False, "error": str(exc)}
        if self._state["recording"]:
            self._toast("正在录制，请先停止录制", "err")
            return {"ok": False, "error": "正在录制"}
        steps = normalize_steps(config.next_action)
        if not steps:
            self._toast("请先录制翻页动作，或填写手动点按/滑动坐标", "err")
            return {"ok": False, "error": "没有翻页动作"}
        self._log("正在试一次翻页动作（对比前后截图）…")
        threading.Thread(
            target=self._do_test_action, args=(config,), name="drpilot-test-action", daemon=True
        ).start()
        return {"ok": True}

    def _do_test_action(self, config: AppConfig) -> None:
        try:
            adb = self._adb_factory(
                adb_path=config.adb_path, serial=config.serial, timeout=20.0
            )
            if config.device_address:
                adb.connect(config.device_address, timeout=20.0)
            adb.ensure_device()
            size = adb.wm_size() or None
            reference = (
                (config.swipe_reference_width, config.swipe_reference_height)
                if config.swipe_reference_width and config.swipe_reference_height
                else None
            )
            plan = build_action_plan(build_next_action(config), reference, size)
            before = adb.screenshot()
            replay_next_action(adb, plan, log=self._log, settle_wait=max(config.wait, 0.8))
            after = adb.screenshot()
            changed = hashlib.md5(before).hexdigest() != hashlib.md5(after).hexdigest()
            if changed:
                self._log(f"翻页动作生效：画面已变化 ✓　{plan.describe()}", "ok")
                self._toast("翻页动作生效：画面已变化 ✓", "ok")
            else:
                self._log(
                    "翻页动作后画面没有变化 ✗（动作可能不对，或已在最后一题）"
                    f"　{plan.describe()}",
                    "warn",
                )
                self._toast("翻页动作后画面没有变化 ✗", "warn")
        except Exception as exc:
            self._log(f"试翻页动作失败：{describe_exception(exc)}", "err")
            self._toast(f"试翻页动作失败：{describe_exception(exc, 80)}", "err")
        self._log("提示：录制/试动作会让手机前进一题，开始提取前请把手机切回起始题")

    def clear_action(self, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        self._update("action", steps=[], summary="", reference="", note=NOT_SET_ACTION_NOTE)
        self._patch_form(next_action="")
        self._log("已清除翻页动作，将使用下方手动滑动坐标")
        return {"ok": True}

    def use_tap(self, payload: Mapping[str, Any] | None) -> dict[str, Any]:
        """手动点按兜底：设备不支持录制时也能适配点击类 App。"""
        data: Mapping[str, Any] = payload or {}
        try:
            x = _as_int(data, "action_tap_x", "手动点按 X")
            y = _as_int(data, "action_tap_y", "手动点按 Y")
            duration = _as_int(data, "action_tap_ms", "手动点按按住时长", 80)
        except ConfigError as exc:
            self._toast(str(exc), "err")
            return {"ok": False, "error": str(exc)}
        if x <= 0 or y <= 0:
            self._toast("请先填写点按 X / Y（屏幕像素坐标）", "err")
            return {"ok": False, "error": "缺少坐标"}
        step = Step(kind="tap", x=x, y=y, duration_ms=max(0, duration), absolute=True)
        payload_steps = steps_to_payload([step])
        self._update(
            "action", steps=payload_steps, summary=steps_summary([step]), note="手动点按"
        )
        self._patch_form(next_action=json.dumps(payload_steps, ensure_ascii=False))
        self._log(f"已设置手动点按：{step.describe()}", "ok")
        return {"ok": True}

    def pick_output_dir(self, initial: str = "") -> str:
        try:
            chosen = self._pick_directory(str(initial or ""))
        except Exception as exc:
            self._log(f"打开文件夹选择框失败：{describe_exception(exc)}", "err")
            return ""
        return chosen or ""

    def open_output_dir(self, directory: str = "") -> dict[str, Any]:
        target = str(directory or self._config.output_dir or "").strip()
        if not target or not os.path.isdir(target):
            self._toast("输出文件夹还不存在，先选择或创建它", "err")
            return {"ok": False}
        try:
            if os.name == "nt":
                os.startfile(target)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", target])
        except Exception as exc:
            self._toast(f"打不开：{describe_exception(exc, 80)}", "err")
            return {"ok": False}
        return {"ok": True}

    def shutdown(self) -> None:
        """窗口关闭时调用：停掉正在跑的任务与录制（不销毁窗口）。"""
        self._record_stop.set()
        if self._session is not None:
            self._session.stop()

    def exit(self) -> dict[str, Any]:
        self.shutdown()
        self._on_exit()
        return {"ok": True}


__all__ = [
    "UiBridge",
    "address_from_payload",
    "config_from_payload",
    "describe_exception",
    "log_tone",
]
