/* 「翻页动作」窗口：点按 / 滑动 / 录制动作 三选一。
   只跟 ActionPanelApi（bridge.action_state / save_action_settings / test_panel_action …）打交道。 */
(function () {
  "use strict";
  const P = window.DrPilotPanel;
  const $ = P.$;

  const KIND_NOTE = {
    tap: "用固定坐标点一下「下一题」：适合点按式 App（坐标是屏幕像素，不随分辨率缩放）。",
    swipe: "用起点 → 终点滑动翻页：坐标按基准分辨率自动缩放到当前机型，换手机不用重填。",
    record: "在手机上做一次真实的「下一题」，程序把触摸序列录下来原样回放：最贴近 App 的实际行为。",
  };

  let kind = "swipe";
  let dirty = false;          // 用户在窗口里改过：轮询别把输入框砸掉
  let refDirty = false;       // 基准分辨率单独记：点「设为当前设备」后要能接住后端读回的值
  let recording = false;
  let running = false;
  let testing = false;        // 「试一次」进行中：按钮先禁用，避免连点两个线程同时翻页

  function setPill(text, tone) {
    const pill = $("pill-device");
    if (!pill) return;
    pill.className = "pill" + (tone ? " " + tone : "");
    pill.querySelector("span").textContent = text || "";
  }

  function collect() {
    return {
      kind: kind,
      tap: { x: P.num("tap_x", 0), y: P.num("tap_y", 0), ms: P.num("tap_ms", 80) },
      swipe: {
        x1: P.num("swipe_x1", 0), y1: P.num("swipe_y1", 0),
        x2: P.num("swipe_x2", 0), y2: P.num("swipe_y2", 0),
        duration: P.num("swipe_duration", 150),
        retries: P.num("swipe_retries", 5),
        retry_wait: P.num("swipe_retry_wait", 1.2),
      },
      reference: ($("swipe_reference").value || "").trim(),
    };
  }

  function renderKind() {
    for (const el of document.querySelectorAll(".choice")) {
      el.classList.toggle("on", el.dataset.kind === kind);
      const radio = el.querySelector("input");
      if (radio) radio.checked = el.dataset.kind === kind;
    }
    for (const [name, id] of [["tap", "card-tap"], ["swipe", "card-swipe"], ["record", "card-record"]]) {
      const card = $(id);
      if (card) card.classList.toggle("hidden", name !== kind);
    }
    $("kind-note").textContent = KIND_NOTE[kind] || "";
  }

  function stepText(s) {
    if (!s) return "";
    if (s.kind === "tap") {
      return "点击 (" + s.x + "," + s.y + ")" + (s.duration_ms >= 80 ? "，按住 " + s.duration_ms + "ms" : "");
    }
    if (s.kind === "swipe") {
      return "滑动 (" + s.x + "," + s.y + ") → (" + s.x2 + "," + s.y2 + ")，" + s.duration_ms + "ms";
    }
    if (s.kind === "path") return "精确滑动 " + ((s.points || []).length) + " 个点，" + (s.duration_ms || 0) + "ms";
    if (s.kind === "key") return "按键 " + s.keycode;
    if (s.kind === "wait") return "等待 " + s.wait_ms + "ms";
    return String(s.kind || "");
  }

  function renderSteps(steps) {
    const list = $("action-steps");
    list.innerHTML = "";
    (steps || []).forEach((s) => {
      const li = document.createElement("li");
      li.textContent = stepText(s);
      list.appendChild(li);
    });
  }

  function applyState(s) {
    if (!s) return;
    setPill(s.device || "", "");
    recording = !!s.recording;
    running = !!s.running;
    testing = !!s.testing;
    if (!dirty) {
      kind = s.kind || "swipe";
      renderKind();
      const tap = s.tap || {};
      const swipe = s.swipe || {};
      const focus = document.activeElement;
      const write = (id, value) => {
        const el = $(id);
        if (el && el !== focus) el.value = value === 0 || value ? String(value) : "";
      };
      write("tap_x", tap.x || "");
      write("tap_y", tap.y || "");
      write("tap_ms", tap.ms === undefined ? "" : tap.ms);
      write("swipe_x1", swipe.x1 || "");
      write("swipe_y1", swipe.y1 || "");
      write("swipe_x2", swipe.x2 || "");
      write("swipe_y2", swipe.y2 || "");
      write("swipe_duration", swipe.duration || "");
      write("swipe_retries", swipe.retries === undefined ? "" : swipe.retries);
      write("swipe_retry_wait", swipe.retry_wait === undefined ? "" : swipe.retry_wait);
    }
    if (!refDirty && $("swipe_reference") !== document.activeElement) {
      $("swipe_reference").value = s.reference || "";
    }
    $("summary").textContent = s.summary || "";
    const record = s.record || {};
    $("record-note").textContent = record.note || (s.recording ? "录制中…" : "未录制");
    renderSteps(record.steps);

    const notice = s.notice || {};
    const box = $("notice");
    box.textContent = notice.text || (recording ? "录制中…请在手机上完成一次「下一题」动作" : "已就绪");
    box.dataset.tone = notice.tone || "";
    box.style.color = notice.tone === "err" ? "var(--red)"
      : (notice.tone === "warn" ? "var(--amber)" : (notice.tone === "ok" ? "var(--green)" : "var(--muted)"));

    // 录制用一个按钮切换：开始录制 ⇄ 结束录制
    const toggle = $("btn-record-toggle");
    if (toggle) {
      toggle.textContent = recording ? "结束录制" : "开始录制";
      toggle.classList.toggle("danger", recording);
      toggle.disabled = running;      // 录制中仍可点它结束
    }
    $("btn-record-clear").disabled = recording || running || !(record.steps || []).length;
    $("btn-test").textContent = testing ? "测试中…" : "试一次";
    $("btn-test").disabled = recording || running || testing;
    $("btn-apply").disabled = recording || running;
    $("btn-set-reference").disabled = running;
  }

  function bind() {
    for (const el of document.querySelectorAll(".choice")) {
      el.onclick = () => { kind = el.dataset.kind; dirty = true; renderKind(); };
    }
    for (const id of ["tap_x", "tap_y", "tap_ms", "swipe_x1", "swipe_y1", "swipe_x2", "swipe_y2",
                      "swipe_duration", "swipe_retries", "swipe_retry_wait"]) {
      const el = $(id);
      if (el) el.addEventListener("input", () => { dirty = true; });
    }
    $("swipe_reference").addEventListener("input", () => { refDirty = true; });
    $("btn-apply").onclick = async () => {
      const res = await P.call("apply", collect());
      if (!res) return;
      if (res.ok === false) { P.toast(res.error || "应用失败", "err"); return; }
      dirty = false;
      if (res.state) applyState(res.state);
      P.toast("已应用：" + ((res.state && res.state.summary) || kind), "ok");
    };
    $("btn-test").onclick = async () => {
      const res = await P.call("test", collect());
      if (res && res.ok === false) { P.toast(res.error || "试一次失败", "err"); return; }
      dirty = false;
      P.toast("正在试一次…（结果看下方状态）", "");
    };
    $("btn-set-reference").onclick = async () => {
      const res = await P.call("set_reference", collect());
      if (res && res.ok === false) P.toast(res.error || "读取分辨率失败", "err");
      else P.toast("正在读取设备分辨率…", "");
      refDirty = false;      // 让后端读回的基准分辨率能显示出来
    };
    $("btn-record-toggle").onclick = async () => {
      if (recording) {
        await P.call("stop_record");
        P.toast("正在结束录制…", "");
        return;
      }
      const res = await P.call("start_record", collect());
      if (res && res.ok === false) { P.toast(res.error || "开始录制失败", "err"); return; }
      dirty = false;
      P.toast("开始录制：请在手机上做一次「下一题」", "");
    };
    $("btn-record-clear").onclick = async () => {
      if (!window.confirm("确定清除已录制的动作吗？\n（点按 / 滑动参数不受影响）")) return;
      const res = await P.call("clear_recorded");
      if (res && res.state) applyState(res.state);
      P.toast("已清除录制的动作", "ok");
    };
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") window.close(); });
  }

  function demo() {
    setPill("设备：示例（预览模式）", "");
    kind = "record";
    renderKind();
    $("tap_x").value = "540"; $("tap_y").value = "2280"; $("tap_ms").value = "80";
    $("swipe_x1").value = "1060"; $("swipe_y1").value = "553";
    $("swipe_x2").value = "270"; $("swipe_y2").value = "551";
    $("swipe_duration").value = "150"; $("swipe_retries").value = "5"; $("swipe_retry_wait").value = "1.2";
    $("summary").textContent = "示例：录制动作 · 3 步";
    $("record-note").textContent = "示例：已录制 3 步";
    renderSteps([
      { kind: "tap", x: 540, y: 2280, duration_ms: 80 },
      { kind: "wait", wait_ms: 300 },
      { kind: "swipe", x: 960, y: 1200, x2: 240, y2: 1200, duration_ms: 260 },
    ]);
    $("notice").textContent = "示例：预览模式不会真的动手机";
    dirty = true;                      // 预览模式别被轮询覆盖
  }

  function boot() {
    bind();
    renderKind();
    if (!P.backend()) { demo(); return; }
    P.waitForBackend(async () => {
      const state = await P.call("ready");
      if (state) applyState(state);
      P.startPoll(applyState, 250);
    }, Date.now() + 2500);
  }

  document.addEventListener("DOMContentLoaded", boot);
  window.addEventListener("error", (event) => P.toast("界面脚本出错：" + (event.message || ""), "err"));
})();
