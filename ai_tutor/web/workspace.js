/* Learning workspace: deterministic plans, saved provenance, retrieval before answers. */
window.StudyUI = (() => {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const today = () => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`; };
  let data = null, bridge, selectedCourse = "", dialogSave;
  const revealed = new Set();
  const recallDrafts = new Map();
  async function api(path, payload) {
    const res = await fetch(path, payload === undefined ? {} : {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
    const result = await res.json();
    if (!res.ok) throw new Error(result.error || "服务暂时不可用，请重试");
    return result;
  }
  function notify(message) { bridge.notify(message); }
  function empty(title, text) { return `<div class="empty compact"><h3>${esc(title)}</h3><p>${esc(text)}</p></div>`; }
  async function refresh() {
    try {
      data = await api("/api/study?today=" + today());
      render();
    } catch (err) {
      $("#todayTasks").innerHTML = empty("学习数据暂时未加载", "请检查本地服务后点击刷新。" + err.message);
      notify("加载失败：" + err.message);
    }
  }
  function fillSelect(selector, label, includeArchived = false) {
    const select = $(selector), value = select.value;
    select.innerHTML = `<option value="">${label}</option>` + data.courses.filter(c => includeArchived || !c.archived).map(c => `<option value="${esc(c.id)}">${esc(c.name)}${c.archived ? "（已归档）" : ""}</option>`).join("");
    select.value = value;
  }
  function taskHtml(t) {
    return `<div class="task-row ${t.done ? "done" : ""}"><label class="checkbox"><input type="checkbox" data-complete="${esc(t.id)}" ${t.done ? "checked" : ""}><span><strong>${esc(t.title)}</strong><small>${esc(t.course_name)} · ${t.minutes} 分钟 · ${esc(t.due_date)}${!t.done && t.due_date < today() ? " · 待补上" : ""}</small></span></label><div class="actions"><button data-learn="${esc(t.id)}">${t.kind === "recall" ? "开始自测" : "去学习"}</button>${!t.done ? `<button data-move="${esc(t.id)}" class="ghost small">调整日期</button>` : ""}</div></div>`;
  }
  function bindTasks(root) {
    root.querySelectorAll("[data-complete]").forEach(el => el.addEventListener("change", async () => {
      const done = el.checked; el.disabled = true;
      try { await api("/api/study/task/update", {id:el.dataset.complete, done}); await refresh(); }
      catch (e) { el.checked = !done; el.disabled = false; notify(e.message); }
    }));
    root.querySelectorAll("[data-learn]").forEach(el => el.addEventListener("click", () => {
      const task = data.tasks.find(t => t.id === el.dataset.learn);
      const course = data.courses.find(c => c.id === task.course_id);
      const questions = {understand:`帮我梳理「${task.topic}」的核心知识、重点机制和常见误区。`,
        practice:`我正在复习「${task.topic}」。请讲解一类典型题的解题思路，最后给一道同类练习让我独立尝试。`,
        recall:`请围绕「${task.topic}」陪我做闭卷自测，每轮只问一个问题，等我回答后再反馈。`,
        custom:`我准备完成「${task.title}」。请帮我拆解学习步骤，先让我尝试。`};
      bridge.startLearning(course.id, questions[task.kind] || questions.custom, task.kind === "understand" ? "M2" : task.kind === "recall" ? "M6" : "M1");
    }));
    root.querySelectorAll("[data-move]").forEach(el => el.addEventListener("click", () => {
      const task = data.tasks.find(t => t.id === el.dataset.move);
      openDialog("调整学习任务", `<label for="taskTitle">任务名称</label><input id="taskTitle" name="title" maxlength="200" value="${esc(task.title)}" required><label for="taskDate">计划日期</label><input id="taskDate" name="due_date" type="date" value="${esc(task.due_date)}" required><label for="taskMinutes">预计分钟</label><input id="taskMinutes" name="minutes" type="number" min="5" max="480" value="${task.minutes}" required>`, async form => {
        await api("/api/study/task/update", {id:task.id, due_date:form.get("due_date"),title:form.get("title"),minutes:Number(form.get("minutes"))});
      });
    }));
  }
  function render() {
    fillSelect("#chatCourse", "自由答疑", true);
    fillSelect("#noteCourse", "全部课程", true);
    const s = data.summary;
    $("#studySummary").innerHTML = [["在学课程",s.active_courses,"门"],["待学任务时长",s.due_minutes,"分钟"],["已完成任务",`${s.completed_tasks} / ${s.total_tasks}`,"项"],["今日已复习",s.reviews_today,"张"]].map(([k,v,unit]) => `<div class="kpi"><div class="k">${k}</div><div class="v">${v}<small> ${unit}</small></div></div>`).join("");
    $("#homeCreate").textContent = s.active_courses ? "管理课程与计划" : "添加第一门课程";
    $("#todayTasks").innerHTML = data.due_tasks.length ? data.due_tasks.map(taskHtml).join("") : empty(s.active_courses ? "今天的任务已清空" : "从一门课程开始", s.active_courses ? "去课程页安排下一步，或开始答疑。未生成计划的课程需要先安排复习。" : "填写考试日期、复习主题和每天可用时间，得到可执行的任务列表。");
    bindTasks($("#todayTasks"));
    $("#todayReviews").innerHTML = `<div class="review-callout"><span class="review-number">${s.due_reviews}</span><div><strong>张卡片等待回忆</strong><p>用自己的话回答，再揭晓参考答案。</p></div></div><button id="goReview">${s.due_reviews ? "开始今日复习" : "打开学习笔记"}</button>`;
    $("#goReview").onclick = () => { $("#dueOnly").checked = !!s.due_reviews; bridge.switchView("notebook"); renderNotes(); };
    const active = data.courses.filter(c => !c.archived);
    $("#recentLearning").innerHTML = `<h3>考试倒计时</h3>` + (active.length ? active.map(c => `<div class="deadline"><strong>${esc(c.name)}</strong><span>${c.days_left < 0 ? "日期已过，更新计划" : c.days_left === 0 ? "今天考试" : `还有 ${c.days_left} 天`}</span></div>`).join("") : `<p class="hintline">添加课程后，在这里看到复习目标。</p>`);
    $("#courseList").innerHTML = data.courses.length ? `<div class="course-grid">` + data.courses.map(c => {
      const pct = c.task_count ? Math.round(c.completed_count / c.task_count * 100) : 0;
      return `<article class="panel course-card ${c.archived ? "archived" : ""}"><div class="panel-head"><span class="tag">${c.archived ? "已归档" : "在学"}</span><span class="hint">${esc(c.exam_date)}</span></div><h3>${esc(c.name)}</h3><p>${esc(c.goal || "循序渐进，学会课程核心内容")}</p><div class="course-stats"><span>每天 ${c.daily_minutes} 分钟</span><span>${c.topics.length} 个主题</span></div><div class="progress-track"><i style="width:${pct}%"></i></div><p class="hintline">任务完成 ${c.completed_count}/${c.task_count} · ${pct}%（不代表掌握程度）</p>${c.over_capacity ? '<p class="notes">有任务排在考试之后，请调整计划。</p>' : ""}<button data-course="${esc(c.id)}">查看计划 →</button></article>`;
    }).join("") + "</div>" : empty("还没有课程", "添加你的第一门课程，复习就有了明确的起点。");
    $("#courseList").querySelectorAll("[data-course]").forEach(el => el.onclick = () => { selectedCourse = el.dataset.course; renderCourse(); $("#courseDetail").scrollIntoView({behavior:"smooth",block:"start"}); });
    renderCourse(); renderNotes();
  }
  function renderCourse() {
    const box = $("#courseDetail"), c = data.courses.find(c => c.id === selectedCourse);
    box.hidden = !c;
    if (!c) return;
    const tasks = data.tasks.filter(t => t.course_id === c.id);
    box.innerHTML = `<div class="panel-head"><h2>${esc(c.name)} · 复习计划</h2><div class="actions"><button id="editCourse">编辑目标</button><button id="archiveCourse">${c.archived ? "恢复课程" : "归档课程"}</button></div></div><p class="hintline">范围：${esc(c.topics.join("、"))}。修改目标不覆盖已有任务；请按新目标手动调整日期。归档后暂停今日提醒，记录仍保留。</p><div class="actions"><button id="planCourse" class="primary" ${c.archived ? "disabled" : ""}>${tasks.some(t => t.plan_key) ? "检查计划安排" : "安排复习任务"}</button><button id="addTask" ${c.archived ? "disabled" : ""}>添加自定义任务</button><button id="askCourse">围绕这门课答疑</button></div><p id="planWarning" class="notes" role="status">${c.over_capacity ? "有未完成任务排在考试之后，请调整时间或复习范围。" : "按每日预算安排梳理、练习、闭卷自测。"}</p><div id="courseTasks">${tasks.length ? tasks.map(taskHtml).join("") : empty("计划还是一张白纸", "点击安排复习任务，或添加你自己的学习安排。")}</div>`;
    bindTasks($("#courseTasks"));
    $("#editCourse").onclick = () => courseDialog(c);
    $("#archiveCourse").onclick = async e => { e.target.disabled = true; try { await api("/api/study/archive", {course_id:c.id,archived:!c.archived}); await refresh(); } catch(err) { e.target.disabled = false; notify(err.message); } };
    $("#planCourse").onclick = async e => { e.target.disabled = true; try { const res = await api("/api/study/plan",{course_id:c.id,today:today()}); await refresh(); $("#planWarning").textContent = res.warning || res.basis; } catch(err) { e.target.disabled = false; notify(err.message); } };
    $("#askCourse").onclick = () => bridge.startLearning(c.id, "", "auto");
    $("#addTask").onclick = () => openDialog("添加学习任务", `<label for="taskTitle">具体要做什么</label><input id="taskTitle" name="title" maxlength="200" placeholder="例如：独立完成教材第 3 章习题" required><div class="row"><div><label for="taskDate">日期</label><input id="taskDate" name="due_date" type="date" value="${today()}" required></div><div><label for="taskMinutes">预计分钟</label><input id="taskMinutes" name="minutes" type="number" min="5" max="480" value="25" required></div></div>`, async f => { await api("/api/study/task",{course_id:c.id,title:f.get("title"),due_date:f.get("due_date"),minutes:Number(f.get("minutes"))}); });
  }
  function openDialog(title, fields, save) {
    dialogSave = save; $("#dialogTitle").textContent = title; $("#dialogFields").innerHTML = fields; $("#dialogError").textContent = "";
    $("#saveDialog").disabled = false; $("#studyDialog").showModal();
  }
  function courseDialog(c = {}) {
    openDialog(c.id ? "编辑课程目标" : "添加一门课程", `<label for="courseName">课程名称</label><input id="courseName" name="name" value="${esc(c.name)}" maxlength="80" placeholder="例如：高等数学" required><div class="row"><div><label for="courseExam">考试日期</label><input id="courseExam" name="exam_date" type="date" value="${esc(c.exam_date)}" required></div><div><label for="courseMinutes">每天可用分钟</label><input id="courseMinutes" name="daily_minutes" type="number" min="15" max="480" value="${c.daily_minutes || 60}" required></div></div><label for="courseGoal">希望达到什么目标</label><input id="courseGoal" name="goal" value="${esc(c.goal)}" maxlength="500" placeholder="例如：不看答案能独立完成重点题型"><label for="courseTopics">复习主题 · 每行一个，最多 30 个</label><textarea id="courseTopics" name="topics" rows="5" required placeholder="函数极限\n导数与微分\n定积分">${esc((c.topics || []).join("\n"))}</textarea><p class="hintline">填写你的真实考纲；计划按预算排程，内容太多会提示时间不足。</p>`, async f => {
      const res = await api("/api/study/course",{id:c.id,name:f.get("name"),exam_date:f.get("exam_date"),daily_minutes:Number(f.get("daily_minutes")),goal:f.get("goal"),topics:String(f.get("topics")).split("\n").map(t => t.trim()).filter(Boolean)});
      selectedCourse = res.id;
    });
  }
  function noteDialog(result, note) {
    if (!data) { notify("请先刷新学习数据再保存笔记"); return; }
    const courseId = result?.session?.meta?.course_id || $("#chatCourse").value;
    const options = `<option value="">未分类</option>` + data.courses.map(c => `<option value="${esc(c.id)}" ${c.id === courseId ? "selected" : ""}>${esc(c.name)}</option>`).join("");
    openDialog(note ? "编辑复习重点" : result ? "把回答存成复习卡片" : "写一张复习卡片", `<label for="noteTitle">标题</label><input id="noteTitle" name="title" maxlength="200" value="${esc(note?.title || result?.question?.slice(0,80) || "")}" required>${!note ? `<label for="noteCourseInput">课程</label><select id="noteCourseInput" name="course_id" ${result?.session?.meta?.course_id ? "disabled" : ""}>${options}</select>` : ""}${!note && !result ? '<label for="noteQuestion">自测问题</label><textarea id="noteQuestion" name="question" rows="3" maxlength="8000" required></textarea><label for="noteAnswer">参考答案</label><textarea id="noteAnswer" name="answer" rows="4" maxlength="50000" required></textarea>' : ""}<label for="noteReflection">错因 / 下次要注意什么</label><textarea id="noteReflection" name="reflection" maxlength="4000" rows="3" placeholder="用自己的话记录，比复制答案更有用">${esc(note?.reflection)}</textarea>${result?.meta?.simulated ? '<p class="notes">这是一条离线演示回答，保存后仍保留演示标记，请核对内容。</p>' : ''}`, async f => {
      if (note) await api("/api/study/note/update",{id:note.id,title:f.get("title"),reflection:f.get("reflection")});
      else await api("/api/study/note",{title:f.get("title"),course_id:result?.session?.meta?.course_id || f.get("course_id"),reflection:f.get("reflection"),question:f.get("question"),answer:f.get("answer"),source_turn_id:result?.turn_id,today:today()});
      notify("已保存，下次复习时先回忆再看答案。");
    });
  }
  function renderNotes() {
    if (!data) return;
    const q = $("#noteSearch").value.trim().toLowerCase(), course = $("#noteCourse").value, due = $("#dueOnly").checked;
    const notes = data.notes.filter(n => (!course || n.course_id === course) && (!due || (!n.archived && n.due_date <= today())) && `${n.title} ${n.question} ${n.reflection}`.toLowerCase().includes(q));
    $("#noteList").innerHTML = notes.length ? notes.map(n => `<article class="panel note-card" data-note="${esc(n.id)}"><div class="panel-head"><div><span class="tag">${esc(n.course_name || "未分类")}${n.archived ? " · 已归档" : ""}</span><span class="tag">${n.simulated ? "离线演示 · 待核对" : esc(n.model)}</span>${n.degraded ? '<span class="tag">降级回答</span>' : ""}</div><span class="hint">${n.due_date <= today() ? "待复习" : "下次 " + esc(n.due_date)} · 已复习 ${n.review_count} 次</span></div><h3>${esc(n.title)}</h3><div class="recall-question">${esc(n.question)}</div>${n.reflection ? `<p class="reflection">我的复习重点：${esc(n.reflection)}</p>` : ""}<label for="recall-${esc(n.id)}">先试着回答（本次草稿）</label><textarea id="recall-${esc(n.id)}" class="recall-draft" rows="2" placeholder="不看答案，你会怎么解释？"></textarea><button data-reveal="${esc(n.id)}">${revealed.has(n.id) ? "收起参考答案" : "揭晓参考答案"}</button><div class="review-answer" ${revealed.has(n.id) ? "" : "hidden"}><div class="answer">${esc(n.answer)}</div><p class="hintline">对照自己的回答，记录自评。每张卡片每天记录一次。</p><div class="actions"><button data-rating="again">还不会 · 1 天后</button><button data-rating="hard">有点难 · 2 天后</button><button data-rating="good" class="primary">已掌握 · 至少 3 天后</button></div></div><div class="note-footer"><button data-edit-note="${esc(n.id)}" class="ghost small">编辑复习重点</button>${n.source_turn_id ? `<button data-source="${esc(n.id)}" class="ghost small">回到原始会话</button>` : ""}<button data-delete-note="${esc(n.id)}" class="ghost small">删除卡片</button></div></article>`).join("") : empty(data.notes.length ? "没有符合条件的卡片" : "把值得再看的内容留下来", data.notes.length ? "换一个搜索条件，或关闭只看待复习。" : "在答疑回答下点击保存，或手写一张自测卡片。");
    $("#noteList").querySelectorAll("[data-note]").forEach(card => {
      const n = data.notes.find(n => n.id === card.dataset.note);
      const draft = card.querySelector(".recall-draft");
      draft.value = recallDrafts.get(n.id) || "";
      draft.oninput = () => recallDrafts.set(n.id, draft.value);
      card.querySelector("[data-reveal]").onclick = e => {
        const answer = card.querySelector(".review-answer"); answer.hidden = !answer.hidden;
        if (answer.hidden) revealed.delete(n.id); else revealed.add(n.id);
        e.target.textContent = answer.hidden ? "揭晓参考答案" : "收起参考答案";
      };
      card.querySelectorAll("[data-rating]").forEach(b => b.onclick = async () => {
        card.querySelectorAll("[data-rating]").forEach(x => x.disabled = true);
        try { const res = await api("/api/study/review", {note_id:n.id,rating:b.dataset.rating,today:today()}); revealed.delete(n.id); recallDrafts.delete(n.id); notify(`已记录，下次复习：${res.due_date}`); await refresh(); }
        catch(err) { notify(err.message); card.querySelectorAll("[data-rating]").forEach(x => x.disabled = false); }
      });
      card.querySelector("[data-edit-note]").onclick = () => noteDialog(null,n);
      const source = card.querySelector("[data-source]");
      if (source) source.onclick = async () => {
        try { if(n.source_session_id) await bridge.resumeSession(n.source_session_id); else notify("原始会话已不可用"); }
        catch(err) { notify(err.message); }
      };
      card.querySelector("[data-delete-note]").onclick = () => openDialog("删除复习卡片", `<p>确认删除「${esc(n.title)}」及其复习自评记录？原始答疑会话仍保留。</p>`, async () => { await api("/api/study/note/delete",{id:n.id}); revealed.delete(n.id); });
    });
  }
  function bindResult(element, result) {
    if (!result.turn_id) return;
    const actions = document.createElement("div"); actions.className = "answer-actions actions";
    const save = document.createElement("button"); save.textContent = "存为复习卡片"; save.onclick = () => {
      const existing = data?.notes.find(n => n.source_turn_id === result.turn_id);
      noteDialog(existing ? null : result, existing);
    };
    const copy = document.createElement("button"); copy.textContent = "复制回答"; copy.onclick = async () => { try { await navigator.clipboard.writeText(result.answer); notify("回答已复制"); } catch { notify("无法访问剪贴板，请选择回答文字手动复制。"); } };
    actions.append(save,copy); element.append(actions);
  }
  async function init(b) {
    bridge = b;
    $("#homeCreate").onclick = () => { bridge.switchView("courses"); if (!data?.summary.active_courses) courseDialog(); };
    $("#homeAsk").onclick = () => bridge.switchView("chat");
    $("#newCourse").onclick = () => courseDialog();
    $("#refreshStudy").onclick = refresh;
    $("#newNote").onclick = () => noteDialog();
    ["#noteSearch","#noteCourse","#dueOnly"].forEach(s => $(s).addEventListener("input",renderNotes));
    $("#chatCourse").addEventListener("change",() => bridge.newSession());
    $("#closeDialog").onclick = $("#cancelDialog").onclick = () => $("#studyDialog").close();
    $("#studyDialogForm").onsubmit = async e => {
      e.preventDefault(); $("#saveDialog").disabled = true;
      $("#closeDialog").disabled = $("#cancelDialog").disabled = true;
      try { await dialogSave(new FormData(e.target)); $("#studyDialog").close(); await refresh(); }
      catch(err) { $("#dialogError").textContent = err.message; }
      finally { $("#saveDialog").disabled = false; $("#closeDialog").disabled = $("#cancelDialog").disabled = false; }
    };
    $("#studyDialog").addEventListener("cancel", e => { if($("#saveDialog").disabled) e.preventDefault(); });
    $("#exportStudy").onclick = async e => {
      e.target.disabled = true;
      try { const exported = await api("/api/study/export"); const url = URL.createObjectURL(new Blob([JSON.stringify(exported,null,2)],{type:"application/json"})); const a = document.createElement("a"); a.href = url; a.download = `ai-tutor-learning-${today()}.json`; a.click(); setTimeout(() => URL.revokeObjectURL(url),1000); notify("学习数据已导出，包含课程、任务、笔记、自评与会话。"); }
      catch(err) { notify(err.message); } finally { e.target.disabled = false; }
    };
    await refresh();
  }
  function mode(health) {
    const real = (health.usable_models || []).filter(n => n !== "mock");
    $("#modeNotice").innerHTML = real.length ? `<strong>模型已就绪</strong> · 答疑可能产生模型服务费用。课程、计划和复习记录保存在本机；发送的问题与图片会提交给所选模型服务。` : `<strong>当前为离线演示</strong> · 课程管理和复习记录可正常使用；答疑为预设示例，不能可靠回答任意新题，也不能识别图片。配置 API Key 后重启服务启用真实答疑。<button id="modeHelp">查看接入说明</button>`;
    if($("#modeHelp")) $("#modeHelp").onclick = () => bridge.switchView("about");
  }
  return {init, refresh, bindResult, mode, courseId:() => $("#chatCourse").value || null};
})();
