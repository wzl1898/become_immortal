"""FastAPI 入口：修仙文字冒险游戏后端。

接口：
- POST /api/new                 新建一局，返回 {session_id}
- GET  /api/opening?sid=...      SSE 流式返回开场叙事
- POST /api/action              SSE 流式返回玩家行动后的剧情
- GET  /api/saves               列出所有存档摘要
- GET  /api/load?sid=...         读档，返回完整剧情用于重放
- POST /api/rename              重命名存档
- POST /api/delete              删除存档
- GET  /api/character-state?sid=... 读取主角状态
- POST /api/inquiry             SSE 流式返回世界记忆问答
- GET  /api/world-memory?sid=... 读取世界记忆
- POST /api/world-memory/delete 删除一条世界记忆
- 静态前端挂在 /
"""

import asyncio
import inspect
import json
import os
import re

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Header, HTTPException, Request  # noqa: E402
from fastapi.responses import HTMLResponse, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import game  # noqa: E402
import story_cards  # noqa: E402
import background  # noqa: E402
import run_store  # noqa: E402
import store  # noqa: E402
import temporal_service  # noqa: E402
from llm import stream_chat  # noqa: E402

app = FastAPI(title="become_immortal")

# 开发用前端热更新：设 LIVE_RELOAD=1 时，后端监视 frontend/ 变化并让页面自动刷新。
# 不设则完全无影响，正常游玩。
LIVE_RELOAD = os.getenv("LIVE_RELOAD") == "1"
_USER_ID_RE = re.compile(r"^[A-Za-z0-9._%~-]{1,64}$")
_CANCEL_TASKS: dict[str, tuple[str, asyncio.Task]] = {}


@app.on_event("startup")
async def _startup() -> None:
    game.init()
    await run_store.init()
    await temporal_service.startup()


@app.on_event("shutdown")
async def _shutdown() -> None:
    await temporal_service.shutdown()

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _stream(
    messages: list[dict],
    on_done,
    *,
    request_type: str,
    session_id: str,
    turn: int | None = None,
    on_error=None,
):
    """公共的流式生成器：边流边发 delta，结束后调 on_done(full_text) 落库。

    on_done 可返回一个 dict 作为 done 事件的 payload（如刷新后的物品库）；返回
    None 则 done 事件为空。
    """
    full = []
    try:
        async for piece in stream_chat(
            messages, request_type=request_type, session_id=session_id, turn=turn
        ):
            full.append(piece)
            yield _sse("delta", {"text": piece})
    except asyncio.CancelledError:
        if on_error:
            on_error()
        raise
    except Exception as e:  # noqa: BLE001
        if on_error:
            on_error()
        yield _sse("error", {"message": str(e)})
        return
    completed = "".join(full)
    if not completed.strip():
        if on_error:
            on_error()
        yield _sse("error", {
            "message": "模型没有返回可显示的正文，请重试。当前回合未保存。"
        })
        return
    payload = on_done(completed)
    if inspect.isawaitable(payload):
        payload = await payload
    payload = payload or {}
    yield _sse("done", payload)


def _narrate(messages: list[dict], sid: str, user_content: str | None):
    """叙事流：结束后把这一轮写入会话历史 + transcript，done 带刷新后的物品库。"""
    async def _done(text: str) -> dict:
        await game.commit(sid, user_content, text)
        return {
            "character_state": game.get_character_state(sid) or {},
            "inventory": game.get_inventory(sid) or [],
            "world_state": game.get_world_state(sid) or {},
            "director_state": game.get_director_state(sid) or {},
        }
    request_type = "opening" if user_content is None else "narrative"
    target_turn = (game.get_turns(sid) or 0) + 1
    return _stream(
        messages,
        _done,
        request_type=request_type,
        session_id=sid,
        turn=target_turn,
        on_error=lambda: game.rollback_prepared_action(sid),
    )


async def _answer_inquiry(sid: str, question: str):
    """问询 ReAct 流：工具循环结束后发送答案并写入世界记忆。"""
    try:
        answer, _tool_calls = await game.run_inquiry_react(sid, question)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        yield _sse("error", {"message": str(exc)})
        return
    if not answer.strip():
        yield _sse("error", {"message": "模型没有返回可显示的问询答案，请重试。"})
        return
    yield _sse("delta", {"text": answer})
    game.commit_inquiry_memory(sid, question, answer)
    yield _sse("done", {})


class NewGameBody(BaseModel):
    name: str | None = None
    story_card_id: str = story_cards.DEFAULT_CARD_ID


class RenameBody(BaseModel):
    sid: str
    name: str


class SidBody(BaseModel):
    sid: str


class ActionBody(BaseModel):
    sid: str
    text: str
    client_request_id: str | None = None
    base_turn: int | None = None
    base_version: int | None = None


class RunCreateBody(BaseModel):
    sid: str
    input_kind: str
    text: str | None = None
    client_request_id: str | None = None
    base_turn: int | None = None
    base_version: int | None = None


class RerunBody(BaseModel):
    node_key: str
    mode: str = "current"


class InquiryBody(BaseModel):
    sid: str
    q: str


def _user_id(x_user_id: str) -> str:
    user_id = x_user_id.strip()
    if not _USER_ID_RE.fullmatch(user_id):
        raise HTTPException(400, "用户标识格式无效")
    return user_id


def _require_owned(sid: str, user_id: str) -> None:
    # Return 404 for foreign saves so their existence is not disclosed.
    if not game.owned_by(sid, user_id):
        raise HTTPException(404, "存档不存在")


def _run_store_http_error(exc: run_store.RunStoreError) -> HTTPException:
    if isinstance(exc, (run_store.SaveNotFound, run_store.RunNotFound)):
        return HTTPException(404, str(exc))
    if isinstance(exc, run_store.AlreadyCommitted):
        return HTTPException(409, str(exc))
    if isinstance(exc, (run_store.TurnConflict, run_store.ActiveRunConflict)):
        return HTTPException(409, str(exc))
    return HTTPException(500, str(exc))


async def _create_and_start_run(
    sid: str,
    *,
    input_kind: str,
    action: str | None,
    client_request_id: str | None,
    base_turn: int | None,
    base_version: int | None,
) -> dict:
    try:
        run, created = await run_store.reserve_primary_run(
            sid,
            input_kind=input_kind,
            action=action,
            client_request_id=client_request_id,
            base_turn=base_turn,
            base_version=base_version,
        )
    except run_store.RunStoreError as exc:
        raise _run_store_http_error(exc) from exc
    if run["status"] in {"starting", "running"}:
        try:
            await temporal_service.start_run(run)
        except temporal_service.TemporalUnavailable as exc:
            raise HTTPException(
                503,
                {
                    "message": str(exc),
                    "run_id": run["id"],
                    "retry_with_same_client_request_id": bool(client_request_id),
                },
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                503,
                {"message": f"Temporal 启动失败：{exc}", "run_id": run["id"]},
            ) from exc
    return (await run_store.get_run(run["id"])) or run


def _persistent_sse(event: dict) -> str:
    return (
        f"id: {event['sequence']}\n"
        f"event: {event['event_type']}\n"
        f"data: {json.dumps(event['payload'], ensure_ascii=False)}\n\n"
    )


async def _run_event_stream(run_id: str, after: int = 0):
    cursor = max(0, int(after))
    terminal = {"done", "error", "canceled"}
    idle_polls = 0
    while True:
        events = await run_store.list_events(run_id, cursor)
        if events:
            idle_polls = 0
            for event in events:
                cursor = int(event["sequence"])
                yield _persistent_sse(event)
                if event["event_type"] in terminal:
                    return
        else:
            run = await run_store.get_run(run_id)
            if run is None:
                yield _sse("error", {"message": "执行不存在"})
                return
            if run["status"] in run_store.TERMINAL_STATUSES:
                # Terminal status and event are committed together. One extra
                # poll closes a race with a replica/driver visibility boundary.
                idle_polls += 1
                if idle_polls >= 2:
                    return
            elif idle_polls and idle_polls % 40 == 0:
                yield ": keepalive\n\n"
            idle_polls += 1
        await asyncio.sleep(0.25)


@app.post("/api/runs")
async def create_run(
    body: RunCreateBody,
    user_id: str = Header(alias="X-User-ID"),
):
    user_id = _user_id(user_id)
    _require_owned(body.sid, user_id)
    input_kind = body.input_kind.strip().lower()
    if input_kind not in {"opening", "action"}:
        raise HTTPException(400, "input_kind 必须是 opening 或 action")
    action = (body.text or "").strip() if input_kind == "action" else None
    if input_kind == "action" and not action:
        raise HTTPException(400, "行动不能为空")
    return await _create_and_start_run(
        body.sid,
        input_kind=input_kind,
        action=action,
        client_request_id=body.client_request_id,
        base_turn=body.base_turn,
        base_version=body.base_version,
    )


@app.get("/api/runs")
async def runs(
    sid: str,
    limit: int = 50,
    user_id: str = Header(alias="X-User-ID"),
):
    user_id = _user_id(user_id)
    _require_owned(sid, user_id)
    return {"runs": await run_store.list_runs(sid, user_id=user_id, limit=limit)}


@app.get("/api/runs/{run_id}")
async def run_detail(run_id: str, user_id: str = Header(alias="X-User-ID")):
    run = await run_store.get_run(run_id, user_id=_user_id(user_id))
    if run is None:
        raise HTTPException(404, "执行不存在")
    return run


@app.get("/api/runs/{run_id}/nodes")
async def run_nodes(run_id: str, user_id: str = Header(alias="X-User-ID")):
    run = await run_store.get_run(run_id, user_id=_user_id(user_id))
    if run is None:
        raise HTTPException(404, "执行不存在")
    return {"nodes": await run_store.list_nodes_with_attempts(run_id)}


@app.get("/api/runs/{run_id}/events")
async def run_events(
    run_id: str,
    after: int = 0,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    user_id: str = Header(alias="X-User-ID"),
):
    run = await run_store.get_run(run_id, user_id=_user_id(user_id))
    if run is None:
        raise HTTPException(404, "执行不存在")
    cursor = after
    if last_event_id:
        try:
            cursor = max(cursor, int(last_event_id))
        except ValueError as exc:
            raise HTTPException(400, "Last-Event-ID 必须是整数") from exc
    return StreamingResponse(
        _run_event_stream(run_id, cursor),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Run-ID": run_id},
    )


async def _request_temporal_cancel(run: dict) -> None:
    await temporal_service.cancel_run(run)
    await run_store.finalize_canceled(run["id"])
    await temporal_service.wait_for_activity_drain(run)
    shadow_sid = f"shadow:{run['id']}"
    game._CACHE.pop(shadow_sid, None)
    store.delete_run_clone(shadow_sid)


def _schedule_temporal_cancel(run: dict) -> asyncio.Task:
    existing = _CANCEL_TASKS.get(run["id"])
    if existing is not None and not existing[1].done():
        return existing[1]
    task = asyncio.create_task(
        _request_temporal_cancel(run), name=f"cancel-run:{run['id']}"
    )
    _CANCEL_TASKS[run["id"]] = (run["sid"], task)

    def forget(completed: asyncio.Task) -> None:
        _CANCEL_TASKS.pop(run["id"], None)
        if not completed.cancelled():
            completed.exception()

    task.add_done_callback(forget)
    return task


@app.post("/api/runs/{run_id}/cancel")
async def cancel_run(run_id: str, user_id: str = Header(alias="X-User-ID")):
    try:
        run = await run_store.cancel_run(run_id, user_id=_user_id(user_id))
    except run_store.RunStoreError as exc:
        raise _run_store_http_error(exc) from exc
    _schedule_temporal_cancel(run)
    return {"ok": True, "run_id": run_id, "status": "canceling"}


@app.post("/api/runs/{run_id}/rerun")
async def rerun(
    run_id: str,
    body: RerunBody,
    user_id: str = Header(alias="X-User-ID"),
):
    try:
        debug_run = await run_store.create_debug_run(
            run_id,
            body.node_key,
            mode=body.mode,
            user_id=_user_id(user_id),
        )
        await temporal_service.start_run(debug_run)
    except run_store.RunStoreError as exc:
        raise _run_store_http_error(exc) from exc
    except temporal_service.TemporalUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    return debug_run


@app.post("/api/new")
async def new_game(
    body: NewGameBody | None = None,
    user_id: str = Header(alias="X-User-ID"),
):
    user_id = _user_id(user_id)
    name = (body.name.strip() if body and body.name else "") or None
    card_id = body.story_card_id if body else story_cards.DEFAULT_CARD_ID
    try:
        sid = game.create_session(name, user_id, card_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    card = game.get_story_card_info(sid)
    persisted = store.load(sid) or {}
    return {
        "session_id": sid,
        "name": name or card["default_save_name"],
        "story_card": card,
        "character_state": game.get_character_state(sid),
        "world_state": game.get_world_state(sid),
        "turns": int(persisted.get("turns") or 0),
        "version": int(persisted.get("version") or 0),
    }


@app.get("/api/story-cards")
async def available_story_cards(user_id: str = Header(alias="X-User-ID")):
    _user_id(user_id)
    return {"story_cards": story_cards.list_cards()}


@app.get("/api/saves")
async def saves(user_id: str = Header(alias="X-User-ID")):
    user_id = _user_id(user_id)
    return {"saves": game.list_saves(user_id)}


@app.get("/api/load")
async def load(
    sid: str, user_id: str = Header(alias="X-User-ID")
):
    _require_owned(sid, _user_id(user_id))
    if temporal_service.enabled():
        # Temporal commits in the Worker process. Refresh the API process's
        # copy before composing a response so turns, transcript and panels all
        # come from the same committed save.
        game.reload_persisted_state(sid)
    transcript = game.get_transcript(sid)
    if transcript is None:
        raise HTTPException(404, "存档不存在")
    persisted = store.load(sid) or {}
    active_run = None
    if temporal_service.enabled():
        active_run = await run_store.active_run_summary(sid, user_id=_user_id(user_id))
    return {
        "session_id": sid,
        "story_card": game.get_story_card_info(sid),
        "transcript": transcript,
        "character_state": game.get_character_state(sid) or {},
        "world_memory": game.get_world_memory(sid) or [],
        "lore": game.get_world_memory(sid) or [],
        "inventory": game.get_inventory(sid) or [],
        "director_state": game.get_director_state(sid) or {},
        "world_state": game.get_world_state(sid) or {},
        "turns": int(persisted.get("turns") or 0),
        "version": int(persisted.get("version") or 0),
        "active_run": active_run,
    }


@app.post("/api/rename")
async def rename(
    body: RenameBody,
    user_id: str = Header(alias="X-User-ID"),
):
    user_id = _user_id(user_id)
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "名字不能为空")
    if not game.rename(body.sid, name, user_id):
        raise HTTPException(404, "存档不存在")
    return {"ok": True}


@app.post("/api/delete")
async def delete(
    body: SidBody,
    user_id: str = Header(alias="X-User-ID"),
):
    user_id = _user_id(user_id)
    if temporal_service.enabled():
        pending = await run_store.inflight_runs(body.sid, user_id=user_id)
        cancel_tasks = [
            task
            for sid, task in _CANCEL_TASKS.values()
            if sid == body.sid and not task.done()
        ]
        for run in pending:
            if run["status"] in {"starting", "running"}:
                try:
                    run = await run_store.cancel_run(run["id"], user_id=user_id)
                except run_store.RunStoreError:
                    continue
            cancel_tasks.append(_schedule_temporal_cancel(run))
        if cancel_tasks:
            await asyncio.gather(*set(cancel_tasks))
    if not game.delete(body.sid, user_id):
        raise HTTPException(404, "存档不存在")
    return {"ok": True}


@app.get("/api/opening")
async def opening(
    sid: str,
    client_request_id: str | None = None,
    base_turn: int | None = None,
    base_version: int | None = None,
    user_id: str = Header(alias="X-User-ID"),
):
    user_id = _user_id(user_id)
    if not game.owned_by(sid, user_id):
        raise HTTPException(404, "会话不存在，请重新开始")
    if temporal_service.enabled():
        run = await _create_and_start_run(
            sid,
            input_kind="opening",
            action=None,
            client_request_id=client_request_id,
            base_turn=base_turn,
            base_version=base_version,
        )
        return StreamingResponse(
            _run_event_stream(run["id"]),
            media_type="text/event-stream",
            headers={"X-Run-ID": run["id"], "Cache-Control": "no-cache"},
        )
    return StreamingResponse(
        _opening_stream(sid),
        media_type="text/event-stream",
    )


async def _opening_stream(sid: str):
    """Create the initial event foundation before streaming opening prose."""
    yield _sse("stage", {"key": "director", "label": "正在建立事件、主角视角与首个钩子；因果模型将在后台生成"})
    try:
        messages = await game.prepare_opening(sid)
    except Exception as e:  # noqa: BLE001
        yield _sse("error", {"message": str(e)})
        return
    yield _sse("stage", {"key": "narrative", "label": "导演骨架已校验，正在生成开场"})
    async for chunk in _narrate(messages, sid, None):
        yield chunk


async def _action_stream(sid: str, text: str):
    """Emit planning stages, then stream prose generated from the validated skeleton."""
    yield _sse("stage", {"key": "constraints", "label": "正在读取世界约束"})
    yield _sse("stage", {"key": "director", "label": "导演正在重新规划事件与爽点"})
    try:
        messages = await game.prepare_action(sid, text)
    except Exception as e:  # noqa: BLE001
        yield _sse("error", {"message": str(e)})
        return
    yield _sse("stage", {"key": "narrative", "label": "导演骨架已校验，正在生成剧情"})
    async for chunk in _narrate(messages, sid, text):
        yield chunk


async def _action_response(
    sid: str,
    text: str,
    user_id: str,
    *,
    client_request_id: str | None = None,
    base_turn: int | None = None,
    base_version: int | None = None,
) -> StreamingResponse:
    if not game.owned_by(sid, user_id):
        raise HTTPException(404, "会话不存在，请重新开始")
    text = (text or "").strip()
    if not text:
        raise HTTPException(400, "行动不能为空")
    if temporal_service.enabled():
        run = await _create_and_start_run(
            sid,
            input_kind="action",
            action=text,
            client_request_id=client_request_id,
            base_turn=base_turn,
            base_version=base_version,
        )
        return StreamingResponse(
            _run_event_stream(run["id"]),
            media_type="text/event-stream",
            headers={"X-Run-ID": run["id"], "Cache-Control": "no-cache"},
        )
    return StreamingResponse(_action_stream(sid, text), media_type="text/event-stream")


@app.post("/api/action")
async def action(
    body: ActionBody,
    user_id: str = Header(alias="X-User-ID"),
):
    return await _action_response(
        body.sid,
        body.text,
        _user_id(user_id),
        client_request_id=body.client_request_id,
        base_turn=body.base_turn,
        base_version=body.base_version,
    )


@app.get("/api/action")
async def action_legacy(
    sid: str,
    text: str,
    client_request_id: str | None = None,
    base_turn: int | None = None,
    base_version: int | None = None,
    user_id: str = Header(alias="X-User-ID"),
):
    return await _action_response(
        sid,
        text,
        _user_id(user_id),
        client_request_id=client_request_id,
        base_turn=base_turn,
        base_version=base_version,
    )


@app.get("/api/character-state")
async def character_state(
    sid: str, user_id: str = Header(alias="X-User-ID")
):
    _require_owned(sid, _user_id(user_id))
    state = game.get_character_state(sid)
    if state is None:
        raise HTTPException(404, "存档不存在")
    return {"character_state": state, "story_card": game.get_story_card_info(sid)}


@app.post("/api/reconcile-character-state")
async def reconcile_character_state(
    body: SidBody,
    user_id: str = Header(alias="X-User-ID"),
):
    if not game.owned_by(body.sid, _user_id(user_id)):
        raise HTTPException(404, "存档不存在")
    transcript = game.get_transcript(body.sid) or []
    latest = next((blk.get("text", "") for blk in reversed(transcript)
                   if blk.get("role") == "narration"), "")
    if not latest.strip():
        raise HTTPException(400, "还没有可校准的剧情正文")
    try:
        return await game.reconcile_character_state_from_text(body.sid, latest)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"状态校准失败：{exc}") from exc


# ---- 世界记忆问询（旁路：不推进剧情，只补全背景）----

class WorldMemoryDeleteBody(BaseModel):
    sid: str
    index: int


def _inquiry_response(sid: str, q: str, user_id: str) -> StreamingResponse:
    if not game.owned_by(sid, user_id):
        raise HTTPException(404, "会话不存在，请重新开始")
    q = (q or "").strip()
    if not q:
        raise HTTPException(400, "问题不能为空")
    return StreamingResponse(
        _answer_inquiry(sid, q),
        media_type="text/event-stream",
    )


@app.post("/api/inquiry")
async def inquiry(
    body: InquiryBody,
    user_id: str = Header(alias="X-User-ID"),
):
    return _inquiry_response(body.sid, body.q, _user_id(user_id))


@app.get("/api/inquiry")
async def inquiry_legacy(
    sid: str,
    q: str,
    user_id: str = Header(alias="X-User-ID"),
):
    return _inquiry_response(sid, q, _user_id(user_id))


@app.get("/api/world-memory")
async def world_memory(
    sid: str, user_id: str = Header(alias="X-User-ID")
):
    _require_owned(sid, _user_id(user_id))
    items = game.get_world_memory(sid)
    if items is None:
        raise HTTPException(404, "存档不存在")
    return {"world_memory": items, "lore": items}


@app.get("/api/director")
async def director(
    sid: str, user_id: str = Header(alias="X-User-ID")
):
    """读取实时事件骨架与异步执行审计状态。"""
    user_id = _user_id(user_id)
    _require_owned(sid, user_id)
    observed_sid = sid
    if temporal_service.enabled():
        active = await run_store.active_run_summary(sid, user_id=user_id)
        shadow_sid = f"shadow:{active['id']}" if active else None
        if shadow_sid and store.exists(shadow_sid):
            observed_sid = shadow_sid
        # The active shadow and the canonical save are both written by the
        # separate Worker process, so the API must not reuse an older copy.
        game.reload_persisted_state(observed_sid)
    state = game.get_director_state(observed_sid)
    if state is None:
        raise HTTPException(404, "存档不存在")
    return {"director_state": state, "turns": game.get_turns(observed_sid) or 0}


@app.get("/api/llm-metrics")
async def llm_metrics(
    sid: str,
    limit: int = 30,
    user_id: str = Header(alias="X-User-ID"),
):
    """Read recent LLM request categories, statuses, and timings."""
    _require_owned(sid, _user_id(user_id))
    items = game.get_llm_request_metrics(sid, limit)
    if items is None:
        raise HTTPException(404, "存档不存在")
    return {"requests": items}


@app.get("/api/agent-token-stats")
async def agent_token_stats(
    sid: str, user_id: str = Header(alias="X-User-ID")
):
    """Return persisted token totals overall and grouped by Agent type."""
    _require_owned(sid, _user_id(user_id))
    stats = game.get_agent_token_stats(sid)
    if stats is None:
        raise HTTPException(404, "存档不存在")
    return stats


@app.get("/api/agent-traces")
async def agent_traces(
    sid: str, turn: int | None = None, limit: int = 100,
    include_content: bool = False, updated_after: float | None = None,
    user_id: str = Header(alias="X-User-ID"),
):
    """Read Agent traces, optionally returning only records changed since a cursor."""
    user_id = _user_id(user_id)
    _require_owned(sid, user_id)
    additional_session_ids: tuple[str, ...] = ()
    if temporal_service.enabled():
        active = await run_store.active_run_summary(sid, user_id=user_id)
        if active:
            additional_session_ids = (f"shadow:{active['id']}",)
    items = game.get_agent_traces(
        sid, turn=turn, limit=limit,
        include_content=include_content or turn is not None,
        updated_after=updated_after,
        additional_session_ids=additional_session_ids,
    )
    if items is None:
        raise HTTPException(404, "存档不存在")
    cursor = max((item["updated_at"] for item in items), default=updated_after or 0)
    return {"traces": items, "cursor": cursor}


@app.get("/api/jobs")
async def jobs(sid: str, user_id: str = Header(alias="X-User-ID")):
    """Observe durable Temporal work, with legacy tasks when disabled."""
    user_id = _user_id(user_id)
    _require_owned(sid, user_id)
    if temporal_service.enabled():
        active = await run_store.active_run_summary(sid, user_id=user_id)
        if not active:
            return {"session_id": sid, "idle": True, "pending": []}
        nodes = await run_store.list_nodes(active["id"])
        return {
            "session_id": sid,
            "idle": active["status"] not in run_store.ACTIVE_STATUSES,
            "pending": [
                {
                    "run_id": active["id"],
                    "kind": node["node_key"],
                    "status": node["status"],
                    "started_at": node.get("started_at"),
                }
                for node in nodes
                if node["status"] in {"pending", "running"}
            ],
        }
    return background.status(sid)


@app.get("/api/world-state")
async def world_state(
    sid: str, user_id: str = Header(alias="X-User-ID")
):
    """读取主角当前位置与知识视野（不展示行动菜单）。"""
    _require_owned(sid, _user_id(user_id))
    state = game.get_world_state(sid)
    if state is None:
        raise HTTPException(404, "存档不存在")
    return {"world_state": state}


@app.post("/api/world-memory/delete")
async def world_memory_delete(
    body: WorldMemoryDeleteBody,
    user_id: str = Header(alias="X-User-ID"),
):
    _require_owned(body.sid, _user_id(user_id))
    items = game.delete_world_memory(body.sid, body.index)
    if items is None:
        raise HTTPException(404, "世界记忆不存在")
    return {"ok": True, "world_memory": items, "lore": items}


@app.get("/api/lore")
async def lore_compat(
    sid: str, user_id: str = Header(alias="X-User-ID")
):
    return await world_memory(sid, user_id)


@app.post("/api/lore/delete")
async def lore_delete_compat(
    body: WorldMemoryDeleteBody,
    user_id: str = Header(alias="X-User-ID"),
):
    return await world_memory_delete(body, user_id)


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
