# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import time
import unittest

from drpilot.ai import (
    AiClient,
    build_messages,
    build_preflight_messages,
    check_model_connection,
    classify_error,
    describe_exception,
)
from drpilot.errors import AiError


class ReadTimeout(Exception):
    pass


class APIConnectionError(Exception):
    pass


class FakeResponse:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


class StatusError(Exception):
    def __init__(self, status_code, headers=None):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code
        self.response = FakeResponse(status_code, headers)


class AiTests(unittest.TestCase):
    def test_retryable_errors(self):
        self.assertTrue(classify_error(ReadTimeout())[0])
        self.assertTrue(classify_error(APIConnectionError())[0])
        retryable, _, retry_after = classify_error(StatusError(429, {"Retry-After": "2.5"}))
        self.assertTrue(retryable)
        self.assertEqual(retry_after, 2.5)

    def test_not_retryable_errors(self):
        retryable, reason, _ = classify_error(StatusError(400))
        self.assertFalse(retryable)
        self.assertIn("400", reason)

    def test_build_messages_has_no_expected_numbers(self):
        messages = build_messages("系统提示", ["abc"])
        content = messages[1]["content"]
        self.assertEqual(content[0]["type"], "image_url")
        self.assertEqual(content[0]["image_url"]["url"], "data:image/jpeg;base64,abc")
        self.assertEqual(content[-1]["type"], "text")
        # 用户提示里不应出现预期题号，避免模型照抄
        self.assertNotIn("1、", content[-1]["text"])

    def test_build_preflight_messages(self):
        messages = build_preflight_messages("预检提示", "abc")
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[1]["content"][0]["type"], "image_url")
        self.assertEqual(messages[1]["content"][-1]["type"], "text")


class DeadlineTests(unittest.TestCase):
    """服务端把请求挂住时，必须到点放弃，不能让 worker 永远卡着。"""

    @staticmethod
    def _hanging_factory():
        class HangingCompletions:
            def create(self, **kwargs):
                time.sleep(30)   # 永远不返回

        class HangingClient:
            def __init__(self):
                self.chat = type("Chat", (), {"completions": HangingCompletions()})()

        return lambda api_key, base_url, timeout: HangingClient()

    def test_gives_up_after_deadline(self):
        logs: list[str] = []
        ai = AiClient(
            api_key="k",
            base_url="u",
            model="m",
            max_retries=3,
            retry_delay=0.01,
            retry_backoff=1.0,
            deadline=2.0,
            log=logs.append,
            client_factory=self._hanging_factory(),
        )
        started = time.monotonic()
        with self.assertRaises(AiError):
            ai.complete([{"role": "user", "content": "hi"}], meta="W1")
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 8.0, "卡住的请求没有在预算内被放弃")
        self.assertTrue(any("没有返回" in line for line in logs), logs)

    def test_without_deadline_it_waits(self):
        """deadline=0 时不加硬超时（保持老行为，交给 httpx 超时）。"""
        ai = AiClient(api_key="k", base_url="u", model="m", deadline=0.0)
        self.assertEqual(ai.deadline, 0.0)


class ModelCheckTests(unittest.TestCase):
    """模型连通性自检：平台下架模型时要能提前发现。"""

    def _factory(self, ids=None, error=None, list_error=None, reply="pong"):
        holder = {}

        class FakeModels:
            def list(self_inner):
                if list_error is not None:
                    raise list_error
                page = type("Page", (), {})()
                page.data = [type("Item", (), {"id": value})() for value in (ids or [])]
                return page

        class FakeCompletions:
            def create(self_inner, **kwargs):
                holder["kwargs"] = kwargs
                if error is not None:
                    raise error
                message = type("Message", (), {"content": reply})()
                choice = type("Choice", (), {"message": message})()
                return type("Response", (), {"choices": [choice]})()

        class FakeClient:
            def __init__(self_inner):
                self_inner.models = FakeModels()
                self_inner.chat = type("Chat", (), {"completions": FakeCompletions()})()
                self_inner.closed = False

            def close(self_inner):
                self_inner.closed = True

        def factory(api_key, base_url, timeout):
            holder["client"] = FakeClient()
            return holder["client"]

        self.holder = holder
        return factory

    def test_ok_when_model_listed_and_callable(self):
        result = check_model_connection(
            "sk-1", "https://api.example/v1", "Qwen/Qwen3.5-4B",
            client_factory=self._factory(ids=["Qwen/Qwen3.5-4B", "other"]),
        )
        self.assertTrue(result.ok)
        self.assertTrue(result.model_listed)
        self.assertEqual(result.model_count, 2)
        self.assertIn("正常", result.summary())
        self.assertTrue(self.holder["client"].closed)

    def test_flags_model_removed_from_platform(self):
        error = type("NotFound", (Exception,), {"body": {"error": {"message": "Model does not exist"}}})()
        result = check_model_connection(
            "sk-1", "https://api.example/v1", "Qwen/Qwen3.5-4B",
            client_factory=self._factory(ids=["Qwen/Qwen3.5-397B-A17B"], error=error),
        )
        self.assertFalse(result.ok)
        self.assertFalse(result.model_listed)
        self.assertIn("下架", result.detail)
        self.assertIn("Model does not exist", result.detail)
        self.assertIn("模型不可用", result.summary())
        self.assertTrue(result.hint())

    def test_ok_even_if_listing_fails(self):
        result = check_model_connection(
            "sk-1", "https://api.example/v1", "custom-model",
            client_factory=self._factory(ids=[], list_error=RuntimeError("404 page not found")),
        )
        self.assertTrue(result.ok)
        self.assertIsNone(result.model_listed)
        self.assertIn("可以调用", result.detail)

    def test_callable_but_not_listed(self):
        result = check_model_connection(
            "sk-1", "https://api.example/v1", "custom-model",
            client_factory=self._factory(ids=["a", "b"]),
        )
        self.assertTrue(result.ok)
        self.assertFalse(result.model_listed)
        self.assertIn("未出现在平台模型列表", result.detail)

    def test_missing_key_or_model(self):
        self.assertFalse(check_model_connection("", "https://x/v1", "m").ok)
        self.assertIn("API Key", check_model_connection("", "https://x/v1", "m").detail)
        self.assertFalse(check_model_connection("sk-1", "https://x/v1", "").ok)

    def test_describe_exception_prefers_api_message(self):
        class Boom(Exception):
            body = {"error": {"message": "  invalid  api key "}}

        self.assertEqual(describe_exception(Boom()), "invalid api key")

        class Raw(Exception):
            response = type("R", (), {"text": '{"error": {"message": "quota exceeded"}}'})()

        self.assertEqual(describe_exception(Raw()), "quota exceeded")
        self.assertEqual(describe_exception(RuntimeError("plain")), "plain")


if __name__ == "__main__":
    unittest.main()
