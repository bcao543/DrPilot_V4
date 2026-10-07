/* 「屏幕调节」窗口：宽 / 长 / 密度三个数，滑块 + 直接输入（不提供拖拽改显示）。
   只跟 DisplayTunerApi（ready/poll/apply/reset/capture/original/pop_thumb/pop_original）打交道。
   手机可能不接受某些参数：应用后拿「请求 vs 手机实际」对比，不一致就提醒用户调小。 */
(function () {
  "use strict";
  const P = window.DrPilotPanel;
  const $ = P.$;

  const state = {
    physical: { width: 0, height: 0 },
    width: 1260,
    height: 2720,
    density: 0,
    limits: { maxScale: 3, maxWidthScale: 2.5, maxDensity: 640 },
    accepted: { width: 0, height: 0, density: 0, has: false },
    requested: { has: false, width: 0, height: 0, actualWidth: 0, actualHeight: 0, density: 0, verified: false },
    thumbSeq: 0,
    originalSeq: 0,
  };

  let metrics = { kx: 1, ky: 1 };
  let initialised = false;
  let dragging = false;          // 本地正在拖滑块：轮询别把数值砸回去
  let lastSent = "";

  function clamp(value, low, high, fallback) {
    const num = Number(value);
    if (!isFinite(num)) return fallback;
    return Math.min(high, Math.max(low, num));
  }

  function maxWidth() { return Math.round((state.physical.width || 1080) * state.limits.maxWidthScale); }
  function maxHeight() { return Math.round((state.physical.height || 1920) * state.limits.maxScale); }

  /* ---------------- 画：两个矩形 + 三个数 ---------------- */
  function computeMetrics() {
    const stage = $("stage-b");
    const pad = 30;
    const availW = Math.max(60, stage.clientWidth - pad);
    const availH = Math.max(60, stage.clientHeight - pad);
    metrics.kx = availW / Math.max(1, maxWidth());
    metrics.ky = availH / Math.max(1, maxHeight());
  }

  function render() {
    const physical = state.physical;
    if (physical.width) {
      $("phone-a").style.width = Math.round(physical.width * metrics.kx) + "px";
      $("phone-a").style.height = Math.round(physical.height * metrics.ky) + "px";
    }
    $("phone-b").style.width = Math.round(state.width * metrics.kx) + "px";
    $("phone-b").style.height = Math.round(state.height * metrics.ky) + "px";
    if (document.activeElement !== $("in-width")) $("in-width").value = String(state.width);
    if (document.activeElement !== $("in-height")) $("in-height").value = String(state.height);
    if (document.activeElement !== $("in-density")) $("in-density").value = String(state.density);
    syncSlider("rng-width", state.width, physical.width, maxWidth());
    syncSlider("rng-height", state.height, physical.height, maxHeight());
    syncSlider("rng-density", state.density, 0, state.limits.maxDensity);
    $("target-note").textContent = state.width + "x" + state.height
      + (state.density ? " @" + state.density : "");
    const accepted = state.accepted;
    $("accepted-note").textContent = accepted.has
      ? ("手机实际 " + accepted.width + "x" + accepted.height
         + (accepted.density ? " @" + accepted.density : ""))
      : (physical.width ? "手机实际：物理尺寸（未改）" : "");
    renderWarn();
  }

  function syncSlider(id, value, low, high) {
    const el = $(id);
    if (!el) return;
    el.min = String(Math.max(0, Math.round(low)));
    el.max = String(Math.max(Math.round(low) + 1, Math.round(high)));
    if (document.activeElement !== el) el.value = String(value);
  }

  /* 请求 vs 手机实际：不一致就是设备静默忽略了参数 */
  function renderWarn() {
    const box = $("warn-note");
    const req = state.requested;
    const note = $("request-note");
    const small = "手机有可能不接受某些参数（超过上限的尺寸会被系统静默忽略，只接受一部分）。"
      + "每次应用后请看「手机实际」：和「请求」不一致就说明设备没接受，把数值调小再试。";
    if (!req.has) {
      box.className = "note warn";
      box.textContent = "提醒：" + small;
      note.textContent = "";
      return;
    }
    const same = req.width === req.actualWidth && req.height === req.actualHeight;
    if (same) {
      box.className = "note ok";
      box.textContent = "已接受：" + req.actualWidth + "x" + req.actualHeight
        + (req.density ? " @" + req.density : "") + "（手机已按这组参数显示）";
    } else {
      box.className = "note warn";
      box.textContent = "手机没有完全接受这组参数：请求 " + req.width + "x" + req.height
        + "，实际 " + req.actualWidth + "x" + req.actualHeight
        + "。请把宽 / 长调小一点再试（" + (req.note || "设备会静默忽略过大的尺寸") + "）。";
    }
    note.textContent = "请求 " + req.width + "x" + req.height
      + (req.density ? " @" + req.density : "")
      + " → 手机实际 " + req.actualWidth + "x" + req.actualHeight;
  }

  function setSize(width, height) {
    if (state.physical.width) {
      state.width = Math.round(clamp(width, state.physical.width, maxWidth(), state.width));
      state.height = Math.round(clamp(height, state.physical.height, maxHeight(), state.height));
    } else {
      state.width = Math.round(clamp(width, 1, 10000, state.width));
      state.height = Math.round(clamp(height, 1, 10000, state.height));
    }
    render();
  }

  /* ---------------- 应用到手机（停手 0.4s 再发，中间值丢掉） ---------------- */
  const scheduleApply = P.debounce(applyNow, 400);

  async function applyNow() {
    if (!state.physical.width) return;
    if (!P.backend()) { demoStatus(); return; }
    const signature = state.width + "x" + state.height + "@" + state.density;
    if (signature === lastSent) return;
    lastSent = signature;
    $("status").textContent = "显示：正在应用…";
    const res = await P.call("apply", {
      scale: state.height / state.physical.height,
      width_scale: state.width / state.physical.width,
      density: state.density,
    });
    if (res && res.ok === false) {
      lastSent = "";
      P.toast(res.error || "应用失败", "err");
    }
  }

  function demoStatus() {
    $("status").textContent = "显示：示例（预览模式不会真的改手机）";
  }

  /* ---------------- 轮询：真机状态 + 两张图 ---------------- */
  function applyState(s) {
    if (s.physical && s.physical.width) {
      state.physical = { width: s.physical.width, height: s.physical.height };
    }
    if (s.limits) {
      state.limits.maxScale = Number(s.limits.max_scale) || state.limits.maxScale;
      state.limits.maxWidthScale = Number(s.limits.max_width_scale) || state.limits.maxWidthScale;
      state.limits.maxDensity = Number(s.limits.max_density) || state.limits.maxDensity;
    }
    const ov = s.override || {};
    state.accepted = {
      width: ov.width || 0,
      height: ov.height || 0,
      density: ov.density || 0,
      has: !!ov.has_override,
    };
    const req = s.requested || {};
    state.requested = {
      has: !!req.has,
      width: req.width || 0,
      height: req.height || 0,
      actualWidth: req.actual_width || 0,
      actualHeight: req.actual_height || 0,
      density: req.density || 0,
      verified: !!req.verified,
      note: req.note || "",
    };
    if (!initialised && state.physical.width) {
      initialised = true;
      if (state.accepted.has && state.accepted.width) {
        // 手机上已经有覆盖：直接照真机显示，别让界面和手机对不上
        state.width = state.accepted.width;
        state.height = state.accepted.height;
        state.density = state.accepted.density || 0;
      } else {
        const settings = s.settings || {};
        state.width = Math.round(state.physical.width * (settings.width_scale || 1));
        state.height = Math.round(state.physical.height * (settings.scale || 1));
        state.density = Number(settings.density) || 0;
      }
      computeMetrics();
      $("original-note").textContent = "物理 " + state.physical.width + "x" + state.physical.height;
    }
    if (!dragging && initialised && !state.accepted.has && s.settings) {
      // 手机上没覆盖、界面也没在改：跟着配置里的三个数显示
    }
    if (s.text) {
      const tone = s.tone === "danger" ? "var(--red)" : (s.tone === "warning" ? "#a2680f" : "var(--muted)");
      const status = $("status");
      status.textContent = s.text + (s.running ? "（提取运行中，暂时改不了）" : "");
      status.style.color = tone;
      status.title = s.detail || "";
    }
    if (s.device) $("pill-device-text").textContent = s.device;
    render();
    const thumbSeq = Number(s.thumb_seq || 0);
    if (thumbSeq && thumbSeq !== state.thumbSeq) {
      state.thumbSeq = thumbSeq;
      loadThumb();
    }
    const originalSeq = Number(s.original_seq || 0);
    if (originalSeq && originalSeq !== state.originalSeq) {
      state.originalSeq = originalSeq;
      loadOriginal();
    }
  }

  async function loadThumb() {
    const res = await P.call("pop_thumb");
    if (res && res.thumb) $("img-live").src = res.thumb;
  }

  async function loadOriginal() {
    const res = await P.call("pop_original");
    if (!res || !res.thumb) return;
    $("img-original").src = res.thumb;
    if (res.size) $("original-note").textContent = "原本 " + res.size;
  }

  /* ---------------- 控件 ---------------- */
  function bindRange(rangeId, apply) {
    const el = $(rangeId);
    el.addEventListener("input", () => {
      dragging = true;
      apply(Number(el.value));
      scheduleApply();
    });
    el.addEventListener("change", () => {
      dragging = false;
      apply(Number(el.value));
      render();
      applyNow();
    });
  }

  function bindNumber(id, apply) {
    const el = $(id);
    el.addEventListener("input", () => {
      if (String(el.value).trim() === "") return;      // 还没输完
      apply(Number(el.value));
      scheduleApply();
    });
    el.addEventListener("change", () => {
      apply(Number(el.value));
      render();
      applyNow();
    });
    el.addEventListener("blur", () => { render(); });
  }

  function bind() {
    window.addEventListener("resize", () => { computeMetrics(); render(); });
    bindRange("rng-width", (value) => setSize(value, state.height));
    bindRange("rng-height", (value) => setSize(state.width, value));
    bindRange("rng-density", (value) => {
      state.density = Math.round(clamp(value, 0, state.limits.maxDensity, state.density));
      render();
    });
    bindNumber("in-width", (value) => setSize(value, state.height));
    bindNumber("in-height", (value) => setSize(state.width, value));
    bindNumber("in-density", (value) => {
      state.density = Math.round(clamp(value, 0, state.limits.maxDensity, state.density));
      render();
    });

    $("btn-apply").onclick = () => { lastSent = ""; applyNow(); };
    $("btn-native").onclick = () => {
      state.density = 0;
      setSize(state.physical.width, state.physical.height);
      lastSent = "";
      applyNow();
      P.toast("已回到物理尺寸", "ok");
    };
    $("btn-reset").onclick = async () => {
      if (!window.confirm("确定复位手机的显示设置吗？\n（wm size / density / scaling 都会恢复默认）")) return;
      const res = await P.call("reset");
      if (res && res.ok === false) { P.toast(res.error || "复位失败", "err"); return; }
      state.accepted = { width: 0, height: 0, density: 0, has: false };
      state.requested = { has: false, width: 0, height: 0, actualWidth: 0, actualHeight: 0, density: 0 };
      state.density = 0;
      lastSent = "";
      setSize(state.physical.width || state.width, state.physical.height || state.height);
      $("status").textContent = "显示：正在复位…";
      P.toast("正在复位手机显示设置…", "");
    };
    $("btn-capture").onclick = async () => {
      const res = await P.call("capture");
      if (res && res.ok === false) P.toast(res.error || "抓图失败", "err");
      else P.toast("正在抓全尺寸截图并打开…", "");
    };
    $("btn-reload-original").onclick = async () => {
      const res = await P.call("original");
      if (res && res.ok === false) P.toast(res.error || "取图失败", "err");
      else P.toast("正在重新抓「原本」…", "");
    };
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") window.close(); });
  }

  function demo() {
    state.physical = { width: 1260, height: 2720 };
    state.width = 1260;
    state.height = 5440;
    state.density = 0;
    state.requested = { has: true, width: 1260, height: 5440, actualWidth: 1260, actualHeight: 4760,
                        density: 0, verified: false, note: "设备静默忽略了过大的高度，已自动降级" };
    initialised = true;
    computeMetrics();
    $("original-note").textContent = "原本 1260x2720（示例）";
    $("status").textContent = "显示：示例（预览模式不会真的改手机）";
    render();
  }

  function boot() {
    bind();
    computeMetrics();
    if (!P.backend()) { demo(); return; }
    P.waitForBackend(async () => {
      const s = await P.call("ready");
      if (s) applyState(s);
      P.startPoll(applyState, 250);
    }, Date.now() + 2500);
  }

  document.addEventListener("DOMContentLoaded", boot);
  window.addEventListener("error", (event) => P.toast("界面脚本出错：" + (event.message || ""), "err"));
})();
