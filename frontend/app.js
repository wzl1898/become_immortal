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

function clearStatus() {
  statusBodyEl.innerHTML = "";
  statusPanel.classList.add("empty");
}

let sessionId = null;
let currentName = "";
let busy = false;
let lore = []; // 见闻录，[{q, a, ts}]
let loreBusy = false; // 问询独立忙态，不锁主行动

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
async function streamSSE(url, onDelta) {
  const resp = await fetch(url);
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
      else if (evt.event === "done") doneData = evt.data;
      else if (evt.event === "error") throw new Error(evt.data.message);
    }
  }
  return doneData; // done 事件的 payload（叙事流会带上刷新后的 inventory）
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
    row.innerHTML = `<span class="obj-name"></span><span class="obj-loc"></span>`;
    row.querySelector(".obj-name").textContent = head;
    row.querySelector(".obj-loc").textContent = it.kind || "";
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

async function narrate(url) {
  const block = addBlock("narration cursor");
  let hintBlock = null;
  let full = "";
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
    });
    // 流结束：用后端刷新后的结构化库补上冷物品折叠区
    if (done && done.inventory) renderColdItems(done.inventory);
  } catch (e) {
    block.remove();
    if (hintBlock) hintBlock.remove();
    addBlock("error", `【出错】${e.message}`);
    throw e;
  } finally {
    block.classList.remove("cursor");
  }
}

// ---- 游戏生命周期 ----
async function newGame() {
  setBusy(true);
  storyEl.innerHTML = "";
  clearStatus();
  lore = [];
  addBlock("narration", "　　天地灵气涌动，你的故事即将开始……").classList.add("cursor");
  try {
    const resp = await fetch("/api/new", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    const data = await resp.json();
    setCurrent(data.session_id, "无名修士");
    storyEl.innerHTML = "";
    await narrate(`/api/opening?sid=${sessionId}`);
  } catch (_) {
    // 错误已在 narrate 里展示
  } finally {
    setBusy(false);
  }
}

// 读档：拉完整剧情并重放到界面
async function loadGame(sid, name) {
  setBusy(true);
  try {
    const resp = await fetch(`/api/load?sid=${sid}`);
    if (!resp.ok) throw new Error((await resp.json()).detail || "读档失败");
    const data = await resp.json();
    setCurrent(sid, name);
    lore = data.lore || [];
    storyEl.innerHTML = "";
    clearStatus();
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
    await narrate(`/api/action?sid=${sessionId}&text=${encodeURIComponent(text)}`);
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
  await fetch("/api/rename", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sid: s.id, name: name.trim() }),
  });
  if (s.id === sessionId) setCurrent(sessionId, name.trim());
  renderSaves();
}

async function deleteSave(s) {
  if (!confirm(`确定删除《${s.name}》？此操作不可恢复。`)) return;
  await fetch("/api/delete", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sid: s.id }),
  });
  if (s.id === sessionId) { sessionId = null; setCurrent(null, ""); }
  renderSaves();
}

savesBtn.addEventListener("click", openDrawer);
drawerClose.addEventListener("click", closeDrawer);
drawer.querySelector(".drawer-mask").addEventListener("click", closeDrawer);

// ---- 见闻抽屉（问询旁路）----
function openLoreDrawer() {
  if (!sessionId) return;
  loreDrawer.classList.remove("hidden");
  renderLore();
  loreInput.focus();
}
function closeLoreDrawer() { loreDrawer.classList.add("hidden"); }

function renderLore() {
  loreListEl.innerHTML = "";
  loreEmptyEl.classList.toggle("hidden", lore.length > 0);
  lore.forEach((item, i) => loreListEl.appendChild(renderLoreItem(item, i)));
}

function renderLoreItem(item, index) {
  const li = document.createElement("li");
  li.className = "lore-item";
  li.innerHTML = `
    <div class="q"></div>
    <div class="a"></div>
    <button class="del" title="删除这条见闻">删除</button>`;
  li.querySelector(".q").textContent = item.q;
  li.querySelector(".a").textContent = item.a;
  li.querySelector(".del").addEventListener("click", () => deleteLoreItem(index));
  return li;
}

async function deleteLoreItem(index) {
  if (loreBusy) return;
  if (!confirm("删除这条见闻？此后剧情将不再受它约束。")) return;
  try {
    const resp = await fetch("/api/lore/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sid: sessionId, index }),
    });
    if (!resp.ok) throw new Error((await resp.json()).detail || "删除失败");
    lore = (await resp.json()).lore || [];
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
    await streamSSE(`/api/inquiry?sid=${sessionId}&q=${encodeURIComponent(q)}`, (text) => {
      aEl.textContent += text;
      li.scrollIntoView({ block: "end" });
    });
    // 后端已落库，同步到内存并重渲染（补上删除按钮）
    lore.push({ q, a: aEl.textContent, ts: Date.now() / 1000 });
    renderLore();
  } catch (e) {
    aEl.classList.remove("cursor");
    aEl.textContent = `【出错】${e.message}`;
    aEl.classList.add("error");
  } finally {
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
