/* DrPilot v4 主页面逻辑：只跟 Python 桥（window.pywebview.api）打交道，
   轮询拿日志/进度，所有耗时操作都在 Python 侧后台线程里跑。

   主页面只保留「每次都要改」的三块（任务范围 / 手机连接 / 识别与输出）：
   翻页动作、提取模块、模型服务、屏幕调节都在入口卡片打开的独立窗口里配，
   改完由桥回填到下面的 hidden 载体（见 index.html 末尾），
   所以 payload() 依旧是一份完整表单。

   直接用浏览器打开时自动进入「界面预览」模式，方便调版面。 */
(function () {
  "use strict";

  // 主页面上的输入（含隐藏载体）：payload() 会把它们原样交给后端
  const TEXT_FIELDS = [
    "page_from", "page_to", "textbook", "chapter", "chapter_no", "chapter_total",
    "output_dir", "batch_size", "workers", "wait_ms", "max_tokens",
    "device_ip", "device_port",
    // 下面这些由四个设置窗口回填（hidden input）
    "model", "base_url", "page_action", "next_action",
    "action_tap_x", "action_tap_y", "action_tap_ms",
    "swipe_x1", "swipe_y1", "swipe_x2", "swipe_y2",
    "swipe_duration", "swipe_retries", "swipe_retry_wait", "swipe_reference",
    "extract_modules", "output_fields",
    "display_scale", "display_width_scale", "display_density",
  ];
  const FLAGS = {
    "opt-md": "generate_markdown",
    "opt-index": "generate_index",
    "opt-preflight": "preflight",
    "opt-check-model": "check_model",
    // 界面上不暴露的隐藏开关：只用来原样带回命令行设过的 display_scaling
    "opt-display-scaling": "display_scaling",
  };
  // 入口卡片 -> 状态摘要字段（tuner 的摘要来自 display 状态）
  const ENTRIES = [
    ["action", "entry-action", "note-action"],
    ["modules", "entry-modules", "note-modules"],
    ["model", "entry-model", "note-model"],
    ["tuner", "entry-tuner", "note-tuner", "display"],
  ];

  const $ = (id) => document.getElementById(id);
  const backend = () => (window.pywebview && window.pywebview.api) || null;

  let lastQuestion = null;
  let busySince = 0;
  let modalOpen = false;
  let previewTimer = null;
  let previewSeq = 0;        // 「将输出」请求序号，防止旧结果覆盖新结果
  let lastState = null;      // 最近一次后端状态，供「并发」提示等即时重算
  let wasRunning = false;    // 上一帧是否在跑（用来在跑完时刷新手机显示状态）
  let horse = null;          // 右下角的点阵马（背景动效）
  let horseRunning = null;

  /* ---------------- 表单 ---------------- */
  const missingReported = new Set();

  function control(id) {
    const el = $(id);
    if (!el && !missingReported.has(id)) {
      missingReported.add(id);
      reportError("界面上找不到控件 #" + id);
    }
    return el;
  }

  function payload() {
    const data = {};
    for (const id of TEXT_FIELDS) {
      const el = control(id);
      data[id] = el && el.value != null ? String(el.value).trim() : "";
    }
    for (const [id, key] of Object.entries(FLAGS)) {
      const el = control(id);
      data[key] = el ? !!el.checked : true;
    }
    // 加长主屏：三个隐藏字段里只要有一个不是默认值，就算要动手机显示设置
    // （长/宽/密度都能单独用；屏幕调节窗口会把这里的值回填好）
    data.display_mode = displayWanted(displayValues());
    return data;
  }

  function fill(data) {
    if (!data) return;
    for (const id of TEXT_FIELDS) {
      const el = control(id);
      if (el && data[id] !== undefined && data[id] !== null) el.value = data[id];
    }
    for (const [id, key] of Object.entries(FLAGS)) {
      const el = control(id);
      if (el && data[key] !== undefined) el.checked = !!data[key];
    }
    // 配置里 display_mode 是关的（或没配过），就把三个字段归零：
    // 否则配置里那个默认的 2.0 倍会在「开始提取」时被当成用户要加长
    if (!data.display_mode) {
      syncDisplayFields({ scale: 1, widthScale: 1, density: 0 });
    }
  }

  /* ---------------- 渲染 ---------------- */
  function setPill(el, text, tone) {
    el.className = "pill" + (tone ? " " + tone : "");
    el.querySelector("span").textContent = text;
  }

  function appendLogs(lines) {
    if (!lines || !lines.length) return;
    const box = $("log");
    const stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 24;
    for (const line of lines) {
      const div = document.createElement("div");
      div.textContent = line.text;
      if (line.tone) div.className = line.tone;
      box.appendChild(div);
    }
    while (box.childElementCount > 800) box.removeChild(box.firstChild);
    if (stick) box.scrollTop = box.scrollHeight;
  }

  function renderWorkers(list) {
    const box = $("worker-chips");
    const data = list || [];
    if (box.dataset.count !== String(data.length)) {
      box.innerHTML = "";
      for (const w of data) {
        const el = document.createElement("div");
        el.className = "worker";
        el.innerHTML = '<span>W' + w.id + '</span><span class="wbar"><i></i></span><span class="wtxt"></span>';
        box.appendChild(el);
      }
      box.dataset.count = String(data.length);
    }
    data.forEach((w, i) => {
      const el = box.children[i];
      if (!el) return;
      el.querySelector(".wtxt").textContent = w.status + " · " + w.done + " 批";
      el.querySelector(".wbar i").style.width = Math.min(100, w.done * 10) + "%";
    });
  }

  /* 「并发」旁边的 Key 提示：并发开得比 Key 多时明确告诉用户 */
  function updateWorkersHint() {
    const hint = $("workers-hint");
    if (!hint) return;
    const keys = lastState && typeof lastState.key_count === "number" ? lastState.key_count : null;
    if (keys === null) {
      hint.textContent = "";
      return;
    }
    const want = Math.max(1, parseInt($("workers").value, 10) || 1);
    if (!keys) {
      hint.textContent = "未配置 Key";
      hint.className = "key-hint warn";
    } else if (want > keys) {
      hint.textContent = keys + " 个 Key，多出的共用";
      hint.className = "key-hint warn";
    } else {
      hint.textContent = keys + " 个 Key";
      hint.className = "key-hint";
    }
  }

  /* 入口卡片上的一行摘要（来自后端 state.panels） */
  function renderEntries(panels) {
    if (!panels) return;
    for (const item of ENTRIES) {
      const note = $(item[2]);
      if (note) note.textContent = panels[item[3] || item[0]] || "";
    }
  }

  function render(s) {
    if (!s) return;
    lastState = s;
    setPill($("pill-device"), s.device.text, s.device.tone);
    setPill($("pill-model"), s.model.text, s.model.tone);
    $("device-detail").textContent = s.device.detail || "";
    $("device-detail").style.color = s.device.tone === "danger" ? "var(--red)" : "var(--muted)";

    $("bar").style.width = (s.progress.percent || 0) + "%";
    $("cur").textContent = s.progress.current || "--";
    const status = $("status");
    status.textContent = s.progress.status || "";
    status.className = "status " + (s.progress.tone || "");
    $("btn-start").disabled = !!s.running;
    $("btn-stop").disabled = !s.running || !!s.stopping;   // 停止中就别再点了
    if (s.total_hint) $("total-hint").textContent = s.total_hint;
    if (s.output_preview) $("output-preview").textContent = s.output_preview;
    $("btn-connect").disabled = !!s.device.busy;
    $("btn-connect").innerHTML = s.device.busy ? "<b>⇄</b> 连接中…" : "<b>⇄</b> 连接";

    const busy = !!s.device.busy;
    if (busy && !busySince) busySince = Date.now();
    if (!busy) busySince = 0;
    if (busy && Date.now() - busySince > 30000) {
      busySince = 0;
      $("btn-connect").disabled = false;
      $("btn-connect").innerHTML = "<b>⇄</b> 连接";
      toast("adb connect 超时，请检查手机 IP、端口与无线调试开关", "err");
    }

    renderWorkers(s.workers);
    renderEntries(s.panels);
    updateWorkersHint();
    // 提取运行中不让开「屏幕调节」：改显示设置会把正在跑的提取搞乱
    const tunerEntry = $("entry-tuner");
    if (tunerEntry) tunerEntry.disabled = !!s.running;
    // 开始提取 → 粒子变蓝、马儿奔跑；点停止（stopping）或跑完 → 立刻灰色静止
    const active = !!s.running && !s.stopping;
    if (horse && active !== horseRunning) {
      horseRunning = active;
      horse.setRunning(active);
    }
    if (s.form_patch) {
      for (const [key, value] of Object.entries(s.form_patch)) {
        const el = $(key);
        if (!el) continue;
        // 勾选框写 .checked（display_scaling 回填的就是布尔），其余写 .value
        if (el.type === "checkbox") el.checked = !!value;
        else el.value = value;
      }
    }
    // 运行结束：管线跑完会自己复位手机，状态栏要跟着回到真机读数
    if (wasRunning && !s.running) call("display_status", payload());
    wasRunning = !!s.running;
    (s.toasts || []).forEach((t) => toast(t.text, t.tone));

    if (s.question) {
      if (!modalOpen || lastQuestion !== s.question.id) showModal(s.question);
    } else if (modalOpen) {
      hideModal();
    }
  }

  function toast(text, tone) {
    const el = document.createElement("div");
    el.className = "toast " + (tone || "");
    el.textContent = text;
    $("toasts").appendChild(el);
    setTimeout(() => el.remove(), 4200);
  }

  function showModal(q) {
    lastQuestion = q.id;
    modalOpen = true;
    $("modal-title").textContent = q.title || "请确认";
    $("modal-body").textContent = q.message || "";
    $("modal-no").style.display = q.kind === "info" ? "none" : "";
    $("overlay").classList.add("show");
  }

  function hideModal() {
    modalOpen = false;
    $("overlay").classList.remove("show");
  }

  function answer(ok) {
    const id = lastQuestion;
    hideModal();
    const api = backend();
    if (api && id !== null) api.answer_question(id, !!ok);
  }

  /* ---------------- 轮询 ---------------- */
  async function poll() {
    const api = backend();
    if (api) {
      try {
        const res = await api.poll();
        if (res) {
          appendLogs(res.logs);
          render(res.state);
        }
      } catch (err) {
        console.error(err);
      }
    }
    setTimeout(poll, 150);
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

  /* 预览模式（没有后端）时本地拼一个「将输出」，让开关也能看到变化 */
  function demoPreview() {
    const dir = $("output_dir").value.trim();
    if (!dir) {
      $("output-preview").textContent = "请先选择输出文件夹（点右侧「选择…」）";
      return;
    }
    const book = $("textbook").value.trim() || "未命名教材";
    const chapter = $("chapter").value.trim();
    const base = chapter ? book + "_" + chapter : book + "_起-止";
    const names = [base + ".jsonl"];
    if ($("opt-md").checked) names.push(base + ".md");
    if ($("opt-index").checked) names.push("index.json");
    $("output-preview").textContent = "将输出：" + dir + " → " + names.join(" / ");
  }

  function schedulePreview() {
    clearTimeout(previewTimer);
    const seq = ++previewSeq;
    previewTimer = setTimeout(async () => {
      if (!backend()) {
        demoPreview();
        return;
      }
      // preview() 顺手把这份表单记在后端：入口窗口打开时用它当基准
      const res = await call("preview", payload());
      if (!res || seq !== previewSeq) return;   // 有更新的请求了，丢掉这份旧结果
      // 出错时后端会返回空 total_hint + 错误文案，这里必须照写，避免留下过期的旧预览
      $("total-hint").textContent = res.total_hint || "";
      $("output-preview").textContent = res.output_preview || "";
    }, 180);
  }

  /* ---------------- 手机显示（屏幕调节窗口回填的三个隐藏字段） ---------------- */
  function displayValues() {
    const read = function (id, fallback) {
      const el = $(id);
      const num = Number(el ? el.value : fallback);
      return isFinite(num) ? num : fallback;
    };
    return {
      scale: Math.max(1, read("display_scale", 1)),
      widthScale: Math.max(1, read("display_width_scale", 1)),
      density: Math.max(0, Math.round(read("display_density", 0))),
    };
  }

  /* 需不需要动手机显示设置：加长 / 加宽 / 只把密度调小，都算 */
  function displayWanted(v) {
    return v.scale > 1 || v.widthScale > 1 || v.density > 0;
  }

  /* 直接写这三个隐藏字段（配置回填 / 复位用） */
  function syncDisplayFields(values) {
    if (values.scale !== undefined && $("display_scale")) $("display_scale").value = String(values.scale);
    if (values.widthScale !== undefined && $("display_width_scale")) $("display_width_scale").value = String(values.widthScale);
    if (values.density !== undefined && $("display_density")) $("display_density").value = String(values.density);
  }

  /* ---------------- 事件绑定 ---------------- */
  function bind() {
    $("btn-start").onclick = async () => {
      const res = await call("start", payload());
      if (res && res.ok === false) toast(res.error || "无法开始", "err");
    };
    $("btn-stop").onclick = () => call("stop");
    $("btn-connect").onclick = () => call("connect_device", payload());
    $("btn-disconnect").onclick = () => call("disconnect_device", payload());
    $("btn-refresh-devices").onclick = () => call("refresh_devices");
    $("btn-pick-dir").onclick = async () => {
      const dir = await call("pick_output_dir", $("output_dir").value.trim());
      if (dir) {
        $("output_dir").value = dir;
        schedulePreview();
      }
    };
    $("btn-open-dir").onclick = () => call("open_output_dir", $("output_dir").value.trim());
    $("btn-clear-log").onclick = () => { $("log").innerHTML = ""; };
    $("btn-exit").onclick = () => { const api = backend(); if (api) api.exit(); else toast("预览模式无法退出", "err"); };
    $("modal-yes").onclick = () => answer(true);
    $("modal-no").onclick = () => answer(false);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && modalOpen) answer(false); });

    // 入口卡片：打开对应的独立设置窗口（窗口里的改动会回填到隐藏载体）
    for (const el of document.querySelectorAll(".entry")) {
      el.onclick = async () => {
        const res = await call("open_panel", el.dataset.panel, payload());
        if (res && res.ok === false) toast(res.error || "打不开设置窗口", "err");
      };
    }

    for (const id of ["page_from", "page_to", "textbook", "chapter", "chapter_no", "chapter_total",
                      "output_dir", "device_ip", "device_port"]) {
      $(id).addEventListener("input", schedulePreview);
    }
    // 开关也会影响「将输出」（生成 Markdown / index.json），必须一起刷新
    for (const id of ["opt-md", "opt-index", "opt-preflight", "opt-check-model"]) {
      $(id).addEventListener("change", schedulePreview);
    }
    $("workers").addEventListener("input", updateWorkersHint);
    for (const id of ["device_ip", "device_port"]) {
      $(id).addEventListener("keydown", (e) => { if (e.key === "Enter") call("connect_device", payload()); });
    }
  }

  /* ---------------- 预览模式（浏览器直开时） ----------------
     只为调版面存在：所有文案都带「示例」并打上预览标签，
     真实配置一律由后端 ready() 的 config 回填，绝不在这里编造。 */
  function demo() {
    // 预览模式按「正在运行」的样子显示；加 #idle 可以看未运行时的灰色静止状态
    if (horse) horse.setRunning(location.hash.indexOf("idle") < 0);
    const tag = document.createElement("div");
    tag.className = "demo-tag";
    tag.textContent = "界面预览模式（没有检测到 Python 后端）：以下均为示例文案";
    $("pill-model").after(tag);
    setPill($("pill-device"), "设备：1 台在线", "success");
    setPill($("pill-model"), "模型：可用", "success");
    $("device-detail").textContent = "示例：已连接手机 · 1080x2340";
    $("device-detail").style.color = "#1f7a4a";
    lastState = { key_count: 3 };
    updateWorkersHint();
    renderEntries({
      action: "录制动作 · 3 步",
      modules: "5 项：题号*、题目*、答案*、考点还原、标准解析",
      model: "deepseek-flash · 3 个 Key",
      display: "显示：示例（物理 1260x2720，未加长）",
    });
    syncDisplayFields({ scale: 1, widthScale: 1, density: 0 });
    $("max_tokens").value = "4096";
    $("bar").style.width = "35%";
    $("cur").textContent = "第 7 题（7/20）";
    const st = $("status");
    st.textContent = "运行中";
    st.className = "status";
    renderWorkers([{ id: 1, status: "处理批次 2", done: 2 }, { id: 2, status: "空闲", done: 1 }]);
    appendLogs([
      { text: "（以下为界面预览示例日志，不是真实运行结果）", tone: "warn" },
      { text: "示例：正在 adb connect 手机 …" },
      { text: "示例：连接成功", tone: "ok" },
      { text: "示例：模型连通性正常 · 812 ms", tone: "ok" },
      { text: "示例：设备分辨率 1080x2340" },
      { text: "示例：翻页动作 点击 → 等待 → 滑动" },
      { text: "示例：输出字段：题号→JSONL/Markdown、题目→JSONL/Markdown、答案→JSONL/Markdown" },
      { text: "示例：计划 Q1~Q20，共 20 张截图；批大小 3，并发 2" },
      { text: "示例：预检当前屏幕显示第 1 题" },
      { text: "示例：截图（7/20），预期第 7 题" },
      { text: "示例：检测到重复截图，重试翻页动作…", tone: "warn" },
      { text: "示例：重试 1 成功" },
      { text: "示例：[W2] 批次 1 失败：HTTP 429（将重试）", tone: "err" },
      { text: "示例：输出文件已写入 20 题（含教材/章节/题号元数据）" },
    ]);
  }

  /* ---------------- 前端自身的异常也要可见 ---------------- */
  function reportError(message) {
    const text = "界面脚本出错：" + message;
    try {
      toast(text, "err");
    } catch (e) { /* 连提示都失败就只能放弃 */ }
    const box = $("log");
    if (box) {
      const div = document.createElement("div");
      div.className = "err";
      div.textContent = text;
      box.appendChild(div);
      box.scrollTop = box.scrollHeight;
    }
  }

  window.addEventListener("error", (event) => reportError(event.message || "未知错误"));
  window.addEventListener("unhandledrejection", (event) => {
    const reason = event.reason;
    reportError((reason && reason.message) || String(reason));
  });

  /* ---------------- 启动 ---------------- */
  /* pywebview 的 API 是异步注入的（注入完会发 pywebviewready），
     所以这里等一下后端；确实没有后端（比如直接在浏览器里打开）才进预览模式。 */
  let booted = false;

  async function boot() {
    if (booted) return;
    booted = true;
    bind();
    const canvas = $("horse");
    if (canvas && window.DrPilotHorse) horse = window.DrPilotHorse.create(canvas);
    if (!backend()) {
      demo();
      return;
    }
    const init = await call("ready");
    if (init) {
      fill(init.config);
      if (init.state) render(init.state);
      appendLogs(init.logs);
    }
    schedulePreview();                   // 顺便把表单记到后端（入口窗口的基准）
    call("display_status", payload());   // 顺便读一次手机显示设置
    poll();
  }

  function waitForBackend(deadline) {
    if (backend()) {
      boot();
      return;
    }
    if (Date.now() > deadline) {
      boot();
      return;
    }
    setTimeout(() => waitForBackend(deadline), 80);
  }

  document.addEventListener("DOMContentLoaded", () => {
    window.addEventListener("pywebviewready", () => boot());
    waitForBackend(Date.now() + 2500);
  });
})();
