# -*- coding: utf-8 -*-
"""AI 接口封装：消息构建、错误分类与带退避的重试。"""

from __future__ import annotations

import difflib
import json
import queue
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from .errors import AiError

# 这些 HTTP 状态码值得重试
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524}


def classify_error(exc: BaseException) -> tuple[bool, str, float | None]:
    """判断异常是否可重试。

    返回 (是否可重试, 原因描述, Retry-After 秒数或 None)。
    不依赖 httpx/openai，便于单独测试。
    """
    name = type(exc).__name__
    status = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status is None and response is not None:
        status = getattr(response, "status_code", None)

    if isinstance(exc, TimeoutError) or "Timeout" in name:
        return True, f"超时（{name}）", None
    if "Connect" in name or "Network" in name or "RemoteProtocol" in name:
        return True, f"连接错误（{name}）", None

    if status is not None:
        if status in RETRYABLE_STATUS:
            retry_after: float | None = None
            headers = getattr(response, "headers", None)
            if headers:
                raw = headers.get("Retry-After") or headers.get("retry-after")
                if raw:
                    try:
                        retry_after = float(raw)
                    except (TypeError, ValueError):
                        retry_after = None
            return True, f"HTTP {status}", retry_after
        if 500 <= int(status) < 600:
            return True, f"HTTP {status}", None
        return False, f"HTTP {status}", None

    # 未知异常默认重试（多为网络抖动）
    return True, name, None


def _image_part(base64_text: str) -> dict[str, Any]:
    return {
        "type": "image_url",
        "image_url": {"url": f"data:image/jpeg;base64,{base64_text}"},
    }


def build_messages(system_prompt: str, images_base64: Sequence[str]) -> list[dict[str, Any]]:
    """构建识别用的 OpenAI 兼容 messages。

    注意：用户提示里**不带**预期题号，避免模型直接照抄提示里的数字；
    题号必须由模型逐张从截图上读取。
    """
    content: list[dict[str, Any]] = [_image_part(text) for text in images_base64]
    content.append(
        {
            "type": "text",
            "text": (
                f"以下 {len(images_base64)} 张截图按顺序排列。"
                "请逐张识别，每张截图的题号必须从该截图右上角读取，"
                "并严格按系统要求只输出 JSON 数组。"
            ),
        }
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": content},
    ]


def build_preflight_messages(system_prompt: str, image_base64: str) -> list[dict[str, Any]]:
    """构建“读取屏幕题号”的预检消息。"""
    return [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": [
                _image_part(image_base64),
                {"type": "text", "text": "请读取这张截图右上角的题号和总题数。"},
            ],
        },
    ]


class AiClient:
    """一个 API Key 对应一个客户端。"""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float = 60.0,
        max_retries: int = 5,
        retry_delay: float = 1.0,
        retry_backoff: float = 2.0,
        temperature: float = 0.01,
        max_tokens: int = 4096,
        log: Callable[[str], None] | None = None,
        deadline: float = 0.0,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.timeout = float(timeout)
        self.max_retries = max(1, int(max_retries))
        self.retry_delay = max(0.0, float(retry_delay))
        self.retry_backoff = max(1.0, float(retry_backoff))
        self.temperature = float(temperature)
        self.max_tokens = int(max_tokens)
        self.log = log or (lambda message: None)
        # 单次 complete() 的总时间预算（含重试）；0 表示不加硬超时
        self.deadline = max(0.0, float(deadline))
        self.client_factory = client_factory
        self._client: Any = None

    def _ensure_client(self) -> Any:
        if self._client is None:
            factory = self.client_factory or make_client
            try:
                self._client = factory(self.api_key, self.base_url, self.timeout)
            except ImportError as exc:  # pragma: no cover
                raise AiError("需要 openai 与 httpx：pip install openai httpx") from exc
        return self._client

    def _call_once(self, messages: Sequence[dict[str, Any]]) -> str:
        client = self._ensure_client()
        response = client.chat.completions.create(
            model=self.model,
            messages=list(messages),
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )
        text = (response.choices[0].message.content or "").strip()
        if not text:
            raise AiError("AI 返回内容为空")
        return text

    def _call_with_deadline(
        self, messages: Sequence[dict[str, Any]], remaining: float, prefix: str
    ) -> str:
        """调用一次；remaining > 0 时加一道硬超时。

        httpx 的 read timeout 是「两次读到数据之间」的超时，服务端把请求挂住
        （排队 / 连接吊着不回）时它可能永远不触发 —— 表现出来就是某个 worker
        卡在批次 0 不动，所以这里必须有一道到点就放弃的兜底。
        """
        if remaining <= 0:
            return self._call_once(messages)
        box: "queue.Queue[tuple[bool, Any]]" = queue.Queue(maxsize=1)

        def work() -> None:
            try:
                box.put((True, self._call_once(messages)))
            except BaseException as exc:  # noqa: BLE001 - 原样带回调用线程
                box.put((False, exc))

        threading.Thread(target=work, name="drpilot-ai-call", daemon=True).start()
        try:
            ok, payload = box.get(timeout=remaining)
        except queue.Empty:
            self.log(f"{prefix}单次请求 {remaining:.0f}s 没有返回，放弃这次调用（服务端可能在排队）")
            raise TimeoutError(f"请求 {remaining:.0f}s 未返回") from None
        if not ok:
            raise payload
        return str(payload)

    def complete(self, messages: Sequence[dict[str, Any]], meta: str = "") -> str:
        """调用一次对话补全，失败按策略重试。

        deadline 是整次调用（含所有重试）的时间预算：服务端把请求挂住时，
        httpx 的「两次读之间」超时可能永远不触发，必须有一道到点就放弃的兜底，
        否则某个 worker 会一直卡在同一个批次上。
        """
        delay = self.retry_delay
        prefix = f"[{meta}] " if meta else ""
        budget = self.deadline
        started = time.monotonic()
        for attempt in range(1, self.max_retries + 1):
            remaining = 0.0
            if budget > 0:
                remaining = budget - (time.monotonic() - started)
                if remaining <= 0.05:
                    # 预算用完：不再开新的一次尝试（第一次总是允许跑，哪怕预算很小）
                    raise AiError(
                        f"{prefix}AI 调用失败：累计 {budget:.0f}s 仍未成功"
                        "（可在配置文件里调大 request_deadline）"
                    )
            try:
                return self._call_with_deadline(messages, remaining, prefix)
            except AiError:
                raise
            except Exception as exc:
                retryable, reason, retry_after = classify_error(exc)
                used = time.monotonic() - started
                if not retryable:
                    raise AiError(f"{prefix}AI 调用失败（不可重试）：{reason}") from exc
                if attempt >= self.max_retries:
                    raise AiError(
                        f"{prefix}AI 调用失败：重试 {self.max_retries} 次仍失败（{reason}）"
                    ) from exc
                wait = retry_after if retry_after is not None else delay
                self.log(
                    f"{prefix}第 {attempt}/{self.max_retries} 次重试：{reason}"
                    f"（已耗时 {used:.0f}s），等待 {wait:.1f}s"
                )
                time.sleep(wait)
                delay = min(delay * self.retry_backoff, 60.0)
        raise AiError(f"{prefix}AI 调用失败")


# ---------------- 客户端构造与模型连通性自检 ----------------
ClientFactory = Callable[[str, str, float], Any]


def make_client(api_key: str, base_url: str, timeout: float = 60.0) -> Any:
    """构造 OpenAI 兼容客户端（缺少依赖时抛 ImportError）。"""
    import httpx
    from openai import OpenAI

    http_client = httpx.Client(
        headers={"Accept-Charset": "utf-8"},
        timeout=httpx.Timeout(float(timeout), connect=min(10.0, float(timeout))),
    )
    return OpenAI(api_key=api_key, base_url=base_url, http_client=http_client)


@dataclass
class ModelCheckResult:
    """一次模型连通性测试的结果。

    典型场景：平台把某个模型下架后，运行前先自检就能立刻发现，
    而不是等截图跑了一半才在重试里失败。
    """

    ok: bool
    model: str
    base_url: str
    detail: str
    latency_ms: int = 0
    model_listed: bool | None = None  # 是否出现在平台 /models 列表中
    model_count: int = 0
    suggestions: tuple[str, ...] = ()
    reply: str = ""
    checked_at: float = field(default_factory=time.time)

    @property
    def tone(self) -> str:
        if self.ok:
            return "success"
        return "danger"

    def summary(self) -> str:
        """一行中文结论，用于日志和状态栏。"""
        if self.ok:
            note = ""
            if self.model_listed is False:
                note = "；注意：该模型未出现在平台模型列表中"
            return f"模型连通性正常：{self.model}（{self.latency_ms} ms）{note}"
        return f"模型不可用：{self.model} —— {self.detail}"

    def hint(self) -> str:
        """给出下一步怎么办。"""
        if self.ok:
            return ""
        tips: list[str] = []
        if self.suggestions:
            tips.append("平台上有相似模型：" + "、".join(self.suggestions))
        tips.append("请在平台确认该模型是否已下架，并修改「模型 ID」后重试")
        tips.append("可先点「测试连通性」验证，再开始提取")
        return "；".join(tips)


def _item_id(item: Any) -> str:
    if isinstance(item, Mapping):
        return str(item.get("id") or "")
    return str(getattr(item, "id", "") or "")


def fetch_model_ids(client: Any) -> list[str]:
    """读取平台可用模型 ID 列表。"""
    page = client.models.list()
    data: Any = getattr(page, "data", None)
    if data is None and isinstance(page, Mapping):
        data = page.get("data")
    if data is None:
        data = page
    return [value for value in (_item_id(item) for item in (data or [])) if value]


def _json_message(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        return ""
    try:
        data = json.loads(raw)
    except Exception:
        return ""
    if isinstance(data, Mapping):
        error = data.get("error")
        if isinstance(error, Mapping):
            return str(error.get("message") or error.get("code") or "")
        return str(data.get("message") or "")
    return ""


def describe_exception(exc: BaseException, limit: int = 220) -> str:
    """把 SDK 异常压成一句人话（优先取接口返回的 message 字段）。"""
    message = ""
    body = getattr(exc, "body", None)
    if isinstance(body, Mapping):
        error = body.get("error")
        if isinstance(error, Mapping):
            message = str(error.get("message") or error.get("code") or "")
        if not message:
            message = str(body.get("message") or "")
    if not message:
        message = _json_message(getattr(getattr(exc, "response", None), "text", ""))
    if not message:
        message = str(exc)
    message = " ".join(str(message).split())
    return message[:limit] or type(exc).__name__


def check_model_connection(
    api_key: str,
    base_url: str,
    model: str,
    *,
    timeout: float = 30.0,
    client_factory: ClientFactory | None = None,
    probe_text: str = "ping",
    probe_tokens: int = 16,
    list_models: bool = True,
) -> ModelCheckResult:
    """测试「这个模型现在还能不能调用」。

    两步走：
        1. 读平台模型列表，确认模型 ID 还在（下架能立刻看出来，并给出相似模型提示）；
        2. 发一次极小的对话请求，真正验证鉴权、模型与网络都通。

    步骤 2 才是结论；步骤 1 只用于把错误说得更清楚。
    """
    model = str(model or "").strip()
    base_url = str(base_url or "").strip()
    if not model:
        return ModelCheckResult(False, model, base_url, "未填写模型 ID")
    if not api_key:
        return ModelCheckResult(False, model, base_url, "未配置 API Key（请检查 .env）")

    factory = client_factory or make_client
    try:
        client = factory(api_key, base_url, float(timeout))
    except ImportError as exc:
        return ModelCheckResult(
            False, model, base_url, f"缺少依赖（{exc}）：请先执行 pip install openai httpx"
        )
    except Exception as exc:
        return ModelCheckResult(False, model, base_url, f"初始化客户端失败：{describe_exception(exc)}")

    started = time.monotonic()
    listed: bool | None = None
    ids: list[str] = []
    suggestions: tuple[str, ...] = ()
    list_error = ""

    if list_models and hasattr(client, "models"):
        try:
            ids = fetch_model_ids(client)
        except Exception as exc:
            list_error = describe_exception(exc)
        else:
            wanted = model.lower()
            listed = any(item.lower() == wanted for item in ids)
            if not listed:
                suggestions = tuple(difflib.get_close_matches(model, ids, n=3, cutoff=0.45))

    def result(ok: bool, detail: str, reply: str = "") -> ModelCheckResult:
        return ModelCheckResult(
            ok=ok,
            model=model,
            base_url=base_url,
            detail=detail,
            latency_ms=int((time.monotonic() - started) * 1000),
            model_listed=listed,
            model_count=len(ids),
            suggestions=suggestions,
            reply=reply,
        )

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": probe_text}],
            temperature=0.0,
            max_tokens=max(1, int(probe_tokens)),
        )
    except Exception as exc:
        reason = describe_exception(exc)
        if listed is False:
            detail = f"平台模型列表（共 {len(ids)} 个）里已经没有该模型，可能已下架"
            if reason:
                detail += f"；接口返回：{reason}"
        elif list_error:
            detail = f"{reason}（顺便读模型列表也失败了：{list_error}）"
        else:
            detail = reason or type(exc).__name__
        return result(False, detail)
    finally:
        closer = getattr(client, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                pass

    reply = ""
    try:
        reply = str(response.choices[0].message.content or "").strip()
    except Exception:
        reply = ""
    if listed is False:
        detail = f"可以调用，但该模型未出现在平台模型列表中（列表共 {len(ids)} 个）"
    else:
        detail = "可以调用"
    return result(True, detail, reply[:80])

