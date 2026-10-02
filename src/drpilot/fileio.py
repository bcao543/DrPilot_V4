# -*- coding: utf-8 -*-
"""文件读写小工具：原子写入，避免中途崩溃留下半截文件。"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def ensure_parent(path: str | os.PathLike[str]) -> Path:
    """确保目标文件的父目录存在。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def atomic_write_text(path: str | os.PathLike[str], text: str, encoding: str = "utf-8") -> str:
    """原子地写入文本：先写临时文件，再 os.replace 覆盖目标。"""
    p = ensure_parent(path)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="\n") as f:
            f.write(text)
        os.replace(tmp, str(p))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return str(p)


def read_json_file(path: str | os.PathLike[str]) -> Any:
    """读取 JSON 文件（utf-8）。"""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
