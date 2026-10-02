# -*- coding: utf-8 -*-
"""DrPilot v4 主入口：python drpilot_v4.py <参数>。

等价于：
    python -m drpilot <参数>
    uv run drpilot <参数>          （推荐，uv 管理依赖与虚拟环境）

例子：
    uv run drpilot doctor --json
    uv run drpilot ask --text "药理学第二章 47到81题，存到 D:\\题库" --json
    uv run drpilot --from 47 --to 81 --output-dir "D:\\题库" --dry-run --json
    uv run drpilot --gui
"""

from __future__ import annotations

import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from drpilot.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
