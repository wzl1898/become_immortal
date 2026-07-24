// 修仙文字冒险 —— 前端逻辑
const storyEl = document.getElementById("story");
const form = document.getElementById("input-form");
const input = document.getElementById("action");
const sendBtn = document.getElementById("send");
const restartBtn = document.getElementById("restart");

let sessionId = null;
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
  if (!state) input.focus();
}

// 解析 SSE 流，onDelta 收到文本增量，返回 Promise 在 done/error 时结束
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
    buffer = events.pop(); // 末尾可能是半个事件
    for (const raw of events) {
      const evt = parseEvent(raw);
      if (!evt) continue;
      if (evt.event === "delta") onDelta(evt.data.text);
      else if (evt.event === "error") throw new Error(evt.data.message);
      // "done" 无需处理，循环自然结束
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

// 把叙事流渲染到一个块里，带打字光标
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

async function startGame() {
  setBusy(true);
  storyEl.innerHTML = "";
  addBlock("narration", "　　天地灵气涌动，你的故事即将开始……").classList.add("cursor");
  try {
    const resp = await fetch("/api/new", { method: "POST" });
    sessionId = (await resp.json()).session_id;
    storyEl.innerHTML = "";
    await narrate(`/api/opening?sid=${sessionId}`);
  } catch (_) {
    // 错误已在 narrate 里展示
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
  startGame();
});

startGame();
