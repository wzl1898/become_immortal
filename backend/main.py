"""FastAPI 入口：修仙文字冒险游戏后端。

接口：
- POST /api/new                 新建一局，返回 {session_id}
- GET  /api/opening?sid=...      SSE 流式返回开场叙事
- GET  /api/action?sid=&text=   SSE 流式返回玩家行动后的剧情
- GET  /api/saves               列出所有存档摘要
- GET  /api/load?sid=...         读档，返回完整剧情用于重放
- POST /api/rename              重命名存档
- POST /api/delete              删除存档
- 静态前端挂在 /
"""

import asyncio
import json
import os

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import HTMLResponse, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import game  # noqa: E402
from llm import stream_chat  # noqa: E402

app = FastAPI(title="become_immortal")

# 开发用前端热更新：设 LIVE_RELOAD=1 时，后端监视 frontend/ 变化并让页面自动刷新。
# 不设则完全无影响，正常游玩。
LIVE_RELOAD = os.getenv("LIVE_RELOAD") == "1"


@app.on_event("startup")
def _startup() -> None:
    game.init()

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _stream(messages: list[dict], on_done):
    """公共的流式生成器：边流边发 delta，结束后调 on_done(full_text) 落库。"""
    full = []
    try:
        async for piece in stream_chat(messages):
            full.append(piece)
            yield _sse("delta", {"text": piece})
    except Exception as e:  # noqa: BLE001
        yield _sse("error", {"message": str(e)})
        return
    on_done("".join(full))
    yield _sse("done", {})


def _narrate(messages: list[dict], sid: str, user_content: str | None):
    """叙事流：结束后把这一轮写入会话历史 + transcript。"""
    return _stream(messages, lambda text: game.commit(sid, user_content, text))


def _answer_inquiry(messages: list[dict], sid: str, question: str):
    """见闻问询流：结束后只把问答追加进见闻录，不推进剧情。"""
    return _stream(messages, lambda text: game.commit_lore(sid, question, text))


class NewGameBody(BaseModel):
    name: str | None = None


class RenameBody(BaseModel):
    sid: str
    name: str


class SidBody(BaseModel):
    sid: str


@app.post("/api/new")
async def new_game(body: NewGameBody | None = None):
    name = (body.name.strip() if body and body.name else "") or game.DEFAULT_NAME
    sid = game.create_session(name)
    return {"session_id": sid}


@app.get("/api/saves")
async def saves():
    return {"saves": game.list_saves()}


@app.get("/api/load")
async def load(sid: str):
    transcript = game.get_transcript(sid)
    if transcript is None:
        raise HTTPException(404, "存档不存在")
    return {"session_id": sid, "transcript": transcript, "lore": game.get_lore(sid) or []}


@app.post("/api/rename")
async def rename(body: RenameBody):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "名字不能为空")
    if not game.rename(body.sid, name):
        raise HTTPException(404, "存档不存在")
    return {"ok": True}


@app.post("/api/delete")
async def delete(body: SidBody):
    if not game.delete(body.sid):
        raise HTTPException(404, "存档不存在")
    return {"ok": True}


@app.get("/api/opening")
async def opening(sid: str):
    if not game.exists(sid):
        raise HTTPException(404, "会话不存在，请重新开始")
    messages = game.messages_for_opening(sid)
    return StreamingResponse(
        _narrate(messages, sid, None),
        media_type="text/event-stream",
    )


@app.get("/api/action")
async def action(sid: str, text: str):
    if not game.exists(sid):
        raise HTTPException(404, "会话不存在，请重新开始")
    text = (text or "").strip()
    if not text:
        raise HTTPException(400, "行动不能为空")
    messages = game.messages_for_action(sid, text)
    return StreamingResponse(
        _narrate(messages, sid, text),
        media_type="text/event-stream",
    )


# ---- 见闻问询（旁路：不推进剧情，只补全背景）----

class LoreDeleteBody(BaseModel):
    sid: str
    index: int


@app.get("/api/inquiry")
async def inquiry(sid: str, q: str):
    if not game.exists(sid):
        raise HTTPException(404, "会话不存在，请重新开始")
    q = (q or "").strip()
    if not q:
        raise HTTPException(400, "问题不能为空")
    messages = game.messages_for_inquiry(sid, q)
    return StreamingResponse(
        _answer_inquiry(messages, sid, q),
        media_type="text/event-stream",
    )


@app.get("/api/lore")
async def lore(sid: str):
    items = game.get_lore(sid)
    if items is None:
        raise HTTPException(404, "存档不存在")
    return {"lore": items}


@app.post("/api/lore/delete")
async def lore_delete(body: LoreDeleteBody):
    items = game.delete_lore(body.sid, body.index)
    if items is None:
        raise HTTPException(404, "见闻不存在")
    return {"ok": True, "lore": items}


# ---- 开发用前端热更新（LIVE_RELOAD=1 时启用）----

_LIVERELOAD_SNIPPET = """
<script>
(function () {
  var es = new EventSource("/__livereload");
  var seen = null;
  es.addEventListener("change", function (e) {
    if (seen !== null && seen !== e.data) location.reload();
    seen = e.data;
  });
})();
</script>
"""


def _frontend_mtime() -> float:
    """frontend/ 目录下所有文件的最新修改时间。"""
    latest = 0.0
    for root, _dirs, files in os.walk(FRONTEND_DIR):
        for f in files:
            try:
                latest = max(latest, os.path.getmtime(os.path.join(root, f)))
            except OSError:
                pass
    return latest


if LIVE_RELOAD:

    from fastapi import Request  # noqa: E402

    @app.get("/__livereload")
    async def _livereload(request: Request):
        async def gen():
            # 客户端断开就退出，避免 reload 时这条长连接把优雅关闭卡死
            while not await request.is_disconnected():
                yield f"event: change\ndata: {_frontend_mtime()}\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/", response_class=HTMLResponse)
    async def _index_dev():
        with open(os.path.join(FRONTEND_DIR, "index.html"), encoding="utf-8") as f:
            html = f.read()
        return html.replace("</body>", _LIVERELOAD_SNIPPET + "</body>")


# 静态前端（放最后，避免覆盖 /api 与热更新路由）
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
