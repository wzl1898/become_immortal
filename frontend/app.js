// 修仙文字冒险 —— 前端逻辑（含存档/读档）
const storyEl = document.getElementById("story");
const form = document.getElementById("input-form");
const input = document.getElementById("action");
const sendBtn = document.getElementById("send");
const restartBtn = document.getElementById("restart");
const savesBtn = document.getElementById("saves-btn");
const saveNameEl = document.getElementById("save-name");
const drawer = document.getElementById("drawer");
const drawerClose = document.getElementById("drawer-close");
const saveListEl = document.getElementById("save-list");
const saveEmptyEl = document.getElementById("save-empty");
const statusPanel = document.getElementById("status-panel");
const statusBodyEl = document.getElementById("status-body");
const loreBtn = document.getElementById("lore-btn");
const loreDrawer = document.getElementById("lore-drawer");
const loreClose = document.getElementById("lore-close");
const loreListEl = document.getElementById("lore-list");
const loreEmptyEl = document.getElementById("lore-empty");
const loreForm = document.getElementById("lore-form");
const loreInput = document.getElementById("lore-input");
const loreSend = document.getElementById("lore-send");
const directorBtn = document.getElementById("director-btn");
const directorDrawer = document.getElementById("director-drawer");
const directorClose = document.getElementById("director-close");
const directorRefresh = document.getElementById("director-refresh");
const directorBodyEl = document.getElementById("director-body");
const directorEmptyEl = document.getElementById("director-empty");
const constraintBtn = document.getElementById("constraint-btn");
const constraintDrawer = document.getElementById("constraint-drawer");
const constraintClose = document.getElementById("constraint-close");
const constraintRefresh = document.getElementById("constraint-refresh");
const constraintBodyEl = document.getElementById("constraint-body");
const constraintEmptyEl = document.getElementById("constraint-empty");
const llmBtn = document.getElementById("llm-btn");
const llmDrawer = document.getElementById("llm-drawer");
const llmClose = document.getElementById("llm-close");
const llmRefresh = document.getElementById("llm-refresh");
const llmLiveEl = document.getElementById("llm-live");
const llmListEl = document.getElementById("llm-list");
const llmEmptyEl = document.getElementById("llm-empty");

function clearStatus() {
  statusBodyEl.innerHTML = "";
  statusPanel.classList.add("empty");
}

let sessionId = null;
let currentName = "";
let busy = false;
let worldMemory = []; // 世界记忆，含 qa 与自动提取的长期事实
let worldState = null; // 世界约束 Agent 的实时状态：位置 + 主角知识视野
let loreBusy = false; // 问询独立忙态，不锁主行动
const liveLLMRequests = new Map();
let liveLLMTimer = null;
let llmRefreshTimer = null;

const LLM_REQUEST_LABELS = {
  opening: "开场生成",
  director_plan: "导演规划",
  director_event: "事件 Agent",
  director_hook: "钩子 Agent",
  director_payoff: "爽点 Agent",
  director_pacing: "节奏 Agent",
  narrative: "剧情生成",
  memory_extract: "记忆提取",
  director_audit: "执行审计",
  inquiry: "记忆问询",
  legacy_director: "旧版导演",
};

function formatDuration(ms) {
  const value = Math.max(0, Number(ms) || 0);
  return value < 1000 ? `${Math.round(value)} ms` : `${(value / 1000).toFixed(1)} s`;
}

function renderLiveLLMRequests() {
  llmLiveEl.innerHTML = "";
  llmLiveEl.classList.toggle("hidden", liveLLMRequests.size === 0);
  for (const request of liveLLMRequests.values()) {
    const row = document.createElement("div");
    row.className = "llm-live-row";
    row.innerHTML = `<span class="llm-pulse"></span><span class="llm-live-label"></span><b></b>`;
    row.querySelector(".llm-live-label").textContent = request.label;
    row.querySelector("b").textContent = formatDuration(Date.now() - request.startedAt);
    llmLiveEl.appendChild(row);
  }
}

function startLiveLLMRequest(channel, type) {
  liveLLMRequests.set(channel, {
    label: LLM_REQUEST_LABELS[type] || type,
    startedAt: Date.now(),
  });
  renderLiveLLMRequests();
  if (!liveLLMTimer) liveLLMTimer = setInterval(renderLiveLLMRequests, 100);
}

function stopLiveLLMRequest(channel) {
  liveLLMRequests.delete(channel);
  if (!liveLLMRequests.size && liveLLMTimer) {
    clearInterval(liveLLMTimer);
    liveLLMTimer = null;
  }
  renderLiveLLMRequests();
}

function addBlock(kind, text = "") {
  const el = document.createElement("p");
  el.className = `block ${kind}`;
  el.textContent = text;
  storyEl.appendChild(el);
  storyEl.scrollTop = storyEl.scrollHeight;
  return el;
}

function setBusy(state) {
  busy = state;
  input.disabled = state;
  sendBtn.disabled = state;
  restartBtn.disabled = state;
  if (!state) input.focus();
}

function setCurrent(sid, name) {
  sessionId = sid;
  currentName = name || "";
  saveNameEl.textContent = currentName ? `《${currentName}》` : "";
}

// ---- SSE 流式 ----
async function streamSSE(url, onDelta, options = {}, onStage = null) {
  const resp = await fetch(url, options);
  if (!resp.ok) {
    let msg = `请求失败 ${resp.status}`;
    try { msg = (await resp.json()).detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let doneData = null;
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const events = buffer.split("\n\n");
    buffer = events.pop();
    for (const raw of events) {
      const evt = parseEvent(raw);
      if (!evt) continue;
      if (evt.event === "delta") onDelta(evt.data.text);
      else if (evt.event === "stage" && onStage) onStage(evt.data);
      else if (evt.event === "done") doneData = evt.data;
      else if (evt.event === "error") throw new Error(evt.data.message);
    }
  }
  return doneData; // done 事件的 payload（叙事流会带上刷新后的 inventory）
}

async function fetchJSON(url, options = {}) {
  const resp = await fetch(url, options);
  let data = {};
  try { data = await resp.json(); } catch (_) {}
  if (!resp.ok) throw new Error(data.detail || `请求失败 ${resp.status}`);
  return data;
}

function parseEvent(raw) {
  let event = "message";
  let data = "";
  for (const line of raw.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) data += line.slice(5).trim();
  }
  if (!data) return null;
  try { return { event, data: JSON.parse(data) }; }
  catch (_) { return null; }
}

// 从 text 里抽出 《标签》…《/标签》 块内容，并返回剔除该块后的剩余文本。
// 未闭合（流式中途）时，把开标签到末尾都当作块内容、从正文剔除。
function extractBlock(text, open, close) {
  const o = text.indexOf(open);
  if (o === -1) return { content: "", rest: text };
  const after = text.slice(o + open.length);
  const c = after.indexOf(close);
  const content = (c === -1 ? after : after.slice(0, c)).trim();
  const endIdx = c === -1 ? text.length : o + open.length + c + close.length;
  return { content, rest: text.slice(0, o) + text.slice(endIdx) };
}

// 把整段回复拆成正文、身体状态、关键物件、灵光提示四部分。
// 顺序约定：正文 → 《状态》 → 《物件》 → 〔灵光提示〕，但解析不依赖顺序。
// 返回 { body, status, objects, hint }，缺失的为 ""。
function splitParts(full) {
  let rest = full;
  const s = extractBlock(rest, "《状态》", "《/状态》");
  const status = s.content;
  rest = s.rest;
  const o = extractBlock(rest, "《物件》", "《/物件》");
  const objects = o.content;
  rest = o.rest;
  let hint = "";
  const hi = rest.indexOf("〔");
  if (hi !== -1) {
    const h = rest.slice(hi + 1);
    const hc = h.indexOf("〕");
    hint = (hc === -1 ? h : h.slice(0, hc)).trim();
    rest = rest.slice(0, hi);
  }
  return { body: rest.trimEnd(), status, objects, hint };
}

// 把身体状态（每行"字段：值"）+ 关键物件块渲染进左侧面板
function renderStatus(status, objects) {
  if (!status && !objects) return;
  statusBodyEl.innerHTML = "";
  for (const line of (status || "").split("\n")) {
    const t = line.trim();
    if (!t) continue;
    const m = t.match(/^(.+?)[：:]\s*(.*)$/);
    const row = document.createElement("div");
    row.className = "status-row";
    if (m) {
      row.innerHTML = `<span class="k"></span><span class="v"></span>`;
      row.querySelector(".k").textContent = m[1].trim();
      row.querySelector(".v").textContent = m[2].trim();
    } else {
      row.textContent = t;
    }
    statusBodyEl.appendChild(row);
  }
  renderObjects(objects);
  statusPanel.classList.remove("empty");
}

function statusTextFromCharacterState(state) {
  if (!state || !Object.keys(state).length) return "";
  const fields = [
    ["realm", "境界"],
    ["health", "气血"],
    ["spiritual_power", "灵力"],
    ["cultivation", "修为"],
    ["condition", "状态"],
    ["resources", "资源"],
    ["artifacts", "法宝"],
  ];
  return fields
    .map(([key, label]) => state[key] ? `${label}：${state[key]}` : "")
    .filter(Boolean)
    .join("\n");
}

function renderCharacterState(state) {
  const status = statusTextFromCharacterState(state);
  if (status) renderStatus(status, "");
}

// 关键物件块：主角未拥有但有剧情分量之物，每行"名称（属性）——归属"
function renderObjects(objects) {
  const lines = (objects || "").split("\n").map((l) => l.trim()).filter(Boolean);
  if (!lines.length) return;
  const head = document.createElement("div");
  head.className = "status-subhead";
  head.textContent = "关注之物";
  statusBodyEl.appendChild(head);
  for (const line of lines) {
    const dash = line.split(/——|—|--/);
    const item = document.createElement("div");
    item.className = "obj-row";
    if (dash.length >= 2) {
      const whereabouts = dash.slice(1).join("——").trim();
      item.innerHTML = `<span class="obj-name"></span><span class="obj-loc"></span>`;
      item.querySelector(".obj-name").textContent = dash[0].trim();
      item.querySelector(".obj-loc").textContent = whereabouts;
    } else {
      item.textContent = line;
    }
    statusBodyEl.appendChild(item);
  }
}

// 冷物品折叠区：后端结构化库里 hot=false 的物品，收进「其他随身物 (N)」折叠块。
// 热物品仍由面板文本（《状态》/《物件》）驱动展示，这里只补冷物品。
function renderColdItems(inventory) {
  // 先清掉旧的折叠区，避免重复
  const old = document.getElementById("cold-items");
  if (old) old.remove();
  const cold = (inventory || []).filter((it) => it && !it.hot);
  if (!cold.length) return;
  if (statusPanel.classList.contains("empty")) statusPanel.classList.remove("empty");

  const details = document.createElement("details");
  details.id = "cold-items";
  details.className = "cold-items";
  const summary = document.createElement("summary");
  summary.textContent = `其他随身物 (${cold.length})`;
  details.appendChild(summary);

  for (const it of cold) {
    const row = document.createElement("div");
    row.className = "obj-row cold";
    const head = it.attrs ? `${it.name}（${it.attrs}）` : it.name;
    const place = it.whereabouts || it.kind || "";
    row.innerHTML = `<span class="obj-name"></span><span class="obj-loc"></span>`;
    row.querySelector(".obj-name").textContent = head;
    row.querySelector(".obj-loc").textContent = place;
    details.appendChild(row);
  }
  statusBodyEl.appendChild(details);
}

// 重放用：把一段完整叙事渲染成正文块（+ 可选提示块），并刷新状态面板
function renderNarration(full) {
  const { body, status, objects, hint } = splitParts(full);
  addBlock("narration", body);
  if (status || objects) renderStatus(status, objects);
  if (hint) addBlock("hint", hint);
}

async function narrate(url, options = {}) {
  const block = addBlock("narration cursor");
  let hintBlock = null;
  let full = "";
  if (url.includes("opening")) startLiveLLMRequest("story", "opening");
  try {
    const done = await streamSSE(url, (text) => {
      full += text;
      const { body, status, objects, hint } = splitParts(full);
      block.textContent = body;
      if (status || objects) renderStatus(status, objects);
      if (hint) {
        if (!hintBlock) hintBlock = addBlock("hint");
        hintBlock.textContent = hint;
      }
      storyEl.scrollTop = storyEl.scrollHeight;
    }, options, (stage) => {
      if (!full) block.textContent = `〔${stage.label || "正在准备"}〕`;
      if (stage.key === "director") startLiveLLMRequest("story", "director_plan");
      else if (stage.key === "narrative") startLiveLLMRequest("story", "narrative");
    });
    // 流结束：用后端刷新后的结构化库补上冷物品折叠区
    if (done && done.inventory) renderColdItems(done.inventory);
    if (done && done.world_state) renderConstraint(done.world_state);
    if (done && done.director_state) renderDirector(done.director_state, done.character_state?.turn || 0);
  } catch (e) {
    block.remove();
    if (hintBlock) hintBlock.remove();
    addBlock("error", `【出错】${e.message}`);
    throw e;
  } finally {
    stopLiveLLMRequest("story");
    refreshLLMMetrics({ quiet: true });
    block.classList.remove("cursor");
  }
}

// ---- 游戏生命周期 ----
async function newGame() {
  setBusy(true);
  storyEl.innerHTML = "";
  clearStatus();
  worldMemory = [];
  renderConstraint(null);
  addBlock("narration", "　　天地灵气涌动，你的故事即将开始……").classList.add("cursor");
  try {
    const data = await fetchJSON("/api/new", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    setCurrent(data.session_id, "无名修士");
    storyEl.innerHTML = "";
    await narrate(`/api/opening?sid=${sessionId}`);
  } catch (e) {
    storyEl.innerHTML = "";
    addBlock("error", `【出错】${e.message}`);
  } finally {
    setBusy(false);
  }
}

// 读档：拉完整剧情并重放到界面
async function loadGame(sid, name) {
  setBusy(true);
  try {
    const data = await fetchJSON(`/api/load?sid=${sid}`);
    setCurrent(sid, name);
    worldMemory = data.world_memory || data.lore || [];
    renderConstraint(data.world_state || null);
    storyEl.innerHTML = "";
    clearStatus();
    renderCharacterState(data.character_state);
    for (const blk of data.transcript) {
      if (blk.role === "player") addBlock("player", blk.text);
      else renderNarration(blk.text);
    }
    // 读档还原冷物品折叠区（热物品已随最近一条面板重放出来）
    if (data.inventory) renderColdItems(data.inventory);
    closeDrawer();
    // 空局（尚未落笔）：直接让 AI 生成开场，而不是停在占位文字上
    if (!data.transcript.length) {
      await narrate(`/api/opening?sid=${sid}`);
    }
  } catch (e) {
    addBlock("error", `【出错】${e.message}`);
  } finally {
    setBusy(false);
  }
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text || busy || !sessionId) return;
  input.value = "";
  addBlock("player", text);
  setBusy(true);
  try {
    await narrate("/api/action", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sid: sessionId, text }),
    });
  } catch (_) {
    // 已展示
  } finally {
    setBusy(false);
  }
});

restartBtn.addEventListener("click", () => {
  if (busy) return;
  newGame();
});

// ---- 存档抽屉 ----
function openDrawer() { drawer.classList.remove("hidden"); renderSaves(); }
function closeDrawer() { drawer.classList.add("hidden"); }

async function fetchSaves() {
  const resp = await fetch("/api/saves");
  return (await resp.json()).saves || [];
}

function fmtTime(ts) {
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

async function renderSaves() {
  saveListEl.innerHTML = "";
  let saves = [];
  try { saves = await fetchSaves(); }
  catch (_) { saveEmptyEl.textContent = "读取存档失败"; saveEmptyEl.classList.remove("hidden"); return; }

  saveEmptyEl.classList.toggle("hidden", saves.length > 0);
  for (const s of saves) {
    saveListEl.appendChild(renderSaveItem(s));
  }
}

function renderSaveItem(s) {
  const li = document.createElement("li");
  li.className = "save-item" + (s.id === sessionId ? " current" : "");
  li.innerHTML = `
    <div class="row1">
      <span class="name"></span>
      <span class="turns">${s.turns} 回合 · ${fmtTime(s.updated_at)}</span>
    </div>
    <p class="preview"></p>
    <div class="btns">
      <button class="load">读档</button>
      <button class="ren">改名</button>
      <button class="del">删除</button>
    </div>`;
  li.querySelector(".name").textContent = s.name;
  li.querySelector(".preview").textContent = s.preview || "（尚未落笔）";
  li.querySelector(".load").addEventListener("click", () => loadGame(s.id, s.name));
  li.querySelector(".ren").addEventListener("click", () => renameSave(s));
  li.querySelector(".del").addEventListener("click", () => deleteSave(s));
  return li;
}

async function renameSave(s) {
  const name = prompt("给这一世取个名字：", s.name);
  if (!name || !name.trim()) return;
  try {
    await fetchJSON("/api/rename", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sid: s.id, name: name.trim() }),
    });
    if (s.id === sessionId) setCurrent(sessionId, name.trim());
    renderSaves();
  } catch (e) {
    alert(e.message);
  }
}

async function deleteSave(s) {
  if (!confirm(`确定删除《${s.name}》？此操作不可恢复。`)) return;
  try {
    await fetchJSON("/api/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sid: s.id }),
    });
    if (s.id === sessionId) {
      sessionId = null;
      setCurrent(null, "");
      storyEl.innerHTML = "";
      clearStatus();
      worldMemory = [];
      renderConstraint(null);
    }
    renderSaves();
  } catch (e) {
    alert(e.message);
  }
}

savesBtn.addEventListener("click", openDrawer);
drawerClose.addEventListener("click", closeDrawer);
drawer.querySelector(".drawer-mask").addEventListener("click", closeDrawer);

// ---- 世界记忆抽屉（问询旁路）----
async function fetchWorldMemory() {
  const data = await fetchJSON(`/api/world-memory?sid=${sessionId}`);
  return data.world_memory || data.lore || [];
}

async function openLoreDrawer() {
  if (!sessionId) return;
  loreDrawer.classList.remove("hidden");
  try {
    worldMemory = await fetchWorldMemory();
  } catch (_) {
    // 保留本地已有记忆，抽屉仍可打开。
  }
  renderLore();
  loreInput.focus();
}
function closeLoreDrawer() { loreDrawer.classList.add("hidden"); }

function renderLore() {
  loreListEl.innerHTML = "";
  loreEmptyEl.classList.toggle("hidden", worldMemory.length > 0);
  worldMemory.forEach((item, i) => loreListEl.appendChild(renderLoreItem(item, i)));
}

function renderLoreItem(item, index) {
  const li = document.createElement("li");
  const type = item.type || "plot";
  li.className = `lore-item ${type === "qa" ? "qa" : "memory"}`;
  li.innerHTML = `
    <div class="q"></div>
    <div class="a"></div>
    <button class="del" title="删除这条世界记忆">删除</button>`;
  if (type === "qa") {
    li.querySelector(".q").textContent = item.q || "";
    li.querySelector(".a").textContent = item.a || item.text || "";
  } else {
    li.querySelector(".q").textContent = `[${type}] ${item.entities && item.entities.length ? item.entities.join("、") : "剧情记忆"}`;
    li.querySelector(".a").textContent = item.text || "";
  }
  li.querySelector(".del").addEventListener("click", () => deleteLoreItem(index));
  return li;
}

async function deleteLoreItem(index) {
  if (loreBusy) return;
  if (!confirm("删除这条世界记忆？此后剧情将不再受它约束。")) return;
  try {
    const data = await fetchJSON("/api/world-memory/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sid: sessionId, index }),
    });
    worldMemory = data.world_memory || data.lore || [];
    renderLore();
  } catch (e) {
    alert(e.message);
  }
}

function setLoreBusy(state) {
  loreBusy = state;
  loreInput.disabled = state;
  loreSend.disabled = state;
}

async function askLore(q) {
  setLoreBusy(true);
  startLiveLLMRequest("inquiry", "inquiry");
  loreEmptyEl.classList.add("hidden");
  // 先落一个"问 + 答（流式）"的临时条目
  const li = document.createElement("li");
  li.className = "lore-item";
  li.innerHTML = `<div class="q"></div><div class="a cursor"></div>`;
  li.querySelector(".q").textContent = q;
  const aEl = li.querySelector(".a");
  loreListEl.appendChild(li);
  li.scrollIntoView({ block: "end" });
  try {
    await streamSSE(
      "/api/inquiry",
      (text) => {
        aEl.textContent += text;
        li.scrollIntoView({ block: "end" });
      },
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sid: sessionId, q }),
      },
    );
    // 后端已落库，同步到内存并重渲染（补上删除按钮）
    try {
      worldMemory = await fetchWorldMemory();
    } catch (_) {
      worldMemory.push({ type: "qa", q, a: aEl.textContent, ts: Date.now() / 1000 });
    }
    renderLore();
  } catch (e) {
    aEl.classList.remove("cursor");
    aEl.textContent = `【出错】${e.message}`;
    aEl.classList.add("error");
  } finally {
    stopLiveLLMRequest("inquiry");
    refreshLLMMetrics({ quiet: true });
    setLoreBusy(false);
    loreInput.focus();
  }
}

loreBtn.addEventListener("click", openLoreDrawer);
loreClose.addEventListener("click", closeLoreDrawer);
loreDrawer.querySelector(".drawer-mask").addEventListener("click", closeLoreDrawer);
loreForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const q = loreInput.value.trim();
  if (!q || loreBusy || !sessionId) return;
  loreInput.value = "";
  askLore(q);
});

// ---- 导演抽屉（幕后剧情调度，只读展示）----
async function fetchDirector() {
  return await fetchJSON(`/api/director?sid=${sessionId}`);
}

async function openDirectorDrawer() {
  if (!sessionId) return;
  directorDrawer.classList.remove("hidden");
  await refreshDirector();
}
function closeDirectorDrawer() { directorDrawer.classList.add("hidden"); }

async function refreshDirector() {
  if (!sessionId) return;
  try {
    const data = await fetchDirector();
    renderDirector(data.director_state || {}, data.turns || 0);
  } catch (e) {
    directorBodyEl.innerHTML = "";
    directorEmptyEl.textContent = `读取失败：${e.message}`;
    directorEmptyEl.classList.remove("hidden");
  }
}

function meterRow(label, value, cls) {
  const pct = Math.round(Math.max(0, Math.min(1, Number(value) || 0)) * 100);
  const row = document.createElement("div");
  row.className = "dir-meter";
  row.innerHTML =
    `<span class="m-label"></span>` +
    `<span class="m-track"><span class="m-fill ${cls}"></span></span>` +
    `<span class="m-num">${pct}%</span>`;
  row.querySelector(".m-label").textContent = label;
  row.querySelector(".m-fill").style.width = `${pct}%`;
  return row;
}

function field(label, value, valClass) {
  const wrap = document.createElement("div");
  wrap.className = "dir-field";
  wrap.innerHTML = `<span class="label"></span><span class="val ${valClass || ""}"></span>`;
  wrap.querySelector(".label").textContent = label;
  wrap.querySelector(".val").textContent = value || "—";
  return wrap;
}

function renderAgentOutputs(outputs) {
  if (!outputs || typeof outputs !== "object" || !Object.keys(outputs).length) return;
  const labels = {
    event: "Event Agent",
    hook: "Hook Agent",
    payoff: "Payoff Agent",
    pacing: "Pacing Agent",
    audit: "Audit Agent",
  };
  const head = document.createElement("div");
  head.className = "dir-section-head";
  head.textContent = "AGENT 原始输出";
  directorBodyEl.appendChild(head);

  for (const key of ["event", "hook", "payoff", "pacing", "audit"]) {
    const entry = outputs[key];
    if (!entry || typeof entry !== "object") continue;
    const details = document.createElement("details");
    details.className = "dir-agent-output";
    details.open = true;
    const summary = document.createElement("summary");
    const name = document.createElement("span");
    name.textContent = labels[key] || key;
    const meta = document.createElement("span");
    meta.className = `dir-agent-source ${entry.source === "fallback" ? "fallback" : ""}`;
    meta.textContent = entry.source === "fallback"
      ? `fallback${entry.fallback_reason ? ` · ${entry.fallback_reason}` : ""}`
      : entry.model || "LLM";
    summary.append(name, meta);
    const pre = document.createElement("pre");
    pre.textContent = JSON.stringify(entry.output ?? null, null, 2);
    details.append(summary, pre);
    directorBodyEl.appendChild(details);
  }
}

function renderDynamicDirector(state) {
  const event = state.event || null;
  const plan = state.current_plan || null;
  const intent = state.intent || null;
  const hook = state.hook_state || null;

  const phaseWrap = document.createElement("div");
  phaseWrap.className = "dir-phase";
  const badge = document.createElement("span");
  const status = event?.status || "idle";
  const activeStatuses = new Set(["active", "resolving", "abandoning"]);
  const statusLabels = {
    active: "事件进行中",
    resolving: "事件结算中",
    resolved: "事件已结算",
    abandoning: "事件离开中",
    abandoned: "事件已离开",
  };
  badge.className = `dir-badge ${activeStatuses.has(status) ? "active" : "cooldown"}`;
  badge.textContent = statusLabels[status] || "自由场景";
  phaseWrap.appendChild(badge);
  const note = document.createElement("span");
  note.className = "dir-phase-note";
  note.textContent = event?.core || "当前没有正式事件";
  phaseWrap.appendChild(note);
  directorBodyEl.appendChild(phaseWrap);

  if (event) {
    const meta = document.createElement("div");
    meta.className = "dir-meta";
    const turns = Number(event.turns) || 0;
    const maxTurns = Number(event.max_turns) || 5;
    const startTurn = Number(event.start_turn) || 0;
    const endedTurn = Number(event.ended_turn) || 0;
    const isFinished = status === "resolved" || status === "abandoned";
    meta.innerHTML = isFinished
      ? `<span class="item">事件历时 <b>${turns}</b> 轮</span>` +
        `<span class="item">第 <b>${startTurn}</b> 至 <b>${endedTurn || startTurn}</b> 回合</span>`
      : `<span class="item${turns >= maxTurns ? " warn" : ""}">事件轮数 <b>${turns}/${maxTurns}</b></span>` +
        `<span class="item">开始于第 <b>${startTurn}</b> 回合</span>`;
    directorBodyEl.appendChild(meta);
  }

  if (intent) {
    const intentMeta = document.createElement("div");
    intentMeta.className = "dir-meta";
    const attempts = Number(intent.attempts) || 1;
    const key = document.createElement("span");
    key.className = "item";
    key.append("当前意图：");
    const strong = document.createElement("b");
    strong.textContent = intent.key || "未归类";
    key.appendChild(strong);
    const count = document.createElement("span");
    count.className = `item${attempts >= 2 ? " warn" : ""}`;
    count.innerHTML = `连续尝试 <b>${attempts}</b> 次${attempts >= 2 ? "（本轮强制结算）" : ""}`;
    intentMeta.append(key, count);
    directorBodyEl.appendChild(intentMeta);
  }

  if (hook?.desc) {
    const hookCard = document.createElement("div");
    hookCard.className = "dir-hook";
    hookCard.appendChild(field("当前钩子", hook.desc, "desc"));
    hookCard.appendChild(field("可选目标", hook.goal));
    const hookMeta = document.createElement("div");
    hookMeta.className = "dir-meta";
    hookMeta.innerHTML = `<span class="item">状态 <b>${hook.status || "offered"}</b></span>` +
      `<span class="item">展示于第 <b>${Number(hook.created_turn) || "—"}</b> 回合</span>`;
    hookCard.appendChild(hookMeta);
    directorBodyEl.appendChild(hookCard);
  }

  if (plan) {
    const card = document.createElement("div");
    card.className = "dir-payoff" + (plan.turn_mode === "resolve" ? " armed" : "");
    const tag = document.createElement("span");
    tag.className = "dir-armed-tag" + (plan.turn_mode === "resolve" ? "" : " off");
    tag.textContent = `${plan.turn_mode || "progress"} · ${plan.event_action || "none"}`;
    card.appendChild(tag);
    card.appendChild(field("本轮目标", plan.turn_objective || plan.current_goal));
    if (plan.hook?.desc) {
      card.appendChild(field("本轮呈现钩子", plan.hook.desc, "desc"));
      card.appendChild(field("对应灵光方向", plan.hook.goal));
    }
    if (plan.payoff?.desc) {
      card.appendChild(field("待触发爽点", plan.payoff.desc, "desc"));
      card.appendChild(field("触发条件", plan.payoff.trigger, "trigger"));
      if (plan.payoff.binding) {
        card.appendChild(field(
          "动态关联",
          `${plan.payoff.binding.opportunity_name || "—"} → ${plan.payoff.binding.reward_name || "—"}`,
        ));
      }
    } else if (plan.payoff) {
      // 兼容拆分前保存的即时爽点结构。
      card.appendChild(field("历史爽点", `[${plan.payoff.type || "—"}] ${plan.payoff.outcome || "—"}`, "desc"));
      card.appendChild(field("兑现证明", plan.payoff.proof));
    }
    if (Array.isArray(plan.beats) && plan.beats.length) {
      card.appendChild(field("剧情骨架", plan.beats.join(" → ")));
    }
    if (Array.isArray(plan.forced_reasons) && plan.forced_reasons.length) {
      card.appendChild(field("后端强制", plan.forced_reasons.join("；"), "trigger"));
    }
    if (Array.isArray(plan.selected_facts) && plan.selected_facts.length) {
      card.appendChild(field("固定世界引用", plan.selected_facts.map((row) => row.id).join("、")));
    }
    if (Array.isArray(plan.must_not) && plan.must_not.length) {
      card.appendChild(field("禁止事项", plan.must_not.join("；")));
    }
    directorBodyEl.appendChild(card);
  }

  if (state.last_audit) {
    const audit = document.createElement("div");
    audit.className = "dir-last";
    const ok = !!state.last_audit.fulfilled;
    audit.appendChild(field("执行审计", ok ? "已落实导演骨架" : "未完整落实，需要修复", ok ? "desc" : "trigger"));
    if (state.last_audit.payoff_triggered != null) {
      audit.appendChild(field("爽点审计", state.last_audit.payoff_triggered ? "本轮已触发" : "本轮未触发", state.last_audit.payoff_triggered ? "desc" : ""));
    }
    if (state.last_audit.evidence) audit.appendChild(field("正文证据", state.last_audit.evidence));
    if (Array.isArray(state.last_audit.violations) && state.last_audit.violations.length) {
      audit.appendChild(field("违规", state.last_audit.violations.join("；"), "trigger"));
    }
    directorBodyEl.appendChild(audit);
  }
  renderAgentOutputs(state.agent_outputs || state.planner?.agents);
}

function renderDirector(state, turns) {
  directorBodyEl.innerHTML = "";
  const hasContent = state && (state.current_plan || state.event || state.hook_state || state.agent_outputs || state.phase || state.payoff || state.last_fired);
  directorEmptyEl.classList.toggle("hidden", !!hasContent);
  if (!hasContent) return;

  if (state.current_plan || Object.prototype.hasOwnProperty.call(state, "event")) {
    renderDynamicDirector(state);
    return;
  }

  const phase = state.phase || "active";

  // 阶段徽标
  const phaseWrap = document.createElement("div");
  phaseWrap.className = "dir-phase";
  const badge = document.createElement("span");
  badge.className = `dir-badge ${phase}`;
  badge.textContent = phase === "cooldown" ? "留白期" : "运作中";
  phaseWrap.appendChild(badge);
  const pnote = document.createElement("span");
  pnote.className = "dir-phase-note";
  pnote.textContent = phase === "cooldown"
    ? "爽点刚退场，暂不上膛，顺着你的路走几轮再孕育下一个"
    : "正在养一个当前爽点";
  phaseWrap.appendChild(pnote);
  directorBodyEl.appendChild(phaseWrap);

  // 场景追踪（黏太久会触发切场指导）
  if (state.scene || state.scene_turns != null) {
    const sceneTurns = Number(state.scene_turns) || 0;
    const stale = sceneTurns >= 3;
    const sc = document.createElement("div");
    sc.className = "dir-meta";
    const label = state.scene ? `当前场景：<b>${state.scene}</b>` : "当前场景：<b>（未标注）</b>";
    sc.innerHTML =
      `<span class="item">${label}</span>` +
      `<span class="item${stale ? " warn" : ""}">已停留 <b>${sceneTurns}</b> 轮${stale ? "（已催切场）" : ""}</span>`;
    directorBodyEl.appendChild(sc);
  }

  // 当前爽点卡片
  const payoff = state.payoff;
  if (payoff && payoff.desc) {
    const card = document.createElement("div");
    card.className = "dir-payoff" + (payoff.armed ? " armed" : "");

    const tag = document.createElement("span");
    tag.className = "dir-armed-tag" + (payoff.armed ? "" : " off");
    tag.textContent = payoff.armed ? "● 已上膛（可同轮兑现）" : "○ 铺垫中（未上膛）";
    card.appendChild(tag);

    card.appendChild(field("爽点", payoff.desc, "desc"));
    card.appendChild(field("触发条件", payoff.trigger, "trigger"));
    card.appendChild(field("本轮指导", payoff.guidance));
    card.appendChild(meterRow("成熟度", payoff.maturity, "maturity"));
    card.appendChild(meterRow("接近度", payoff.proximity, "proximity"));

    // 元信息
    const meta = document.createElement("div");
    meta.className = "dir-meta";
    const bits = [];
    if (payoff.start_turn != null) bits.push(`<span class="item">孕育于第 <b>${payoff.start_turn}</b> 回合</span>`);
    const converge = Number(payoff.converge_turns) || 0;
    bits.push(`<span class="item${converge > 0 ? " warn" : ""}">连续配合 <b>${converge}</b> 轮${converge >= 2 ? "（已催上膛）" : ""}</span>`);
    const drift = Number(state.drift_turns) || 0;
    bits.push(`<span class="item${drift > 0 ? " warn" : ""}">连续偏离 <b>${drift}</b> 轮</span>`);
    meta.innerHTML = bits.join("");
    card.appendChild(meta);
    directorBodyEl.appendChild(card);
  } else if (phase === "cooldown") {
    const cd = document.createElement("div");
    cd.className = "dir-meta";
    const until = Number(state.cooldown_until) || 0;
    const left = Math.max(0, until - (Number(turns) || 0));
    cd.innerHTML = `<span class="item">留白剩余约 <b>${left}</b> 回合（第 ${until} 回合后孕育新爽点）</span>`;
    directorBodyEl.appendChild(cd);
  }

  // 上一个退场的爽点
  const last = state.last_fired;
  if (last && last.desc) {
    const el = document.createElement("div");
    el.className = "dir-last";
    const outcome = last.outcome === "fired" ? "已兑现" : "已废弃";
    el.innerHTML =
      `<span class="dir-section-head">上一个爽点</span>` +
      `<div style="margin-top:8px"><span class="tag ${last.outcome}">${outcome}</span>` +
      `（第 ${last.turn} 回合）：<span class="desc-txt"></span></div>`;
    el.querySelector(".desc-txt").textContent = last.desc;
    directorBodyEl.appendChild(el);
  }

  // 备忘
  if (state.note) {
    const note = document.createElement("div");
    note.className = "dir-note";
    note.textContent = state.note;
    directorBodyEl.appendChild(note);
  }
}

directorBtn.addEventListener("click", openDirectorDrawer);
directorClose.addEventListener("click", closeDirectorDrawer);
directorRefresh.addEventListener("click", refreshDirector);
directorDrawer.querySelector(".drawer-mask").addEventListener("click", closeDirectorDrawer);

// ---- 世界约束 Agent 抽屉（位置 + 知识视野，只读展示）----
async function fetchConstraintState() {
  const data = await fetchJSON(`/api/world-state?sid=${sessionId}`);
  return data.world_state || null;
}

async function openConstraintDrawer() {
  if (!sessionId) return;
  constraintDrawer.classList.remove("hidden");
  if (!worldState) await refreshConstraint();
  else renderConstraint(worldState);
}
function closeConstraintDrawer() { constraintDrawer.classList.add("hidden"); }

async function refreshConstraint() {
  if (!sessionId) return;
  try {
    const state = await fetchConstraintState();
    renderConstraint(state);
  } catch (e) {
    constraintBodyEl.innerHTML = "";
    constraintEmptyEl.textContent = `读取失败：${e.message}`;
    constraintEmptyEl.classList.remove("hidden");
  }
}

function renderConstraint(state) {
  worldState = state || null;
  constraintBodyEl.innerHTML = "";
  const hasContent = !!(worldState && worldState.location);
  constraintEmptyEl.classList.toggle("hidden", hasContent);
  if (!hasContent) return;

  const loc = worldState.location || {};
  constraintBodyEl.appendChild(constraintSection("真实位置", [
    ["大区域", loc.region_name],
    ["地点", loc.location_name],
    ["子地点", loc.site_name],
    ["场景状态", loc.location_state],
    ["迷路风险", loc.lost_risk],
    ["行动意图", loc.intended_destination_name],
  ]));

  const knowledge = worldState.knowledge || {};
  constraintBodyEl.appendChild(chipSection("已确认地点", knowledge.confirmed_locations));
  constraintBodyEl.appendChild(chipSection("听闻地点", knowledge.rumored_locations));
  constraintBodyEl.appendChild(chipSection("已确认路线", knowledge.confirmed_routes));
  constraintBodyEl.appendChild(chipSection("已知势力", knowledge.known_factions));
  constraintBodyEl.appendChild(chipSection("已知功法", knowledge.known_arts));
  constraintBodyEl.appendChild(chipSection("机缘线索", knowledge.known_opportunities));
}

function constraintSection(title, rows) {
  const section = document.createElement("section");
  section.className = "constraint-section";
  const h = document.createElement("h3");
  h.textContent = title;
  section.appendChild(h);
  for (const [label, value] of rows) {
    if (!value) continue;
    const row = document.createElement("div");
    row.className = "constraint-row";
    row.innerHTML = `<span class="c-label"></span><span class="c-value"></span>`;
    row.querySelector(".c-label").textContent = label;
    row.querySelector(".c-value").textContent = value;
    section.appendChild(row);
  }
  return section;
}

function chipSection(title, items) {
  const section = document.createElement("section");
  section.className = "constraint-section";
  const h = document.createElement("h3");
  h.textContent = `${title}${items && items.length ? ` (${items.length})` : ""}`;
  section.appendChild(h);
  const wrap = document.createElement("div");
  wrap.className = "constraint-chips";
  const list = (items || []).filter(Boolean);
  if (list.length) {
    for (const item of list) {
      const chip = document.createElement("span");
      chip.className = "constraint-chip";
      chip.textContent = item;
      wrap.appendChild(chip);
    }
  } else {
    const empty = document.createElement("span");
    empty.className = "constraint-none";
    empty.textContent = "无";
    wrap.appendChild(empty);
  }
  section.appendChild(wrap);
  return section;
}

constraintBtn.addEventListener("click", openConstraintDrawer);
constraintClose.addEventListener("click", closeConstraintDrawer);
constraintRefresh.addEventListener("click", refreshConstraint);
constraintDrawer.querySelector(".drawer-mask").addEventListener("click", closeConstraintDrawer);

// ---- LLM 请求指标 ----
function formatRequestTime(ts) {
  const d = new Date(Number(ts) * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function renderLLMMetrics(items) {
  llmListEl.innerHTML = "";
  llmEmptyEl.textContent = "还没有 LLM 请求记录。";
  llmEmptyEl.classList.toggle("hidden", items.length > 0);
  const statusLabels = { success: "成功", timeout: "超时", api_error: "接口错误", error: "错误" };
  for (const item of items) {
    const li = document.createElement("li");
    const statusClass = item.status === "api_error" ? "error" : item.status;
    li.className = "llm-item";
    li.innerHTML = `
      <div class="llm-item-head">
        <span class="llm-kind"></span>
        <span class="llm-status ${statusClass}"></span>
        <span class="llm-duration"></span>
      </div>
      <div class="llm-item-meta">
        <span class="llm-model"></span>
        <time class="llm-time"></time>
      </div>`;
    li.querySelector(".llm-kind").textContent = LLM_REQUEST_LABELS[item.request_type] || item.request_type;
    li.querySelector(".llm-status").textContent = statusLabels[item.status] || item.status;
    li.querySelector(".llm-duration").textContent = formatDuration(item.duration_ms);
    li.querySelector(".llm-model").textContent = item.model || "未记录模型";
    li.querySelector(".llm-time").textContent = formatRequestTime(item.created_at);
    if (item.error_type) {
      const error = document.createElement("div");
      error.className = "llm-error";
      error.textContent = item.error_type;
      li.appendChild(error);
    }
    llmListEl.appendChild(li);
  }
}

async function refreshLLMMetrics({ quiet = false } = {}) {
  if (!sessionId) return;
  try {
    const data = await fetchJSON(`/api/llm-metrics?sid=${sessionId}&limit=30`);
    renderLLMMetrics(data.requests || []);
  } catch (e) {
    if (quiet) return;
    llmListEl.innerHTML = "";
    llmEmptyEl.textContent = `读取失败：${e.message}`;
    llmEmptyEl.classList.remove("hidden");
  }
}

async function openLLMDrawer() {
  if (!sessionId) return;
  llmDrawer.classList.remove("hidden");
  renderLiveLLMRequests();
  await refreshLLMMetrics();
  if (!llmRefreshTimer) {
    llmRefreshTimer = setInterval(() => refreshLLMMetrics({ quiet: true }), 2000);
  }
}

function closeLLMDrawer() {
  llmDrawer.classList.add("hidden");
  if (llmRefreshTimer) clearInterval(llmRefreshTimer);
  llmRefreshTimer = null;
}

llmBtn.addEventListener("click", openLLMDrawer);
llmClose.addEventListener("click", closeLLMDrawer);
llmRefresh.addEventListener("click", refreshLLMMetrics);
llmDrawer.querySelector(".drawer-mask").addEventListener("click", closeLLMDrawer);

// ---- 启动：有存档则续上最近一局，否则开新局 ----
async function boot() {
  try {
    const saves = await fetchSaves();
    if (saves.length > 0) {
      await loadGame(saves[0].id, saves[0].name);
      return;
    }
  } catch (_) { /* 忽略，退回新局 */ }
  await newGame();
}

boot();
