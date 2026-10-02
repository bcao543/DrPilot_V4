# -*- coding: utf-8 -*-
"""把 96 帧马奔跑 PNG 序列压成一张小尺寸精灵图（只保留 coverage/透明度）。

界面背景里只需要「点阵马」的疏密信息，所以：
    * 按所有帧的并集裁剪，去掉四周空白；
    * 缩到 240x136（点阵网格约 60x34，够采样了）；
    * 只存 alpha 通道（原片就是不透明=马、透明=背景的剪影），体积最小。

    python tools/build_horse_sheet.py --src D:/running-pixel-horse/video --out src/drpilot/webui/assets/horse-sheet.png
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image

COLS = 12
FRAME_W, FRAME_H = 240, 136


def main() -> int:
    parser = argparse.ArgumentParser(description="生成点阵马精灵图")
    parser.add_argument("--src", default="D:/running-pixel-horse/video", help="序列帧目录")
    parser.add_argument("--out", default="src/drpilot/webui/assets/horse-sheet.png")
    parser.add_argument("--meta", default="src/drpilot/webui/assets/horse-sheet.json")
    parser.add_argument("--cols", type=int, default=COLS)
    args = parser.parse_args()

    src = Path(args.src)
    files = sorted(src.glob("*.png"))
    if not files:
        print(f"没找到序列帧：{src}")
        return 1

    frames = [Image.open(path).convert("RGBA") for path in files]
    union = None
    for frame in frames:
        box = frame.getchannel("A").getbbox()
        if not box:
            continue
        union = box if union is None else (
            min(union[0], box[0]), min(union[1], box[1]),
            max(union[2], box[2]), max(union[3], box[3]),
        )
    if union is None:
        print("所有帧都是全透明，检查素材")
        return 1
    print(f"共 {len(frames)} 帧，裁剪范围 {union}")

    cols = args.cols
    rows = (len(frames) + cols - 1) // cols
    sheet = Image.new("L", (cols * FRAME_W, rows * FRAME_H), 0)
    for index, frame in enumerate(frames):
        # 用 alpha 当 coverage，并顺手做一次预乘，避免缩放时出现暗边
        alpha = frame.getchannel("A").crop(union).resize((FRAME_W, FRAME_H), Image.LANCZOS)
        sheet.paste(alpha, ((index % cols) * FRAME_W, (index // cols) * FRAME_H))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, optimize=True)
    meta = {"frames": len(frames), "cols": cols, "rows": rows, "frame_w": FRAME_W, "frame_h": FRAME_H}
    Path(args.meta).write_text(json.dumps(meta), encoding="utf-8")
    print(f"已生成 {out}（{sheet.width}x{sheet.height}，{out.stat().st_size / 1024:.0f} KB）")
    print("元信息：", meta)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
