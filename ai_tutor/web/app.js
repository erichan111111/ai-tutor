/* 大学生期末复习 AI 答疑助手 · 前端逻辑（零依赖原生 JS） */
(function () {
  "use strict";

  const state = {
    modules: [],
    methodology: null,
    models: [],
    routes: [],
    module: "auto",
    sessionId: null,
    busy: false,
    images: [],          // [{id, name, dataUri, size}] 待发送的图片
    imageGeneration: 0,
    pendingImages: 0,
    imageCfg: { max_images: 4, max_bytes: 8388608, allowed_types: ["png", "jpg", "jpeg", "webp", "gif"] },
    visionModel: null,
    health: null,
  };

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));
  const local = {
    get(key) { try { return localStorage.getItem("ai-tutor:" + key); } catch { return null; } },
    set(key, value) { try { if (value) localStorage.setItem("ai-tutor:" + key, value); else localStorage.removeItem("ai-tutor:" + key); } catch { /* Storage may be disabled. Server records remain available. */ } },
  };

  // ------------------------------------------------------------ 工具
  function esc(text) {
    return String(text == null ? "" : text)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  /** 先转义再渲染极少量 Markdown（仅 **粗体**），用于表格单元格等需要强调的位置。 */
  function escMd(text) {
    return esc(text).replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  }

  function fmtAnswer(text) {
    let html = esc(text || "");
    html = html.replace(/【([^】]{1,24})】/g, '<strong>【$1】</strong>');
    return html;
  }

  async function api(path, options) {
    const res = await fetch(path, options);
    const text = await res.text();
    let data;
    try { data = JSON.parse(text); } catch (e) { data = { raw: text }; }
    if (!res.ok) throw new Error((data && data.error) || ("HTTP " + res.status));
    return data;
  }

  function toast(message) {
    const box = document.createElement("div");
    box.className = "notification";
    box.textContent = message;
    $("#notifications").append(box);
    setTimeout(() => box.remove(), 7000);
  }

  // ------------------------------------------------------------ 启动
  async function boot() {
    // 支持用 URL 查询参数直接打开某个视图 / 预置提问（便于演示与自动化截图）
    //   例：/?view=risk      打开风险实证
    //       /?q=...&module=M2&ask=1   进入页面后自动提问
    const params = new URLSearchParams(location.search);
    const wantView = params.get("view");
    await StudyUI.init({ notify: toast, switchView, newSession, startLearning, resumeSession });

    try {
      const health = await api("/api/health");
      state.health = health;
      const dot = $("#statusDot");
      dot.className = "dot ok";
      const usable = (health.usable_models || []).filter(n => n !== "mock");
      const sim = !usable.length;
      $("#statusText").innerHTML = sim
        ? '离线演示 · 预设回答'
        : '已连接 · 可用模型：' + esc(usable.join("、"));
      if (health.image) state.imageCfg = Object.assign(state.imageCfg, health.image);
      state.visionModel = health.vision_model || null;
      $("#visionHint").textContent = state.visionModel
        ? `由 ${state.visionModel} 识别（支持题目照片 / 截图 / 手写演算）`
        : "当前无视觉模型，将使用离线模型（只做链路验证）";
      $("#footInfo").textContent =
        health.app + " v" + health.version + " · " + health.team;
      renderAbout(health);
      StudyUI.mode(health);
    } catch (err) {
      $("#statusDot").className = "dot bad";
      $("#statusText").textContent = "后端未响应：" + err.message;
      $("#modeNotice").textContent = "本地服务未响应，请重新启动服务后刷新页面。";
    }

    try {
      const mods = await api("/api/prompts");
      state.modules = mods.modules || [];
      state.methodology = mods.methodology || null;
      fillModules();
      renderLibrary();
    } catch (e) { /* ignore */ }

    try {
      const data = await api("/api/models");
      state.models = data.models || [];
      fillModels();
    } catch (e) { /* ignore */ }

    try {
      const data = await api("/api/routes");
      state.routes = data.routes || [];
    } catch (e) { /* ignore */ }

    if (state.health) renderAbout(state.health);
    renderExamples();
    loadSessions();
    loadEvals();
    if (wantView) switchView(wantView);

    const presetQ = params.get("q");
    if (!presetQ && local.get("session")) {
      await resumeSession(local.get("session"), false);
    }
    $("#questionInput").value = local.get("draft") || "";
    if (presetQ) {
      if (params.get("module")) setModule(params.get("module"));
      $("#questionInput").value = presetQ;
      if (params.get("ask") === "1") send();
    }
  }

  function switchView(view) {
    const tab = $$("#tabs .tab").find((t) => t.dataset.view === view);
    if (!tab) return;
    $$("#tabs .tab").forEach((t) => t.classList.toggle("active", t === tab));
    $$(".view").forEach((v) => v.classList.toggle("active", v.id === "view-" + view));
    if (view === "records") { loadSessions(); loadEvals(); }
    if (view === "risk") { loadCases(); }
  }

  function newSession() {
    if (state.busy) return;
    state.sessionId = null;
    local.set("session", "");
    clearImages();
    ["#m5Answers", "#m6Reply", "#m3Answer"].forEach(s => $(s).value = "");
    $("#sessionHint").textContent = "新会话 · 课程范围会作为学习背景";
    $("#messages").innerHTML = '<div class="empty" id="emptyState"><h3>从你卡住的地方开始</h3><p>写下问题和你的尝试，或选择一个学习例子。</p><div class="examples" id="examples"></div></div>';
    renderExamples();
  }

  function startLearning(courseId, question, module) {
    if (state.busy) { toast("请等当前回答完成后再切换课程"); return; }
    newSession();
    $("#chatCourse").value = courseId;
    setModule(module);
    $("#questionInput").value = question;
    local.set("draft",question);
    switchView("chat");
    $("#questionInput").focus();
  }

  async function resumeSession(id, navigate = true) {
    if (state.busy) { toast("请等当前回答完成后再切换会话"); return; }
    try {
      const session = await api("/api/session/" + encodeURIComponent(id));
      state.sessionId = id; local.set("session",id);
      clearImages();
      $("#chatCourse").value = session.meta?.course_id || "";
      setModule(session.module || "auto");
      $("#messages").innerHTML = "";
      (session.turns || []).forEach(t => {
        appendUser(t.user_prompt, []);
        const result = {answer:t.output_raw, question:t.user_prompt, module:t.module, prompt:t.prompt_raw,
          turn_id:t.id, session_id:id, session, checks:t.metrics, meta:{provider:t.provider,model:t.model,simulated:!!t.simulated,degraded:!!t.degraded},
          notes:t.degraded_reason ? [t.degraded_reason] : []};
        const el = appendAi(resultHtml(result)); StudyUI.bindResult(el,result);
      });
      $("#sessionHint").textContent = "继续会话：" + session.title;
      if (navigate) switchView("chat");
    } catch(err) { local.set("session", ""); toast("无法恢复会话：" + err.message); }
  }

  function fillModules() {
    const sel = $("#moduleSelect");
    sel.innerHTML = '<option value="auto">自动识别（推荐）</option>' +
      state.modules.map((m) => `<option value="${m.id}">${m.id} · ${esc(m.title)}</option>`).join("");
    sel.value = state.module;
  }

  function fillModels() {
    const sel = $("#modelSelect");
    sel.innerHTML = '<option value="">使用默认模型</option>' +
      state.models.map((m) => {
        const tags = [];
        if (m.supports_vision) tags.push("图片");
        if (!m.available) tags.push("未配置 Key");
        const suffix = tags.length ? "（" + tags.join(" · ") + "）" : "";
        return `<option value="${m.provider}"${m.available ? "" : " disabled"}>` +
          `${esc(m.provider)} · ${esc(m.model)}${suffix}</option>`;
      }).join("");
  }

  function renderExamples() {
    const examples = [
      ["M1", "帮我解这道题：lim(x→0) (sin x - x) / x³"],
      ["M2", "帮我整理一下 TCP 协议的知识点"],
      ["M4", "帮我解释一下递归是什么"],
      ["M3", "这道题我第 2 步把 cos x − 1 直接替换成了 0，帮我分析错在哪"],
      ["M5", "帮我做个高等数学的复习计划，还剩 5 天"],
      ["M6", "陪我练一下计算机网络三次握手的背诵"],
      ["M1", "1998 年教育部颁布的《高等学校教学质量标准》中，本科实验课时占比是多少？"],
    ];
    $("#examples").innerHTML = examples
      .map(([m, q]) => `<button data-example="${esc(q)}" data-module="${m}">${esc(q.slice(0, 32))}…</button>`)
      .join("");
    $$("#examples button").forEach((btn) => {
      btn.addEventListener("click", () => {
        $("#questionInput").value = btn.dataset.example;
        setModule(btn.dataset.module);
        local.set("draft",btn.dataset.example);
        $("#questionInput").focus();
      });
    });
  }

  // ------------------------------------------------------------ 模块与模式
  function setModule(id) {
    state.module = id;
    $("#moduleSelect").value = id;
    updateModuleUI();
  }

  function updateModuleUI() {
    const id = state.module;
    const auto = id === "auto";
    const mod = state.modules.find((m) => m.id === id);
    $("#moduleDesc").textContent = auto
      ? "自动识别：按你的提问内容路由到最合适的模块"
      : `${mod ? mod.title : ""} —— 对应痛点：${mod ? mod.pain_point : ""}｜结构：${mod ? mod.pattern_label : ""}`;
    $("#extraM5").hidden = id !== "M5";
    $("#extraM5b").hidden = id !== "M5";
    $("#extraM6").hidden = id !== "M6";
    $("#extraM3").hidden = id !== "M3";
    if (id === "M5") $("#m5Subject").value = $("#m5Subject").value || "高等数学";
  }

  // ------------------------------------------------------------ 提问
  function checksHtml(checks) {
    if (!checks) return "";
    const items = [
      ["结构完整度", checks.structure_score, true],
      ["不确定标记", checks.honesty_score, true],
      ["教学价值", checks.teaching_score, true],
      ["结构覆盖率", checks.section_coverage, true],
    ];
    return '<div class="checks">' + items.map(([k, v]) => {
      const pct = Math.round((Number(v) || 0) * 100);
      return `<div class="check"><div class="k">${k}</div><div class="v">${pct}%</div>
        <div class="bar"><i style="width:${pct}%"></i></div></div>`;
    }).join("") + "</div>" +
      (checks.redline_hits && Object.keys(checks.redline_hits).length
        ? '<div class="notes" style="color:var(--muted)">' + Object.entries(checks.redline_hits)
            .map(([k, v]) => `${esc(k)} ${v ? "✓" : "✗"}`).join("　") + "</div>"
        : "");
  }

  function metaHtml(result) {
    const meta = result.meta || {};
    const bits = [];
    bits.push('<span class="badge fine">精细提示</span>');
    bits.push(`<span class="badge muted">${esc(result.module)}</span>`);
    bits.push(`<span class="badge muted">${esc(meta.provider)} / ${esc(meta.model)}</span>`);
    if (meta.elapsed_ms != null) bits.push(`<span class="badge muted">${meta.elapsed_ms} ms</span>`);
    if (meta.total_tokens) bits.push(`<span class="badge muted">${meta.total_tokens} tokens</span>`);
    if (meta.vision) bits.push(`<span class="badge vision">图片 ×${(meta.images || []).length}</span>`);
    if (meta.source === "router") bits.push(`<span class="badge muted">任务路由：${esc(meta.task)}</span>`);
    if (meta.vision_switch) {
      bits.push(`<span class="badge vision">视觉切换：${esc(meta.vision_switch.to)}</span>`);
    }
    if (meta.truncated) bits.push('<span class="badge warn">输出被截断</span>');
    if (meta.simulated) bits.push('<span class="badge sim">离线模拟输出</span>');
    if (meta.degraded) bits.push('<span class="badge warn">已降级</span>');
    return bits.join(" ");
  }

  /** 展示图片缩略图（用户消息里的原图 / 结果里的图片元信息） */
  function imagesHtml(images) {
    if (!images || !images.length) return "";
    return '<div class="msg-images">' + images.map((img) => {
      const src = img.dataUri || img.preview || "";
      const meta = esc(img.name || "image") +
        (img.size ? ` · ${(img.size / 1024).toFixed(0)} KB` : "");
      return src
        ? `<figure class="msg-image"><img src="${src}" alt="${meta}"><figcaption>${meta}</figcaption></figure>`
        : `<span class="tag">${meta}</span>`;
    }).join("") + "</div>";
  }

  function resultHtml(result) {
    const notes = (result.notes || []).map((n) => `<div>⚠ ${esc(n)}</div>`).join("");
    return `
      <div class="msg-head">${metaHtml(result)}</div>
      <div class="answer">${fmtAnswer(result.answer)}</div>
      ${state.module === "auto" && result.meta && result.meta.preflight && result.meta.preflight.length
        ? `<div class="notes">${result.meta.preflight.map((p) => `<div>· ${esc(p)}</div>`).join("")}</div>` : ""}
      <details><summary>查看回答的格式检查（不能验证事实正确性或掌握程度）</summary>${checksHtml(result.checks)}</details>
      ${notes ? `<div class="notes">${notes}</div>` : ""}
      <details><summary>查看实际发送的提示词原文（${(result.prompt || "").length} 字符）</summary>
        <div class="prompt-box">${esc(result.prompt)}</div>
      </details>`;
  }

  function appendUser(text, images) {
    const div = document.createElement("div");
    div.className = "msg user";
    div.innerHTML = `<div class="msg-head"><span class="badge muted">你</span></div>
      ${imagesHtml(images)}
      <div class="answer">${esc(text)}</div>`;
    $("#messages").appendChild(div);
    $("#messages").scrollTop = $("#messages").scrollHeight;
  }

  function appendAi(inner, cls) {
    const div = document.createElement("div");
    div.className = "msg ai " + (cls || "");
    div.innerHTML = inner;
    $("#messages").appendChild(div);
    $("#messages").scrollTop = $("#messages").scrollHeight;
    return div;
  }

  async function send() {
    let question = $("#questionInput").value.trim();
    if (!question && state.module === "M5" && $("#m5Answers").value.trim()) question = "请根据我的诊断作答继续制定复习计划。";
    if (!question && state.module === "M6" && $("#m6Reply").value.trim()) question = "请反馈本轮作答并继续陪练。";
    if (!question && state.module === "M3" && $("#m3Answer").value.trim()) question = "请分析我的解题过程并定位错误。";
    const replySelector = {M5:"#m5Answers",M6:"#m6Reply",M3:"#m3Answer"}[state.module];
    if (replySelector && $(replySelector).value.trim()) question += "\n\n【我的作答】\n" + $(replySelector).value.trim();
    const images = state.images.map((i) => i.dataUri);
    if ((!question && !images.length) || state.busy) return;
    if (state.pendingImages) { toast("图片还在读取，请稍后发送"); return; }
    const attached = state.images.slice();
    $("#emptyState") && $("#emptyState").remove();
    appendUser(question || "（仅图片，请识别并解析）", state.images.slice());
    setBusy(true);
    const placeholder = appendAi('<div class="msg-head"><span class="spinner"></span> 正在生成…</div>');

    const payload = {
      question: question,
      module: state.module,
      model: $("#modelSelect").value || null,
      use_router: $("#routerToggle").checked,
      session_id: state.sessionId,
      course_id: StudyUI.courseId(),
      images: images,
      subject: $("#m5Subject").value || "",
      days: parseInt($("#m5Days").value || "5", 10),
      answers: $("#m5Answers").value || "",
      student_reply: $("#m6Reply").value || "",
      student_answer: $("#m3Answer").value || "",
      level: "零基础",
    };

    try {
      const data = await api("/api/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      state.sessionId = data.session_id || state.sessionId;
      local.set("session",state.sessionId);
      local.set("draft", "");
      $("#questionInput").value = "";
      clearImages();
      placeholder.innerHTML = resultHtml(data);
      StudyUI.bindResult(placeholder,data);
      $("#sessionHint").textContent = "当前会话：" + (data.session?.title || "学习答疑");
      StudyUI.refresh();
      loadSessions();
      if (state.module === "M5") $("#m5Answers").value = "";
      if (state.module === "M6") $("#m6Reply").value = "";
    } catch (err) {
      state.images = attached; renderThumbs();
      placeholder.innerHTML = `<div class="notes"><div>调用失败：${esc(err.message)}。问题与附件已保留，可再次发送。</div></div>`;
    } finally {
      setBusy(false);
      $("#messages").scrollTop = $("#messages").scrollHeight;
    }
  }

  // ------------------------------------------------------------ 图片输入
  function clearImages() {
    state.imageGeneration += 1;
    state.pendingImages = 0;
    state.images = [];
    renderThumbs();
    $("#fileInput").value = "";
  }

  function renderThumbs() {
    $("#sendBtn").disabled = state.busy || state.pendingImages > 0;
    const box = $("#thumbs");
    box.innerHTML = state.images.map((img) => `
      <figure class="thumb" data-id="${img.id}">
        <img src="${img.dataUri}" alt="${esc(img.name)}">
        <button type="button" class="thumb-del" title="移除">×</button>
        <figcaption>${esc(img.name)}<br>${(img.size / 1024).toFixed(0)} KB</figcaption>
      </figure>`).join("");
    $$(".thumb .thumb-del").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        const id = e.target.closest(".thumb").dataset.id;
        state.images = state.images.filter((i) => i.id !== id);
        renderThumbs();
      });
    });
    const hint = $("#imageHint");
    if (state.images.length) {
      hint.hidden = false;
      hint.textContent = `已附加 ${state.images.length}/${state.imageCfg.max_images} 张图片`;
    } else {
      hint.hidden = true;
    }
  }

  function addFiles(fileList) {
    const files = Array.from(fileList || []);
    if (!files.length) return;
    const maxBytes = state.imageCfg.max_bytes || 8388608;
    const allowed = state.imageCfg.allowed_types || ["png", "jpg", "jpeg", "webp", "gif"];
    if (state.busy) return;
    files.forEach((file) => {
      const ext = (file.name.split(".").pop() || "").toLowerCase();
      if (!allowed.includes(ext)) {
        toast(`不支持的图片格式：${file.name}（支持 ${allowed.join(" / ")}）`);
        return;
      }
      if (file.size > maxBytes) {
        toast(`图片过大：${file.name}（${(file.size / 1048576).toFixed(1)} MB，上限 ${(maxBytes / 1048576).toFixed(0)} MB）`);
        return;
      }
      if (state.images.length + state.pendingImages >= (state.imageCfg.max_images || 4)) {
        toast(`一次最多 ${state.imageCfg.max_images || 4} 张图片`);
        return;
      }
      const reader = new FileReader();
      const generation = state.imageGeneration;
      state.pendingImages += 1;
      $("#sendBtn").disabled = true;
      reader.onload = () => {
        if (generation !== state.imageGeneration) return;
        state.pendingImages -= 1;
        if (state.images.length >= (state.imageCfg.max_images || 4)) { toast("已达到图片数量上限"); renderThumbs(); return; }
        state.images.push({
          id: "img" + Date.now() + Math.random().toString(16).slice(2, 6),
          name: file.name || "pasted-image.png",
          size: file.size,
          dataUri: String(reader.result || ""),
        });
        renderThumbs();
      };
      reader.onerror = () => {
        if(generation !== state.imageGeneration) return;
        state.pendingImages -= 1; renderThumbs(); toast(`读取失败：${file.name}`);
      };
      reader.readAsDataURL(file);
    });
  }

  function setBusy(busy) {
    state.busy = busy;
    $("#sendBtn").disabled = busy || state.pendingImages > 0;
    ["#resetBtn", "#chatCourse", "#moduleSelect", "#modelSelect", "#routerToggle", "#questionInput", "#m5Subject", "#m5Days", "#m5Answers", "#m6Reply", "#m3Answer", "#fileInput"].forEach(s => $(s).disabled = busy);
    $$("#examples button, .thumb-del").forEach(el => el.disabled = busy);
    $("#composerHint").innerHTML = busy ? '<span class="spinner"></span> 正在组织讲解，通常需要 10–60 秒…'
      : "Enter 发送 · Shift+Enter 换行 · Ctrl+V 粘贴图片";
  }

  // ------------------------------------------------------------ 提示词库
  function renderLibrary() {
    const m = state.methodology || {};
    $("#methodology").innerHTML = m.five_elements ? `
      <table><tr><th>设计要素</th><th>做法</th></tr>
      ${m.five_elements.map((e) => `<tr><td><strong>${esc(e.element)}</strong></td>
        <td>${escMd(e.desc)}</td></tr>`).join("")}</table>
      <p class="hintline" style="margin-top:12px">
        精细提示 = 角色锁定 + 格式模板 + 质量红线 + 诚实边界 + 受众适配</p>` : "";

    $("#structures").innerHTML = m.structures ? Object.entries(m.structures).map(([key, p]) => `
      <div class="lib-card"><h3>${esc(p.label)} <span class="tag">${esc(key)}</span>
        <span class="tag">${p.requirements.length} 段</span></h3>
      <ul class="req-list">${p.requirements.map((r) => `<li>${escMd(r)}</li>`).join("")}</ul>
      </div>`).join("") : "";

    $("#libraryBody").innerHTML = state.modules.map((mod) => {
      return `<div class="lib-card">
        <h3>${mod.id} · ${esc(mod.title)}</h3>
        <div class="lib-meta">
          <span class="tag">痛点：${esc(mod.pain_point)}</span>
          <span class="tag">${esc(mod.pattern_label)}</span>
          <span class="tag">路由：${esc(mod.route_task)}</span>
          <span class="tag">温度：${mod.temperature}</span>
        </div>
        <p class="hintline">${esc(mod.goal)}</p>
        <p class="hintline"><strong>受众：</strong>${esc(mod.audience)}</p>
        <div class="prompt-box">${esc(mod.fine_prompt || "")}</div>
      </div>`;
    }).join("");
  }

  // ------------------------------------------------------------ 风险实证
  function summaryHtml(report) {
    const summaries = report.summaries || [];
    const rows = summaries.map((s) => {
      const v = s.verdict || (s.extra && s.extra.verdict) || {};
      return `<tr>
        <td>${esc(s.label || s.model)}</td>
        <td>${s.cases}</td>
        <td class="${s.hallucination_rate > 0.2 ? "pill-fail" : "pill-pass"}">
          ${s.hallucination_cases ? Math.round(s.hallucination_rate * 100) + "%" : "—"}</td>
        <td class="${s.logic_error_rate > 0.2 ? "pill-fail" : "pill-pass"}">
          ${s.logic_cases ? Math.round(s.logic_error_rate * 100) + "%" : "—"}</td>
        <td>${s.repetition_overall ? Math.round(s.repetition_overall * 100) + "%" : "—"}</td>
        <td>${Math.round((s.avg_structure || 0) * 100)}%</td>
        <td>${Math.round((s.avg_uncertainty || 0) * 100)}%</td>
        <td>${s.avg_chars}</td>
        <td>${s.simulated ? '<span class="badge sim">离线模拟</span>' : '<span class="badge muted">真实模型</span>'}</td>
      </tr>`;
    }).join("");

    const findings = (report.findings || []).map((f) => `<li>${esc(f)}</li>`).join("");
    const suggestions = (report.suggestions || []).map((f) => `<li>${esc(f)}</li>`).join("");
    const details = (report.details || []).slice(0, 12).map((d) => `
      <details><summary>${esc(d.case_id)} · ${esc(d.title)}（${d.mode === "plain" ? "无约束提示" : "精细提示"}）
        ${d.verdict && d.verdict.risk_hit ? '<span class="pill-fail">命中风险</span>' : '<span class="pill-pass">通过</span>'}</summary>
        <div class="prompt-box">${esc(d.answer)}</div>
      </details>`).join("");

    return `
      <h3 style="margin-top:6px">${esc(report.title)}</h3>
      <table><tr><th>对照组</th><th>题数</th><th>幻觉率</th><th>逻辑错误率</th><th>重复度</th>
        <th>结构完整度</th><th>不确定标记</th><th>平均字数</th><th>来源</th></tr>${rows}</table>
      <div class="findings"><h4>关键发现</h4><ul>${findings}</ul></div>
      <div class="findings"><h4>改进建议</h4><ul>${suggestions}</ul></div>
      <h4>逐条记录（可展开查看输出原文）</h4>${details}`;
  }

  async function runRisk(kind, btn) {
    const status = $("#riskStatus");
    const buttons = $$(".risk-actions button");
    buttons.forEach((b) => (b.disabled = true));
    status.innerHTML = '<span class="spinner"></span> 正在执行测试，可能需要几十秒…';
    try {
      const data = await api("/api/risk", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind: kind, rounds: 3 }),
      });
      const reports = kind === "all" ? ["risk_pair", "repetition", "comparison"].map((k) => data[k]).filter(Boolean)
        : [data];
      $("#riskResult").innerHTML = reports.map(summaryHtml).join("<hr style='border:none;border-top:1px solid var(--line);margin:22px 0'>");
      status.textContent = "测试完成，报告已存盘（data/reports/）";
      loadEvals();
    } catch (err) {
      status.innerHTML = `<span class="pill-fail">执行失败：${esc(err.message)}</span>`;
    } finally {
      buttons.forEach((b) => (b.disabled = false));
    }
  }

  async function loadCases() {
    try {
      const data = await api("/api/prompts");
      const mods = data.modules || [];
      $("#caseList").innerHTML = `<p class="hintline">用例定义在 <code>ai_tutor/eval/cases.py</code>，
        可通过 <code>python -m ai_tutor risk list</code> 查看，<code>risk run</code> 执行。</p>
        <table><tr><th>类别</th><th>编号</th><th>标题</th><th>判定方式</th></tr>
        <tr><td>事实幻觉</td><td>H1–H6</td><td>虚构教材例题数值 / 不存在的 SCI 文献 DOI / 不存在的政策条文 / 概念混淆 / 无来源精确数据 / 虚构页码</td><td>正则识别编造模式 + 合规声明</td></tr>
        <tr><td>逻辑缺陷</td><td>L1–L5</td><td>极值充分性 / 单一归因 / 方向倒置 / 充要条件混淆 / 连续与可导关系链</td><td>核对正确结论关键词</td></tr>
        <tr><td>同质化</td><td>T1</td><td>同一任务重复 3 次（+ 反同质化提示对照）</td><td>两两平均相似度</td></tr>
        <tr><td>多工具对比</td><td>C1–C10</td><td>幻觉 5 题 + 逻辑 5 题，三模型同题对比</td><td>按维度汇总</td></tr>
        </table>`;
    } catch (e) { /* ignore */ }
  }

  // ------------------------------------------------------------ 记录
  async function loadSessions() {
    try {
      const data = await api("/api/sessions?limit=20");
      const sessions = data.sessions || [];
      $("#sessionList").innerHTML = sessions.length
        ? sessions.map((s) => `<div class="session-item" data-id="${s.id}">
            <div class="t">${esc(s.title)} <span class="tag">${esc(s.module || "—")}</span>
              <span class="tag">${s.turn_count} 轮</span></div>
            <div class="m">${esc(s.updated_at)} · ${esc(s.model || "")} · 点击展开对话</div>
            <div class="turns"></div></div>`).join("")
        : '<p class="hintline">暂无会话记录，去「答疑工作台」问一个问题吧。</p>';
      $$(".session-item").forEach((el) => {
        const resume = document.createElement("button"); resume.textContent = "继续学习"; resume.className = "ghost small";
        resume.onclick = e => { e.stopPropagation(); resumeSession(el.dataset.id); };
        el.querySelector(".t").append(resume);
        el.addEventListener("click", async () => {
          const box = el.querySelector(".turns");
          if (box.dataset.loaded) { box.innerHTML = ""; box.dataset.loaded = ""; return; }
          let detail;
          try { detail = await api("/api/session/" + el.dataset.id); } catch(err) { toast(err.message); return; }
          box.dataset.loaded = "1";
          box.innerHTML = (detail.turns || []).map((t) => `
            <div class="turn"><div class="q">Q：${esc(t.user_prompt)}</div>
            <div class="a">${esc((t.output_raw || "").slice(0, 1200))}</div></div>`).join("")
            || '<p class="hintline">该会话暂无对话。</p>';
        });
      });
    } catch (e) { /* ignore */ }
  }

  async function loadEvals() {
    try {
      const data = await api("/api/evals?limit=20");
      const runs = data.runs || [];
      $("#evalList").innerHTML = runs.length
        ? `<table><tr><th>时间</th><th>类型</th><th>标题</th><th>报告文件</th></tr>
           ${runs.map((r) => `<tr><td>${esc(r.created_at)}</td><td>${esc(r.kind)}</td>
             <td>${esc(r.title)}</td><td class="mono">${esc(r.file_path || "—")}</td></tr>`).join("")}</table>`
        : '<p class="hintline">暂无评测批次，点击「风险实证」中的按钮执行测试。</p>';
    } catch (e) { /* ignore */ }
  }

  async function loadReport() {
    $("#reportBox").textContent = "生成中…";
    try {
      const res = await fetch("/api/report?limit=15");
      $("#reportBox").textContent = await res.text();
    } catch (err) {
      $("#reportBox").textContent = "生成失败：" + err.message;
    }
  }

  // ------------------------------------------------------------ 关于
  function renderAbout(health) {
    const stats = health.storage || {};
    $("#aboutBox").innerHTML = `
      <h2>大学生期末复习 AI 答疑助手 · 项目说明</h2>
      <p class="hintline">智能驱动的个性化学习解决方案 —— 从选题调研、功能模块与提示词库设计，
        到风险实证分析与多工具横向对比的完整实践。</p>
      <div class="about-grid">
        <div class="about-card"><h3>阶段一 · 选题与能力设计</h3>
          <ul><li>六大复习痛点：解析难懂 / 框架难理 / 错题难归 / 概念难明 / 弱点难辨 / 练习难伴</li>
          <li>系统能力：习题解析、知识梳理、错题归纳、通俗讲解、薄弱检测、口语陪练</li>
          <li>能力边界：抵制作弊、拒绝虚假押题、聚焦教学大纲、非官方批改</li></ul></div>
        <div class="about-card"><h3>阶段二 · 模块与提示词库</h3>
          <ul><li>M1–M6 六大功能模块，一一对应六大痛点</li>
          <li>精细提示词库：六大模块的结构化模板（六/七/八段式契约）</li>
          <li>方法论：角色锁定 + 格式模板 + 质量红线 + 诚实边界 + 受众适配</li>
          <li>对外无提示词模式开关，提问即按对应模块模板执行</li></ul></div>
        <div class="about-card"><h3>阶段三 · 风险实证</h3>
          <ul><li>事实幻觉 H1–H6：教材例题数值、SCI 文献 DOI、政策条文、页码等</li>
          <li>逻辑缺陷 L1–L5：充分性、单一归因、方向倒置、充要条件混淆</li>
          <li>同质化 T1：同一任务重复 3 次，两两相似度 + 反同质化缓解验证</li>
          <li>提示词消融对照：精细提示 vs 无约束提示（内部评测机制）</li></ul></div>
        <div class="about-card"><h3>工程实现</h3>
          <ul><li>零第三方依赖（仅 Python 标准库），完全离线可运行</li>
          <li>多模型接入：OpenAI 兼容协议 / Anthropic / 百度千帆，任一失败自动降级</li>
          <li>图片输入：题目照片 / 截图 / 手写演算，自动走视觉模型（点击/拖拽/粘贴）</li>
          <li>任务路由：事实核查走保守模型、结构化走结构最优模型（可开关）</li>
          <li>全量落库：SQLite 保存提示词原文、输出原文、质检指标与评测批次</li></ul></div>
      </div>
      <h3 style="margin-top:18px">运行状态</h3>
      <table><tr><th>项目</th><th>值</th></tr>
        <tr><td>应用 / 团队</td><td>${esc(health.app)} v${esc(health.version)} · ${esc(health.team)}</td></tr>
        <tr><td>默认模型</td><td class="mono">${esc(health.default_model)}</td></tr>
        <tr><td>可用模型</td><td>${esc((health.usable_models || health.available_models || []).join("、") || "（无，使用离线模型）")}</td></tr>
        <tr><td>图片识别模型</td><td>${esc(health.vision_model || "（无可用视觉模型）")}</td></tr>
        <tr><td>图片限制</td><td>最多 ${(health.image || {}).max_images || 4} 张，
          单张 ≤ ${Math.round(((health.image || {}).max_bytes || 8388608) / 1048576)} MB，
          支持 ${esc(((health.image || {}).allowed_types || []).join(" / "))}</td></tr>
        <tr><td>会话 / 轮次 / 产出物</td><td>${stats.sessions || 0} / ${stats.turns || 0} / ${stats.artifacts || 0}</td></tr>
        <tr><td>风险记录 / 评测批次</td><td>${stats.risk_interactions || 0} / ${stats.eval_runs || 0}</td></tr>
        <tr><td>数据库</td><td class="mono">${esc(stats.db_path || "")}</td></tr>
      </table>
      <h3 style="margin-top:18px">命令行入口</h3>
      <div class="prompt-box">python -m ai_tutor status              查看运行状态
python -m ai_tutor chat                交互式答疑（:image 附加图片）
python -m ai_tutor ask "问题" -i 图.png  单次提问（支持图片）
python -m ai_tutor web                 启动本界面
python -m ai_tutor risk run --kind all 执行四类风险实证测试
python -m ai_tutor export all          导出交付文档与提示词库
python -m ai_tutor selftest            一键自检（离线、可复现）</div>
      <h3 style="margin-top:18px">任务路由表</h3>
      <table><tr><th>任务类型</th><th>当前选用</th><th>优先级</th><th>温度</th><th>依据</th></tr>
      ${state.routes.map((r) => `<tr><td>${esc(r.label)}</td><td class="mono">${esc(r.resolved_model)}</td>
        <td>${esc((r.preferred || []).join(" → "))}</td><td>${r.temperature}</td>
        <td>${esc(r.reason)}</td></tr>`).join("")}</table>
      <p class="hintline" style="margin-top:14px">
        接入真实答疑：设置提供方环境变量（如 <code>DEEPSEEK_API_KEY</code>），或在本地 <code>config.json</code> 配置密钥，然后重启服务。不要提交含密钥的配置文件。离线模型只提供预设演示，不能可靠回答任意新题。<br>
        数据与隐私：本产品用于个人本地学习，默认只监听 127.0.0.1；课程、会话、笔记保存在本地 SQLite。真实模型会接收问题、会话上下文、课程背景与图片。导出入口在「笔记与复习」。<br>
        学习提醒：任务完成率不是掌握程度；格式检查不是事实核验。请以课程教材、独立作答与自测结果核对 AI 讲解。</p>`;
  }

  // ------------------------------------------------------------ 事件绑定
  function bind() {
    $$("#tabs .tab").forEach((tab) => {
      tab.addEventListener("click", () => {
        switchView(tab.dataset.view);
      });
    });

    $("#moduleSelect").addEventListener("change", (e) => setModule(e.target.value));

    // 图片输入：点击选择 / 拖拽 / 粘贴
    $("#dropzone").addEventListener("click", () => $("#fileInput").click());
    $("#dropzone").addEventListener("keydown", e => { if(e.key === "Enter" || e.key === " ") { e.preventDefault(); $("#fileInput").click(); } });
    $("#fileInput").addEventListener("change", (e) => {
      addFiles(e.target.files);
      e.target.value = "";
    });
    ["dragenter", "dragover"].forEach((evt) => {
      $("#dropzone").addEventListener(evt, (e) => {
        e.preventDefault();
        $("#dropzone").classList.add("over");
      });
    });
    ["dragleave", "drop"].forEach((evt) => {
      $("#dropzone").addEventListener(evt, (e) => {
        e.preventDefault();
        $("#dropzone").classList.remove("over");
      });
    });
    $("#dropzone").addEventListener("drop", (e) => {
      if (e.dataTransfer && e.dataTransfer.files) addFiles(e.dataTransfer.files);
    });
    document.addEventListener("paste", (e) => {
      if (!e.clipboardData || !$("#view-chat").classList.contains("active") || state.busy) return;
      const files = Array.from(e.clipboardData.files || []);
      const items = Array.from(e.clipboardData.items || [])
        .filter((it) => it.kind === "file" && it.type.startsWith("image/"))
        .map((it) => it.getAsFile())
        .filter(Boolean);
      const all = files.length ? files : items;
      if (all.length) { e.preventDefault(); addFiles(all); }
    });

    $("#askForm").addEventListener("submit", (e) => { e.preventDefault(); send(); });
    $("#questionInput").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
    });
    $("#questionInput").addEventListener("input", e => local.set("draft",e.target.value));
    $("#resetBtn").addEventListener("click", () => {
      newSession();
    });
    $$(".risk-actions button").forEach((btn) => {
      btn.addEventListener("click", () => runRisk(btn.dataset.risk, btn));
    });
    $("#reloadSessions").addEventListener("click", loadSessions);
    $("#loadReport").addEventListener("click", loadReport);
  }

  bind();
  boot();
  setBusy(false);
})();
