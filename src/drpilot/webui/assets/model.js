/* 「模型服务」窗口：模型 ID / API 地址 / API Key / 连通性。
   只跟 ModelPanelApi（bridge.model_settings / save_model_settings / test_model / *_keys）打交道。 */
(function () {
  "use strict";
  const P = window.DrPilotPanel;
  const $ = P.$;

  let dirty = false;
  let revealing = false;
  let keysSignature = "";

  function setPill(text, tone) {
    const pill = $("pill-model");
    if (!pill) return;
    pill.className = "pill" + (tone ? " " + tone : "");
    pill.querySelector("span").textContent = text || "";
  }

  /* ---- 输入框里的 Key：隐私模式 + 一次性新增 ---- */
  function keyInputText() {
    const el = $("api_key_input");
    let text = (el.value || "").trim();
    if (!text) return "";
    // 单行输入框粘贴多行时换行会被浏览器压掉；没有 "=" 时统一按空白切成多个 Key
    if (text.indexOf("=") < 0) text = text.replace(/\s+/g, ",");
    return text;
  }

  function setKeyVisible(on) {
    const el = $("api_key_input");
    const btn = $("btn-toggle-key");
    el.type = on ? "text" : "password";
    btn.classList.toggle("active", !!on);
    btn.title = on ? "当前是明文：点一下回到隐私输入模式（圆点）" : "隐私输入模式：点一下临时显示输入内容";
    $("key-eye").textContent = on ? "🙈" : "👁";
  }

  function clearKeyInput() {
    $("api_key_input").value = "";
    setKeyVisible(false);
  }

  /* ---- 已保存列表（只渲染脱敏值） ---- */
  function renderKeys(items) {
    const box = $("key-chips");
    const list = items || [];
    const clearBtn = $("btn-clear-keys");
    clearBtn.classList.toggle("hidden", !list.length);
    $("btn-reveal").classList.toggle("hidden", !list.length);
    const signature = JSON.stringify(list);
    if (signature === keysSignature) return;   // 轮询很勤：没变就别重建 DOM
    keysSignature = signature;
    box.innerHTML = "";
    list.forEach((item) => {
      const chip = document.createElement("span");
      chip.className = "key-chip";
      const label = document.createElement("span");
      label.textContent = item.masked || ("#" + item.index);
      const del = document.createElement("button");
      del.type = "button";
      del.className = "chip-del";
      del.title = "删除这个 API Key";
      del.textContent = "✕";
      del.onclick = () => deleteKey(item.index, label.textContent);
      chip.appendChild(label);
      chip.appendChild(del);
      box.appendChild(chip);
    });
    if (revealing) { revealing = false; $("key-plain").classList.add("hidden"); }
  }

  async function addKey() {
    const text = keyInputText();
    if (!text) {
      P.toast("请先填写要新增的 API Key", "err");
      $("api_key_input").focus();
      return;
    }
    const res = await P.call("add_keys", { text: text });
    if (!res) return;
    if (res.ok === false) { P.toast(res.error || "新增失败", "err"); return; }
    clearKeyInput();
    keysSignature = "";
    P.toast(res.added ? "已新增 " + res.added + " 个 API Key（共 " + res.count + " 个）"
                      : "这些 Key 已经保存过了，没有重复写入", "ok");
  }

  async function deleteKey(index, label) {
    if (!window.confirm("确定删除 API Key " + label + " 吗？\n删除后运行将不再使用这个 Key。")) return;
    const res = await P.call("delete_key", { index: index });
    if (!res) return;
    if (res.ok === false) { P.toast(res.error || "删除失败", "err"); return; }
    keysSignature = "";
    P.toast("已删除 " + label, "ok");
  }

  async function clearKeys() {
    if (!window.confirm("确定删除全部 API Key 吗？\n删除后需要重新新增才能开始提取。")) return;
    const res = await P.call("clear_keys");
    if (!res) return;
    if (res.ok === false) { P.toast(res.error || "删除失败", "err"); return; }
    clearKeyInput();
    keysSignature = "";
    P.toast("已删除全部 API Key", "ok");
  }

  async function toggleReveal() {
    if (revealing) {
      revealing = false;
      $("key-plain").classList.add("hidden");
      $("btn-reveal").textContent = "显示已保存";
      return;
    }
    const res = await P.call("reveal_keys");
    if (!res || res.ok === false) { P.toast((res && res.error) || "没有可显示的 Key", "err"); return; }
    revealing = true;
    const box = $("key-plain");
    box.textContent = res.text || "";
    box.classList.remove("hidden");
    $("btn-reveal").textContent = "隐藏";
    P.toast("明文只在本窗口显示，不会写进日志 / 配置", "warn");
  }

  function applyState(s) {
    if (!s) return;
    if (!dirty) {
      const focus = document.activeElement;
      if ($("model") !== focus) $("model").value = s.model || "";
      if ($("base_url") !== focus) $("base_url").value = s.base_url || "";
    }
    const status = s.status || {};
    setPill(status.text || "模型：未测试", status.tone || "");
    $("model-note").textContent = status.note || "开始提取前会自动测试模型连通性，模型下架时会立刻停下。";
    $("model-note").style.color = status.tone === "danger" ? "var(--red)" : "var(--ink-soft)";
    $("key-info").textContent = s.keys || "";
    renderKeys(s.key_items);
    const notice = s.notice || {};
    const box = $("notice");
    box.textContent = notice.text || "Key 明文只写进 .env；日志、配置、界面列表一律脱敏。";
    box.style.color = notice.tone === "err" ? "var(--red)"
      : (notice.tone === "ok" ? "var(--green)" : "var(--muted)");
    $("btn-test").disabled = !!s.checking;
    $("btn-test").textContent = s.checking ? "测试中…" : "测试连通性";
  }

  function bind() {
    $("btn-add-key").onclick = addKey;
    $("btn-toggle-key").onclick = () => setKeyVisible($("api_key_input").type === "password");
    $("api_key_input").addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); addKey(); }   // 粘贴后回车即新增
    });
    $("btn-clear-keys").onclick = clearKeys;
    $("btn-reveal").onclick = toggleReveal;
    $("btn-defaults").onclick = () => {
      $("model").value = "deepseek-flash";
      $("base_url").value = "https://api.deepseek.com";
      dirty = true;
      P.toast("已填回默认值，点「保存并应用」生效", "");
    };
    for (const id of ["model", "base_url"]) {
      $(id).addEventListener("input", () => { dirty = true; });
    }
    $("btn-save").onclick = async () => {
      const res = await P.call("save", { model: $("model").value.trim(), base_url: $("base_url").value.trim() });
      if (!res) return;
      if (res.ok === false) { P.toast(res.error || "保存失败", "err"); return; }
      dirty = false;
      if (res.state) applyState(res.state);
      P.toast("已保存模型服务设置", "ok");
    };
    $("btn-test").onclick = async () => {
      const res = await P.call("test", { model: $("model").value.trim(), base_url: $("base_url").value.trim() });
      if (res && res.ok === false) P.toast(res.error || "测试失败", "err");
      else P.toast("正在测试模型连通性…", "");
    };
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") window.close(); });
  }

  function demo() {
    setPill("模型：可用", "success");
    $("model").value = "deepseek-flash";
    $("base_url").value = "https://api.deepseek.com";
    $("model-note").textContent = "✓ 示例：模型可调用 · 812 ms";
    $("key-info").textContent = "示例：已加载 3 个 API Key（.env 3 个）";
    renderKeys([
      { index: 1, masked: "sk-********ab12" },
      { index: 2, masked: "sk-********cd34" },
      { index: 3, masked: "sk-********ef56" },
    ]);
    $("notice").textContent = "示例：预览模式（不会真的读写 .env）";
    dirty = true;
  }

  function boot() {
    bind();
    setKeyVisible(false);
    if (!P.backend()) { demo(); return; }
    P.waitForBackend(async () => {
      const state = await P.call("ready");
      if (state) applyState(state);
      P.startPoll(applyState, 400);
    }, Date.now() + 2500);
  }

  document.addEventListener("DOMContentLoaded", boot);
  window.addEventListener("error", (event) => P.toast("界面脚本出错：" + (event.message || ""), "err"));
})();
