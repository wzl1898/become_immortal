"""玩家 Agent：读最新剧情，生成下一步行动文本。

它是自动化游玩框架自带的旁路 Agent，独立于叙事引擎的 Agent 拓扑：
- 不属于引导层/叙事引擎/观察层，不参与它们的提示词体系
- 调用不进项目 trace（trace 记录的是叙事引擎的 Agent）
- 通过环境变量 PLAYER_LLM_* 单独配置，未设置时继承主模型 LLM_*

生成结果会被当作玩家输入提交给 /api/action，因此必须是一行可执行的具体行动，
而不是解释、分析或选项列表。
"""

from __future__ import annotations

import json
import os
import re

import httpx

PROMPT = """你是一个文字冒险游戏的玩家。根据最新一段剧情，决定主角接下来做什么。

要求：
- 只输出主角要执行的具体行动，一到两句话，直接可执行。
- 不要解释理由，不要分析局势，不要列出选项，不要输出任何前缀、标题或引号。
- 行动要具体到人物、地点、物件或动作，避免"继续观察""静观其变"这类空泛表述。
- 结合主角当前处境和已知信息推进剧情，可以主动试探、交涉、调查或行动。
- 如果剧情停在悬念处，选择能推动局面明朗的动作。

【当前状态】
{state}

【上一轮主角行动】
{previous_action}

【最新剧情】
{narrative}

直接输出行动："""


class PlayerAgentError(Exception):
    """玩家 Agent 调用失败。"""


def _env(prefix: str, name: str, fallback: str = "") -> str:
    return os.getenv(f"{prefix}_{name}", fallback).strip()


def _sanitize_no_proxy() -> None:
    """去掉 no_proxy 里带方括号的 IPv6 条目，否则 httpx 会把它当成端口解析。"""
    for name in ("no_proxy", "NO_PROXY"):
        value = os.getenv(name)
        if not value or "[" not in value:
            continue
        kept = [p for p in value.split(",") if "[" not in p and "]" not in p]
        os.environ[name] = ",".join(kept)


def _config() -> dict:
    """玩家 Agent 的模型配置；PLAYER_LLM_* 优先，否则继承主 LLM_*。"""
    protocol = _env("PLAYER_LLM", "PROTOCOL", os.getenv("LLM_PROTOCOL", "openai")).lower()
    return {
        "protocol": protocol,
        "base_url": _env("PLAYER_LLM", "BASE_URL", os.getenv("LLM_BASE_URL", "")).rstrip("/"),
        "api_key": _env("PLAYER_LLM", "API_KEY", os.getenv("LLM_API_KEY", "")),
        "model": _env("PLAYER_LLM", "MODEL", os.getenv("LLM_MODEL", "deepseek-v4-flash")),
    }


def _clean(text: str) -> str:
    """把模型输出收敛成一行行动文本。"""
    text = (text or "").strip()
    # 去掉常见的包装：标题行、引号、列表符号、前缀标签。
    text = re.sub(r"^(行动|玩家行动|输出|答案)[:：]\s*", "", text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    text = lines[0]
    text = re.sub(r"^[-*•\d.、)\s]+", "", text).strip()
    text = text.strip("\"'“”‘’《》 ")
    return text.strip()


def _payload(messages: list[dict], cfg: dict, temperature: float, max_tokens: int) -> dict:
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    # 与后端一致：DeepSeek V4 的推理输出会占用 max_tokens 但不出现在正文里。
    if cfg["model"].lower().startswith("deepseek-v4"):
        payload["thinking"] = {"type": "disabled"}
    return payload


def _request(messages: list[dict], cfg: dict, temperature: float, max_tokens: int,
             timeout: float) -> str:
    headers = {"content-type": "application/json"}
    if cfg["protocol"] == "anthropic":
        url = f"{cfg['base_url']}/messages"
        headers["x-api-key"] = cfg["api_key"]
        headers["anthropic-version"] = os.getenv("ANTHROPIC_VERSION", "2023-06-01")
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        body = {
            "model": cfg["model"],
            "system": system,
            "messages": [m for m in messages if m["role"] != "system"],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
    else:
        url = f"{cfg['base_url']}/chat/completions"
        headers["authorization"] = f"Bearer {cfg['api_key']}"
        body = _payload(messages, cfg, temperature, max_tokens)

    try:
        # trust_env 保持开启：访问外部网关通常需要机器代理（本机走 127.0.0.1:7897）。
        # no_proxy 中带方括号的 IPv6 条目会破坏 httpx 的代理解析，auto_play.py 已在
        # 启动时清理过；这里再清理一次，保证单独调用本模块时同样安全。
        _sanitize_no_proxy()
        with httpx.Client(timeout=timeout, trust_env=True) as client:
            response = client.post(url, headers=headers, json=body)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as exc:
        raise PlayerAgentError(
            f"玩家 Agent 请求失败 HTTP {exc.response.status_code}："
            f"{exc.response.text[:200]}"
        ) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise PlayerAgentError(f"玩家 Agent 请求失败：{exc}") from exc

    try:
        if cfg["protocol"] == "anthropic":
            blocks = data.get("content") or []
            return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise PlayerAgentError(f"玩家 Agent 响应结构异常：{json.dumps(data)[:200]}") from exc


def generate_action(
    narrative: str,
    *,
    state: dict | None = None,
    previous_action: str = "",
    temperature: float = 0.9,
    max_tokens: int = 200,
    timeout: float = 60,
    model: str | None = None,
) -> str:
    """根据最新剧情生成一条玩家行动。失败抛 PlayerAgentError。"""
    cfg = _config()
    if model:
        cfg["model"] = model
    if not cfg["api_key"]:
        raise PlayerAgentError(
            "未配置 LLM_API_KEY（或 PLAYER_LLM_API_KEY），无法生成玩家输入"
        )
    if not cfg["base_url"]:
        raise PlayerAgentError("未配置 LLM_BASE_URL（或 PLAYER_LLM_BASE_URL）")

    prompt = PROMPT.format(
        state=json.dumps(state or {}, ensure_ascii=False, indent=None)[:1200],
        previous_action=previous_action or "（暂无，本局刚开始）",
        narrative=narrative or "（暂无正文）",
    )
    raw = _request(
        [{"role": "user", "content": prompt}],
        cfg,
        temperature,
        max_tokens,
        timeout,
    )
    action = _clean(raw)
    if not action:
        raise PlayerAgentError("玩家 Agent 没有产出可用行动")
    return action
