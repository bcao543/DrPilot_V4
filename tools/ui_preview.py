# -*- coding: utf-8 -*-
"""界面预览：用 Edge / Chrome 的无头模式把 index.html 渲染成 PNG。

页面在没有 Python 后端时会自动进入「界面预览模式」（填好假数据），
所以调版面不需要真机、也不需要 API Key。

    python tools/ui_preview.py                       # 默认 1180x920
    python tools/ui_preview.py --size 1024x700       # 小窗口
    python tools/ui_preview.py --expand              # 展开「翻页动作（点击 / 滑动）」
    python tools/ui_preview.py --out tools/ui.png
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "src" / "drpilot" / "webui" / "assets" / "index.html"

WINDOWS_BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]
UNIX_BROWSERS = ["chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "microsoft-edge"]

# WSL 里可以直接调用 Windows 的浏览器
WSL_BROWSERS = [
    "/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "/mnt/c/Program Files/Microsoft/Edge/Application/msedge.exe",
    "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe",
]


def in_wsl() -> bool:
    return os.name != "nt" and Path("/mnt/c/Windows").exists()


def find_browser() -> str | None:
    if os.name == "nt":
        for path in WINDOWS_BROWSERS:
            if Path(path).is_file():
                return path
        return None
    if in_wsl():
        for path in WSL_BROWSERS:
            if Path(path).is_file():
                return path
        return None
    for name in UNIX_BROWSERS:
        found = shutil.which(name)
        if found:
            return found
    return None


def browser_path(path: Path) -> str:
    """给（可能跑在 Windows 上的）浏览器用的路径写法。"""
    if os.name == "nt":
        return str(path)
    if in_wsl():
        resolved = path.resolve()
        parts = resolved.parts
        if len(parts) > 2 and parts[1] == "mnt" and len(parts[2]) == 1:
            rest = "\\".join(parts[3:])
            return f"{parts[2].upper()}:\\{rest}"
        # 不在 /mnt 下（例如 /tmp）：交给 wslpath 转成 \\wsl.localhost\...
        try:
            result = subprocess.run(
                ["wslpath", "-w", str(resolved)], capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except Exception:
            pass
    return str(path)


def browser_url(path: Path) -> str:
    """浏览器能打开的 file:// URL（WSL 里要用 Windows 路径）。"""
    if os.name != "nt" and in_wsl():
        windows = browser_path(path)
        if len(windows) > 2 and windows[1] == ":" and windows[2] == "\\":
            return "file:///" + windows.replace("\\", "/")
        if windows.startswith("\\\\"):
            return "file://" + windows[2:].replace("\\", "/")
    return path.as_uri()


def main() -> int:
    parser = argparse.ArgumentParser(description="渲染 DrPilot 界面预览图")
    parser.add_argument("--size", default="1180x920", help="窗口尺寸，如 1180x920")
    parser.add_argument("--out", default=str(ROOT / "tools" / "ui_preview.png"))
    parser.add_argument("--expand", action="store_true", help="展开「翻页动作（点击 / 滑动）」面板")
    parser.add_argument("--idle", action="store_true", help="按「未运行」的样子渲染（灰色静止的马）")
    args = parser.parse_args()

    try:
        width, height = (int(part) for part in args.size.lower().split("x", 1))
    except ValueError:
        print("--size 格式应为 宽x高，例如 1180x920", file=sys.stderr)
        return 2

    browser = find_browser()
    if not browser:
        print("没找到 Edge / Chrome，无法生成预览图。", file=sys.stderr)
        return 1
    if not INDEX.is_file():
        print(f"界面文件缺失：{INDEX}", file=sys.stderr)
        return 1

    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    fragment = ""
    if args.expand:
        fragment = "expand"
    if args.idle:
        fragment = (fragment + ",idle") if fragment else "idle"
    url = browser_url(INDEX) + (("#" + fragment) if fragment else "")
    profile = Path(tempfile.gettempdir()) / "drpilot_uipreview_profile"

    cmd = [
        browser,
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--hide-scrollbars",
        "--disk-cache-size=1",
        # file:// 下画布会被视为跨域，读不了像素（点阵马需要 getImageData）
        "--allow-file-access-from-files",
        # 页面等后端 2.5s 才进预览模式，之后点阵马还要异步加载精灵图；
        # 用虚拟时间把这些等待直接快进过去
        "--virtual-time-budget=9000",
        f"--user-data-dir={browser_path(profile)}",
        f"--window-size={width},{height}",
        f"--screenshot={browser_path(out)}",
        url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if not out.is_file():
        print("渲染失败：", file=sys.stderr)
        print("命令：" + " ".join(cmd), file=sys.stderr)
        print((result.stderr or result.stdout or "").strip()[-800:], file=sys.stderr)
        return 1
    print(f"已生成预览：{out}（{width}x{height}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
