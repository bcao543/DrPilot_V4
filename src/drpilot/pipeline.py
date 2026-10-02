# -*- coding: utf-8 -*-
"""运行编排：预检起始题号 -> 截图 -> 分批 -> 并发识别 -> 按题号合并落盘。

题号规则：以 AI 从截图右上角读到的编号为准（screen_id）。
程序里的预期序号只用于提示不一致，不覆盖截图编号，
这样中途开始、跳题、漏题补录都不会连累其他题的编号。
"""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable, Sequence

from .actions import build_action_plan, build_next_action, replay_next_action
from .adb import AdbClient, DeviceInfo
from .ai import (
    AiClient,
    ModelCheckResult,
    build_messages,
    build_preflight_messages,
    check_model_connection,
)
from .config import AppConfig, OutputPaths, resolve_output_paths
from .errors import DrPilotError
from .images import md5_hex, to_jpeg_base64
from .models import Question
from .output import ResultWriter
from .parser import parse_ai_response, parse_screen_info
from .prompts import PREFLIGHT_PROMPT, SYSTEM_PROMPT

LogFn = Callable[[str], None]
ProgressFn = Callable[[int, int, int], None]
WorkerFn = Callable[[int, str], None]
WorkerProgressFn = Callable[[int, int], None]
DoneFn = Callable[[], None]
ErrorFn = Callable[[BaseException], None]
PreflightFn = Callable[[int, int], bool]
AiFactory = Callable[[str, AppConfig], Any]
Encoder = Callable[[bytes], str]
ModelCheckFn = Callable[[ModelCheckResult], bool]
ModelChecker = Callable[..., ModelCheckResult]


def _short(exc: BaseException, limit: int = 80) -> str:
    text = str(exc).strip().replace("\n", " ")
    return text[:limit] if text else type(exc).__name__


class PilotSession:
    """一次完整的运行。

    所有回调都可能在子线程中被调用；GUI 侧必须自行调度回主线程。
    """

    def __init__(
        self,
        config: AppConfig,
        on_log: LogFn | None = None,
        on_progress: ProgressFn | None = None,
        on_worker: WorkerFn | None = None,
        on_worker_progress: WorkerProgressFn | None = None,
        on_done: DoneFn | None = None,
        on_error: ErrorFn | None = None,
        on_preflight: PreflightFn | None = None,
        on_model_check: ModelCheckFn | None = None,
        adb: AdbClient | None = None,
        ai_factory: AiFactory | None = None,
        image_encoder: Encoder | None = None,
        result_writer: ResultWriter | None = None,
        model_checker: ModelChecker | None = None,
    ) -> None:
        self.config = config
        self.on_log = on_log or (lambda message: None)
        self.on_progress = on_progress or (lambda current, total, number: None)
        self.on_worker = on_worker or (lambda worker_id, status: None)
        self.on_worker_progress = on_worker_progress or (lambda worker_id, done: None)
        self.on_done = on_done or (lambda: None)
        self.on_error = on_error or (lambda exc: None)
        self.on_preflight = on_preflight
        self.on_model_check = on_model_check
        self._model_checker = model_checker or check_model_connection

        self.stop_event = threading.Event()
        self.error: BaseException | None = None
        # ---- 给 Agent 的诊断信息（报错时用来定位问题） ----
        self.phase = "init"                 # 当前阶段：connect / preflight / capture / batch / write / done
        self.error_context: dict[str, Any] = {}
        self.batch_failures: list[dict[str, Any]] = []
        self.failed_numbers: list[int] = []
        self.missing_numbers: list[int] = []
        self._diag_lock = threading.Lock()
        self.questions: list[Question] = []
        self.output_paths: dict[str, str | None] = {}
        self.paths: OutputPaths | None = None
        self.device: DeviceInfo | None = None
        self.resolution: tuple[int, int] | None = None
        self.reference_recorded = False
        self.action_plan = None
        self.model_check: ModelCheckResult | None = None

        self._adb = adb
        self._ai_factory = ai_factory
        self._encode = image_encoder
        self._writer = result_writer
        self._threads: list[threading.Thread] = []
        self._task_queue: "queue.Queue[tuple[int, list[tuple[str, int]]]] | None" = None
        self._result_store: dict[int, list[Question]] = {}
        self._result_lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self._last_flush_count = 0

    # ---- 对外 ----
    def log(self, message: str) -> None:
        self.on_log(message)

    def set_phase(self, phase: str) -> None:
        """记录当前阶段，出错时随 error_context 一起报给 Agent。"""
        self.phase = str(phase)

    def _record_failure(
        self,
        worker_id: int,
        batch_id: int,
        numbers: Sequence[int],
        message: str,
        *,
        phase: str = "batch",
    ) -> None:
        """记录一次批次/写盘失败：Agent 拿 question_numbers 就能直接续跑。"""
        detail = {
            "phase": phase,
            "worker": int(worker_id),
            "batch": int(batch_id),
            "question_numbers": [int(n) for n in numbers],
            "error": str(message),
            "at": time.strftime("%H:%M:%S"),
        }
        with self._diag_lock:
            self.batch_failures.append(detail)
            self.failed_numbers.extend(int(n) for n in numbers if int(n) > 0)
            self.error_context = dict(detail)

    def diagnostics(self) -> dict[str, Any]:
        """结构化诊断：失败阶段、批次、题号，供 CLI 的 error_report 使用。"""
        with self._diag_lock:
            failed = sorted({n for n in self.failed_numbers if n > 0})
            return {
                "phase": self.phase,
                "error_context": dict(self.error_context),
                "failed_question_numbers": failed,
                "missing_question_numbers": list(self.missing_numbers),
                "batch_failures": list(self.batch_failures)[-10:],
            }

    def stop(self) -> None:
        """停止：不再截新图，并丢掉还没被 worker 取走的批次。

        已经开始的批次会正常做完（避免白花 API 调用），已经写盘的结果也不会丢
        （ResultWriter 是按题号合并写出的）。重复点停止不会有副作用。
        """
        if self.stop_event.is_set():
            return
        self.stop_event.set()
        self._drop_pending_batches()

    def _drop_pending_batches(self) -> int:
        queue_ = self._task_queue
        if queue_ is None:
            return 0
        dropped = 0
        while True:
            try:
                queue_.get_nowait()
            except queue.Empty:
                break
            queue_.task_done()   # 取出来就必须 task_done，否则 task_queue.join() 会一直等
            dropped += 1
        if dropped:
            self.log(f"已停止：丢弃 {dropped} 个尚未开始的批次，只等已经开始的那几个收尾")
        else:
            self.log("已停止：没有排队中的批次，只有进行中的批次在收尾")
        return dropped

    def run(self) -> bool:
        """执行一次运行；返回是否成功。无论成功失败都会调用 on_done。"""
        try:
            self._run()
            return True
        except Exception as exc:
            self.error = exc
            with self._diag_lock:
                self.error_context = {
                    "phase": self.phase,
                    "error": _short(exc, 300),
                    "error_type": type(exc).__name__,
                }
            self.log(f"运行失败（阶段 {self.phase}）：{exc}")
            try:
                self.on_error(exc)
            except Exception:
                pass
            return False
        finally:
            try:
                self.on_done()
            except Exception:
                pass

    # ---- 内部工具 ----
    def _encode_image(self, image_bytes: bytes) -> str:
        if self._encode is not None:
            return self._encode(image_bytes)
        return to_jpeg_base64(image_bytes, self.config.jpeg_quality)

    def _reference_size(self) -> tuple[int, int] | None:
        width = int(self.config.swipe_reference_width or 0)
        height = int(self.config.swipe_reference_height or 0)
        if width > 0 and height > 0:
            return width, height
        return None

    def _do_next_action(self, adb: AdbClient) -> None:
        """执行一次「下一题」动作：录制的动作优先，否则回退到固定滑动坐标。"""
        plan = self.action_plan or build_action_plan(
            build_next_action(self.config),
            self._reference_size(),
            self.resolution,
        )
        replay_next_action(adb, plan, log=self.log, settle_wait=self.config.wait)

    def _next_action_until_changed(
        self,
        adb: AdbClient,
        previous_hash: str,
        last_encoded: str,
    ) -> tuple[str, str]:
        """重复截图时重试滑动；仍失败则接受当前帧，避免死循环。"""
        config = self.config
        if config.max_swipe_retries <= 0:
            return last_encoded, previous_hash
        for attempt in range(1, config.max_swipe_retries + 1):
            if self.stop_event.is_set():
                return last_encoded, previous_hash
            if config.retry_wait > 0:
                time.sleep(config.retry_wait)
            try:
                self._do_next_action(adb)
            except Exception as exc:
                self.log(f"重试翻页失败（{attempt}）：{exc}")
                continue
            encoded = self._encode_image(adb.screenshot())
            current_hash = md5_hex(encoded)
            if current_hash != previous_hash:
                self.log(f"重试 {attempt} 成功")
                return encoded, current_hash
        self.log("多次重试仍重复，接受当前帧继续")
        return last_encoded, previous_hash

    def _make_client(self, api_key: str) -> Any:
        if self._ai_factory is not None:
            return self._ai_factory(api_key, self.config)
        config = self.config
        return AiClient(
            api_key=api_key,
            base_url=config.base_url,
            model=config.model,
            timeout=config.request_timeout,
            max_retries=config.max_retries,
            retry_delay=config.retry_delay,
            retry_backoff=config.retry_backoff,
            log=self.log,
            deadline=config.effective_request_deadline,
        )

    def _check_model(self) -> None:
        """开始前测试模型连通性：平台下架模型时立刻报错，而不是跑到一半才失败。"""
        config = self.config
        self.log("正在测试模型连通性…")
        try:
            result = self._model_checker(
                config.api_keys[0],
                config.base_url,
                config.model,
                timeout=min(float(config.request_timeout), 30.0),
            )
        except Exception as exc:
            result = ModelCheckResult(
                ok=False,
                model=config.model,
                base_url=config.base_url,
                detail=f"测试过程出错：{_short(exc, 200)}",
            )
        self.model_check = result
        self.log(result.summary())
        if result.ok:
            return
        hint = result.hint()
        if hint:
            self.log(f"提示：{hint}")
        proceed = bool(self.on_model_check(result)) if self.on_model_check is not None else False
        if not proceed:
            raise DrPilotError(f"模型连通性测试未通过：{result.detail}")

    def _preflight(self, adb: AdbClient, client: Any) -> tuple[str, int | None, int | None]:
        """截一张图，让 AI 读右上角题号，返回 (截图, 当前题号, 总题数)。"""
        encoded = self._encode_image(adb.screenshot())
        text = client.complete(
            build_preflight_messages(PREFLIGHT_PROMPT, encoded),
            meta="preflight",
        )
        screen_id, screen_total = parse_screen_info(text)
        return encoded, screen_id, screen_total

    def _assign_questions(
        self,
        parsed: Sequence[Question | None],
        expected_numbers: Sequence[int],
        worker_id: int,
        batch_id: int,
    ) -> list[Question]:
        """按“截图题号优先”的规则给每题确定 id。"""
        questions: list[Question] = []
        for index, question in enumerate(parsed):
            expected = expected_numbers[index] if index < len(expected_numbers) else 0
            if question is None:
                self.log(
                    f"[W{worker_id}] 批次 {batch_id} 第 {index + 1} 张截图未识别，"
                    f"用第 {expected} 题占位"
                )
                questions.append(Question.placeholder(expected))
                continue
            screen_id = question.screen_id
            if screen_id:
                question.id = int(screen_id)
                if expected and screen_id != expected:
                    self.log(
                        f"[提示] 截图显示第 {screen_id} 题，与预期第 {expected} 题不一致（以截图为准）"
                    )
            elif question.id > 0:
                if expected and question.id != expected:
                    self.log(
                        f"[提示] AI 返回题号 {question.id}，与预期第 {expected} 题不一致（以 AI 题号为准）"
                    )
            else:
                question.id = expected
            questions.append(question)
        return questions

    def _start_workers(
        self,
        task_queue: "queue.Queue[tuple[int, list[tuple[str, int]]]]",
        writer: ResultWriter,
        worker_count: int,
    ) -> None:
        keys = self.config.api_keys
        for index in range(worker_count):
            client = self._make_client(keys[index % len(keys)])
            thread = threading.Thread(
                target=self._worker,
                args=(index + 1, client, task_queue, writer),
                name=f"drpilot-worker-{index + 1}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

    def _ordered_questions(self) -> list[Question]:
        """按批次顺序收集已完成的结果，**允许中间缺批次**。

        以前这里是「遇到第一个缺口就停」，只要 0 号批次还在跑（比如在重试），
        后面所有已经识别好的批次就一个都写不出盘 —— 用户看到的就是「worker 明明在干活，
        文件夹却是空的」。写盘本身按题号排序合并，所以缺口不影响文件正确性。
        """
        with self._result_lock:
            batches = sorted(self._result_store.items())
        ordered: list[Question] = []
        for _, questions in batches:
            ordered.extend(questions)
        return ordered

    def _flush(self, writer: ResultWriter) -> None:
        """串行写出，且在同一把锁内取最新结果，保证最后一次写的是全量。"""
        with self._flush_lock:
            ordered = self._ordered_questions()
            if ordered and len(ordered) != self._last_flush_count:
                writer.flush(ordered)
                self._last_flush_count = len(ordered)

    def _worker(
        self,
        worker_id: int,
        client: Any,
        task_queue: "queue.Queue[tuple[int, list[tuple[str, int]]]]",
        writer: ResultWriter,
    ) -> None:
        self.log(f"Worker-{worker_id} 已启动")
        self.on_worker(worker_id, "空闲")
        done = 0
        while True:
            try:
                batch_id, items = task_queue.get(timeout=0.5)
            except queue.Empty:
                if self.stop_event.is_set() and task_queue.empty():
                    break
                continue
            numbers = [number for _, number in items]
            try:
                if self.stop_event.is_set():
                    # 刚取到就被叫停：不浪费这次调用，直接跳过
                    self.log(f"[W{worker_id}] 已停止，跳过批次 {batch_id}")
                    continue
                try:
                    self.log(f"[W{worker_id}] 批次 {batch_id}，预期题号 {numbers}")
                    self.on_worker(worker_id, f"处理批次 {batch_id}")
                    messages = build_messages(
                        SYSTEM_PROMPT,
                        [encoded for encoded, _ in items],
                    )
                    call_started = time.monotonic()
                    text = client.complete(messages, meta=f"W{worker_id} batch{batch_id}")
                    call_used = time.monotonic() - call_started
                    if call_used > 30:
                        self.log(
                            f"[W{worker_id}] 批次 {batch_id} 用了 {call_used:.0f}s"
                            "（模型慢或在排队：可换更快的模型，或调大 request_deadline）"
                        )
                    parsed = parse_ai_response(text, expected_count=len(items))
                    questions = self._assign_questions(parsed, numbers, worker_id, batch_id)
                except Exception as exc:
                    short = _short(exc, 200)
                    self.log(
                        f"[W{worker_id}] 批次 {batch_id} 失败：{short}"
                        f"（涉及题号 {numbers}，可用 --from/--to 补录）"
                    )
                    self._record_failure(worker_id, batch_id, numbers, short, phase="batch")
                    questions = [
                        Question.placeholder(number, reason=short)
                        for number in numbers
                    ]
                with self._result_lock:
                    self._result_store.setdefault(batch_id, questions)
                try:
                    self._flush(writer)
                except Exception as exc:
                    # 写盘失败（磁盘满 / 没权限）必须报出来，否则结果会悄悄丢掉
                    short = _short(exc, 200)
                    self.log(f"[W{worker_id}] 写入失败：{short}")
                    self._record_failure(worker_id, batch_id, numbers, short, phase="write")
                done += 1
                self.on_worker_progress(worker_id, done)
                self.on_worker(worker_id, "空闲")
            except Exception as exc:
                # 兜底：单个批次出任何意外都不该让 worker 线程消失
                short = _short(exc, 200)
                self.log(f"[W{worker_id}] 批次 {batch_id} 处理异常：{short}")
                self._record_failure(worker_id, batch_id, numbers, short, phase="worker")
                with self._result_lock:
                    self._result_store.setdefault(
                        batch_id,
                        [Question.placeholder(number) for number in numbers],
                    )
            finally:
                task_queue.task_done()
        self.log(f"Worker-{worker_id} 已退出")
        self.on_worker(worker_id, "已退出")

    def _run(self) -> None:
        config = self.config
        config.normalize()
        config.validate(require_keys=True)
        total = config.total
        self.set_phase("connect")

        adb = self._adb or AdbClient(adb_path=config.adb_path, serial=config.serial, log=self.log)
        if config.device_address:
            try:
                message = adb.connect(config.device_address)
                self.log(f"无线连接 {config.device_address}：{message}")
            except Exception as exc:
                self.log(f"无线连接失败（改用已连接的设备）：{_short(exc, 200)}")
        self.device = adb.ensure_device()
        self.log(f"设备：{self.device.describe()}")
        try:
            self.resolution = adb.wm_size()
        except Exception as exc:
            self.log(f"读取分辨率失败：{exc}")
        if self.resolution:
            self.log(f"设备分辨率：{self.resolution[0]}x{self.resolution[1]}")
            if config.swipe_reference_width <= 0 or config.swipe_reference_height <= 0:
                config.swipe_reference_width, config.swipe_reference_height = self.resolution
                self.reference_recorded = True
                self.log(
                    f"已自动记录滑动基准分辨率：{self.resolution[0]}x{self.resolution[1]}"
                    "（以后换机型会按此自动缩放）"
                )
        self.action_plan = build_action_plan(
            build_next_action(config),
            self._reference_size(),
            self.resolution,
        )
        self.log(f"翻页动作：{self.action_plan.describe()}")

        # 并发按用户设置来；Key 不够时多个 worker 共用一个 Key（轮着用），只是可能触发限流
        key_count = max(1, len(config.api_keys))
        worker_count = max(1, config.workers)
        self.log(f"模型：{config.model}  API：{config.base_url}")
        self.log(
            f"计划：Q{config.page_from}~Q{config.page_to}，共 {total} 张截图；"
            f"批大小 {config.batch_size}，并发 {worker_count}"
        )
        if worker_count > key_count:
            self.log(
                f"[提示] 并发 {worker_count} 大于 API Key 数 {key_count}："
                "多出来的 worker 会轮换共用同一个 Key，遇到 429 限流会自动重试；"
                "想真正提速就在 .env 里再加一个 SILICONFLOW_API_KEY_3"
            )
        if config.textbook or config.chapter:
            self.log(f"教材/章节：{config.textbook or '（未填）'} / {config.chapter or '（未填）'}")

        # ---- 模型连通性自检：模型被下架时立刻停下 ----
        if config.check_model:
            self.set_phase("model")
            self._check_model()

        # ---- 预检：确认手机停在正确的题号 ----
        pending_encoded: str | None = None
        if config.preflight:
            self.set_phase("preflight")
            preflight_client = self._make_client(config.api_keys[0])
            try:
                pending_encoded, screen_id, screen_total = self._preflight(adb, preflight_client)
            except Exception as exc:
                self.log(f"起始题号预检失败（跳过预检）：{_short(exc, 200)}")
            else:
                if screen_total and not config.chapter_total:
                    config.chapter_total = int(screen_total)
                    self.log(f"预检：本章共 {screen_total} 题")
                if screen_id:
                    self.log(f"预检：当前屏幕显示第 {screen_id} 题")
                    if int(screen_id) != config.page_from:
                        message = f"当前屏幕显示第 {screen_id} 题，但起始题号设置为 {config.page_from}"
                        if self.on_preflight is not None:
                            if not self.on_preflight(int(screen_id), config.page_from):
                                raise DrPilotError(f"已取消：{message}")
                        elif config.force_start:
                            self.log(f"[警告] {message}（已按 --force-start 继续）")
                        else:
                            raise DrPilotError(
                                f"{message}；请把手机停在正确的题目，或加 --force-start 继续"
                            )
                else:
                    self.log("预检：未能读到题号，继续执行")

        # ---- 输出准备 ----
        self.set_phase("prepare")
        self.paths = resolve_output_paths(config)
        Path(self.paths.jsonl).parent.mkdir(parents=True, exist_ok=True)
        writer = self._writer or ResultWriter(self.paths, config.metadata(), log=self.log)
        if config.output_prefix:
            self.log(f"输出前缀：{config.output_prefix}")
        else:
            self.log(f"输出文件：{self.paths.base_name}.jsonl")

        self._result_store = {}
        self._last_flush_count = 0
        self._threads = []
        task_queue: "queue.Queue[tuple[int, list[tuple[str, int]]]]" = queue.Queue()
        self._task_queue = task_queue
        self._start_workers(task_queue, writer, worker_count)

        self.set_phase("capture")
        collected = 0
        batch_id = 0
        previous_hash: str | None = None
        batch: list[tuple[str, int]] = []
        expected = config.page_from
        try:
            while collected < total and not self.stop_event.is_set():
                if pending_encoded is not None:
                    encoded = pending_encoded
                    pending_encoded = None
                else:
                    encoded = self._encode_image(adb.screenshot())
                current_hash = md5_hex(encoded)
                if previous_hash is not None and current_hash == previous_hash:
                    self.log("检测到重复截图，重试翻页动作…")
                    encoded, current_hash = self._next_action_until_changed(adb, previous_hash, encoded)
                previous_hash = current_hash

                batch.append((encoded, expected))
                collected += 1
                qnum = expected
                expected += 1
                self.log(f"截图（{collected}/{total}），预期第 {qnum} 题")
                self.on_progress(collected, total, qnum)

                if collected < total and not self.stop_event.is_set():
                    self._do_next_action(adb)

                if len(batch) >= config.batch_size or collected == total:
                    task_queue.put((batch_id, batch))
                    self.log(f"[队列] 批次 {batch_id}：{len(batch)} 张")
                    batch_id += 1
                    batch = []
        except Exception:
            self.stop_event.set()
            raise
        finally:
            if batch and not self.stop_event.is_set():
                task_queue.put((batch_id, batch))
                self.log(f"[队列] 批次 {batch_id}：{len(batch)} 张")

        stopped_early = self.stop_event.is_set()
        if stopped_early:
            self.log("已停止截图，等待进行中的批次处理完…")
        else:
            self.log("截图完成，等待 AI 处理…")
        self.set_phase("batch")
        task_queue.join()
        self.stop_event.set()
        for thread in self._threads:
            thread.join(timeout=config.request_timeout + 30)

        self.set_phase("write")
        self.questions = self._ordered_questions()
        self.output_paths = writer.finalize(self.questions)

        ids = sorted({question.id for question in self.questions if question.id > 0})
        if ids:
            missing = sorted(set(range(ids[0], ids[-1] + 1)) - set(ids))
            self.missing_numbers = missing
            if missing:
                self.log(f"[提示] 检测到缺号：{missing}（可用同章节重新提取补录）")
            self.log(f"本次题号：{ids[0]}~{ids[-1]}，共 {len(ids)} 题")

        # ---- 诊断汇总：失败批次 / 失败题号一目了然，Agent 直接照抄补录命令 ----
        failed = sorted({n for n in self.failed_numbers if n > 0})
        if self.batch_failures:
            self.log(
                f"[诊断] 有 {len(self.batch_failures)} 个批次失败，涉及题号 {failed}；"
                f"可用 --from {failed[0]} --to {failed[-1]} 配同一输出目录重新补录"
            )

        if stopped_early:
            self.log(
                f"已停止：本次共识别 {len(self.questions)} 道题（已写入的结果都保留，"
                "随时可以重新运行续跑补录）"
            )
        else:
            self.log(f"全部完成！共 {len(self.questions)} 道题")
        self.set_phase("done")
        self.log(f"JSONL：{self.output_paths.get('jsonl')}")
        if self.output_paths.get("markdown"):
            self.log(f"Markdown：{self.output_paths.get('markdown')}")
        if self.output_paths.get("index"):
            self.log(f"索引：{self.output_paths.get('index')}")
