"use strict";

const $ = (id) => document.getElementById(id);
const state = { config: null, currentId: null, generation: 0, timer: null, busy: false, failures: 0 };
const stages = ["planner", "investigator", "reviewer"];
const statusLabels = { queued: "等待执行", running: "正在调查", completed: "调查完成", failed: "执行失败", interrupted: "执行中断" };
const stageLabels = { planner: "正在制定调查计划", investigator: "正在检索代码与证据", reviewer: "正在核验引用与结论", queued: "等待执行" };

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}

function showError(message) {
  $("error-text").textContent = message;
  $("error-banner").hidden = false;
}

function clearError() { $("error-banner").hidden = true; }

function connection(online) {
  $("connection").className = `connection ${online ? "online" : "offline"}`;
  $("connection").replaceChildren(element("i"), document.createTextNode(online ? "本地服务在线" : "连接暂时中断"));
}

async function api(path, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch(path, { ...options, signal: controller.signal,
      headers: { "Content-Type": "application/json", ...(options.headers || {}) } });
    if (!response.ok) {
      let message = `请求失败（${response.status}）`;
      try {
        const payload = await response.json();
        if (typeof payload.detail === "string") message = payload.detail;
        else if (Array.isArray(payload.detail)) message = "输入校验失败，请检查描述长度与目标仓库。";
      } catch { /* Error body can be non-JSON. */ }
      throw new Error(message);
    }
    connection(true);
    return await response.json();
  } catch (error) {
    if (error.name === "AbortError") throw new Error("请求超时，请检查本地服务是否仍在运行。");
    if (error instanceof TypeError) {
      connection(false);
      throw new Error("无法连接本地服务。请确认 triageflow serve 正在运行。");
    }
    throw error;
  } finally { clearTimeout(timeout); }
}

function updateSubmit() {
  $("char-count").textContent = `${$("issue").value.length} / 6000`;
  $("submit").disabled = !state.config || state.busy || $("issue").value.trim().length < 10;
  $("submit").firstElementChild.textContent = state.busy ? "正在提交…" : "开始分析";
}

function setTab(tab) {
  for (const name of ["report", "trace"]) {
    const active = name === tab;
    $(`${name}-tab`).classList.toggle("active", active);
    $(`${name}-tab`).setAttribute("aria-selected", String(active));
    $(`${name}-view`).hidden = !active;
  }
}

function stopPolling() {
  state.generation += 1;
  clearTimeout(state.timer);
  state.timer = null;
}

function resetView() {
  stopPolling();
  state.currentId = null;
  state.failures = 0;
  clearError();
  $("empty-state").hidden = false;
  for (const id of ["running-state", "failure-state", "report-content", "download", "run-meta"]) $(id).hidden = true;
  $("run-status").textContent = "等待输入";
  $("run-status").className = "status-pill idle";
  for (const id of ["metric-tools", "metric-models", "metric-tokens", "metric-duration"]) $(id).textContent = "—";
  $("events").replaceChildren(element("p", "trace-empty", "运行开始后，执行记录会显示在这里。"));
  $("event-count").textContent = "0";
  document.querySelectorAll(".stage").forEach((node) => {
    node.classList.remove("done", "current");
    node.querySelector(".stage-check").textContent = "";
  });
  document.querySelectorAll(".history-item").forEach((node) => node.classList.remove("selected"));
  setTab("report");
}

function newRun() {
  if (state.busy) return;
  resetView();
  $("issue").value = "";
  updateSubmit();
  $("issue").focus();
}

function formatTime(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

async function history() {
  const runs = await api("/api/runs");
  const items = runs.slice(0, 12).map((run) => {
    const button = element("button", `history-item${run.id === state.currentId ? " selected" : ""}`);
    button.dataset.id = run.id;
    button.title = run.issue;
    button.append(element("span", "history-title", run.issue));
    const meta = element("span", "history-date");
    meta.append(element("i", `history-dot ${run.status}`), document.createTextNode(`${formatTime(run.created_at)} · ${statusLabels[run.status] || run.status}`));
    button.append(meta);
    button.addEventListener("click", () => { if (!state.busy) selectRun(run.id); });
    return button;
  });
  $("history").replaceChildren(...(items.length ? items : [element("p", "subtle", "暂无运行记录")]));
}

function renderStages(run) {
  const current = stages.indexOf(run.stage);
  document.querySelectorAll(".stage").forEach((node, index) => {
    const done = run.status === "completed" || (current >= 0 && index < current);
    const active = run.status === "running" && index === current;
    node.classList.toggle("done", done);
    node.classList.toggle("current", active);
    node.querySelector(".stage-check").textContent = done ? "✓" : "";
  });
}

function listSection(title, values, className = "") {
  if (!values || !values.length) return null;
  const section = element("section", `report-section ${className}`);
  section.append(element("h4", "", title));
  const list = element("ul", "report-list");
  values.forEach((text) => list.append(element("li", "", text)));
  section.append(list);
  return section;
}

function renderReport(run) {
  const report = run.report;
  const article = $("report-content");
  const top = element("div", "report-summary-top");
  const confidence = { low: "低", medium: "中", high: "高" }[report.confidence] || report.confidence;
  top.append(element("h3", "", report.summary), element("span", `confidence ${report.confidence}`, `${confidence}置信度`));
  const nodes = [top, element("p", "root-cause", report.root_cause)];
  const evidence = element("section", "report-section");
  const heading = element("h4", "", `代码证据 · ${report.evidence.length}`);
  heading.append(element("span", "verified-label", report.verified ? "引用已核验" : "暂无可核验证据"));
  evidence.append(heading);
  for (const item of report.evidence) {
    const card = element("div", "evidence-card");
    const head = element("div", "evidence-head");
    head.append(element("span", "", `▱ ${item.path}`), element("span", "", `L${item.start_line}–${item.end_line}`));
    const code = element("code", "", item.quote.split("\n").map((line, i) => `${String(item.start_line + i).padStart(3, " ")}  ${line}`).join("\n"));
    const pre = element("pre");
    pre.append(code);
    card.append(head, pre);
    evidence.append(card);
  }
  nodes.push(evidence);
  for (const section of [listSection("修复建议", report.recommendations), listSection("验证计划 · 尚未执行", report.test_plan), listSection("结论边界与局限", report.limitations, "limitations")]) {
    if (section) nodes.push(section);
  }
  nodes.push(element("p", "report-provenance", "引用核验确认的是代码出处与读取记录，不证明根因或修复一定正确。请结合复现测试作出判断。"));
  article.replaceChildren(...nodes);
}

function renderRun(run) {
  $("run-status").textContent = statusLabels[run.status] || run.status;
  $("run-status").className = `status-pill ${run.status}`;
  $("run-meta").hidden = false;
  $("run-meta").textContent = `RUN ${run.id.slice(0, 12)} · ${run.mode.toUpperCase()} · ${formatTime(run.created_at)}`;
  const metrics = run.metrics;
  $("metric-tools").textContent = String(metrics.tool_calls);
  $("metric-models").textContent = String(metrics.model_calls);
  $("metric-tokens").textContent = (metrics.input_tokens + metrics.output_tokens).toLocaleString("zh-CN");
  $("metric-duration").textContent = metrics.duration_ms < 1000 ? `${metrics.duration_ms}ms` : `${(metrics.duration_ms / 1000).toFixed(1)}s`;
  renderStages(run);
  const ready = run.status === "completed" && run.report;
  const failed = ["failed", "interrupted"].includes(run.status);
  $("empty-state").hidden = true;
  $("running-state").hidden = Boolean(ready || failed);
  $("failure-state").hidden = !failed;
  $("report-content").hidden = !ready;
  $("download").hidden = !ready;
  if (ready) {
    renderReport(run);
    $("download").href = `/api/runs/${encodeURIComponent(run.id)}/report`;
    $("download").setAttribute("download", `triageflow-${run.id.slice(0, 8)}.md`);
  } else if (failed) {
    $("failure-message").textContent = run.error || "执行中断，可以从最近检查点继续。";
    $("resume").disabled = false;
  } else {
    $("running-title").textContent = stageLabels[run.stage] || "正在分析";
    $("plan-list").replaceChildren(...run.plan.map((text) => element("li", "", text)));
  }
}

function renderEvents(events) {
  const open = new Set([...$("events").querySelectorAll("details[open]")].map((node) => node.dataset.seq));
  $("event-count").textContent = String(events.length);
  const labels = { stage_started: "STAGE", tool_call: "TOOL", tool_result: "RESULT", model_call: "MODEL", verification: "VERIFY", completed: "DONE", failed: "ERROR", resumed: "RESUME" };
  const nodes = events.map((event) => {
    const item = element("details", "event");
    item.dataset.seq = String(event.seq);
    item.open = open.has(String(event.seq));
    const summary = element("summary");
    const date = new Date(event.created_at);
    summary.append(element("span", "event-seq", `#${event.seq}`), element("span", `event-type ${event.type}`, labels[event.type] || event.type), element("span", "event-message", event.message), element("time", "", Number.isNaN(date.getTime()) ? "" : date.toLocaleTimeString("zh-CN", { hour12: false })));
    const data = { stage: event.stage, ...event.data };
    item.append(summary, element("pre", "", JSON.stringify(data, null, 2)));
    return item;
  });
  $("events").replaceChildren(...(nodes.length ? nodes : [element("p", "trace-empty", "等待第一条执行记录…")]));
}

async function poll(runId, generation) {
  if (generation !== state.generation || runId !== state.currentId) return;
  try {
    const [run, events] = await Promise.all([api(`/api/runs/${encodeURIComponent(runId)}`), api(`/api/runs/${encodeURIComponent(runId)}/events`)]);
    if (generation !== state.generation || runId !== state.currentId) return;
    state.failures = 0;
    clearError();
    renderRun(run);
    renderEvents(events);
    if (["completed", "failed", "interrupted"].includes(run.status)) {
      history().catch((error) => showError(error.message));
    } else {
      state.timer = setTimeout(() => poll(runId, generation), 800);
    }
  } catch (error) {
    if (generation !== state.generation) return;
    state.failures += 1;
    showError(`${error.message}${state.failures < 6 ? " 正在自动重连…" : " 点击重新连接以继续读取任务。"}`);
    if (state.failures < 6) state.timer = setTimeout(() => poll(runId, generation), Math.min(1000 * 2 ** state.failures, 10000));
  }
}

async function selectRun(runId, initial) {
  stopPolling();
  state.currentId = runId;
  state.failures = 0;
  clearError();
  const generation = state.generation;
  setTab("report");
  try {
    const run = initial || await api(`/api/runs/${encodeURIComponent(runId)}`);
    if (generation !== state.generation) return;
    $("issue").value = run.issue;
    if ([...$("project").options].some((option) => option.value === run.project_id)) $("project").value = run.project_id;
    updateSubmit();
    renderRun(run);
    document.querySelectorAll(".history-item").forEach((node) => node.classList.toggle("selected", node.dataset.id === runId));
    await poll(runId, generation);
  } catch (error) { if (generation === state.generation) showError(error.message); }
}

async function initialize() {
  try {
    const [config, examples] = await Promise.all([api("/api/config"), api("/api/examples")]);
    state.config = config;
    const live = config.mode === "live";
    $("mode-badge").textContent = live ? "LIVE" : "DEMO";
    $("mode-badge").classList.toggle("live", live);
    $("mode-note").classList.toggle("live", live);
    $("mode-note").querySelector("strong").textContent = live ? "真实模型模式已启用" : "离线演示已启用";
    $("mode-note").querySelector("p span").textContent = live ? "选中的源码片段将发送至配置的模型服务，调用可能产生费用。" : "使用内置故障规则与真实源码检索，无需 API Key，不调用大模型。";
    $("mode-note").querySelector(".note-tag").textContent = live ? "模型驱动" : "零 API 调用";
    $("model-name").textContent = config.model;
    $("budget-label").textContent = `最多 ${config.limits.max_steps} 轮调查 · 有界工具调用`;
    $("project").replaceChildren(...config.projects.map((project) => {
      const option = element("option", "", project.name);
      option.value = project.id;
      option.title = project.description;
      return option;
    }));
    $("examples").replaceChildren(...examples.map((example) => {
      const button = element("button", "example-button", example.title);
      button.type = "button";
      button.append(element("span", "", "↗"));
      button.addEventListener("click", () => {
        if (state.busy) return;
        resetView();
        $("project").value = example.project_id;
        $("issue").value = example.issue;
        updateSubmit();
        $("issue").focus();
      });
      return button;
    }));
    updateSubmit();
    clearError();
    await history();
  } catch (error) { showError(error.message); }
}

$("issue-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.busy || !state.config || $("issue").value.trim().length < 10) return;
  state.busy = true;
  updateSubmit();
  clearError();
  try {
    const run = await api("/api/runs", { method: "POST", body: JSON.stringify({ project_id: $("project").value, issue: $("issue").value.trim() }) });
    await selectRun(run.id, run);
    await history();
  } catch (error) { showError(error.message); }
  finally { state.busy = false; updateSubmit(); }
});

$("resume").addEventListener("click", async () => {
  if (!state.currentId || state.busy) return;
  const id = state.currentId;
  state.busy = true;
  $("resume").disabled = true;
  updateSubmit();
  try {
    const run = await api(`/api/runs/${encodeURIComponent(id)}/resume`, { method: "POST" });
    await selectRun(id, run);
  } catch (error) { showError(error.message); }
  finally { state.busy = false; $("resume").disabled = false; updateSubmit(); }
});

$("issue").addEventListener("input", updateSubmit);
$("new-run").addEventListener("click", newRun);
$("refresh-history").addEventListener("click", () => history().catch((error) => showError(error.message)));
$("retry-connection").addEventListener("click", async () => {
  if (!state.config) await initialize();
  else if (state.currentId) await selectRun(state.currentId);
  else { clearError(); await history().catch((error) => showError(error.message)); }
});
for (const name of ["report", "trace"]) $(`${name}-tab`).addEventListener("click", () => setTab(name));
document.addEventListener("keydown", (event) => {
  const editing = event.target.matches("input,textarea,select,[contenteditable='true']");
  if (!editing && !event.ctrlKey && !event.metaKey && !event.altKey && event.key.toLowerCase() === "n") { event.preventDefault(); newRun(); }
});
initialize();
