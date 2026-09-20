"""Trace 抓取与去重。

去重口径（与用户确认的「口径 A」一致）：凡是逐字节相同的消息内容，全局只保留
一份，各回合各 Agent 通过 sha256 引用它；凡是本回合实际变化的内容，全部原样保留，
不做截断。

这样报告里仍能看到每个 Agent 的完整视野，只是不再重复贴 N 遍相同的系统提示词。
实测真实存档（215 回合、1689 条 trace）可省 31.5%，其中 narrative 省 57%、
director_payoff_retry 省 99.8%。

判定必须按内容哈希动态进行，不能写死消息索引：同一个 Agent 在不同回合的消息条数
会浮动（director_pacing 实测有 2 条也有 3 条的情况）。
"""

from __future__ import annotations

import hashlib
import json

# state_reconcile 在真实数据里 202 次调用全部失败（成功率 0%），属于已知问题。
# 按用户决定先跳过修复，但 trace 照常收录——它是真实发生的调用。这里把它单独
# 归组并从健康度统计里排除，避免满屏红色错误掩盖其他 Agent 的真实问题。
KNOWN_FAILING_AGENTS = {"state_reconcile"}

# Agent 的展示名与分组，用于报告的导航结构。
AGENT_LABELS = {
    "director_pacing": ("节奏", "导演层"),
    "director_progression": ("推进", "导演层"),
    "director_payoff": ("爽点", "导演层"),
    "director_payoff_retry": ("爽点重试", "导演层"),
    "director_hook": ("钩子", "导演层"),
    "director_skeleton": ("骨架", "导演层"),
    "director_event": ("事件", "事件层"),
    "director_next_event": ("下一事件", "事件层"),
    "director_causal": ("因果", "事件层"),
    "director_viewpoint": ("视角", "事件层"),
    "director_audit": ("执行审计", "审计与观察"),
    "narrative_observer": ("叙事观察", "审计与观察"),
    "guidance_conflict": ("冲突引导", "审计与观察"),
    "narrative": ("剧情正文", "叙事与记忆"),
    "opening": ("开场", "叙事与记忆"),
    "memory_extract": ("记忆提取", "叙事与记忆"),
    "inquiry": ("世界问询", "叙事与记忆"),
    "character_setting": ("人物设定", "状态"),
    "state_reconcile": ("状态校准（已知失败）", "状态"),
    "location_state": ("位置校准", "状态"),
    "director_state": ("导演状态", "导演层"),
    "legacy_director": ("旧导演（兼容）", "其他"),
}

GROUP_ORDER = ["导演层", "事件层", "叙事与记忆", "审计与观察", "状态", "其他"]


class DedupPool:
    """全局共享的消息内容池：sha256 -> 原文。"""

    def __init__(self) -> None:
        self._pool: dict[str, str] = {}
        self._raw_chars = 0
        self._unique_chars = 0

    def put(self, content: str) -> str:
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        self._raw_chars += len(content)
        if digest not in self._pool:
            self._pool[digest] = content
            self._unique_chars += len(content)
        return digest

    def get(self, digest: str) -> str:
        return self._pool.get(digest, "")

    def blocks(self) -> dict:
        return self._pool

    def stats(self) -> dict:
        saved = self._raw_chars - self._unique_chars
        return {
            "raw_chars": self._raw_chars,
            "unique_chars": self._unique_chars,
            "saved_chars": saved,
            "saved_ratio": (saved / self._raw_chars) if self._raw_chars else 0.0,
            "unique_blocks": len(self._pool),
        }


def label_for(agent_type: str) -> tuple[str, str]:
    return AGENT_LABELS.get(agent_type, (agent_type, "其他"))


def _messages_of(row: dict) -> list[dict]:
    """取出 trace 的输入消息。

    HTTP 接口返回的是已解析的 list，但直接读库或旧数据可能是 JSON 字符串。
    两种都接受，并跳过结构异常的元素，避免一条坏数据毁掉整份报告。
    """
    raw = row.get("input_messages")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if not isinstance(raw, list):
        return []
    result = []
    for item in raw:
        if isinstance(item, dict):
            result.append(item)
        elif isinstance(item, str):
            result.append({"role": "unknown", "content": item})
    return result


def collect_turn_traces(raw_traces: list[dict], pool: DedupPool) -> list[dict]:
    """把一轮的原始 trace 转成报告用的紧凑结构，消息正文进池。"""
    items = []
    for row in raw_traces:
        agent_type = row.get("agent_type") or "unknown"
        name, group = label_for(agent_type)
        messages = []
        for message in _messages_of(row):
            content = message.get("content")
            if not isinstance(content, str):
                content = "" if content is None else str(content)
            messages.append(
                {
                    "role": message.get("role", ""),
                    "ref": pool.put(content),
                    "len": len(content),
                }
            )
        raw_output = row.get("raw_output") or ""
        items.append(
            {
                "id": row.get("id"),
                "agent_type": agent_type,
                "name": name,
                "group": group,
                "model": row.get("model") or "",
                "status": row.get("status") or "unknown",
                "duration_ms": row.get("duration_ms") or 0,
                "error_type": row.get("error_type") or "",
                "error_message": row.get("error_message") or "",
                "input_tokens": row.get("input_tokens"),
                "output_tokens": row.get("output_tokens"),
                "total_tokens": row.get("total_tokens"),
                "cache_hit_tokens": row.get("cache_hit_tokens"),
                "known_failing": agent_type in KNOWN_FAILING_AGENTS,
                "messages": messages,
                "input_chars": sum(m["len"] for m in messages),
                "raw_output": raw_output,
                "output_ref": pool.put(raw_output),
                "output_chars": len(raw_output),
                "created_at": row.get("created_at"),
            }
        )
    return items


def summarize_turn(items: list[dict]) -> dict:
    """一轮的健康度统计，排除已知失败 Agent。"""
    counted = [i for i in items if not i["known_failing"]]
    failed = [i for i in counted if i["status"] != "success"]
    return {
        "agents": len(items),
        "counted": len(counted),
        "failed": len(failed),
        "ok": len(counted) - len(failed),
        "duration_ms": sum(i["duration_ms"] for i in items),
        "tokens": sum(i["total_tokens"] or 0 for i in items),
        "known_failing": len(items) - len(counted),
    }
