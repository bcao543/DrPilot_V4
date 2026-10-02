# -*- coding: utf-8 -*-
"""兼容入口：python main.py 等价于 python -m drpilot。"""

from __future__ import annotations

import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from drpilot.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
