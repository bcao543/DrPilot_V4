/* 「提取模块」窗口：题号 / 题目 / 答案（本体，可删）+ 自定义模块的统一清单。
   只跟 ModulePanelApi（bridge.module_settings / save_module_settings）打交道。 */
(function () {
  "use strict";
  const P = window.DrPilotPanel;
  const $ = P.$;

  let fields = [];        // 服务端给的清单（未编辑时的现场）
  let dirty = false;
  let presets = [];
  let maxModules = 12;

  function switchHtml(cls, text, title) {
    return '<label class="switch" title="' + (title || "") + '">'
      + '<input type="checkbox" class="' + cls + '">'
      + '<span class="track"><span class="knob"></span></span><span>' + text + '</span></label>';
  }

  function baseRow(item) {
    const row = document.createElement("div");
    row.className = "module-row base";
    row.dataset.kind = "base";
    row.dataset.key = item.key;

    const tag = document.createElement("span");
    tag.className = "module-tag";
    tag.textContent = "本体";
    const name = document.createElement("span");
    name.className = "module-name-text";
    name.style.minWidth = "52px";
    name.innerHTML = "<b>" + item.name + "</b>";
    row.appendChild(tag);
    row.appendChild(name);

    const keys = document.createElement("span");
    keys.className = "hint";
    keys.style.flex = "1";
    keys.textContent = "JSON 键：" + (item.json_keys || []).join(" / ");
    row.appendChild(keys);

    const outputs = document.createElement("span");
    outputs.className = "module-keys";
    outputs.innerHTML = switchHtml("out-json", "写入 JSON", "关掉后 JSONL 不写这个字段")
      + switchHtml("out-md", "写入 Markdown", "关掉后 Markdown 不渲染这一段");
    outputs.querySelector(".out-json").checked = item.in_json !== false;
    outputs.querySelector(".out-md").checked = item.in_markdown !== false;
    row.appendChild(outputs);

    const del = document.createElement("button");
    del.type = "button";
    del.className = "btn small ghost module-del";
    del.textContent = "✕";
    del.title = "删除这一项（" + item.name + "）";
    del.onclick = () => removeRow(row, item);
    row.appendChild(del);

    const note = document.createElement("span");
    note.className = "module-note";
    note.textContent = item.note || "";
    row.appendChild(note);

    row.querySelectorAll("input").forEach((el) => el.addEventListener("change", () => { dirty = true; }));
    return row;
  }

  function moduleRow(item) {
    const row = document.createElement("div");
    row.className = "module-row";
    row.dataset.kind = "module";
    row.dataset.hint = item.hint || "";

    const name = document.createElement("input");
    name.className = "module-name";
    name.placeholder = "模块名（如 标准解析）";
    name.value = item.name || "";
    const aliases = document.createElement("input");
    aliases.className = "module-aliases";
    aliases.placeholder = "别名，逗号分隔";
    aliases.value = (item.aliases || []).join("，");
    row.appendChild(name);
    row.appendChild(aliases);

    const outputs = document.createElement("span");
    outputs.className = "module-keys";
    outputs.innerHTML = switchHtml("out-json", "写入 JSON")
      + switchHtml("out-md", "写入 Markdown")
      + switchHtml("below-fold", "首屏内", "勾上 = 不用加长屏幕就能截到")
      + switchHtml("required", "必读", "勾上后：这一题没识别出该模块时，日志会点名题号");
    outputs.querySelector(".out-json").checked = item.in_json !== false;
    outputs.querySelector(".out-md").checked = item.in_markdown !== false;
    outputs.querySelector(".below-fold").checked = !item.below_fold;   // 「首屏内」= below_fold 取反
    outputs.querySelector(".required").checked = !!item.required;
    row.appendChild(outputs);

    for (const spec of [["module-up", "↑", "上移"], ["module-down", "↓", "下移"], ["module-del", "✕", "删除"]]) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn small ghost " + spec[0];
      btn.textContent = spec[1];
      btn.title = spec[2];
      btn.onclick = () => {
        if (spec[0] === "module-del") { row.remove(); dirty = true; return; }
        move(row, spec[0] === "module-up" ? -1 : 1);
      };
      row.appendChild(btn);
    }
    name.addEventListener("input", () => { dirty = true; });
    aliases.addEventListener("input", () => { dirty = true; });
    row.querySelectorAll('input[type="checkbox"]').forEach((el) => el.addEventListener("change", () => { dirty = true; }));
    return row;
  }

  function move(row, delta) {
    const list = $("module-list");
    const rows = Array.prototype.slice.call(list.querySelectorAll(".module-row"));
    const index = rows.indexOf(row);
    const target = index + delta;
    if (index < 0 || target < 0 || target >= rows.length) return;
    if (delta < 0) list.insertBefore(row, rows[target]);
    else list.insertBefore(rows[target], row);
    dirty = true;
  }

  function removeRow(row, item) {
    if (item && item.key === "id") {
      const ok = window.confirm(
        "确定把「题号」从输出里删掉吗？\n\n"
        + "题号仍然会被读取、用于翻页与本次按题号合并；\n"
        + "但 JSONL 里不再有 id 字段：再次提取同一章节时只能追加、无法按题号合并，"
        + "index.json 也记不了题号列表。");
      if (!ok) return;
    }
    row.remove();
    dirty = true;
    updateStatus();
  }

  function render(fieldsIn) {
    const list = $("module-list");
    list.innerHTML = "";
    (fieldsIn || []).forEach((item) => {
      list.appendChild(item.kind === "base" ? baseRow(item) : moduleRow(item));
    });
    updateStatus();
  }

  function readRows() {
    const out = [];
    for (const row of $("module-list").querySelectorAll(".module-row")) {
      const json = row.querySelector(".out-json");
      const md = row.querySelector(".out-md");
      if (row.dataset.kind === "base") {
        out.push({
          kind: "base",
          key: row.dataset.key,
          in_json: !!(json && json.checked),
          in_markdown: !!(md && md.checked),
        });
      } else {
        out.push({
          kind: "module",
          name: (row.querySelector(".module-name").value || "").trim(),
          aliases: (row.querySelector(".module-aliases").value || "")
            .split(/[,，、;；|]/).map((s) => s.trim()).filter(Boolean),
          in_json: !!(json && json.checked),
          in_markdown: !!(md && md.checked),
          below_fold: !row.querySelector(".below-fold").checked,
          required: !!row.querySelector(".required").checked,
          hint: row.dataset.hint || "",
        });
      }
    }
    return out;
  }

  function updateStatus() {
    const rows = readRows();
    const base = rows.filter((r) => r.kind === "base");
    const custom = rows.filter((r) => r.kind === "module");
    $("count-hint").textContent = base.length + " 个本体字段 + " + custom.length + " 个自定义模块";
    const status = $("module-status");
    if (!rows.length) status.textContent = "清单是空的：这次不会写出任何题目内容";
    else if (!custom.length) status.textContent = "没有自定义模块：只输出题目本体的 " + base.length + " 项";
    else status.textContent = "共 " + rows.length + " 项，其中 " + custom.length + " 个自定义模块";
    $("max-hint").textContent = "自定义模块上限 " + maxModules + " 个";
  }

  function addModule(spec) {
    const item = {
      kind: "module",
      name: (spec && spec.name) || "",
      aliases: (spec && spec.aliases) || [],
      below_fold: spec && spec.below_fold !== undefined ? !!spec.below_fold : true,
      required: !!(spec && spec.required),
      in_json: true,
      in_markdown: true,
      hint: (spec && spec.hint) || "",
    };
    if (item.name) {
      const taken = readRows().some((r) => r.kind === "module"
        && (r.name || "").toLowerCase() === item.name.toLowerCase());
      if (taken) { P.toast("「" + item.name + "」已经在列表里了", "warn"); return; }
    }
    const row = moduleRow(item);
    $("module-list").appendChild(row);
    dirty = true;
    updateStatus();
    if (!item.name) row.querySelector(".module-name").focus();
  }

  function renderPresets(list) {
    const box = $("module-presets");
    box.querySelectorAll(".preset-chip").forEach((el) => el.remove());
    (list || []).forEach((preset) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn small ghost preset-chip";
      btn.textContent = preset.name || "";
      btn.title = (preset.hint || "内置模块") + (preset.below_fold ? "（通常需要加长屏幕）" : "（首屏内）");
      btn.onclick = () => addModule(preset);
      box.appendChild(btn);
    });
  }

  function applyState(s) {
    if (!s) return;
    presets = s.presets || presets;
    maxModules = s.max_modules || maxModules;
    renderPresets(presets);
    if (!dirty) {
      fields = s.fields || [];
      render(fields);
    }
    $("pill-summary-text").textContent = s.summary || "";
    const display = s.display || {};
    const status = $("display-status");
    status.textContent = display.text || "显示：未读取";
    status.title = display.detail || "";
    status.style.color = display.tone === "danger" ? "var(--red)"
      : (display.tone === "warning" ? "var(--amber)" : "var(--muted)");
    updateStatus();
  }

  function bind() {
    $("btn-add-module").onclick = () => addModule(null);
    $("btn-open-display").onclick = async () => {
      const res = await P.call("open_display");
      if (res && res.ok === false) P.toast(res.error || "打不开屏幕调节窗口", "err");
    };
    $("btn-save").onclick = async () => {
      const res = await P.call("save", { fields: readRows() });
      if (!res) return;
      if (res.ok === false) { P.toast(res.error || "保存失败", "err"); return; }
      dirty = false;
      if (res.state) applyState(res.state);
      P.toast("已保存：" + (($("pill-summary-text").textContent) || ""), "ok");
    };
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") window.close(); });
  }

  function demo() {
    renderPresets([
      { name: "标准解析", below_fold: true, hint: "「标准解析」小节的正文" },
      { name: "考点还原", below_fold: true, hint: "「考点还原」小节的正文" },
      { name: "总结分析", below_fold: true, hint: "「总结分析」小节的正文" },
      { name: "难度", below_fold: false, hint: "题干下方的难度星级" },
    ]);
    render([
      { kind: "base", key: "id", name: "题号", json_keys: ["id"], in_json: true, in_markdown: true,
        note: "截图右上角的题号（JSON 键 id），也是按题号合并写回时用的键" },
      { kind: "base", key: "stem", name: "题目", json_keys: ["type", "stem", "options"], in_json: true, in_markdown: true,
        note: "题干与选项（JSON 键 type / stem / options）" },
      { kind: "base", key: "answer", name: "答案", json_keys: ["answer"], in_json: true, in_markdown: true,
        note: "正确选项字母（JSON 键 answer）" },
      { kind: "module", name: "考点还原", aliases: ["考点"], below_fold: true, required: false,
        in_json: true, in_markdown: true, hint: "「考点还原」小节的正文" },
      { kind: "module", name: "标准解析", aliases: ["解析"], below_fold: true, required: false,
        in_json: false, in_markdown: true, hint: "「标准解析」小节的正文" },
    ]);
    $("pill-summary-text").textContent = "示例：5 项";
    $("display-status").textContent = "显示：示例（物理 1260x2720，未加长）";
    dirty = true;
  }

  function boot() {
    bind();
    P.bootPanel(async () => {
      const state = await P.call("ready");
      if (state) applyState(state);
      P.startPoll(applyState, 400);
    }, demo);
  }

  document.addEventListener("DOMContentLoaded", boot);
  window.addEventListener("error", (event) => P.toast("界面脚本出错：" + (event.message || ""), "err"));
})();
