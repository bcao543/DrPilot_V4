# -*- coding: utf-8 -*-
"""界面自检：把每个页面放进无头浏览器，用假后端把每个按钮都点一遍。

检查三件事：
    1. 每个按钮都能调到对应的 Python 接口（id 写错、脚本报错都会露出来）；
    2. 页面没有 JS 异常；
    3. 关键联动还在（入口窗口回填、模块清单写回、滑块应用、复位…）。

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

# 每个页面：html / js / 假后端 / 探针 / 期望被调到的接口 / 断言
# （flags 的取值由探针写进 <pre id="selftest">，形如 NAME=value）

MAIN_MOCK = r"""
(function () {
  window.__calls = [];
  window.__panels = [];
  window.__lastPreview = null;
  window.__state = {
    device: { text: "设备：1 台在线", tone: "success", detail: "ok", busy: false },
    model: { text: "模型：可用", tone: "success", note: "ok" },
    keys: "已加载 2 个 API Key", key_masked: "sk-********ab12", key_count: 2,
    key_items: [{ index: 1, masked: "sk-********ab12" }, { index: 2, masked: "sk-********cd34" }],
    progress: { percent: 35, current: "第 7 题（7/20）", status: "运行中", tone: "" },
    running: false, stopping: false, model_checking: false,
    workers: [{ id: 1, status: "空闲", done: 2 }],
    question: null, toasts: [], form_patch: {},
    total_hint: "共 20 题", output_preview: "将输出：x",
    panels: {
      action: "录制动作 · 3 步",
      modules: "5 项：题号*、题目*、答案*、考点还原、标准解析",
      model: "deepseek-flash · 2 个 Key",
      display: "显示：物理 1260x2720"
    }
  };
  const replies = {
    ready: function () {
      return { config: {
        page_from: 1, page_to: 20, output_dir: "D:/题库", workers: 2, max_tokens: 4096,
        model: "deepseek-flash", base_url: "https://api.deepseek.com",
        page_action: "auto", next_action: "", action_tap_x: "", action_tap_y: "", action_tap_ms: 80,
        extract_modules: "", output_fields: "[]", display_scale: 1, display_density: 0,
        display_mode: false, display_scaling: false
      }, state: window.__state, logs: [] };
    },
    poll: function () { return { logs: [], state: window.__state }; },
    preview: function (payload) {
      window.__lastPreview = payload || null;
      return { total_hint: "共 20 题", output_preview: "将输出：x" };
    },
    start: function () { return { ok: true }; },
    stop: function () { return { ok: true }; },
    connect_device: function () { return { ok: true }; },
    disconnect_device: function () { return { ok: true }; },
    refresh_devices: function () { return { ok: true }; },
    pick_output_dir: function () { return "D:/题库"; },
    open_output_dir: function () { return { ok: true }; },
    display_status: function () { return { ok: true, text: "显示：物理 1260x2720" }; },
    answer_question: function () { return { ok: true }; },
    exit: function () { return { ok: true }; },
    open_panel: function (name) { window.__panels.push(name); return { ok: true }; }
  };
  const api = {};
  ["ready", "poll", "preview", "start", "stop", "connect_device", "disconnect_device",
   "refresh_devices", "pick_output_dir", "open_output_dir", "display_status",
   "answer_question", "exit", "open_panel"].forEach(function (name) {
    api[name] = function () {
      const args = Array.prototype.slice.call(arguments);
      window.__calls.push(name);
      return Promise.resolve((replies[name] || function () { return { ok: true }; }).apply(null, args));
    };
  });
  window.pywebview = { api: api };
})();
"""

MAIN_PROBE = r"""
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
function fire(el, type) {
  if (!el) { window.__errors.push("缺少元素（" + type + "）"); return; }
  el.dispatchEvent(new Event(type, { bubbles: true }));
}
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

async function run() {
  await sleep(700);
  // 四个入口都要把当前表单一起带过去
  for (const id of ["entry-action", "entry-modules", "entry-model", "entry-tuner"]) {
    click(id);
    await sleep(150);
  }
  window.__entries = window.__panels.join(",");
  window.__noteAction = (document.getElementById("note-action") || {}).textContent || "";
  // 任务范围改动 -> 自动刷新「将输出」
  const from = document.getElementById("page_from");
  from.value = "1";
  fire(from, "input");
  await sleep(350);
  click("opt-md");
  await sleep(400);
  window.__previewCalls = window.__calls.filter(function (c) { return c === "preview"; }).length;
  // 后端回填三个显示字段（屏幕调节窗口改完就是这么回填的）
  window.__state.form_patch = { display_scale: 2.0, display_width_scale: 1.0, display_density: 420 };
  await sleep(400);
  window.__state.form_patch = {};
  window.__patchOk = Number((document.getElementById("display_density") || {}).value) === 420;
  fire(from, "input");                       // 再触发一次 preview，把 payload 送出去
  await sleep(400);
  const payload = window.__lastPreview || {};
  window.__payloadOk = payload.display_mode === true
    && typeof payload.page_action === "string"
    && typeof payload.output_fields === "string"
    && payload.model === "deepseek-flash";
  // 运行态：开始 / 停止
  click("btn-start");
  await sleep(200);
  window.__state.running = true;
  await sleep(400);
  click("btn-stop");
  await sleep(200);
  // 后端提问 -> 弹窗点「继续」
  window.__state.question = { id: 1, title: "起始题号不一致", message: "测试", kind: "yesno" };
  await sleep(400);
  click("modal-yes");
  await sleep(300);
  window.__state.question = null;
  click("btn-connect");
  await sleep(150);
  click("btn-disconnect");
  await sleep(150);
  click("btn-refresh-devices");
  await sleep(150);
  click("btn-pick-dir");
  await sleep(200);
  click("btn-open-dir");
  await sleep(150);
  click("btn-clear-log");
  await sleep(150);
  // 并发 3 > 2 个 Key：旁边的提示要说明会共用
  const workers = document.getElementById("workers");
  workers.value = "3";
  fire(workers, "input");
  await sleep(300);
  window.__keysHint = (document.getElementById("workers-hint") || {}).textContent || "";
  click("btn-exit");
  await sleep(300);
}

setTimeout(function () {
  document.title = "SELFTEST-RAN";
  run().then(horsePainted).then(function (painted) {
    const pre = document.createElement("pre");
    pre.id = "selftest";
    pre.textContent = "CALLS=" + JSON.stringify(window.__calls) +
      "\nERRORS=" + JSON.stringify(window.__errors) +
      "\nHORSE=" + String(painted) +
      "\nENTRIES=" + String(window.__entries) +
      "\nNOTE_ACTION=" + String(window.__noteAction) +
      "\nPREVIEW_CALLS=" + String(window.__previewCalls) +
      "\nPATCH_OK=" + String(window.__patchOk) +
      "\nPAYLOAD_OK=" + String(window.__payloadOk) +
      "\nKEYS_HINT=" + String(window.__keysHint);
    document.body.appendChild(pre);
  }, function (e) {
    window.__errors.push("探针异常：" + ((e && (e.stack || e.message)) || String(e)));
    const pre = document.createElement("pre");
    pre.id = "selftest";
    pre.textContent = "CALLS=[]\nERRORS=" + JSON.stringify(window.__errors) + "\nHORSE=0";
    document.body.appendChild(pre);
  });
}, 2200);
"""

ACTIONS_MOCK = r"""
(function () {
  window.__calls = [];
  window.__applied = [];
  window.__tested = null;
  const state = {
    ok: true, device: "设备：1 台在线", kind: "swipe",
    summary: "滑动 (1060,553) → (270,551)",
    tap: { x: 0, y: 0, ms: 80 },
    swipe: { x1: 1060, y1: 553, x2: 270, y2: 551, duration: 150, retries: 5, retry_wait: 1.2 },
    record: { steps: [], summary: "", note: "未设置" },
    reference: "", recording: false, running: false, notice: { text: "", tone: "" }
  };
  const replies = {
    ready: function () { return state; },
    poll: function () { return state; },
    apply: function (values) {
      const v = values || {};
      window.__applied.push(v);
      state.kind = v.kind || state.kind;
      state.tap = v.tap || state.tap;
      state.swipe = v.swipe || state.swipe;
      state.reference = v.reference || "";
      state.summary = (v.kind === "tap" ? "点按 (" + state.tap.x + ", " + state.tap.y + ")"
        : v.kind === "record" ? "录制动作 · 0 步"
        : "滑动 (" + state.swipe.x1 + "," + state.swipe.y1 + ") → (" + state.swipe.x2 + "," + state.swipe.y2 + ")");
      return { ok: true, state: state };
    },
    test: function (values) { window.__tested = values || null; return { ok: true }; },
    set_reference: function () { state.reference = "1260x2720"; return { ok: true }; },
    start_record: function () { state.recording = true; state.notice = { text: "录制中…", tone: "info" }; return { ok: true }; },
    stop_record: function () {
      state.recording = false;
      state.record = { steps: [{ kind: "tap", x: 630, y: 2380, duration_ms: 80 }],
                       summary: "1. 点击 (630,2380)", note: "已录制 1 步" };
      return { ok: true };
    },
    clear_recorded: function () {
      state.record = { steps: [], summary: "", note: "已清除录制的动作" };
      return { ok: true, state: state };
    }
  };
  const api = {};
  ["ready", "poll", "apply", "test", "set_reference", "start_record", "stop_record", "clear_recorded"]
    .forEach(function (name) {
      api[name] = function () {
        const args = Array.prototype.slice.call(arguments);
        window.__calls.push(name);
        return Promise.resolve((replies[name] || function () { return { ok: true }; }).apply(null, args));
      };
    });
  // 模拟 pywebview 的异步注入：DOMContentLoaded 时 window.pywebview 还不存在，
  // 250ms 后才注入并派发 pywebviewready。窗口必须等它，不能直接掉进预览模式。
  setTimeout(function () {
    window.pywebview = { api: api };
    window.dispatchEvent(new Event("pywebviewready"));
  }, 250);
})();
"""

ACTIONS_PROBE = r"""
window.__errors = [];
window.addEventListener("error", function (e) { window.__errors.push("onerror: " + e.message); });
window.addEventListener("unhandledrejection", function (e) { window.__errors.push("reject: " + e.reason); });
window.confirm = function () { return true; };
function click(id) {
  const el = document.getElementById(id);
  if (!el) { window.__errors.push("缺少元素 " + id); return; }
  if (el.disabled) { window.__errors.push("按钮被禁用（未点到）：" + id); return; }
  try { el.click(); } catch (e) { window.__errors.push(id + " 点击异常：" + e.message); }
}
function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
function visible(id) {
  const el = document.getElementById(id);
  return !!el && !el.classList.contains("hidden");
}
function setValue(id, value) {
  const el = document.getElementById(id);
  if (!el) { window.__errors.push("缺少元素 " + id); return; }
  el.value = value;
  el.dispatchEvent(new Event("input", { bubbles: true }));
}

async function run() {
  await sleep(700);
  window.__notPreview = ((document.getElementById("pill-device-text") || {}).textContent || "")
    .indexOf("预览模式") < 0;
  window.__initialCard = visible("card-swipe") && !visible("card-tap") && !visible("card-record");
  // 三选一：点「点按」应只显示点按参数
  const tapChoice = document.querySelector('.choice[data-kind="tap"]');
  if (tapChoice) tapChoice.click(); else window.__errors.push("找不到点按选项");
  await sleep(200);
  window.__tapCard = visible("card-tap") && !visible("card-swipe") && !visible("card-record");
  setValue("tap_x", "540");
  setValue("tap_y", "2280");
  setValue("tap_ms", "90");
  await sleep(200);
  click("btn-apply");
  await sleep(400);
  const applied = window.__applied[0] || {};
  window.__applyOk = applied.kind === "tap" && applied.tap && applied.tap.x === 540
    && applied.tap.y === 2280 && applied.tap.ms === 90;
  click("btn-test");
  await sleep(300);
  window.__testOk = !!(window.__tested && window.__tested.kind === "tap");
  // 滑动参数
  const swipeChoice = document.querySelector('.choice[data-kind="swipe"]');
  if (swipeChoice) swipeChoice.click();
  await sleep(200);
  setValue("swipe_x1", "1000");
  setValue("swipe_y1", "600");
  setValue("swipe_x2", "300");
  setValue("swipe_y2", "700");
  setValue("swipe_duration", "220");
  setValue("swipe_reference", "1080x2340");
  await sleep(200);
  click("btn-apply");
  await sleep(400);
  const swipeApplied = window.__applied[window.__applied.length - 1] || {};
  window.__swipeOk = swipeApplied.kind === "swipe" && swipeApplied.swipe.x1 === 1000
    && swipeApplied.swipe.duration === 220 && swipeApplied.reference === "1080x2340";
  // 「设为当前设备」：后端读回的分辨率要能显示回输入框（不能被轮询打回去）
  click("btn-set-reference");
  await sleep(700);
  window.__refFromDevice = document.getElementById("swipe_reference").value === "1260x2720";
  // 录制
  const recordChoice = document.querySelector('.choice[data-kind="record"]');
  if (recordChoice) recordChoice.click();
  await sleep(200);
  window.__recordCard = visible("card-record") && !visible("card-tap") && !visible("card-swipe");
  // 录制按钮是「开始录制 ⇄ 结束录制」一个按钮切换
  const recordToggle = document.getElementById("btn-record-toggle");
  window.__recordLabelStart = recordToggle ? recordToggle.textContent.trim() : "";
  click("btn-record-toggle");
  await sleep(400);
  window.__recordLabelDuring = ((document.getElementById("btn-record-toggle") || {}).textContent || "").trim();
  click("btn-record-toggle");
  await sleep(500);
  window.__recordLabelEnd = ((document.getElementById("btn-record-toggle") || {}).textContent || "").trim();
  window.__steps = document.querySelectorAll("#action-steps li").length;
  click("btn-record-clear");
  await sleep(400);
  window.__stepsCleared = document.querySelectorAll("#action-steps li").length === 0;
}

setTimeout(function () {
  document.title = "SELFTEST-RAN";
  run().then(function () {
    const pre = document.createElement("pre");
    pre.id = "selftest";
    pre.textContent = "CALLS=" + JSON.stringify(window.__calls) +
      "\nERRORS=" + JSON.stringify(window.__errors) +
      "\nBACKEND_CONNECTED=" + String(window.__notPreview) +
      "\nINITIAL_CARD=" + String(window.__initialCard) +
      "\nTAP_CARD=" + String(window.__tapCard) +
      "\nRECORD_CARD=" + String(window.__recordCard) +
      "\nAPPLY_OK=" + String(window.__applyOk) +
      "\nSWIPE_OK=" + String(window.__swipeOk) +
      "\nAPPLIED=" + JSON.stringify(window.__applied) +
      "\nREF_FROM_DEVICE=" + String(window.__refFromDevice) +
      "\nTEST_OK=" + String(window.__testOk) +
      "\nRECORD_LABEL_START=" + String(window.__recordLabelStart) +
      "\nRECORD_LABEL_DURING=" + String(window.__recordLabelDuring) +
      "\nRECORD_LABEL_END=" + String(window.__recordLabelEnd) +
      "\nSTEPS=" + String(window.__steps) +
      "\nSTEPS_CLEARED=" + String(window.__stepsCleared);
    document.body.appendChild(pre);
  }, function (e) {
    window.__errors.push("探针异常：" + ((e && (e.stack || e.message)) || String(e)));
  });
}, 2200);
"""

MODULES_MOCK = r"""
(function () {
  window.__calls = [];
  window.__saved = null;
  const base = [
    { kind: "base", key: "id", name: "题号", json_keys: ["id"], in_json: true, in_markdown: true,
      note: "截图右上角的题号（JSON 键 id）" },
    { kind: "base", key: "stem", name: "题目", json_keys: ["type", "stem", "options"],
      in_json: true, in_markdown: true, note: "题干与选项" },
    { kind: "base", key: "answer", name: "答案", json_keys: ["answer"], in_json: true,
      in_markdown: true, note: "正确选项字母" }
  ];
  const state = {
    ok: true,
    fields: base.concat([
      { kind: "module", name: "考点还原", aliases: ["考点"], below_fold: true, required: false,
        in_json: true, in_markdown: true, hint: "「考点还原」小节的正文" }
    ]),
    presets: [
      { name: "标准解析", aliases: ["解析"], below_fold: true, required: false, in_markdown: true,
        in_json: true, hint: "「标准解析」小节的正文" },
      { name: "考点还原", aliases: ["考点"], below_fold: true, required: false, in_markdown: true,
        in_json: true, hint: "「考点还原」小节的正文" },
      { name: "难度", aliases: [], below_fold: false, required: false, in_markdown: true,
        in_json: true, hint: "题干下方的难度星级" }
    ],
    max_modules: 12,
    summary: "4 项：题号*、题目*、答案*、考点还原",
    display: { text: "显示：物理 1260x2720", tone: "muted", detail: "" }
  };
  const replies = {
    ready: function () { return state; },
    poll: function () { return state; },
    save: function (values) {
      window.__saved = values || null;
      const fields = (values && values.fields) || [];
      state.fields = fields;
      state.summary = fields.length + " 项";
      return { ok: true, state: state };
    },
    open_display: function () { window.__openedDisplay = true; return { ok: true }; }
  };
  const api = {};
  ["ready", "poll", "save", "open_display"].forEach(function (name) {
    api[name] = function () {
      const args = Array.prototype.slice.call(arguments);
      window.__calls.push(name);
      return Promise.resolve((replies[name] || function () { return { ok: true }; }).apply(null, args));
    };
  });
  window.pywebview = { api: api };
})();
"""

MODULES_PROBE = r"""
window.__errors = [];
window.addEventListener("error", function (e) { window.__errors.push("onerror: " + e.message); });
window.addEventListener("unhandledrejection", function (e) { window.__errors.push("reject: " + e.reason); });
window.confirm = function () { return true; };
function click(id) {
  const el = document.getElementById(id);
  if (!el) { window.__errors.push("缺少元素 " + id); return; }
  if (el.disabled) { window.__errors.push("按钮被禁用（未点到）：" + id); return; }
  try { el.click(); } catch (e) { window.__errors.push(id + " 点击异常：" + e.message); }
}
function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

async function run() {
  await sleep(700);
  window.__rows = document.querySelectorAll("#module-list .module-row").length;
  window.__baseRows = document.querySelectorAll("#module-list .module-row.base").length;
  window.__chips = document.querySelectorAll("#module-presets .preset-chip").length;
  // 常用模块快捷加一行
  const chip = document.querySelector("#module-presets .preset-chip");
  if (chip) chip.click(); else window.__errors.push("没有常用模块按钮");
  await sleep(250);
  window.__rowsAfterChip = document.querySelectorAll("#module-list .module-row").length;
  // 手动加一个自定义模块：只填模块名
  click("btn-add-module");
  await sleep(200);
  const blank = document.querySelector("#module-list .module-row:last-child .module-name");
  if (blank) {
    blank.value = "我的模块";
    blank.dispatchEvent(new Event("input", { bubbles: true }));
  }
  await sleep(200);
  window.__rowsAfterAdd = document.querySelectorAll("#module-list .module-row").length;
  // 删掉「题号」本体行：confirm 已固定为 true
  const idRow = document.querySelector('#module-list .module-row.base[data-key="id"] .module-del');
  if (idRow) idRow.click(); else window.__errors.push("找不到题号行的删除按钮");
  await sleep(200);
  window.__baseAfterDelete = document.querySelectorAll("#module-list .module-row.base").length;
  // 关掉「答案」的 JSON 输出
  const answerJson = document.querySelector('#module-list .module-row.base[data-key="answer"] .out-json');
  if (answerJson) { answerJson.checked = false; answerJson.dispatchEvent(new Event("change", { bubbles: true })); }
  await sleep(200);
  click("btn-save");
  await sleep(500);
  const saved = (window.__saved && window.__saved.fields) || [];
  const savedBase = saved.filter(function (item) { return item.kind === "base"; });
  const savedModules = saved.filter(function (item) { return item.kind === "module"; });
  window.__saveOk = savedBase.length === 2
    && !savedBase.some(function (item) { return item.key === "id"; })
    && savedBase.some(function (item) { return item.key === "answer" && item.in_json === false; })
    && savedModules.length === 3
    && savedModules.some(function (item) { return item.name === "标准解析"; })
    && savedModules.some(function (item) { return item.name === "我的模块"; });
  click("btn-open-display");
  await sleep(300);
  window.__openedDisplay = !!window.__openedDisplay;
}

setTimeout(function () {
  document.title = "SELFTEST-RAN";
  run().then(function () {
    const pre = document.createElement("pre");
    pre.id = "selftest";
    pre.textContent = "CALLS=" + JSON.stringify(window.__calls) +
      "\nERRORS=" + JSON.stringify(window.__errors) +
      "\nROWS=" + String(window.__rows) +
      "\nBASE_ROWS=" + String(window.__baseRows) +
      "\nCHIPS=" + String(window.__chips) +
      "\nROWS_AFTER_CHIP=" + String(window.__rowsAfterChip) +
      "\nROWS_AFTER_ADD=" + String(window.__rowsAfterAdd) +
      "\nBASE_AFTER_DELETE=" + String(window.__baseAfterDelete) +
      "\nSAVE_OK=" + String(window.__saveOk) +
      "\nOPENED_DISPLAY=" + String(window.__openedDisplay);
    document.body.appendChild(pre);
  }, function (e) {
    window.__errors.push("探针异常：" + ((e && (e.stack || e.message)) || String(e)));
  });
}, 2200);
"""

MODEL_MOCK = r"""
(function () {
  window.__calls = [];
  window.__saved = null;
  window.__tested = null;
  const state = {
    ok: true,
    model: "deepseek-flash",
    base_url: "https://api.deepseek.com",
    default_model: "deepseek-flash",
    default_base_url: "https://api.deepseek.com",
    keys: "已加载 2 个 API Key", key_count: 2,
    key_items: [{ index: 1, masked: "sk-********ab12" }, { index: 2, masked: "sk-********cd34" }],
    status: { text: "模型：可用", tone: "success", note: "✓ deepseek-flash 可调用 · 812 ms" },
    checking: false, running: false, notice: { text: "", tone: "" }
  };
  const replies = {
    ready: function () { return state; },
    poll: function () { return state; },
    save: function (values) {
      window.__saved = values || null;
      state.model = (values || {}).model || state.model;
      state.base_url = (values || {}).base_url || state.base_url;
      return { ok: true, state: state };
    },
    test: function (values) { window.__tested = values || null; return { ok: true }; },
    add_keys: function (values) {
      window.__added = (values || {}).text || "";
      state.key_items = [{ index: 1, masked: "sk-********9999" }];
      state.keys = "已加载 3 个 API Key";
      return { ok: true, count: 3, added: 1, items: state.key_items, error: null };
    },
    delete_key: function (values) {
      window.__deleted = (values || {}).index;
      return { ok: true, count: 1, items: [{ index: 1, masked: "sk-********cd34" }], error: null };
    },
    clear_keys: function () { return { ok: true, count: 0, items: [], error: null }; },
    reveal_keys: function () { return { ok: true, keys: ["sk-real-1"], text: "sk-real-1", count: 1 }; }
  };
  const api = {};
  ["ready", "poll", "save", "test", "add_keys", "delete_key", "clear_keys", "reveal_keys"]
    .forEach(function (name) {
      api[name] = function () {
        const args = Array.prototype.slice.call(arguments);
        window.__calls.push(name);
        return Promise.resolve((replies[name] || function () { return { ok: true }; }).apply(null, args));
      };
    });
  window.pywebview = { api: api };
})();
"""

MODEL_PROBE = r"""
window.__errors = [];
window.addEventListener("error", function (e) { window.__errors.push("onerror: " + e.message); });
window.addEventListener("unhandledrejection", function (e) { window.__errors.push("reject: " + e.reason); });
window.confirm = function () { return true; };
function click(id) {
  const el = document.getElementById(id);
  if (!el) { window.__errors.push("缺少元素 " + id); return; }
  if (el.disabled) { window.__errors.push("按钮被禁用（未点到）：" + id); return; }
  try { el.click(); } catch (e) { window.__errors.push(id + " 点击异常：" + e.message); }
}
function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

async function run() {
  await sleep(700);
  window.__filled = document.getElementById("model").value === "deepseek-flash"
    && document.getElementById("base_url").value === "https://api.deepseek.com";
  window.__chips = document.querySelectorAll("#key-chips .key-chip").length;
  window.__privateStart = document.getElementById("api_key_input").type === "password";
  // 改模型 + 保存
  const model = document.getElementById("model");
  model.value = "deepseek-flash";
  model.dispatchEvent(new Event("input", { bubbles: true }));
  const url = document.getElementById("base_url");
  url.value = "https://api.deepseek.com/v1";
  url.dispatchEvent(new Event("input", { bubbles: true }));
  click("btn-save");
  await sleep(400);
  window.__saveOk = !!(window.__saved && window.__saved.model === "deepseek-flash"
    && window.__saved.base_url === "https://api.deepseek.com/v1");
  click("btn-test");
  await sleep(300);
  window.__testOk = !!(window.__tested && window.__tested.model === "deepseek-flash");
  // 恢复默认按钮
  click("btn-defaults");
  await sleep(200);
  window.__defaultsOk = document.getElementById("base_url").value === "https://api.deepseek.com";
  // Key：新增 -> 眼睛 -> 删除 -> 显示 -> 全删
  const keyBox = document.getElementById("api_key_input");
  keyBox.value = "sk-abcdefgh1234 sk-sk-ijklmnop5678";
  await sleep(150);
  click("btn-add-key");
  await sleep(350);
  window.__added = window.__added || "";
  click("btn-toggle-key");
  await sleep(200);
  window.__revealed = keyBox.type === "text";
  click("btn-toggle-key");
  await sleep(200);
  window.__restored = keyBox.type === "password";
  const chipDel = document.querySelector("#key-chips .chip-del");
  if (chipDel) chipDel.click();
  await sleep(300);
  window.__deletedIndex = window.__deleted;
  click("btn-reveal");
  await sleep(300);
  window.__plainShown = (document.getElementById("key-plain") || {}).textContent || "";
  click("btn-reveal");
  await sleep(200);
  click("btn-clear-keys");
  await sleep(300);
}

setTimeout(function () {
  document.title = "SELFTEST-RAN";
  run().then(function () {
    const pre = document.createElement("pre");
    pre.id = "selftest";
    pre.textContent = "CALLS=" + JSON.stringify(window.__calls) +
      "\nERRORS=" + JSON.stringify(window.__errors) +
      "\nFILLED=" + String(window.__filled) +
      "\nCHIPS=" + String(window.__chips) +
      "\nPRIVATE=" + String(window.__privateStart) +
      "\nSAVE_OK=" + String(window.__saveOk) +
      "\nTEST_OK=" + String(window.__testOk) +
      "\nDEFAULTS_OK=" + String(window.__defaultsOk) +
      "\nADDED=" + String(window.__added) +
      "\nREVEALED=" + String(window.__revealed) +
      "\nRESTORED=" + String(window.__restored) +
      "\nDELETED=" + String(window.__deletedIndex) +
      "\nPLAIN=" + String(window.__plainShown);
    document.body.appendChild(pre);
  }, function (e) {
    window.__errors.push("探针异常：" + ((e && (e.stack || e.message)) || String(e)));
  });
}, 2200);
"""

TUNER_MOCK = r"""
(function () {
  const TINY = "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q==";
  window.__calls = [];
  window.__applies = [];
  const state = {
    ok: true,
    device: "设备：1 台在线",
    text: "显示：物理 1260x2720",
    tone: "success",
    detail: "物理 1260x2720",
    physical: { width: 1260, height: 2720, text: "1260x2720" },
    override: { width: 0, height: 0, density: 0, has_override: false },
    requested: { has: false, width: 0, height: 0, actual_width: 0, actual_height: 0,
                 density: 0, verified: false, note: "" },
    settings: { scale: 1.0, width_scale: 1.0, density: 0 },
    limits: { max_scale: 3.0, max_width_scale: 2.5, max_density: 640 },
    thumb_seq: 0, original_seq: 1, capture: {}, running: false
  };
  const replies = {
    ready: function () { return state; },
    poll: function () { return state; },
    apply: function (values) {
      const v = values || {};
      window.__applies.push(v);
      state.override = {
        width: Math.round(1260 * (Number(v.width_scale) || 1)),
        height: Math.round(2720 * (Number(v.scale) || 1)),
        density: Number(v.density) || 0,
        has_override: true
      };
      // 假装设备只接受了「长」的一半：界面必须提醒用户
      state.requested = {
        has: true,
        width: state.override.width, height: state.override.height,
        actual_width: state.override.width, actual_height: Math.round(state.override.height / 2),
        density: state.override.density, verified: true, note: "设备静默忽略了过大的高度"
      };
      state.text = "显示：已加长 " + state.override.width + "x" + state.override.height;
      state.thumb_seq += 1;
      return { ok: true };
    },
    reset: function () {
      state.override = { width: 0, height: 0, density: 0, has_override: false };
      state.requested = { has: false, width: 0, height: 0, actual_width: 0, actual_height: 0,
                          density: 0, verified: false, note: "" };
      state.text = "显示：已复位";
      return { ok: true };
    },
    pop_thumb: function () { return { ok: true, thumb: TINY }; },
    pop_original: function () { return { ok: true, thumb: TINY, image: "/tmp/o.png", size: "1260x2720" }; },
    capture: function () { return { ok: true }; },
    original: function () { return { ok: true }; }
  };
  const api = {};
  ["ready", "poll", "apply", "reset", "pop_thumb", "pop_original", "capture", "original"]
    .forEach(function (name) {
      api[name] = function () {
        const args = Array.prototype.slice.call(arguments);
        window.__calls.push(name);
        return Promise.resolve((replies[name] || function () { return { ok: true }; }).apply(null, args));
      };
    });
  window.pywebview = { api: api };
})();
"""

TUNER_PROBE = r"""
window.__errors = [];
window.addEventListener("error", function (e) { window.__errors.push("onerror: " + e.message); });
window.addEventListener("unhandledrejection", function (e) { window.__errors.push("reject: " + e.reason); });
window.confirm = function () { return true; };
function click(id) {
  const el = document.getElementById(id);
  if (!el) { window.__errors.push("缺少元素 " + id); return; }
  if (el.disabled) { window.__errors.push("按钮被禁用（未点到）：" + id); return; }
  try { el.click(); } catch (e) { window.__errors.push(id + " 点击异常：" + e.message); }
}
function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
function num(id) { return Number((document.getElementById(id) || {}).value); }
function fire(el, type) {
  if (!el) { window.__errors.push("缺少元素（" + type + "）"); return; }
  el.dispatchEvent(new Event(type, { bubbles: true }));
}
function setRange(id, value) {
  const el = document.getElementById(id);
  if (!el) { window.__errors.push("缺少滑块 " + id); return; }
  el.value = String(value);
  fire(el, "input");
}
function setNumber(id, value) {
  const el = document.getElementById(id);
  if (!el) { window.__errors.push("缺少输入框 " + id); return; }
  el.value = String(value);
  fire(el, "input");
}

async function run() {
  await sleep(700);
  window.__initialOk = num("in-width") === 1260 && num("in-height") === 2720 && num("in-density") === 0;
  const rngW = document.getElementById("rng-width");
  const rngH = document.getElementById("rng-height");
  window.__rangeOk = !!rngW && !!rngH
    && Number(rngW.min) === 1260 && Number(rngW.max) === Math.round(1260 * 2.5)
    && Number(rngH.min) === 2720 && Number(rngH.max) === Math.round(2720 * 3);
  // 拖滑块改宽：输入框要跟着变，并且防抖后发 apply
  setRange("rng-width", 1500);
  await sleep(200);
  window.__sliderSync = num("in-width") === 1500;
  await sleep(700);
  const first = window.__applies[window.__applies.length - 1] || {};
  window.__widthApplied = Math.abs((Number(first.width_scale) || 0) - 1500 / 1260) < 0.01;
  // 直接输入改长
  setNumber("in-height", 4080);
  await sleep(900);
  const second = window.__applies[window.__applies.length - 1] || {};
  window.__heightApplied = Math.abs((Number(second.scale) || 0) - 4080 / 2720) < 0.01;
  // 密度
  setNumber("in-density", 420);
  await sleep(900);
  const third = window.__applies[window.__applies.length - 1] || {};
  window.__densityApplied = Number(third.density) === 420;
  // 手机没完全接受 -> 必须提醒
  await sleep(400);
  const warn = (document.getElementById("warn-note") || {}).textContent || "";
  window.__warnShown = warn.indexOf("没有完全接受") >= 0;
  const accepted = (document.getElementById("accepted-note") || {}).textContent || "";
  window.__acceptedShown = accepted.indexOf("手机实际") >= 0;
  // 回到物理尺寸 + 抓图 + 重新取图 + 复位
  click("btn-native");
  await sleep(600);
  window.__nativeOk = num("in-width") === 1260 && num("in-height") === 2720 && num("in-density") === 0;
  click("btn-capture");
  await sleep(200);
  click("btn-reload-original");
  await sleep(400);
  window.__originalImg = !!(document.getElementById("img-original") || {}).src;
  window.__liveImg = !!(document.getElementById("img-live") || {}).src;
  click("btn-reset");
  await sleep(600);
  window.__resetOk = num("in-width") === 1260 && num("in-height") === 2720;
}

setTimeout(function () {
  document.title = "SELFTEST-RAN";
  run().then(function () {
    const pre = document.createElement("pre");
    pre.id = "selftest";
    pre.textContent = "CALLS=" + JSON.stringify(window.__calls) +
      "\nERRORS=" + JSON.stringify(window.__errors) +
      "\nINITIAL_OK=" + String(window.__initialOk) +
      "\nRANGE_OK=" + String(window.__rangeOk) +
      "\nSLIDER_SYNC=" + String(window.__sliderSync) +
      "\nWIDTH_APPLIED=" + String(window.__widthApplied) +
      "\nHEIGHT_APPLIED=" + String(window.__heightApplied) +
      "\nDENSITY_APPLIED=" + String(window.__densityApplied) +
      "\nWARN_SHOWN=" + String(window.__warnShown) +
      "\nACCEPTED_SHOWN=" + String(window.__acceptedShown) +
      "\nNATIVE_OK=" + String(window.__nativeOk) +
      "\nRESET_OK=" + String(window.__resetOk) +
      "\nORIGINAL_IMG=" + String(window.__originalImg) +
      "\nLIVE_IMG=" + String(window.__liveImg);
    document.body.appendChild(pre);
  }, function (e) {
    window.__errors.push("探针异常：" + ((e && (e.stack || e.message)) || String(e)));
  });
}, 2200);
"""

PAGES: list[dict] = [
    {
        "name": "主界面",
        "html": "index.html",
        "js": "app.js",
        "mock": MAIN_MOCK,
        "probe": MAIN_PROBE,
        "expected": [
            "ready", "poll", "preview", "start", "stop", "connect_device", "disconnect_device",
            "refresh_devices", "pick_output_dir", "open_output_dir", "display_status",
            "answer_question", "exit", "open_panel",
        ],
        "flags": [
            ("ENTRIES", "eq", "action,modules,model,tuner", "四个入口没有全部打开对应窗口"),
            ("NOTE_ACTION", "contains", "录制动作", "入口卡片没有显示后端摘要"),
            ("PREVIEW_CALLS", "min", 3, "改动表单 / 开关没有触发「将输出」刷新"),
            ("PATCH_OK", "true", None, "后端回填的三个显示字段没有写进隐藏载体"),
            ("PAYLOAD_OK", "true", None, "payload 里缺 display_mode / page_action / output_fields"),
            ("KEYS_HINT", "contains", "共用", "并发超过 Key 数量时没有提示"),
            ("HORSE", "nonzero", None, "点阵马没有画出来"),
        ],
    },
    {
        "name": "翻页动作窗口",
        "html": "actions.html",
        "js": "actions.js",
        "mock": ACTIONS_MOCK,
        "probe": ACTIONS_PROBE,
        "expected": ["ready", "poll", "apply", "test", "set_reference",
                     "start_record", "stop_record", "clear_recorded"],
        "flags": [
            ("BACKEND_CONNECTED", "true", None, "后端异步注入时窗口没等待，掉进了预览模式"),
            ("INITIAL_CARD", "true", None, "打开时没有按当前设置显示参数卡片"),
            ("TAP_CARD", "true", None, "选「点按」没有只显示点按参数"),
            ("RECORD_CARD", "true", None, "选「录制动作」没有只显示录制卡片"),
            ("APPLY_OK", "true", None, "点按参数没有按三选一发出去"),
            ("SWIPE_OK", "true", None, "滑动参数 / 基准分辨率没有发出去"),
            ("REF_FROM_DEVICE", "true", None, "「设为当前设备」读回的基准分辨率没有显示出来"),
            ("TEST_OK", "true", None, "「试一次」没有带上当前选择"),
            ("RECORD_LABEL_START", "eq", "开始录制", "未录制时按钮不是「开始录制」"),
            ("RECORD_LABEL_DURING", "eq", "结束录制", "开始录制后按钮没有切换成「结束录制」"),
            ("RECORD_LABEL_END", "eq", "开始录制", "结束录制后按钮没有切回「开始录制」"),
            ("STEPS", "min", 1, "录制完成后没有渲染步骤列表"),
            ("STEPS_CLEARED", "true", None, "清除录制后步骤列表没有清空"),
        ],
    },
    {
        "name": "提取模块窗口",
        "html": "modules.html",
        "js": "modules.js",
        "mock": MODULES_MOCK,
        "probe": MODULES_PROBE,
        "expected": ["ready", "poll", "save", "open_display"],
        "flags": [
            ("ROWS", "eq", 4, "打开时没有按清单渲染（3 个本体 + 1 个自定义）"),
            ("BASE_ROWS", "eq", 3, "默认三个本体字段没有渲染出来"),
            ("CHIPS", "min", 2, "「常用模块」快捷按钮没渲染出来"),
            ("ROWS_AFTER_CHIP", "eq", 5, "点常用模块没有加一行"),
            ("ROWS_AFTER_ADD", "eq", 6, "「＋ 添加模块」没有加一行"),
            ("BASE_AFTER_DELETE", "eq", 2, "删掉题号后本体字段数量不对（应该允许删）"),
            ("SAVE_OK", "true", None, "保存的清单不对（本体/自定义分组或开关丢了）"),
            ("OPENED_DISPLAY", "true", None, "「打开屏幕调节」没有调后端"),
        ],
    },
    {
        "name": "模型服务窗口",
        "html": "model.html",
        "js": "model.js",
        "mock": MODEL_MOCK,
        "probe": MODEL_PROBE,
        "expected": ["ready", "poll", "save", "test", "add_keys", "delete_key",
                     "clear_keys", "reveal_keys"],
        "flags": [
            ("FILLED", "true", None, "默认模型 / 地址没有填进输入框（应为 deepseek）"),
            ("CHIPS", "min", 2, "已保存 Key 列表没有渲染"),
            ("PRIVATE", "true", None, "API Key 输入框默认不是隐私模式"),
            ("SAVE_OK", "true", None, "「保存并应用」没有把模型 / 地址发出去"),
            ("TEST_OK", "true", None, "「测试连通性」没有带上当前的模型"),
            ("DEFAULTS_OK", "true", None, "「恢复默认」没有填回 DeepSeek 地址"),
            ("ADDED", "contains", "sk-abcdefgh1234", "新增 Key 的内容不对"),
            ("REVEALED", "true", None, "点眼睛后输入框没有切到明文"),
            ("RESTORED", "true", None, "再点眼睛没有回到隐私输入模式"),
            ("DELETED", "eq", 1, "删除 Key 没有传对序号"),
            ("PLAIN", "contains", "sk-real-1", "「显示已保存」没有拿到明文列表"),
        ],
    },
    {
        "name": "屏幕调节窗口",
        "html": "tuner.html",
        "js": "tuner.js",
        "mock": TUNER_MOCK,
        "probe": TUNER_PROBE,
        "expected": ["ready", "poll", "apply", "reset", "capture", "original",
                     "pop_thumb", "pop_original"],
        "flags": [
            ("INITIAL_OK", "true", None, "打开时没有按物理尺寸初始化宽 / 长 / 密度"),
            ("RANGE_OK", "true", None, "滑块的上下限不是「物理尺寸 ~ 经验上限」"),
            ("SLIDER_SYNC", "true", None, "拖滑块没有同步到数字输入框"),
            ("WIDTH_APPLIED", "true", None, "滑块改宽后 apply 的 width_scale 不对"),
            ("HEIGHT_APPLIED", "true", None, "直接输入改长后 apply 的 scale 不对"),
            ("DENSITY_APPLIED", "true", None, "改密度后没有把 density 发出去"),
            ("WARN_SHOWN", "true", None, "手机没完全接受参数时没有提醒用户"),
            ("ACCEPTED_SHOWN", "true", None, "没有显示「手机实际」尺寸"),
            ("NATIVE_OK", "true", None, "「回到物理尺寸」没有把三个数归位"),
            ("RESET_OK", "true", None, "复位后界面数字没有回到物理尺寸"),
            ("ORIGINAL_IMG", "true", None, "左侧「原本」没有拿到截图"),
            ("LIVE_IMG", "true", None, "右侧「调节后」没有拿到截图"),
        ],
    },
]

PAGES_BY_HTML = {(page["html"], page["js"]) for page in PAGES}


def check_ids() -> list[str]:
    """静态检查 html/js 的 id 一致性（历史上就是这里出的问题）。"""
    problems: list[str] = []
    for page in PAGES:
        html = (ASSETS / page["html"]).read_text(encoding="utf-8")
        js = (ASSETS / page["js"]).read_text(encoding="utf-8")
        html_ids = re.findall(r'id="([A-Za-z0-9_-]+)"', html)
        duplicates = sorted({name for name in html_ids if html_ids.count(name) > 1})
        if duplicates:
            problems.append(f"{page['html']} 里有重复 id：" + "、".join(duplicates))
        used = set(re.findall(r'\$\("([A-Za-z0-9_-]+)"\)', js))
        missing = sorted(used - set(html_ids))
        if missing:
            problems.append(f"{page['js']} 用到但 {page['html']} 里没有的 id：" + "、".join(missing))
    return problems


def build_page(workdir: Path, page: dict) -> Path:
    stem = page["html"].replace(".html", "")
    (workdir / f"{stem}_mock.js").write_text(page["mock"], encoding="utf-8")
    (workdir / f"{stem}_probe.js").write_text(page["probe"], encoding="utf-8")
    source = (ASSETS / page["html"]).read_text(encoding="utf-8")
    needle = f'<script src="{page["js"]}"></script>'
    if needle not in source:
        raise SystemExit(f"{page['html']} 里找不到 {needle}")
    source = source.replace(
        needle,
        f'<script src="{stem}_mock.js"></script>\n{needle}\n<script src="{stem}_probe.js"></script>',
    )
    target = workdir / f"{stem}_selftest.html"
    target.write_text(source, encoding="utf-8")
    return target


def parse_body(body: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in body.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def check_flags(values: dict[str, str], flags: list[tuple]) -> list[str]:
    problems: list[str] = []
    for name, kind, expected, message in flags:
        raw = values.get(name)
        ok = False
        if raw is None:
            problems.append(f"{message}（探针没输出 {name}）")
            continue
        if kind == "true":
            ok = raw.lower() == "true"
        elif kind == "false":
            ok = raw.lower() == "false"
        elif kind == "eq":
            ok = raw == str(expected)
        elif kind == "contains":
            ok = str(expected) in raw
        elif kind == "min":
            try:
                ok = int(float(raw)) >= int(expected)
            except ValueError:
                ok = False
        elif kind == "nonzero":
            ok = raw not in ("", "0") and not raw.startswith("ERR")
        if not ok:
            problems.append(f"{message}（{name}={raw}）")
    return problems


def run_page(browser: str, workdir: Path, page: dict) -> tuple[list[str], dict[str, str], list[str]]:
    target = build_page(workdir, page)
    profile = workdir / f"profile-{page['html'].replace('.html', '')}"
    cmd = [
        browser, "--headless=new", "--disable-gpu", "--no-first-run",
        # 探针里全是 setTimeout/sleep，虚拟时间要够它跑完
        "--virtual-time-budget=30000",
        "--allow-file-access-from-files",
        "--window-size=1180,900",
        "--dump-dom",
        f"--user-data-dir={browser_path(profile)}", browser_url(target),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, errors="replace").stdout
    match = re.search(r'<pre id="selftest">(.*?)</pre>', out or "", re.S)
    if not match:
        return [f"{page['name']}没有输出结果（脚本可能报错了）"], {}, []
    values = parse_body(match.group(1))
    try:
        calls = json.loads(values.get("CALLS", "[]"))
    except ValueError:
        calls = []
    try:
        errors = json.loads(values.get("ERRORS", "[]"))
    except ValueError:
        errors = []
    problems: list[str] = []
    for name in page["expected"]:
        if name not in calls:
            problems.append(f"没有调用 {name}")
    problems.extend(check_flags(values, page["flags"]))
    return problems, values, errors


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
    print("id 一致性检查通过 ✓（5 个页面）")

    workdir = Path(tempfile.gettempdir()) / "drpilot_ui_selftest"
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    for path in sorted(ASSETS.iterdir()):        # 含 horse.js / horse-sheet.png / panel.js
        if path.is_file():
            shutil.copy(path, workdir / path.name)

    failed = False
    for page in PAGES:
        page_problems, values, errors = run_page(browser, workdir, page)
        calls = json.loads(values.get("CALLS", "[]")) if values else []
        print(f"—— {page['name']}：{len(set(calls))} 个接口，"
              f"{len(page['flags'])} 项联动检查")
        if errors:
            failed = True
            for item in errors:
                print(f"  页面异常：{item}", file=sys.stderr)
        if page_problems:
            failed = True
            for item in page_problems:
                print(f"  {item}", file=sys.stderr)
        if not errors and not page_problems:
            print("  通过 ✓")
    if failed:
        print("界面自检未通过 ✗", file=sys.stderr)
        return 1
    print("界面自检通过 ✓（5 个页面全部触达，无脚本异常）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())



