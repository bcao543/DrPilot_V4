/* 入口窗口（翻页动作 / 提取模块 / 模型服务 / 屏幕调节）共用的小工具。
   每个窗口只跟自己的瘦 API 打交道（见 src/drpilot/webui/panels.py 与 tuner.py），
   这里只做 DOM / 轮询 / 提示这些前端杂事。 */
window.DrPilotPanel = (function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const backend = () => (window.pywebview && window.pywebview.api) || null;
  const preview = () => !backend();     // 浏览器直开 = 界面预览模式

  function toast(text, tone) {
    const box = $("toasts");
    if (!box) return;
    const el = document.createElement("div");
    el.className = "toast " + (tone || "");
    el.textContent = text;
    box.appendChild(el);
    setTimeout(() => el.remove(), 4200);
  }

  async function call(name, ...args) {
    const api = backend();
    if (!api) {
      toast("界面预览模式：未连接 Python 后端", "err");
      return null;
    }
    try {
      return await api[name](...args);
    } catch (err) {
      toast(String(err), "err");
      return null;
    }
  }

  /* 轮询：每个窗口自己的 pollFn(state) 负责渲染 */
  function startPoll(pollFn, interval) {
    async function tick() {
      const api = backend();
      if (api) {
        try {
          const state = await api.poll();
          if (state) pollFn(state);
        } catch (err) {
          console.error(err);
        }
      }
      setTimeout(tick, interval || 200);
    }
    tick();
  }

  function waitForBackend(boot, deadline) {
    if (backend() || Date.now() > (deadline || 0)) {
      boot();
      return;
    }
    setTimeout(() => waitForBackend(boot, deadline), 80);
  }

  function num(id, fallback) {
    const el = $(id);
    const value = Number(el ? el.value : fallback);
    return isFinite(value) ? value : fallback;
  }

  function setValue(id, text) {
    const el = $(id);
    if (el) el.value = text === null || text === undefined ? "" : String(text);
  }

  function setText(id, text, tone) {
    const el = $(id);
    if (!el) return;
    el.textContent = text === null || text === undefined ? "" : String(text);
    if (tone) el.dataset.tone = tone;
  }

  /* 复选框开关：包一层，省得每个窗口都写一遍 */
  function bindSwitch(id, onChange) {
    const el = $(id);
    if (!el) return null;
    if (onChange) el.addEventListener("change", () => onChange(!!el.checked));
    return el;
  }

  function debounce(fn, delay) {
    let timer = null;
    return function () {
      const args = arguments;
      clearTimeout(timer);
      timer = setTimeout(() => fn.apply(null, args), delay);
    };
  }

  return {
    $, backend, preview, toast, call, startPoll, waitForBackend,
    num, setValue, setText, bindSwitch, debounce,
  };
})();
