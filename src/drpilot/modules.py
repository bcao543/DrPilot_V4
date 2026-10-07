# -*- coding: utf-8 -*-
"""提取模块：让用户自定义「要提取哪些内容块」。

背景：不同刷题 App 的页面结构不一样 —— 有的有「考点还原」「标准解析」，
有的叫「总结分析」「答案解析」。程序固定提取题干/选项/答案三块，
这里把「额外还要提取什么」变成可配置清单：

    * 模块名同时是 JSONL 的键与 Markdown 的小标题（想要英文键就起英文名）；
    * below_fold 表示「通常要下滑或加长屏幕才截得到」，供加长屏/拼接决定是否启用；
    * aliases 是模型可能写成的别名，解析时一并认；
    * hint 是给模型的定位说明（命中内置预设时自动带出）。

只依赖标准库，纯函数，方便单测。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Sequence

from .errors import ConfigError

# 这些键是 JSONL 里已经固定使用的，模块名不能撞上去，否则会覆盖题目本体
RESERVED_KEYS = frozenset(
    {
        "id",
        "textbook",
        "chapter",
        "chapter_no",
        "total",
        "type",
        "stem",
        "options",
        "answer",
        "screen_id",
        "screen_total",
        "modules",
    }
)

# 模块名分隔符：中英文逗号、顿号、分号、竖线（**不拆空格** —— 英文模块名里可能带空格）
_SPLIT_CHARS = str.maketrans({",": "\n", "，": "\n", "、": "\n", ";": "\n", "；": "\n", "|": "\n"})

MAX_MODULES = 12
MAX_NAME_LENGTH = 24


@dataclass
class ModuleSpec:
    """一个要提取的模块。"""

    name: str
    aliases: list[str] = field(default_factory=list)
    # 是否通常需要下滑 / 加长屏幕才截得到（决定 capture 策略）
    below_fold: bool = True
    # 缺了这个模块是否要在日志 / diagnostics 里点名
    required: bool = False
    # 是否写进 Markdown
    in_markdown: bool = True
    # 是否写进 JSONL（关掉 = 只进 Markdown，或两边都不要）
    in_json: bool = True
    # 给模型的定位说明
    hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "aliases": list(self.aliases),
            "below_fold": bool(self.below_fold),
            "required": bool(self.required),
            "in_markdown": bool(self.in_markdown),
            "in_json": bool(self.in_json),
            "hint": self.hint,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ModuleSpec":
        if isinstance(data, ModuleSpec):
            return data
        if isinstance(data, str):
            return cls(name=data.strip())
        if not isinstance(data, Mapping):
            raise ConfigError(f"模块定义必须是对象或字符串：{data!r}")
        raw_aliases = data.get("aliases") or data.get("alias") or []
        if isinstance(raw_aliases, str):
            aliases = [item.strip() for item in raw_aliases.replace("，", ",").split(",")]
        elif isinstance(raw_aliases, Sequence):
            aliases = [str(item).strip() for item in raw_aliases]
        else:
            aliases = []
        return cls(
            name=str(data.get("name") or data.get("label") or "").strip(),
            aliases=[item for item in aliases if item],
            below_fold=bool(data.get("below_fold", True)),
            required=bool(data.get("required", False)),
            in_markdown=bool(data.get("in_markdown", data.get("markdown", True))),
            in_json=bool(data.get("in_json", data.get("json", True))),
            hint=str(data.get("hint") or "").strip(),
        )

    def matches(self, key: Any) -> bool:
        """AI 返回的键是否属于本模块（名字或别名，ASCII 忽略大小写）。"""
        text = str(key or "").strip()
        if not text:
            return False
        candidates = {self.name, *self.aliases}
        lowered = {item.lower() for item in candidates}
        return text in candidates or text.lower() in lowered

    def describe(self) -> str:
        parts = [self.name]
        if self.aliases:
            parts.append("（别名：" + "、".join(self.aliases) + "）")
        if not self.below_fold:
            parts.append("[首屏内]")
        if self.required:
            parts.append("[必读]")
        return "".join(parts)


# ---------------- 题目本体字段（题号 / 题目 / 答案） ----------------
# 这三个字段由提示词固定提取（AI 始终要 id / stem / options / answer），
# output_fields 只决定「写不写进 JSONL / Markdown」，不影响识别与翻页。


@dataclass(frozen=True)
class BaseField:
    """一个题目本体字段：界面上是一行，对应 JSONL 里的固定键。"""

    key: str                      # id / stem / answer
    name: str                     # 界面与文档里的中文名
    json_keys: tuple[str, ...]    # 写进 JSONL 的键
    note: str


BASE_FIELDS: tuple[BaseField, ...] = (
    BaseField(
        key="id",
        name="题号",
        json_keys=("id",),
        note="截图右上角的题号（JSON 键 id），也是按题号合并写回时用的键",
    ),
    BaseField(
        key="stem",
        name="题目",
        json_keys=("type", "stem", "options"),
        note="题干与选项（JSON 键 type / stem / options）",
    ),
    BaseField(
        key="answer",
        name="答案",
        json_keys=("answer",),
        note="正确选项字母（JSON 键 answer）",
    ),
)

BASE_FIELD_KEYS: tuple[str, ...] = tuple(item.key for item in BASE_FIELDS)


def _base_field(key: Any) -> BaseField | None:
    """按 key（id/stem/answer）或中文名（题号/题目/答案）找本体字段。"""
    text = str(key or "").strip()
    if not text:
        return None
    for item in BASE_FIELDS:
        if item.key == text.lower() or item.name == text:
            return item
    return None


def default_output_fields() -> list[dict[str, Any]]:
    """默认输出字段：题号 / 题目 / 答案 都写进 JSONL 与 Markdown。"""
    return [{"key": item.key, "in_json": True, "in_markdown": True} for item in BASE_FIELDS]


def normalize_output_fields(raw: Any) -> list[dict[str, Any]]:
    """归一化输出字段清单：只认内置键、按内置顺序排、去掉重复。

    None / 空串 = 「没配过」，回落到默认三件套；显式的空列表是合法值
    （用户把三个本体字段都删了）。顺序固定为题号 → 题目 → 答案，
    因为 JSONL 的键顺序与 Markdown 的版式都是结构化的。
    """
    if raw is None:
        return default_output_fields()
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return default_output_fields()
        try:
            raw = json.loads(text)
        except ValueError:
            return default_output_fields()
    if isinstance(raw, Mapping):
        raw = raw.get("output_fields") or raw.get("fields") or []
    if not isinstance(raw, Sequence) or isinstance(raw, (bytes, bytearray)):
        return default_output_fields()
    seen: dict[str, dict[str, Any]] = {}
    for item in raw:
        if isinstance(item, str):
            key, in_json, in_markdown = item, True, True
        elif isinstance(item, Mapping):
            key = item.get("key", item.get("name", ""))
            in_json = bool(item.get("in_json", item.get("json", True)))
            in_markdown = bool(item.get("in_markdown", item.get("markdown", True)))
        else:
            continue
        base = _base_field(key)
        if base is None or base.key in seen:
            continue
        seen[base.key] = {
            "key": base.key,
            "in_json": in_json,
            "in_markdown": in_markdown,
        }
    return [seen[item.key] for item in BASE_FIELDS if item.key in seen]


def output_field_map(raw: Any) -> dict[str, dict[str, Any]]:
    """key -> {in_json, in_markdown}；只含保留下来的字段。"""
    return {item["key"]: item for item in normalize_output_fields(raw)}


def output_field_enabled(fields: Any, key: str, target: str) -> bool:
    """这个本体字段要不要写进目标文件；target 取 "json" / "markdown"。"""
    mapping = fields if isinstance(fields, Mapping) else output_field_map(fields)
    item = mapping.get(key)
    if item is None:
        return False
    return bool(item.get("in_json" if target == "json" else "in_markdown"))


# 内置模块目录：drpilot modules --json、GUI 快捷添加、CLI 命中预设时都用它。
# below_fold 的取值来自真机实测（医考帮：题干/选项/答案在首屏，其余在下方）。
PRESETS: tuple[ModuleSpec, ...] = (
    ModuleSpec(
        name="标准解析",
        aliases=["答案解析", "解析", "试题解析"],
        below_fold=True,
        hint="「标准解析」小节的正文",
    ),
    ModuleSpec(
        name="考点还原",
        aliases=["考点", "考点回顾"],
        below_fold=True,
        hint="「考点还原」小节的正文（教材原文引用与逐项对错说明）",
    ),
    ModuleSpec(name="总结分析", aliases=["总结", "分析"], below_fold=True, hint="「总结分析」小节的正文"),
    ModuleSpec(name="技巧点拨", aliases=["解题技巧", "技巧"], below_fold=True, hint="「技巧点拨」小节的正文"),
    ModuleSpec(name="难度", aliases=["难度星级"], below_fold=False, hint="题干下方的难度星级（如「难度：★★★★」）"),
    ModuleSpec(name="统计", aliases=["作答统计"], below_fold=False, hint="「统计」一行的原文"),
    ModuleSpec(name="标签", aliases=["题目标签"], below_fold=False, hint="「标签」栏里的标签与票数"),
    ModuleSpec(name="来源", aliases=["题目来源"], below_fold=False, hint="「来源」一行的原文"),
    ModuleSpec(name="笔记", aliases=["我的笔记"], below_fold=False, hint="用户自己的笔记内容"),
    ModuleSpec(name="评论", aliases=["精彩评论"], below_fold=False, hint="精选评论内容"),
    ModuleSpec(name="纠错", aliases=["纠错内容"], below_fold=False, hint="「纠错」里用户指出的问题"),
)


def preset_for(name: Any) -> ModuleSpec | None:
    """按名字或别名找内置预设。"""
    text = str(name or "").strip()
    if not text:
        return None
    for spec in PRESETS:
        if spec.matches(text):
            return spec
    return None


def split_module_names(text: Any) -> list[str]:
    """把 "考点还原,标准解析" 拆成名字清单。"""
    raw = str(text or "").translate(_SPLIT_CHARS)
    return [item.strip() for item in raw.split("\n") if item.strip()]


def _spec_from_name(name: str, *, use_presets: bool = True) -> ModuleSpec:
    """按名字造模块；命中内置预设就带上别名/定位说明/首屏属性。"""
    preset = preset_for(name) if use_presets else None
    if preset is None:
        return ModuleSpec(name=name, below_fold=True)
    if preset.name == name:
        return replace(preset, aliases=list(preset.aliases))
    # 用户写的是别名：保留用户写的名字，但继承预设的元信息。
    # 别名里要带上预设的规范名 —— 页面小标题写的往往是「标准解析」，
    # 而用户把模块起名成「解析」，两者都要能认出来。
    return ModuleSpec(
        name=name,
        aliases=[preset.name, *[item for item in preset.aliases if item != name]],
        below_fold=preset.below_fold,
        required=preset.required,
        in_markdown=preset.in_markdown,
        hint=preset.hint,
    )


def parse_modules_text(text: Any, *, use_presets: bool = True) -> list[ModuleSpec]:
    """CLI 的 --modules 文本 -> 模块清单。"""
    return [_spec_from_name(name, use_presets=use_presets) for name in split_module_names(text)]


def normalize_modules(
    raw: Any,
    *,
    strict: bool = False,
    use_presets: bool = True,
    limit: int = MAX_MODULES,
) -> list[ModuleSpec]:
    """把配置 / 前端 JSON / 文本归一成模块清单。

    strict=False（默认）：能修就修（去空名、去重、截断）；
    strict=True：遇到冲突直接抛 ConfigError，给用户看清楚哪里写错了。
    """
    if raw in (None, "", []):
        return []
    if isinstance(raw, str):
        text = raw.strip()
        # 前端 / 配置文件里可能是 JSON 字符串（hidden input 就是这么传的）
        if text[:1] in ("[", "{"):
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
            if parsed is not None:
                return normalize_modules(
                    parsed, strict=strict, use_presets=use_presets, limit=limit
                )
        items: list[Any] = split_module_names(raw)
    elif isinstance(raw, Mapping):
        items = list(raw.get("modules") or [])
    elif isinstance(raw, Sequence):
        items = list(raw)
    else:
        raise ConfigError(f"模块清单格式不对：{raw!r}")

    specs: list[ModuleSpec] = []
    seen: dict[str, str] = {}          # lowercase -> 已占用的名字/别名
    for item in items:
        try:
            spec = ModuleSpec.from_dict(item)
        except ConfigError:
            if strict:
                raise
            continue
        if isinstance(item, str) and use_presets:
            spec = _spec_from_name(spec.name, use_presets=True)
        elif use_presets and preset_for(spec.name) and not spec.hint:
            preset = preset_for(spec.name)
            assert preset is not None
            merged = [alias for alias in preset.aliases if alias != spec.name]
            spec = replace(spec, aliases=list(dict.fromkeys([*spec.aliases, *merged])), hint=preset.hint)
        name = spec.name.strip()
        if not name:
            if strict:
                raise ConfigError("模块名不能为空（--modules 里可能有多余的分隔符）")
            continue
        if len(name) > MAX_NAME_LENGTH:
            if strict:
                raise ConfigError(f"模块名太长（≤{MAX_NAME_LENGTH} 字）：{name}")
            name = name[:MAX_NAME_LENGTH]
        if name in RESERVED_KEYS or name.lower() in RESERVED_KEYS:
            message = f"模块名「{name}」与题目固定字段重名，换一个名字（如「{name}正文」）"
            if strict:
                raise ConfigError(message)
            continue
        keys = [name, *spec.aliases]
        clash = next((key for key in keys if key.lower() in seen), "")
        if clash:
            if strict:
                raise ConfigError(f"模块名或别名重复：「{clash}」（已被「{seen[clash.lower()]}」占用）")
            continue
        for key in keys:
            seen[key.lower()] = name
        specs.append(replace(spec, name=name))
        if len(specs) >= limit:
            break
    return specs


def modules_to_payload(specs: Iterable[ModuleSpec]) -> list[dict[str, Any]]:
    """模块清单 -> 可写进 drpilot_config.json 的纯数据。"""
    return [spec.to_dict() for spec in specs]


def module_names(specs: Sequence[ModuleSpec]) -> list[str]:
    return [spec.name for spec in specs]


def has_below_fold(specs: Sequence[ModuleSpec]) -> bool:
    """是否有模块通常需要下滑/加长屏幕才截得到。"""
    return any(spec.below_fold for spec in specs)


def match_module_name(specs: Sequence[ModuleSpec], key: Any) -> str | None:
    """AI 返回的键 -> 规范模块名；认不出来返回 None。"""
    for spec in specs:
        if spec.matches(key):
            return spec.name
    return None


def modules_summary(specs: Sequence[ModuleSpec]) -> str:
    """一行中文说明，用于日志与 JSON。"""
    if not specs:
        return "未配置额外模块（只提取题干/选项/答案）"
    names = "、".join(module_names(specs))
    below = "，其中需要加长屏幕/下滑：" + "、".join(spec.name for spec in specs if spec.below_fold) \
        if has_below_fold(specs) else ""
    return f"{len(specs)} 个模块：{names}{below}"


def output_fields_summary(raw: Any) -> str:
    """一行中文说明：题目本体字段里，哪些写进 JSONL / Markdown。"""
    items = output_field_map(raw)
    if not items:
        return "题目本体字段全部不写入（只写自定义模块）"
    parts: list[str] = []
    for base in BASE_FIELDS:
        item = items.get(base.key)
        if item is None:
            continue
        targets = []
        if item.get("in_json"):
            targets.append("JSONL")
        if item.get("in_markdown"):
            targets.append("Markdown")
        parts.append(base.name + "→" + ("/".join(targets) if targets else "不写"))
    return "、".join(parts) if parts else "题目本体字段全部不写入（只写自定义模块）"


def build_module_prompt_section(specs: Sequence[ModuleSpec]) -> str:
    """给系统提示词用的模块字段说明；没有模块时返回空串（提示词保持原样）。"""
    if not specs:
        return ""
    lines = [
        "",
        "除了上面这些固定字段，每个元素还要额外交出下列模块（键名必须一字不差地照抄）：",
    ]
    for spec in specs:
        alias = f"（页面上也可能写成：{'、'.join(spec.aliases)}）" if spec.aliases else ""
        where = spec.hint or f"页面上「{spec.name}」小节的内容"
        lines.append(f'  - "{spec.name}"：字符串，{where}{alias}。')
    lines.extend(
        [
            "模块补充规则（必须遵守）：",
            "1. 截图可能是「加长屏幕」一次截下的整页：题干/选项/答案在上半部分，解析类模块通常在中下部；",
            "2. 截图里没有的模块一律填空字符串 \"\"，严禁编造、严禁用相似小节的文字顶替；",
            "3. 只输出上面列出的模块键，不要自行增加字段；模块的值必须是字符串。",
        ]
    )
    return "\n".join(lines)


__all__ = [
    "BASE_FIELDS",
    "BASE_FIELD_KEYS",
    "MAX_MODULES",
    "PRESETS",
    "RESERVED_KEYS",
    "BaseField",
    "ModuleSpec",
    "build_module_prompt_section",
    "default_output_fields",
    "has_below_fold",
    "match_module_name",
    "module_names",
    "modules_summary",
    "modules_to_payload",
    "normalize_modules",
    "normalize_output_fields",
    "output_field_enabled",
    "output_fields_summary",
    "output_field_map",
    "parse_modules_text",
    "preset_for",
    "split_module_names",
]
