# -*- coding: utf-8 -*-
"""截图编码工具。"""

from __future__ import annotations

import base64
import hashlib
import io


def to_jpeg_base64(image_bytes: bytes, quality: int = 65) -> str:
    """把 PNG/JPEG 截图转成 JPEG 的 base64，减小请求体积。"""
    if not image_bytes:
        raise ValueError("截图数据为空")
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - 依赖缺失时给出明确提示
        raise RuntimeError("需要 Pillow 才能处理截图：pip install pillow") from exc

    with Image.open(io.BytesIO(image_bytes)) as image:
        if image.mode != "RGB":
            image = image.convert("RGB")
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=int(quality), optimize=True)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def md5_hex(base64_text: str) -> str:
    """对 base64 文本取 MD5，用于判断两张截图是否相同。"""
    return hashlib.md5(base64_text.encode("ascii")).hexdigest()


def to_data_url(base64_text: str) -> str:
    return f"data:image/jpeg;base64,{base64_text}"
