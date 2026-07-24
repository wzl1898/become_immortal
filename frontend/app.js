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

let sessionId = null;
let currentName = "";
let busy = false;

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
      else if (evt.event === "error") throw new Error(evt.data.message);
    }
  }
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

async function narrate(url) {
  const block = addBlock("narration cursor");
  try {
    await streamSSE(url, (text) => {
      block.textContent += text;
      storyEl.scrollTop = storyEl.scrollHeight;
    });
  } catch (e) {
    block.remove();
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
    storyEl.innerHTML = "";
    for (const blk of data.transcript) {
      addBlock(blk.role === "player" ? "player" : "narration", blk.text);
    }
    if (!data.transcript.length) {
      addBlock("narration", "（这一世尚未落笔，输入你的第一个行动。）");
    }
    closeDrawer();
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
