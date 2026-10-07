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


def image_size(image_bytes: bytes) -> tuple[int, int]:
    """读图片尺寸（Pillow 只读文件头，不会加载整张图）。"""
    if not image_bytes:
        raise ValueError("截图数据为空")
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - 依赖缺失时给出明确提示
        raise RuntimeError("需要 Pillow 才能处理截图：pip install pillow") from exc
    with Image.open(io.BytesIO(image_bytes)) as image:
        return int(image.size[0]), int(image.size[1])


def md5_hex(base64_text: str) -> str:
    """对 base64 文本取 MD5，用于判断两张截图是否相同。"""
    return hashlib.md5(base64_text.encode("ascii")).hexdigest()


def to_data_url(base64_text: str) -> str:
    return f"data:image/jpeg;base64,{base64_text}"


def to_thumb_data_url(
    image_bytes: bytes, max_width: int = 240, max_height: int = 640, quality: int = 60
) -> str:
    """截图 -> 缩小后的 JPEG data URL（GUI 实时预览用）。

    只缩不放；调用方拿到的是可以直接塞进 <img src> 的字符串，
    尺寸压到几十 KB，避免大图跟着状态轮询来回搬。
    """
    if not image_bytes:
        raise ValueError("截图数据为空")
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - 依赖缺失时给出明确提示
        raise RuntimeError("需要 Pillow 才能处理截图：pip install pillow") from exc

    with Image.open(io.BytesIO(image_bytes)) as image:
        if image.mode != "RGB":
            image = image.convert("RGB")
        image.thumbnail((int(max_width), int(max_height)))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=int(quality), optimize=True)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"
