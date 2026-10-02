# -*- coding: utf-8 -*-
"""API Key 的收集、脱敏与安全落盘。

设计约定（与 config.py 的分工）：
    * config.load_api_keys()：只负责「从环境变量收集」，不碰文件；
    * 本模块：负责「用户从 GUI/CLI 输入的 Key 如何写进 .env」「如何安全展示」。

安全底线：
    1. Key 只写入项目根目录的 .env（已被 .gitignore 忽略），绝不写进
       drpilot_config.json、源码或日志。
    2. 写入用 fileio.atomic_write_text（临时文件 + 替换），中途崩溃不会留下半截 .env。
    3. 落盘后尝试收紧文件权限到 0600；Windows / NTFS / DrvFs 上不支持时静默跳过。
    4. 对外展示一律走 mask_key()，只有用户主动点「显示」才返回明文。
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Iterable, Mapping

from .config import PROJECT_ROOT, dedupe, load_api_keys
from .errors import ConfigError
from .fileio import atomic_write_text

ENV_FILENAME = ".env"

# 我们写入的块标记：重复保存时要先清掉，否则注释行会一条条堆积
BLOCK_MARKER = "# 由 DrPilot GUI/CLI 写入（本文件已被 .gitignore 忽略，请勿提交）"

# 与 config.load_api_keys() 支持的 Key 变量名保持一致，集中在一处维护
SINGLE_KEY_NAMES = ("SILICONFLOW_API_KEY", "DRPILOT_API_KEY")
PLURAL_KEY_NAMES = ("SILICONFLOW_API_KEYS", "DRPILOT_API_KEYS")
INDEXED_KEY_TEMPLATE = "SILICONFLOW_API_KEY_{index}"
MAX_KEY_INPUT = 16
MIN_KEY_LENGTH = 8

# 归一化：去掉换行/制表符、首尾空白、误粘贴的引号
_NOISE_RE = re.compile(r"[\r\n\t]+")
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\s*=")
_TEXT_SPLIT_RE = re.compile(r"[\n,;，；]+")
NEEDS_QUOTE_RE = re.compile(r"[\s\"'#]")


def normalize_key(raw: object) -> str:
    """把用户输入的一行整理成干净的 Key；无效输入返回空串。"""
    text = "" if raw is None else str(raw).strip()
    if not text:
        return ""
    text = _NOISE_RE.sub("", text).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1].strip()
    if _ASSIGNMENT_RE.match(text):          # 用户整行粘了 "SILICONFLOW_API_KEY_1=sk-xxx"
        text = text.split("=", 1)[1].strip()
    return text


def parse_key_input(text: object) -> list[str]:
    """解析多行 / 逗号 / 分号分隔的 Key 输入，去重并保持顺序。"""
    if text is None:
        return []
    keys: list[str] = []
    for part in _TEXT_SPLIT_RE.split(str(text)):
        key = normalize_key(part)
        if key:
            keys.append(key)
    return dedupe(keys)[:MAX_KEY_INPUT]


def mask_key(key: object, visible_head: int = 3, visible_tail: int = 4) -> str:
    """脱敏展示：只露头尾。例："sk-abcdefgh1234" -> "sk-****1234"。"""
    text = normalize_key(key)
    if not text:
        return ""
    head = max(0, visible_head)
    tail = max(0, visible_tail)
    if len(text) <= head + tail:             # 太短：整体打码，避免几乎全泄露
        return "*" * max(4, len(text))
    return f"{text[:head]}{'*' * (len(text) - head - tail)}{text[-tail:]}"


def mask_keys(keys: Iterable[object]) -> str:
    """多 Key 的整体脱敏摘要，用于界面/日志一行展示。"""
    return " / ".join(mask_key(k) for k in keys if normalize_key(k))


def validate_key(key: object) -> str:
    """校验单个 Key；返回归一化结果，非法时抛 ConfigError。"""
    text = normalize_key(key)
    if not text:
        raise ConfigError("API Key 不能为空")
    if len(text) < MIN_KEY_LENGTH:
        raise ConfigError(f"API Key 太短（至少 {MIN_KEY_LENGTH} 个字符）：{mask_key(text)}")
    if any(ch.isspace() for ch in text):
        raise ConfigError("API Key 不能包含空格")
    return text


def validate_keys(keys: Iterable[object]) -> list[str]:
    """校验一组 Key，返回归一化结果。"""
    result: list[str] = []
    for index, key in enumerate(keys, start=1):
        try:
            result.append(validate_key(key))
        except ConfigError as exc:
            raise ConfigError(f"第 {index} 个 Key 无效：{exc}") from exc
    if not result:
        raise ConfigError("请至少填写 1 个 API Key")
    return dedupe(result)


# ---------------- .env 落盘 ----------------
def env_file_path(path: str | os.PathLike[str] | None = None) -> Path:
    """目标 .env 路径；默认项目根目录下的 .env。"""
    return Path(path) if path else PROJECT_ROOT / ENV_FILENAME


def _read_env_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().splitlines()
    except OSError as exc:
        raise ConfigError(f"读取 {path} 失败：{exc}") from exc


def _render_value(value: str) -> str:
    """需要时给值加引号：值里有空格 / 引号 / # 就必须引起来。"""
    if NEEDS_QUOTE_RE.search(value):
        return '"' + value.replace('"', '\\"') + '"'
    return value


def _key_var_names() -> set[str]:
    indexed = {INDEXED_KEY_TEMPLATE.format(index=i) for i in range(1, MAX_KEY_INPUT + 1)}
    return set(SINGLE_KEY_NAMES) | set(PLURAL_KEY_NAMES) | indexed


def env_file_keys(path: str | os.PathLike[str] | None = None) -> list[str]:
    """只从指定 .env 文件读取 Key，顺序与 load_api_keys() 完全一致。

    GUI 的「已保存 Key」列表和「删除第 N 个」都以这个顺序为准，
    因此列表下标、.env 行号、运行时 Key 顺序三者不会错位。
    """
    mapping: dict[str, str] = {}
    for line in _read_env_lines(env_file_path(path)):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        mapping[name.strip()] = value.strip().strip("\"'")
    return load_api_keys(mapping)


def append_api_keys(
    keys: Iterable[object],
    path: str | os.PathLike[str] | None = None,
) -> dict[str, object]:
    """在现有 Key 之后追加新 Key（去重），返回不含明文的摘要。

    与 save_api_keys(replace=True) 的区别：这里保留 .env 里已有的 Key，
    对应 GUI 的「新增 API Key」按钮；summary 里带 added 表示真正新增了几个。
    """
    incoming = validate_keys(keys)
    existing = env_file_keys(path)
    merged = dedupe(list(existing) + incoming)
    info = save_api_keys(merged, path=path)
    info["added"] = len(merged) - len(existing)
    return info


def remove_api_key(
    index: int,
    path: str | os.PathLike[str] | None = None,
) -> dict[str, object]:
    """删除 .env 里第 index 个 Key（1 起，顺序同 env_file_keys()）。

    返回 {path, count, masked, removed, removed_key}；removed_key 是脱敏值，
    只用于提示「删掉的是哪一个」，绝不回显明文。
    """
    current = env_file_keys(path)
    try:
        position = int(index)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"要删除的 API Key 序号无效：{index!r}") from exc
    if position < 1 or position > len(current):
        raise ConfigError(f"要删除的 API Key 序号 {position} 不存在（当前共 {len(current)} 个）")
    removed_key = mask_key(current[position - 1])
    remaining = current[: position - 1] + current[position:]
    if remaining:
        info = save_api_keys(remaining, path=path)
    else:  # 删到最后一个：清空 Key 块，但保留 .env 里的其它变量
        info = clear_api_keys(path=path)
        info["masked"] = ""
        info["count"] = 0
    info["removed_key"] = removed_key
    return info


def save_api_keys(
    keys: Iterable[object],
    path: str | os.PathLike[str] | None = None,
    replace: bool = True,
) -> dict[str, object]:
    """把 Key 写入 .env（一 Key 一行：SILICONFLOW_API_KEY_1 / _2 …）。

    保留原有注释与其它变量；replace=True 时先清掉旧的 Key 行，
    避免删掉某个 Key 后旧值残留、下次启动又被读回来。
    返回不含任何明文的摘要：{path, count, masked, removed, chmod_ok}。
    """
    cleaned = validate_keys(keys)
    target = env_file_path(path)

    leaked = _key_var_names()
    kept: list[str] = []
    removed = 0
    for line in _read_env_lines(target):
        stripped = line.strip()
        if replace and stripped == BLOCK_MARKER:      # 清掉上次写入的块标记
            removed += 1
            continue
        if stripped and not stripped.startswith("#") and "=" in stripped:
            if stripped.split("=", 1)[0].strip() in leaked and replace:
                removed += 1
                continue
        kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()

    block = [BLOCK_MARKER]
    for index, key in enumerate(cleaned, start=1):
        block.append(f"{INDEXED_KEY_TEMPLATE.format(index=index)}={_render_value(key)}")

    body = kept + ([""] if kept else []) + block
    atomic_write_text(target, "\n".join(body) + "\n")

    for name in _key_var_names():
        os.environ.pop(name, None)
    for index, key in enumerate(cleaned, start=1):
        os.environ[INDEXED_KEY_TEMPLATE.format(index=index)] = key

    return {
        "path": str(target),
        "count": len(cleaned),
        "masked": mask_keys(cleaned),
        "removed": removed,
        "chmod_ok": _restrict_permissions(target),
    }


def clear_api_keys(path: str | os.PathLike[str] | None = None) -> dict[str, object]:
    """删除 .env 里的 Key 行（保留文件与其它变量），并清除当前进程的环境变量。"""
    target = env_file_path(path)
    leaked = _key_var_names()
    kept: list[str] = []
    removed = 0
    for line in _read_env_lines(target):
        stripped = line.strip()
        if stripped == BLOCK_MARKER:
            removed += 1
            continue
        if stripped and not stripped.startswith("#") and "=" in stripped:
            if stripped.split("=", 1)[0].strip() in leaked:
                removed += 1
                continue
        kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    if target.is_file():
        atomic_write_text(target, ("\n".join(kept) + "\n") if kept else "")
    for name in leaked:
        os.environ.pop(name, None)
    return {"path": str(target), "count": 0, "removed": removed}


def _restrict_permissions(path: Path) -> bool:
    """尽力把 .env 权限收紧到 0600；不支持的文件系统（Windows/NTFS/DrvFs）返回 False。"""
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        return stat.S_IMODE(os.stat(path).st_mode) == (stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        return False


def loaded_keys_summary(env: Mapping[str, str] | None = None) -> dict[str, object]:
    """给界面用的一行摘要：Key 数量 + 脱敏预览。

    优先级与真实运行一致：.env 优先于系统环境变量（load_dotenv_candidates 不覆盖已有值，
    这里只是沿用同一套读取口径）。
    """
    candidate = env if env is not None else os.environ
    keys = load_api_keys(candidate)
    return {"count": len(keys), "masked": mask_keys(keys), "source": "env" if keys else "none"}


__all__ = [
    "ENV_FILENAME",
    "MAX_KEY_INPUT",
    "append_api_keys",
    "clear_api_keys",
    "env_file_keys",
    "env_file_path",
    "loaded_keys_summary",
    "mask_key",
    "mask_keys",
    "normalize_key",
    "parse_key_input",
    "remove_api_key",
    "save_api_keys",
    "validate_key",
    "validate_keys",
]
