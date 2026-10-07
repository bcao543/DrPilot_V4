# -*- coding: utf-8 -*-
"""配置模型，以及 .env / 配置文件的加载与保存。

约定：
    * API Key 只从环境变量读取，绝不写进源码或配置文件。
    * GUI 配置文件只保存非敏感参数。
    * 题号以截图右上角显示的数字为准（见 pipeline）。
"""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Iterable, Mapping

from .actions import normalize_steps, steps_to_payload
from .errors import ConfigError
from .fileio import atomic_write_text, read_json_file
from .modules import (
    ModuleSpec,
    default_output_fields,
    modules_to_payload,
    normalize_modules,
    normalize_output_fields,
    output_field_map,
)

# 默认模型服务：DeepSeek 官方接口（GUI「模型服务」窗口里的初始值）
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"

# 老版本的默认值：配置文件里存着这两个值（说明用户从没改过）时，
# 读配置时一并换成新默认，免得「改了默认值」对新老安装都不生效
LEGACY_DEFAULT_MODEL = "Qwen/Qwen3.5-4B"
LEGACY_DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"

# 翻页动作的选择：auto = 旧行为（录制动作 > 手动点按 > 滑动坐标）
PAGE_ACTIONS = ("auto", "tap", "swipe", "record")

DEFAULT_SWIPE_START_X = 1060
DEFAULT_SWIPE_START_Y = 553
DEFAULT_SWIPE_END_X = 270
DEFAULT_SWIPE_END_Y = 551
DEFAULT_SWIPE_DURATION_MS = 150

MAX_API_KEYS = 16
CONFIG_FILENAME = "drpilot_config.json"
LEGACY_CONFIG_FILENAME = "drpilot_adb_config.json"
INDEX_FILENAME = "index.json"

_ILLEGAL_FILENAME_RE = re.compile(r'[\\/:*?"<>|\r\n\t]+')
_CHAPTER_PREFIX_RE = re.compile(
    r"^\s*第\s*([0-9]+|[零一二两三四五六七八九十百]+)\s*[章节]\s*[、:：.\-]?\s*"
)
_CN_DIGITS = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}


def _project_root() -> Path:
    """优先返回仓库根目录；安装到 site-packages 时退回当前工作目录。"""
    root = Path(__file__).resolve().parents[2]
    if (root / "pyproject.toml").is_file():
        return root
    return Path.cwd()


PROJECT_ROOT = _project_root()


def _env(name: str, fallback: str) -> str:
    value = os.environ.get(name)
    return value if value and value.strip() else fallback


def dedupe(values: Iterable[str]) -> list[str]:
    """去重且保持顺序。"""
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


# ---------------- 教材 / 章节 / 文件命名 ----------------
def sanitize_filename(name: Any, fallback: str = "未命名") -> str:
    """把任意文本变成安全的文件名片段（去掉 Windows 非法字符）。"""
    text = _ILLEGAL_FILENAME_RE.sub("_", str(name or "").strip())
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"_+", "_", text).strip("._ ")
    return text or fallback


def _cn_number(text: str) -> int:
    """中文数字转整数，支持 1~99（十一、二十、三十五…）。"""
    if not text:
        return 0
    if "十" in text:
        left, _, right = text.partition("十")
        tens = _CN_DIGITS.get(left, 1) if left else 1
        ones = _CN_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    if len(text) == 1:
        return _CN_DIGITS.get(text, 0)
    return 0


def parse_chapter_no(chapter: Any) -> int:
    """从“第二章 药物代谢动力学” / “第12章 xxx” 里解析章号，失败返回 0。"""
    match = _CHAPTER_PREFIX_RE.match(str(chapter or ""))
    if not match:
        return 0
    raw = match.group(1)
    if raw.isdigit():
        return int(raw)
    return _cn_number(raw)


def strip_chapter_prefix(chapter: Any) -> str:
    """去掉“第X章”前缀，只留章节名称。"""
    return _CHAPTER_PREFIX_RE.sub("", str(chapter or "")).strip()


def effective_chapter_no(chapter_no: Any, chapter: Any) -> int:
    """优先用手填章号，否则从章节名解析。"""
    try:
        value = int(chapter_no or 0)
    except (TypeError, ValueError):
        value = 0
    return value if value > 0 else parse_chapter_no(chapter)


def chapter_slug(textbook: Any, chapter: Any, chapter_no: Any = 0) -> str:
    """生成文件主名，例如 药理学 + 第二章 药物代谢动力学 -> 药理学_02_药物代谢动力学。"""
    book = sanitize_filename(textbook, "未命名教材")
    name = sanitize_filename(strip_chapter_prefix(chapter) or chapter, "未命名章节")
    number = effective_chapter_no(chapter_no, chapter)
    if number > 0:
        return f"{book}_{number:02d}_{name}"
    return f"{book}_{name}"


@dataclass
class OutputPaths:
    """一次运行的输出路径集合。"""

    jsonl: str
    markdown: str | None
    index: str | None
    directory: str
    base_name: str

    def as_dict(self) -> dict[str, str | None]:
        return {"jsonl": self.jsonl, "markdown": self.markdown, "index": self.index}


def resolve_output_paths(config: "AppConfig") -> OutputPaths:
    """决定 .jsonl / .md / index.json 的位置。

    output_prefix 非空时作为显式前缀（兼容旧用法）；
    否则用 output_dir + 教材/章节名（缺省时退回 题目_起-止）。
    """
    if config.output_prefix:
        prefix = config.output_prefix
        directory = os.path.dirname(prefix) or "."
        base_name = os.path.basename(prefix)
    else:
        directory = config.output_dir or "."
        if config.textbook or config.chapter:
            base_name = chapter_slug(config.textbook, config.chapter, config.chapter_no)
        else:
            base_name = f"题目_{config.page_from}-{config.page_to}"
        prefix = os.path.join(directory, base_name)
    return OutputPaths(
        jsonl=prefix + ".jsonl",
        markdown=(prefix + ".md") if config.generate_markdown else None,
        index=os.path.join(directory, INDEX_FILENAME) if config.generate_index else None,
        directory=directory,
        base_name=base_name,
    )


_SIZE_TEXT_RE = re.compile(r"(\d+)\s*[xX×*]\s*(\d+)")


def parse_size(text: Any) -> tuple[int, int] | None:
    """解析 "1080x2340" / "1080*2340" / "1080×2340" 这类分辨率文本。"""
    if not text:
        return None
    match = _SIZE_TEXT_RE.search(str(text))
    if not match:
        return None
    width, height = int(match.group(1)), int(match.group(2))
    if width <= 0 or height <= 0:
        return None
    return width, height


@dataclass
class SwipePlan:
    """一次滑动的实际坐标（可能已按分辨率缩放）。"""

    start_x: int
    start_y: int
    end_x: int
    end_y: int
    duration_ms: int
    reference: tuple[int, int] | None = None
    actual: tuple[int, int] | None = None
    scaled: bool = False

    def describe(self) -> str:
        text = f"({self.start_x},{self.start_y}) -> ({self.end_x},{self.end_y})，{self.duration_ms}ms"
        if self.scaled and self.reference and self.actual:
            text += (
                f"（基准 {self.reference[0]}x{self.reference[1]}"
                f" 缩放至 {self.actual[0]}x{self.actual[1]}）"
            )
        return text


@dataclass
class AppConfig:
    """一次运行所需的全部参数。"""

    # 任务范围
    page_from: int = 1
    page_to: int = 1

    # 教材与章节（用于元数据与文件命名）
    textbook: str = ""
    chapter: str = ""
    chapter_no: int = 0
    chapter_total: int = 0

    # 输出
    output_dir: str = ""
    output_prefix: str = ""
    generate_markdown: bool = True
    generate_index: bool = True

    # 预检
    preflight: bool = True
    force_start: bool = False
    # 开始前先自动测试模型连通性（防止平台下架模型导致整批跑挂）
    check_model: bool = True

    # 模型
    model: str = field(default_factory=lambda: _env("DRPILOT_MODEL", DEFAULT_MODEL))
    base_url: str = field(default_factory=lambda: _env("DRPILOT_BASE_URL", DEFAULT_BASE_URL))
    batch_size: int = 3
    workers: int = 2
    wait: float = 1.0
    request_timeout: float = 60.0
    # 单次 AI 调用的总时间预算（含重试），0 = 自动（请求超时 x2 + 10s）
    request_deadline: float = 0.0
    jpeg_quality: int = 65
    # 单次 AI 回复的最大 token 数（模块多、正文长时调大；避免 JSON 被截断）
    max_tokens: int = 4096

    # 重试
    max_retries: int = 5
    retry_delay: float = 1.0
    retry_backoff: float = 2.0

    # 滑动（固定坐标，默认适配当前机型）
    swipe_start_x: int = DEFAULT_SWIPE_START_X
    swipe_start_y: int = DEFAULT_SWIPE_START_Y
    swipe_end_x: int = DEFAULT_SWIPE_END_X
    swipe_end_y: int = DEFAULT_SWIPE_END_Y
    swipe_duration_ms: int = DEFAULT_SWIPE_DURATION_MS
    max_swipe_retries: int = 5
    retry_wait: float = 1.2
    # 滑动坐标的基准分辨率；为 0 表示首次运行自动记录当前设备分辨率
    swipe_reference_width: int = 0
    swipe_reference_height: int = 0

    # 翻页动作（GUI 录制得到，tap/swipe/path/key/wait 步骤列表）；
    # 为空时回退到上面的固定滑动坐标，保持旧配置行为不变
    next_action: list[dict[str, Any]] = field(default_factory=list)

    # 用哪一种翻页方式（GUI 的「翻页动作」窗口里三选一）：
    #   auto   = 录制动作 > 手动点按 > 滑动坐标（旧行为，CLI 默认）
    #   tap    = 只用下面的手动点按坐标
    #   swipe  = 只用 swipe_* 滑动坐标
    #   record = 只用 next_action 里录制的动作
    page_action: str = "auto"
    tap_x: int = 0
    tap_y: int = 0
    tap_ms: int = 80

    # 提取模块：除题干/选项/答案之外，用户还想提取哪些内容块
    # （每项是 ModuleSpec 的纯数据形式，见 modules.py）
    modules: list[dict[str, Any]] = field(default_factory=list)

    # 题目本体字段写不写进 JSONL / Markdown（每项 {"key","in_json","in_markdown"}，
    # 见 modules.BASE_FIELDS）；删掉某项就是不写进文件，识别与翻页照旧
    output_fields: list[dict[str, Any]] = field(default_factory=default_output_fields)

    # 加长主屏：把逻辑屏高度临时放大，一屏装下考点还原/标准解析等首屏之外的内容。
    # off = 不改手机显示设置（默认）；wide = 运行期间临时加长，结束/异常都会复位。
    display_mode: str = "off"
    display_scale: float = 2.0     # 逻辑高 = 物理高 × scale（实测 >2 会被系统静默忽略）
    display_width_scale: float = 1.0  # 逻辑宽 = 物理宽 × width_scale（1.0 = 不加宽）
    display_density: int = 0       # 0 = 不改密度（保住字形大小）；420 可用但字形会缩小
    display_scaling: str = "auto"  # off = 物理屏 1:1 裁剪（不变形），不影响截图内容

    # 设备
    serial: str | None = None
    adb_path: str | None = None
    # 手机无线调试地址（IP:端口）；运行前会先 adb connect
    device_address: str = ""

    # 运行时注入，不落盘
    api_keys: list[str] = field(default_factory=list)

    # ---- 构造与转换 ----
    @classmethod
    def field_names(cls) -> set[str]:
        return {f.name for f in fields(cls)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "AppConfig":
        known = cls.field_names()
        kwargs: dict[str, Any] = {}
        for key, value in (data or {}).items():
            if key in known and value is not None:
                kwargs[key] = value
        cfg = cls(**kwargs)
        cfg.normalize()
        return cfg

    def to_dict(self, include_api_keys: bool = False) -> dict[str, Any]:
        data = asdict(self)
        if not include_api_keys:
            data.pop("api_keys", None)
        return data

    def replace(self, overrides: Mapping[str, Any] | None) -> "AppConfig":
        """在现有配置基础上覆盖部分字段，返回新配置。"""
        merged = self.to_dict()
        for key, value in (overrides or {}).items():
            if key in self.field_names() and value is not None:
                merged[key] = value
        cfg = AppConfig.from_dict(merged)
        cfg.api_keys = list(self.api_keys)
        return cfg

    # ---- 规整与校验 ----
    def normalize(self) -> "AppConfig":
        self.page_from = _to_int(self.page_from, 1)
        self.page_to = _to_int(self.page_to, 1)
        self.chapter_no = max(0, _to_int(self.chapter_no, 0))
        self.chapter_total = max(0, _to_int(self.chapter_total, 0))
        self.batch_size = max(1, _to_int(self.batch_size, 1))
        self.workers = max(1, _to_int(self.workers, 1))
        self.wait = max(0.0, _to_float(self.wait, 0.0))
        self.request_timeout = max(1.0, _to_float(self.request_timeout, 60.0))
        self.request_deadline = max(0.0, _to_float(self.request_deadline, 0.0))
        self.jpeg_quality = min(100, max(1, _to_int(self.jpeg_quality, 65)))
        self.max_tokens = max(256, _to_int(self.max_tokens, 4096))
        self.max_retries = max(1, _to_int(self.max_retries, 1))
        self.retry_delay = max(0.0, _to_float(self.retry_delay, 0.0))
        self.retry_backoff = max(1.0, _to_float(self.retry_backoff, 1.0))
        self.max_swipe_retries = max(0, _to_int(self.max_swipe_retries, 0))
        self.retry_wait = max(0.0, _to_float(self.retry_wait, 0.0))
        self.swipe_duration_ms = max(1, _to_int(self.swipe_duration_ms, 1))
        self.swipe_start_x = _to_int(self.swipe_start_x, 0)
        self.swipe_start_y = _to_int(self.swipe_start_y, 0)
        self.swipe_end_x = _to_int(self.swipe_end_x, 0)
        self.swipe_end_y = _to_int(self.swipe_end_y, 0)
        self.swipe_reference_width = max(0, _to_int(self.swipe_reference_width, 0))
        self.swipe_reference_height = max(0, _to_int(self.swipe_reference_height, 0))
        self.next_action = steps_to_payload(normalize_steps(self.next_action))
        self.page_action = str(self.page_action or "auto").strip().lower()
        if self.page_action not in PAGE_ACTIONS:
            self.page_action = "auto"
        self.tap_x = max(0, _to_int(self.tap_x, 0))
        self.tap_y = max(0, _to_int(self.tap_y, 0))
        self.tap_ms = max(0, _to_int(self.tap_ms, 80))
        self.modules = modules_to_payload(normalize_modules(self.modules, strict=False))
        self.output_fields = normalize_output_fields(self.output_fields)
        self.display_mode = str(self.display_mode or "off").strip().lower()
        if self.display_mode not in ("off", "wide"):
            self.display_mode = "off"
        self.display_scale = max(1.0, min(4.0, _to_float(self.display_scale, 2.0)))
        self.display_width_scale = max(
            1.0, min(2.5, _to_float(self.display_width_scale, 1.0))
        )
        self.display_density = max(0, _to_int(self.display_density, 0))
        self.display_scaling = str(self.display_scaling or "auto").strip().lower()
        if self.display_scaling not in ("auto", "off"):
            self.display_scaling = "auto"
        self.textbook = str(self.textbook or "").strip()
        self.chapter = str(self.chapter or "").strip()
        self.output_dir = str(self.output_dir or "").strip()
        self.output_prefix = str(self.output_prefix or "").strip()
        self.model = str(self.model or "").strip()
        self.base_url = str(self.base_url or "").strip().rstrip("/")
        self.serial = (str(self.serial).strip() or None) if self.serial is not None else None
        self.adb_path = (str(self.adb_path).strip() or None) if self.adb_path is not None else None
        self.device_address = str(self.device_address or "").strip()
        self.check_model = bool(self.check_model)
        self.api_keys = dedupe([str(k).strip() for k in self.api_keys if k and str(k).strip()])
        return self

    def validate(self, require_keys: bool = True) -> None:
        errors: list[str] = []
        if self.page_from < 1:
            errors.append("起始题号必须 >= 1")
        if self.page_to < self.page_from:
            errors.append("结束题号必须 >= 起始题号")
        if not self.output_prefix and not self.output_dir:
            errors.append("请指定输出目录（或输出文件前缀）")
        if not self.model:
            errors.append("模型 ID 不能为空")
        if not self.base_url:
            errors.append("API 地址不能为空")
        if self.batch_size < 1:
            errors.append("批量大小必须 >= 1")
        if self.workers < 1:
            errors.append("并发数必须 >= 1")
        if require_keys and not self.api_keys:
            errors.append(
                "未找到 API Key，请在 .env 中配置 SILICONFLOW_API_KEY_1 / _2 …"
            )
        try:
            normalize_modules(self.modules, strict=True)
        except ConfigError as exc:
            errors.append(f"提取模块配置有误：{exc}")
        if self.display_mode == "wide" and not self.uses_display_override:
            # 倍数 1.0 是允许的（只改密度 / 只加宽），但总得改点什么，否则这次设置没有意义
            errors.append(
                "显示设置要么把倍数设成 > 1（例如 --display-scale 2.0）、"
                "要么加宽（--display-width-scale 1.2）、"
                "要么设一个密度（例如 --display-density 420，只把字变小）"
            )
        if errors:
            raise ConfigError("；".join(errors))

    @property
    def modules_specs(self) -> list[ModuleSpec]:
        """提取模块清单（归一化后的对象形式）。"""
        return normalize_modules(self.modules, strict=False)

    @property
    def output_field_map(self) -> dict[str, dict[str, Any]]:
        """本体字段 -> {in_json, in_markdown}（见 modules.BASE_FIELDS）。"""
        return output_field_map(self.output_fields)

    @property
    def uses_display_override(self) -> bool:
        """这次运行是否需要临时改手机显示设置（加长 / 加宽 / 只把密度调小）。"""
        if self.display_mode != "wide":
            return False
        return (
            self.display_scale > 1
            or self.display_width_scale > 1.0
            or self.display_density > 0
        )

    @property
    def effective_request_deadline(self) -> float:
        """单次 AI 调用的硬超时（含重试）；0 表示按请求超时自动推算。"""
        if self.request_deadline > 0:
            return float(self.request_deadline)
        return float(self.request_timeout) * 2 + 10.0

    @property
    def total(self) -> int:
        return max(0, self.page_to - self.page_from + 1)

    def metadata(self) -> dict[str, Any]:
        """写入每一题 JSONL 行的教材/章节元数据。"""
        number = effective_chapter_no(self.chapter_no, self.chapter)
        return {
            "textbook": self.textbook,
            "chapter": self.chapter,
            "chapter_no": number if number > 0 else None,
            "total": self.chapter_total if self.chapter_total > 0 else None,
        }


def build_swipe_plan(config: "AppConfig", actual_size: tuple[int, int] | None = None) -> SwipePlan:
    """按基准分辨率把滑动坐标换算到实际设备分辨率。

    * 基准为 0（首次运行）：不做缩放，由 pipeline 记录当前分辨率作为基准。
    * 基准与实际相同：原样使用，行为与固定坐标完全一致。
    * 基准与实际不同：x 按宽度、y 按高度分别等比缩放，并夹到屏幕范围内。
    """
    start_x = int(config.swipe_start_x)
    start_y = int(config.swipe_start_y)
    end_x = int(config.swipe_end_x)
    end_y = int(config.swipe_end_y)
    duration_ms = max(1, int(config.swipe_duration_ms))

    base_w = max(0, int(config.swipe_reference_width or 0))
    base_h = max(0, int(config.swipe_reference_height or 0))
    reference: tuple[int, int] | None = None
    actual: tuple[int, int] | None = None
    scaled = False

    if actual_size and base_w > 0 and base_h > 0:
        actual_w, actual_h = int(actual_size[0]), int(actual_size[1])
        if actual_w > 0 and actual_h > 0:
            reference = (base_w, base_h)
            actual = (actual_w, actual_h)
            if actual_w != base_w or actual_h != base_h:
                scale_x = actual_w / base_w
                scale_y = actual_h / base_h
                start_x = round(start_x * scale_x)
                end_x = round(end_x * scale_x)
                start_y = round(start_y * scale_y)
                end_y = round(end_y * scale_y)
                scaled = True
            start_x = min(max(start_x, 0), actual_w - 1)
            end_x = min(max(end_x, 0), actual_w - 1)
            start_y = min(max(start_y, 0), actual_h - 1)
            end_y = min(max(end_y, 0), actual_h - 1)

    return SwipePlan(
        start_x=start_x,
        start_y=start_y,
        end_x=end_x,
        end_y=end_y,
        duration_ms=duration_ms,
        reference=reference,
        actual=actual,
        scaled=scaled,
    )


def _to_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _to_float(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


# ---------------- .env 加载 ----------------
def load_env_file(path: str | os.PathLike[str] | None) -> bool:
    """加载 .env。优先用 python-dotenv，缺失时用内置简易解析。"""
    if not path:
        return False
    env_path = Path(path)
    if not env_path.is_file():
        return False
    try:
        from dotenv import load_dotenv  # 延迟导入，缺少依赖也不影响
    except ImportError:
        return _parse_env_file(env_path)
    try:
        load_dotenv(str(env_path), override=False)
        return True
    except Exception:
        return _parse_env_file(env_path)


def _parse_env_file(path: Path) -> bool:
    """极简 .env 解析：KEY=VALUE，支持 # 注释和成对引号。"""
    loaded = False
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                if key and key not in os.environ:
                    os.environ[key] = value
                    loaded = True
    except OSError:
        return False
    return loaded


def load_dotenv_candidates(extra: Iterable[str | os.PathLike[str]] = ()) -> list[str]:
    """按顺序加载候选 .env，返回实际加载的路径。"""
    candidates: list[Path] = []
    for item in extra:
        candidates.append(Path(item))
    candidates.append(Path.cwd() / ".env")
    candidates.append(PROJECT_ROOT / ".env")
    loaded: list[str] = []
    for path in dedupe([str(p) for p in candidates]):
        if load_env_file(path):
            loaded.append(path)
    return loaded


# ---------------- API Key ----------------
_KEY_SPLIT_RE = re.compile(r"[,\s;，；]+")


def load_api_keys(env: Mapping[str, str] | None = None) -> list[str]:
    """从环境变量收集 API Key。

    支持：
        SILICONFLOW_API_KEY_1 / _2 / …
        DRPILOT_API_KEY_1 / _2 / …
        SILICONFLOW_API_KEYS / DRPILOT_API_KEYS（逗号或空格分隔）
        SILICONFLOW_API_KEY / DRPILOT_API_KEY（单个）
    """
    source = env if env is not None else os.environ
    keys: list[str] = []
    for name in ("SILICONFLOW_API_KEYS", "DRPILOT_API_KEYS"):
        raw = source.get(name)
        if raw:
            keys.extend(part for part in _KEY_SPLIT_RE.split(raw) if part)
    for index in range(1, MAX_API_KEYS + 1):
        for name in (f"SILICONFLOW_API_KEY_{index}", f"DRPILOT_API_KEY_{index}"):
            value = source.get(name)
            if value:
                keys.append(value)
    for name in ("SILICONFLOW_API_KEY", "DRPILOT_API_KEY"):
        value = source.get(name)
        if value:
            keys.append(value)
    return dedupe([k.strip() for k in keys if k and k.strip()])


# ---------------- 配置文件 ----------------
def default_config_path() -> Path:
    return PROJECT_ROOT / CONFIG_FILENAME


def legacy_config_path() -> Path:
    return PROJECT_ROOT / LEGACY_CONFIG_FILENAME


def load_config_file(path: str | os.PathLike[str]) -> dict[str, Any]:
    """读取配置文件；不存在时返回空 dict。"""
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        data = read_json_file(p)
    except Exception as exc:  # 配置文件损坏不应导致崩溃
        raise ConfigError(f"读取配置文件失败：{p}（{exc}）") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件内容必须是 JSON 对象：{p}")
    return data


def save_config_file(path: str | os.PathLike[str], config: AppConfig) -> str:
    """保存配置（不含 API Key）。"""
    import json

    text = json.dumps(config.to_dict(), ensure_ascii=False, indent=2) + "\n"
    return atomic_write_text(path, text)


def load_config_with_fallback(path: str | os.PathLike[str] | None = None) -> tuple[AppConfig, str | None]:
    """读取指定配置；未指定时依次尝试默认与旧版配置。"""
    candidates: list[Path] = []
    if path:
        candidates.append(Path(path))
    else:
        candidates.append(default_config_path())
        candidates.append(legacy_config_path())
    for candidate in candidates:
        if candidate.is_file():
            return _migrate_defaults(AppConfig.from_dict(load_config_file(candidate))), str(candidate)
    return AppConfig(), None


def _migrate_defaults(config: AppConfig) -> AppConfig:
    """把配置里「从没改过的老默认值」跟着新版默认走。"""
    if config.model == LEGACY_DEFAULT_MODEL:
        config.model = DEFAULT_MODEL
    if config.base_url == LEGACY_DEFAULT_BASE_URL:
        config.base_url = DEFAULT_BASE_URL
    return config
