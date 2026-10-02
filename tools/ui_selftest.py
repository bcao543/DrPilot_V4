# -*- coding: utf-8 -*-
"""界面自检：把前端放进无头浏览器，用假后端把每个按钮都点一遍。

检查两件事：
    1. 每个按钮都能调到对应的 Python 接口（id 写错、脚本报错都会露出来）；
    2. 页面没有 JS 异常。
历史上踩过的坑：工具栏的 worker 容器和「并发」输入框撞了同一个 id，
导致 payload() 抛异常，一半按钮点了没反应 —— 这个脚本就是为此加的。

    python tools/ui_selftest.py
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ui_preview import browser_path, browser_url, find_browser  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "src" / "drpilot" / "webui" / "assets"

# 期望被点到的接口（answer_question 走弹窗那条路）
EXPECTED = [
    "ready", "poll", "preview", "start", "stop", "test_model",
    "connect_device", "disconnect_device", "refresh_devices",
    "pick_output_dir", "open_output_dir", "set_reference", "test_swipe",
    "start_record", "stop_record", "test_action", "clear_action", "use_tap",
    "answer_question", "exit",
    "add_keys", "delete_key", "clear_keys",
]

MOCK_JS = r"""
/* 假后端：记录被调用的接口；状态可被探针脚本改写 */
(function () {
  window.__calls = [];
  window.__state = {
    device: { text: "设备：1 台在线", tone: "success", detail: "ok", busy: false },
    model: { text: "模型：可用", tone: "success", note: "ok" },
    keys: "已加载 2 个 API Key",
    key_masked: "sk-********ab12 / sk-********cd34",
    key_items: [
      { index: 1, masked: "sk-********ab12" },
      { index: 2, masked: "sk-********cd34" }
    ],
    progress: { percent: 35, current: "第 7 题（7/20）", status: "运行中", tone: "" },
    running: false, model_checking: false, key_count: 2,
    workers: [{ id: 1, status: "空闲", done: 2 }],
    question: null, toasts: [], form_patch: {},
    total_hint: "共 20 题", output_preview: "将输出：x",
    recording: false,
    action: { recording: false, steps: [], summary: "", reference: "", note: "" }
  };
  const replies = {
    ready: function () { return { config: { page_from: 1, page_to: 20, output_dir: "D:/题库", workers: 3 }, state: window.__state, logs: [] }; },
    poll: function () { return { logs: [], state: window.__state }; },
    preview: function () { return { total_hint: "共 20 题", output_preview: "将输出：x" }; },
    pick_output_dir: function () { return "D:/题库"; },
    start: function () { return { ok: true }; },
    add_keys: function () {
      return { ok: true, count: 3, added: 2, masked: "sk-********ab12 / sk-********cd34 / sk-********1234",
               items: [{ index: 1, masked: "sk-********ab12" }, { index: 2, masked: "sk-********cd34" },
                       { index: 3, masked: "sk-********1234" }], error: null };
    },
    delete_key: function () {
      return { ok: true, count: 2, removed_key: "sk-********ab12",
               items: [{ index: 1, masked: "sk-********cd34" }, { index: 2, masked: "sk-********1234" }], error: null };
    },
    clear_keys: function () { return { ok: true, count: 0, removed: 3, items: [], error: null }; }
  };
  const api = {};
  ["ready", "poll", "preview", "start", "stop", "test_model", "connect_device", "disconnect_device",
   "refresh_devices", "pick_output_dir", "open_output_dir", "set_reference", "test_swipe",
   "start_record", "stop_record", "test_action", "clear_action", "use_tap",
   "answer_question", "exit", "add_keys", "delete_key", "clear_keys"].forEach(function (name) {
    api[name] = function () {
      window.__calls.push(name);
      return Promise.resolve((replies[name] || function () { return { ok: true }; })());
    };
  });
  window.pywebview = { api: api };
})();
"""

PROBE_JS = r"""
/* 把每个按钮点一遍，结果写进 <pre id="selftest">，供 --dump-dom 读取 */
window.__errors = [];
window.addEventListener("error", function (e) { window.__errors.push("onerror: " + e.message); });
window.addEventListener("unhandledrejection", function (e) { window.__errors.push("reject: " + e.reason); });

function click(id) {
  const el = document.getElementById(id);
  if (!el) { window.__errors.push("缺少元素 " + id); return; }
  if (el.disabled) { window.__errors.push("按钮被禁用（未点到）：" + id); return; }
  try { el.click(); } catch (e) { window.__errors.push(id + " 点击异常：" + e.message); }
}
function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
function countPainted(canvas) {
  try {
    const img = canvas.getContext("2d").getImageData(0, 0, canvas.width, canvas.height).data;
    let painted = 0;
    for (let i = 3; i < img.length; i += 4) if (img[i] > 4) painted++;
    return painted;
  } catch (e) { return "ERR:" + e.message; }
}

async function horsePainted() {
  for (let attempt = 0; attempt < 12; attempt++) {
    const canvas = document.getElementById("horse");
    if (canvas) {
      const n = countPainted(canvas);
      if (typeof n === "string" || n > 0) return n;
    }
    await sleep(350);
  }
  return 0;
}

function dump(horse) {
  if (document.getElementById("selftest")) return;
  const pre = document.createElement("pre");
  pre.id = "selftest";
  pre.textContent = "CALLS=" + JSON.stringify(window.__calls) +
    "\nERRORS=" + JSON.stringify(window.__errors) +
    "\nHORSE=" + String(horse) +
    "\nPREVIEW_CALLS=" + String(window.__calls.filter(function (c) { return c === "preview"; }).length) +
    "\nKEYS_HINT=" + ((document.getElementById("workers-hint") || {}).textContent || "") +
    "\nKEYS_PRIVATE=" + String(window.__keyPrivateStart) +
    "\nKEYS_REVEALED=" + String(window.__keyRevealed) +
    "\nKEYS_RESTORED=" + String(window.__keyPrivateRestored) +
    "\nKEYS_CHIPS=" + String(window.__keyChips) +
    "\nKEYS_CLEARED_INPUT=" + String(window.__keyInputCleared);
  document.body.appendChild(pre);
}

async function run() {
  // API Key 隐私输入：默认必须是 password，点眼睛才切明文，新增后输入框立即清空
  const keyBox = document.getElementById("api_key_input");
  window.__keyPrivateStart = !!(keyBox && keyBox.type === "password");
  window.__keyRevealed = false;
  window.__keyPrivateRestored = false;
  window.__keyChips = 0;
  window.__keyInputCleared = false;
  // 删除 Key 需要显式确认，探针里把 confirm 固定为「确认」
  window.confirm = function () { return true; };
  ["btn-start", "btn-test-model", "btn-connect", "btn-disconnect", "btn-refresh-devices",
   "btn-pick-dir", "btn-open-dir", "btn-clear-log", "btn-swipe-toggle", "btn-set-reference",
   "btn-test-swipe"].forEach(click);
  await sleep(400);
  // 翻页动作：录制 → 停止 → 试一次 → 清除 / 手动点按
  click("btn-record-start");
  await sleep(300);
  window.__state.action = { recording: true, steps: [], summary: "", reference: "", note: "录制中…" };
  await sleep(400);
  click("btn-record-stop");
  await sleep(300);
  window.__state.action = {
    recording: false,
    steps: [{ kind: "tap", x: 630, y: 2380, duration_ms: 80 }],
    summary: "1. 点击 (630,2380)",
    reference: "1260x2720",
    note: "已录制 1 步"
  };
  await sleep(400);
  click("btn-test-action");
  await sleep(300);
  click("btn-action-clear");
  await sleep(200);
  click("btn-use-tap");
  await sleep(300);
  // 模拟“开始提取”后的运行态：等一次轮询点亮「停止」
  window.__state.running = true;
  await sleep(400);
  click("btn-stop");
  // 模拟后端提问：弹窗点「继续」
  window.__state.question = { id: 1, title: "起始题号不一致", message: "测试", kind: "yesno" };
  await sleep(400);
  click("modal-yes");
  await sleep(300);
  window.__state.question = null;
  // API Key：填写 → 新增 → 眼睛切明文 → 回隐私模式 → 删除一个 → 全部删除
  if (keyBox) {
    // 用「空格分隔」模拟多行粘贴被压平的情况：前端要能还原成多个 Key
    keyBox.value = "sk-abcdefgh1234 sk-ijklmnop5678";
    await sleep(150);
    click("btn-add-key");            // add_keys
    await sleep(250);
    window.__keyChips = (document.getElementById("key-chips") || {}).childElementCount || 0;
    window.__keyInputCleared = keyBox.value === "";
    click("btn-toggle-key");         // 明文模式（只影响正在输入的内容）
    await sleep(200);
    window.__keyRevealed = keyBox.type === "text";
    click("btn-toggle-key");         // 回到隐私输入模式
    await sleep(200);
    window.__keyPrivateRestored = keyBox.type === "password";
    const chipDel = document.querySelector("#key-chips .chip-del");
    if (chipDel) { chipDel.click(); }  // delete_key（confirm 已固定为 true）
    await sleep(300);
    click("btn-clear-keys");         // clear_keys
    await sleep(250);
    window.__state.key_count = 2;    // 假后端保持 2 个 Key，后面的并发提示才有意义
  }
  // 「生成 Markdown / index.json」开关必须让「将输出」实时刷新（每次都会调一次 preview）
  click("opt-md");
  await sleep(450);
  click("opt-index");
  await sleep(450);
  // 并发改成 3（假后端只有 2 个 Key）→ 旁边的提示要说明会共用
  const workersInput = document.getElementById("workers");
  workersInput.value = "3";
  workersInput.dispatchEvent(new Event("input", { bubbles: true }));
  await sleep(300);
  click("btn-exit");
  await sleep(300);
}

setTimeout(function () {
  document.title = "SELFTEST-RAN";
  run()
    .then(horsePainted)
    .then(function (painted) { dump(painted); }, function (e) {
      window.__errors.push("探针异常：" + ((e && (e.stack || e.message)) || String(e)));
      dump(0);
    });
}, 2500);
"""


def check_ids() -> list[str]:
    """静态检查 html/js 的 id 一致性（历史上就是这里出的问题）。"""
    html = (ASSETS / "index.html").read_text(encoding="utf-8")
    js = (ASSETS / "app.js").read_text(encoding="utf-8")
    html_ids = re.findall(r'id="([A-Za-z0-9_-]+)"', html)
    problems: list[str] = []
    duplicates = sorted({name for name in html_ids if html_ids.count(name) > 1})
    if duplicates:
        problems.append("index.html 里有重复 id：" + "、".join(duplicates))
    used = set(re.findall(r'\$\("([A-Za-z0-9_-]+)"\)', js))
    missing = sorted(used - set(html_ids))
    if missing:
        problems.append("app.js 用到但 index.html 里没有的 id：" + "、".join(missing))
    return problems


def build_page(workdir: Path) -> Path:
    # 整个 assets 目录都拷过去（含 horse.js / horse-sheet.png，少了点阵马就画不出来）
    for path in sorted(ASSETS.iterdir()):
        if path.is_file():
            shutil.copy(path, workdir / path.name)
    (workdir / "mock.js").write_text(MOCK_JS, encoding="utf-8")
    (workdir / "probe.js").write_text(PROBE_JS, encoding="utf-8")
    index = workdir / "index.html"
    html = index.read_text(encoding="utf-8")
    html = html.replace(
        '<script src="app.js"></script>',
        '<script src="mock.js"></script>\n<script src="app.js"></script>\n<script src="probe.js"></script>',
    )
    index.write_text(html, encoding="utf-8")
    return index


def main() -> int:
    browser = find_browser()
    if not browser:
        print("没找到 Edge / Chrome，无法自检。", file=sys.stderr)
        return 2
    if not (ASSETS / "index.html").is_file():
        print(f"界面文件缺失：{ASSETS}", file=sys.stderr)
        return 2

    problems = check_ids()
    if problems:
        for item in problems:
            print("静态检查未通过：" + item, file=sys.stderr)
        return 1
    print("id 一致性检查通过 ✓")

    workdir = Path(tempfile.gettempdir()) / "drpilot_ui_selftest"
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    index = build_page(workdir)

    profile = workdir / "profile"
    cmd = [
        browser, "--headless=new", "--disable-gpu", "--no-first-run", "--virtual-time-budget=12000",
        # file:// 下 Chromium 会把脚本错误吞成 "Script error."，这个开关让错误信息可见
        "--allow-file-access-from-files",
        "--dump-dom", f"--user-data-dir={browser_path(profile)}", browser_url(index),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    match = re.search(r'<pre id="selftest">(.*?)</pre>', result.stdout or "", re.S)
    if not match:
        print("自检失败：页面没有输出结果。", file=sys.stderr)
        print((result.stdout or "")[-600:], file=sys.stderr)
        return 1

    body = match.group(1)
    calls = json.loads(re.search(r"CALLS=(\[.*?\])", body, re.S).group(1))
    errors = json.loads(re.search(r"ERRORS=(\[.*?\])", body, re.S).group(1))
    horse = re.search(r"HORSE=(\S+)", body)
    horse_value = horse.group(1) if horse else "0"

    preview_match = re.search(r"PREVIEW_CALLS=(\d+)", body)
    preview_calls = int(preview_match.group(1)) if preview_match else 0

    missing = [name for name in EXPECTED if name not in calls]
    if preview_calls < 3:
        missing.append(f"开关没有触发「将输出」刷新（preview 只调了 {preview_calls} 次）")
    for label, flag, problem in (
        ("KEYS_PRIVATE", "true", "API Key 输入框默认不是隐私模式（type 不是 password）"),
        ("KEYS_REVEALED", "true", "点眼睛图标后输入框没有切到明文"),
        ("KEYS_RESTORED", "true", "再点眼睛没有回到隐私输入模式"),
        ("KEYS_CLEARED_INPUT", "true", "新增 Key 后明文仍留在输入框里"),
    ):
        found = re.search(label + r"=(\S+)", body)
        if not found or found.group(1) != flag:
            missing.append(problem)
    chips_match = re.search(r"KEYS_CHIPS=(\d+)", body)
    if not chips_match or int(chips_match.group(1)) < 3:
        missing.append("新增 Key 后没有渲染出「已保存」列表（缺少 key_items 或渲染失败）")
    keys_hint_match = re.search(r"KEYS_HINT=([^\n]*)", body)
    keys_hint = (keys_hint_match.group(1) if keys_hint_match else "").strip()
    if "共用" not in keys_hint:
        missing.append(f"并发超过 Key 数量时没有提示（当前提示：{keys_hint or '空'}）")
    if horse_value.startswith("ERR"):
        missing.append("点阵马读不到画布（" + horse_value + "）")
    elif horse_value in ("0", "MISSING", ""):
        missing.append("点阵马没有画出来（" + horse_value + "）")
    print(f"接口调用：{', '.join(dict.fromkeys(calls))}")
    print(f"点阵马像素：{horse_value}")
    print(f"「将输出」刷新次数：{preview_calls}")
    print(f"并发提示：{keys_hint or '（空）'}")
    if errors:
        print("页面异常：", file=sys.stderr)
        for item in errors:
            print(f"  - {item}", file=sys.stderr)
    if missing:
        print("没有被点到的接口：" + "、".join(missing), file=sys.stderr)
    if errors or missing:
        print("界面自检未通过 ✗", file=sys.stderr)
        return 1
    print(f"界面自检通过 ✓（{len(set(calls))} 个接口全部触达，无脚本异常）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
