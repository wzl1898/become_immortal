"""Isolated integration-test server. Model replies are deterministic; never use real APIs."""

import argparse
import asyncio
import json
import socket

import game
import main
import uvicorn


async def complete(messages, **kwargs):
    kind = kwargs["request_type"]
    if kind in {
        "memory_extract",
        "director_causal",
        "director_audit",
        "narrative_observer",
    }:
        await asyncio.sleep(0.08)
    results = {
        "character_setting": {
            "reason": "记录有差异",
            "attitude": "谨慎",
            "goal": "查明差异",
        },
        "guidance_conflict": {"conflict_seed": "负责人要求核对记录"},
        "director_event": {
            "title": "记录差异",
            "core": "负责人要求核对记录",
            "benefit": "确认问题来源",
            "end_condition": "差异已查明",
        },
        "director_payoff": {"desc": "", "trigger": ""},
        "director_pacing": {
            "intent": {"key": "观察", "same_as_previous": False},
            "resolved": True,
        },
        "director_progression": {
            "reason": "检查记录",
            "direction": "展示差异",
            "ended": False,
        },
        "director_hook": {"goal": "询问记录来源"},
        "director_skeleton": {
            "turn_objective": "确认记录差异",
            "beats": ["负责人展示记录"],
            "scene": "当前地点",
            "scene_change": False,
        },
        "director_audit": {
            "fulfilled": True,
            "payoff_triggered": False,
            "event_end_reached": False,
        },
        "narrative_observer": {"conflict_present": True, "evidence": "负责人提出要求"},
        "memory_extract": [
            {
                "type": "plot",
                "text": "负责人要求核对记录。",
                "scope": "event",
                "entities": ["负责人"],
            }
        ],
        "state_reconcile": {"updates": {}, "evidence": {}},
        "inquiry": {"answer": "你可以向当班负责人核对记录。"},
    }
    if kind in {"director_viewpoint", "director_causal"}:
        return "# 当前事实\n负责人正在核对记录，主角尚未接受任务。"
    return json.dumps(results.get(kind, {}), ensure_ascii=False)


async def stream(messages, **kwargs):
    if "FAIL_GENERATION" in messages[-1]["content"]:
        yield "尚未完成的正文"
        raise RuntimeError("fixture generation failed")
    card = game._get(kwargs["session_id"])["story_card"]
    panel = "\n".join(
        f"{field['label']}：{field['initial']}" for field in card["status_fields"]
    )
    text = (
        "负责人请你核对记录。\n\n《状态》\n"
        + panel
        + "\n《/状态》\n〔行动提示：核对记录 · 询问来由——你也可自行选择〕"
    )
    for offset in range(0, len(text), 7):
        yield text[offset : offset + 7]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fd", type=int, required=True)
    args = parser.parse_args()
    game.complete_chat = complete
    main.stream_chat = stream
    server = uvicorn.Server(
        uvicorn.Config(main.app, log_level="critical", timeout_graceful_shutdown=1)
    )
    server.run(sockets=[socket.socket(fileno=args.fd)])
