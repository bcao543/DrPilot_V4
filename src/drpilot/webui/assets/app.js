/* DrPilot v4 前端逻辑：只跟 Python 桥（window.pywebview.api）打交道，
   轮询拿日志/进度，所有耗时操作都在 Python 侧后台线程里跑。
   直接用浏览器打开时自动进入「界面预览」模式，方便调版面。 */
(function () {
  "use strict";

  const TEXT_FIELDS = [
    "page_from", "page_to", "textbook", "chapter", "chapter_no", "chapter_total",
    "output_dir", "model", "base_url", "batch_size", "workers", "wait_ms",
    "device_ip", "device_port",
    "swipe_x1", "swipe_y1", "swipe_x2", "swipe_y2",
    "swipe_duration", "swipe_retries", "swipe_retry_wait", "swipe_reference",
    "next_action", "action_tap_x", "action_tap_y", "action_tap_ms",
  ];
  const FLAGS = {
    "opt-md": "generate_markdown",
    "opt-index": "generate_index",
    "opt-preflight": "preflight",
    "opt-check-model": "check_model",
  };
  const $ = (id) => document.getElementById(id);
  const backend = () => (window.pywebview && window.pywebview.api) || null;

  let lastQuestion = null;
  let busySince = 0;
  let modalOpen = false;
  let previewTimer = null;
  let previewSeq = 0;        // 「将输出」请求序号，防止旧结果覆盖新结果
  let lastState = null;      // 最近一次后端状态，供「并发」提示等即时重算
  let horse = null;          // 右下角的点阵马（背景动效）
  let horseRunning = null;
  // API Key 的隐私输入状态只存在 DOM 里（type=password），后端只回脱敏列表

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

  function render(s) {
    if (!s) return;
    lastState = s;
    setPill($("pill-device"), s.device.text, s.device.tone);
    setPill($("pill-model"), s.model.text, s.model.tone);
    $("model-note").textContent = s.model.note || "";
    $("model-note").style.color = s.model.tone === "danger" ? "var(--red)" : "var(--muted)";
    $("key-info").textContent = s.keys || "";
    renderKeys(s);
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
    if (s.model_checking) {
      $("btn-test-model").disabled = true;
      $("btn-test-model").textContent = "测试中…";
    } else {
      $("btn-test-model").disabled = false;
      $("btn-test-model").textContent = "测试连通性";
    }
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
    renderAction(s.action);
    updateWorkersHint();
    // 开始提取 → 粒子变蓝、马儿奔跑；点停止（stopping）或跑完 → 立刻灰色静止
    const active = !!s.running && !s.stopping;
    if (horse && active !== horseRunning) {
      horseRunning = active;
      horse.setRunning(active);
    }
    if (s.form_patch) {
      for (const [key, value] of Object.entries(s.form_patch)) {
        const el = $(key);
        if (el) el.value = value;
      }
    }
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
      const res = await call("preview", payload());
      if (!res || seq !== previewSeq) return;   // 有更新的请求了，丢掉这份旧结果
      // 出错时后端会返回空 total_hint + 错误文案，这里必须照写，避免留下过期的旧预览
      $("total-hint").textContent = res.total_hint || "";
      $("output-preview").textContent = res.output_preview || "";
    }, 180);
  }

  /* ---------------- API Key（模型服务卡片内：隐私输入 + 新增 / 删除） ---------------- */
  function keyInputText() {
    const el = $("api_key_input");
    if (!el) return "";
    let text = el.value.trim();
    if (!text) return "";
    // 单行输入框粘贴多行时换行会被浏览器压掉；没有 "=" 时统一按空白切成多个 Key
    if (text.indexOf("=") < 0) text = text.replace(/\s+/g, ",");
    return text;
  }

  function setKeyVisible(on) {
    const el = $("api_key_input");
    const btn = $("btn-toggle-key");
    const eye = $("key-eye");
    if (el) el.type = on ? "text" : "password";
    if (btn) {
      btn.classList.toggle("active", !!on);
      btn.title = on ? "当前是明文：点一下回到隐私输入模式（圆点）" : "隐私输入模式：点一下临时显示输入内容";
      btn.setAttribute("aria-label", btn.title);
    }
    if (eye) eye.textContent = on ? "🙈" : "👁";
  }

  function clearKeyInput() {
    const el = $("api_key_input");
    if (el) el.value = "";
    setKeyVisible(false);              // 新增完立刻回到密文，明文不在 DOM 里多待
  }

  let keysSignature = "";    // 已保存列表的内容指纹：没变就不重建 DOM

  function renderKeys(s) {
    const box = $("key-chips");
    if (!box) return;
    const items = (s && s.key_items) || [];
    const clearBtn = $("btn-clear-keys");
    if (clearBtn) clearBtn.classList.toggle("hidden", !items.length);
    const signature = JSON.stringify(items);
    // 每 150ms 轮询一次；无脑重建会把「鼠标正按着的按钮」换掉，点击就丢了
    if (signature === keysSignature) return;
    keysSignature = signature;
    box.innerHTML = "";
    items.forEach((item) => {
      const chip = document.createElement("span");
      chip.className = "key-chip";
      const label = document.createElement("span");
      label.textContent = item.masked || ("#" + item.index);
      const del = document.createElement("button");
      del.type = "button";
      del.className = "chip-del";
      del.title = "删除这个 API Key";
      del.setAttribute("aria-label", "删除 " + label.textContent);
      del.textContent = "✕";
      del.onclick = () => deleteKey(item.index);
      chip.appendChild(label);
      chip.appendChild(del);
      box.appendChild(chip);
    });
  }

  function applyKeyResult(res) {
    if (!lastState) lastState = {};
    if (typeof res.count === "number") lastState.key_count = res.count;
    if (res.items) lastState.key_items = res.items;
    renderKeys(lastState);
    updateWorkersHint();               // 「并发」旁边的提示马上跟着变
  }

  async function addKey() {
    const text = keyInputText();
    if (!text) {
      toast("请先填写要新增的 API Key", "err");
      const el = $("api_key_input");
      if (el) el.focus();
      return;
    }
    const res = await call("add_keys", { text: text });
    if (!res || res.ok === false) {
      if (res) toast(res.error || "新增失败", "err");
      return;
    }
    clearKeyInput();
    applyKeyResult(res);
    toast(res.added
      ? "已新增 " + res.added + " 个 API Key（共 " + res.count + " 个）"
      : "这些 Key 已经保存过了，没有重复写入", "ok");
  }

  async function deleteKey(index) {
    const items = (lastState && lastState.key_items) || [];
    const target = items.filter((it) => it.index === index)[0];
    const label = (target && target.masked) || ("#" + index);
    if (!window.confirm("确定删除 API Key " + label + " 吗？\n删除后运行将不再使用这个 Key。")) return;
    const res = await call("delete_key", { index: index });
    if (!res) return;
    if (res.ok === false) {
      toast(res.error || "删除失败", "err");
      return;
    }
    applyKeyResult(res);
    toast("已删除 " + label, "ok");
  }

  async function clearKeys() {
    const items = (lastState && lastState.key_items) || [];
    if (!items.length) return;
    if (!window.confirm("确定删除全部 " + items.length + " 个 API Key 吗？\n删除后需要重新新增才能开始提取。")) return;
    const res = await call("clear_keys");
    if (!res) return;
    if (res.ok === false) {
      toast(res.error || "删除失败", "err");
      return;
    }
    clearKeyInput();
    applyKeyResult(res);
    toast("已删除全部 API Key", "ok");
  }

  /* ---------------- 事件绑定 ---------------- */
  function bind() {
    $("btn-start").onclick = async () => {
      const res = await call("start", payload());
      if (res && res.ok === false) toast(res.error || "无法开始", "err");
    };
    $("btn-stop").onclick = () => call("stop");
    $("btn-test-model").onclick = () => call("test_model", payload());
    $("btn-add-key").onclick = addKey;
    $("btn-toggle-key").onclick = () => setKeyVisible($("api_key_input").type === "password");
    $("btn-clear-keys").onclick = clearKeys;
    $("api_key_input").addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); addKey(); }   // 粘贴后回车即新增
    });
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
    $("btn-set-reference").onclick = () => call("set_reference", payload());
    $("btn-test-swipe").onclick = () => call("test_swipe", payload());
    $("btn-record-start").onclick = () => call("start_record", payload());
    $("btn-record-stop").onclick = () => call("stop_record");
    $("btn-test-action").onclick = () => call("test_action", payload());
    $("btn-action-clear").onclick = () => call("clear_action");
    $("btn-use-tap").onclick = () => call("use_tap", payload());
    $("btn-swipe-toggle").onclick = (e) => { e.stopPropagation(); toggleSwipe(); };
    $("swipe-head").onclick = toggleSwipe;
    $("btn-exit").onclick = () => { const api = backend(); if (api) api.exit(); else toast("预览模式无法退出", "err"); };
    $("modal-yes").onclick = () => answer(true);
    $("modal-no").onclick = () => answer(false);
    $("modal-title").onclick = null;
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && modalOpen) answer(false); });

    for (const id of ["page_from", "page_to", "textbook", "chapter", "chapter_no", "chapter_total", "output_dir"]) {
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

  /* ---------------- 翻页动作（录制 / 步骤列表） ---------------- */
  function stepText(s) {
    if (!s) return "";
    if (s.kind === "tap") {
      return "点击 (" + s.x + "," + s.y + ")" + (s.duration_ms >= 80 ? "，按住 " + s.duration_ms + "ms" : "");
    }
    if (s.kind === "swipe") {
      return "滑动 (" + s.x + "," + s.y + ") → (" + s.x2 + "," + s.y2 + ")，" + s.duration_ms + "ms";
    }
    if (s.kind === "path") {
      return "精确滑动 " + ((s.points || []).length) + " 个点，" + (s.duration_ms || 0) + "ms";
    }
    if (s.kind === "key") return "按键 " + s.keycode;
    if (s.kind === "wait") return "等待 " + s.wait_ms + "ms";
    return String(s.kind || "");
  }

  function renderAction(a) {
    const status = $("action-status");
    const list = $("action-steps");
    if (!status || !list) return;
    const data = a || {};
    const steps = data.steps || [];
    const recording = !!data.recording;
    const running = !!(lastState && lastState.running);

    if (recording) {
      status.textContent = data.note || "录制中…请在手机上完成一次「下一题」动作";
      status.style.color = "var(--blue, #2f6fed)";
    } else if (steps.length) {
      const note = data.note || ("已录制 " + steps.length + " 步");
      status.textContent = note + (data.reference ? "　基准 " + data.reference : "");
      status.style.color = "var(--muted)";
    } else {
      status.textContent = data.note || "未设置：将使用下方手动滑动坐标";
      status.style.color = "var(--muted)";
    }

    list.innerHTML = "";
    steps.forEach((s) => {
      const li = document.createElement("li");
      li.textContent = stepText(s);
      list.appendChild(li);
    });

    const start = $("btn-record-start");
    const stop = $("btn-record-stop");
    const test = $("btn-test-action");
    if (start) start.disabled = recording || running;
    if (stop) stop.disabled = !recording;
    if (test) test.disabled = recording || running || !steps.length;
  }

  function toggleSwipe() {
    const body = $("swipe-body");
    const open = body.classList.toggle("hidden");
    $("btn-swipe-toggle").textContent = open ? "展开 ▾" : "收起 ▴";
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
    $("model-note").textContent = "✓ 示例：模型可调用 · 812 ms";
    $("model-note").style.color = "#1f7a4a";
    $("device-detail").textContent = "示例：已连接手机 · 1080x2340";
    $("device-detail").style.color = "#1f7a4a";
    $("key-info").textContent = "示例：已加载 3 个 API Key（.env 3 个）";
    lastState = {
      key_count: 3,
      key_masked: "sk-********ab12 / sk-********cd34 / sk-********ef56",
      key_items: [
        { index: 1, masked: "sk-********ab12" },
        { index: 2, masked: "sk-********cd34" },
        { index: 3, masked: "sk-********ef56" },
      ],
    };
    renderKeys(lastState);
    updateWorkersHint();
    renderAction({
      recording: false,
      steps: [
        { kind: "tap", x: 540, y: 2280, duration_ms: 80 },
        { kind: "wait", wait_ms: 300 },
        { kind: "swipe", x: 960, y: 1200, x2: 240, y2: 1200, duration_ms: 260 },
      ],
      summary: "示例步骤",
      reference: "示例 1080x2340",
      note: "示例：已加载 3 步",
    });
    $("bar").style.width = "35%";
    $("cur").textContent = "第 7 题（7/20）";
    const st = $("status");
    st.textContent = "运行中";
    st.className = "status";
    renderWorkers([{ id: 1, status: "处理批次 2", done: 2 }, { id: 2, status: "空闲", done: 1 }]);
    if (location.hash === "#expand") toggleSwipe();
    appendLogs([
      { text: "（以下为界面预览示例日志，不是真实运行结果）", tone: "warn" },
      { text: "示例：正在 adb connect 手机 …" },
      { text: "示例：连接成功", tone: "ok" },
      { text: "示例：模型连通性正常 · 812 ms", tone: "ok" },
      { text: "示例：设备分辨率 1080x2340" },
      { text: "示例：翻页动作 点击 → 等待 → 滑动" },
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
