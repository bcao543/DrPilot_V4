# -*- coding: utf-8 -*-
"""旧版入口（已过时）：保留只是为了不打断老脚本。

新入口请用 drpilot_v4.py / python -m drpilot / uv run drpilot：
    uv run drpilot --from 47 --to 81 --output-dir "D:\\题库"
    uv run drpilot --gui
    uv run drpilot doctor --json
真正的代码都在 src/drpilot/ 下，本文件只是转发壳。
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
