"""流式 LLM 客户端，同时支持 OpenAI 与 Anthropic 两种协议。

通过环境变量 LLM_PROTOCOL 选择：
- openai    : OpenAI Chat Completions 协议 /v1/chat/completions
              （DeepSeek / 通义 / Kimi / 本地 vLLM / 兼容网关等）
- anthropic : Anthropic Messages 协议 /v1/messages

两种协议对上层暴露同一个 stream_chat(messages)，逐段 yield 文本增量。
"""

import json
import os

import httpx

PROTOCOL = os.getenv("LLM_PROTOCOL", "openai").strip().lower()
BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
API_KEY = os.getenv("LLM_API_KEY", "")
MODEL = os.getenv("LLM_MODEL", "deepseek-chat")
TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.9"))
MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "2048"))
ANTHROPIC_VERSION = os.getenv("ANTHROPIC_VERSION", "2023-06-01")

_TIMEOUT = httpx.Timeout(120.0, connect=15.0)


async def stream_chat(messages: list[dict]):
    """向 LLM 发起流式对话，逐段 yield 文本增量。

    Args:
        messages: 统一的 OpenAI 风格消息列表，形如
            [{"role": "system"|"user"|"assistant", "content": "..."}]

    Yields:
        str: 模型输出的文本片段。
    """
    if not API_KEY:
        raise RuntimeError("未配置 LLM_API_KEY，请复制 .env.example 为 .env 并填写。")

    if PROTOCOL == "anthropic":
        async for piece in _stream_anthropic(messages):
            yield piece
    elif PROTOCOL == "openai":
        async for piece in _stream_openai(messages):
            yield piece
    else:
        raise RuntimeError(f"未知的 LLM_PROTOCOL={PROTOCOL!r}，应为 openai 或 anthropic。")


async def complete_chat(
    messages: list[dict],
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> str:
    """向 LLM 发起非流式对话，用于后台结构化任务。"""
    if not API_KEY:
        raise RuntimeError("未配置 LLM_API_KEY，请复制 .env.example 为 .env 并填写。")

    if PROTOCOL == "anthropic":
        return await _complete_anthropic(messages, temperature, max_tokens)
    if PROTOCOL == "openai":
        return await _complete_openai(messages, temperature, max_tokens)
    raise RuntimeError(f"未知的 LLM_PROTOCOL={PROTOCOL!r}，应为 openai 或 anthropic。")


def _iter_sse_data(line: str) -> str | None:
    """从一行 SSE 中取出 data: 后的内容，非 data 行返回 None。"""
    if not line or not line.startswith("data:"):
        return None
    return line[len("data:"):].strip()


async def _stream_openai(messages: list[dict]):
    url = f"{BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": MODEL,
        "messages": messages,
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
        "stream": True,
    }

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        async with client.stream("POST", url, headers=headers, json=payload) as resp:
            await _raise_for_status(resp)
            async for line in resp.aiter_lines():
                data = _iter_sse_data(line)
                if data is None:
                    continue
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                piece = (choices[0].get("delta") or {}).get("content")
                if piece:
                    yield piece


async def _complete_openai(
    messages: list[dict],
    temperature: float | None,
    max_tokens: int | None,
) -> str:
    url = f"{BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": MODEL,
        "messages": messages,
        "temperature": TEMPERATURE if temperature is None else temperature,
        "max_tokens": MAX_TOKENS if max_tokens is None else max_tokens,
        "stream": False,
    }

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(url, headers=headers, json=payload)
        await _raise_for_status(resp)
    body = resp.json()
    choices = body.get("choices") or []
    if not choices:
        return ""
    return ((choices[0].get("message") or {}).get("content") or "").strip()


def _split_system(messages: list[dict]) -> tuple[str, list[dict]]:
    """Anthropic 把 system 拆成顶层参数，其余消息保持 user/assistant 交替。"""
    system_parts = []
    convo = []
    for m in messages:
        if m.get("role") == "system":
            system_parts.append(m.get("content", ""))
        else:
            convo.append({"role": m["role"], "content": m["content"]})
    return "\n\n".join(p for p in system_parts if p), convo


async def _stream_anthropic(messages: list[dict]):
    system, convo = _split_system(messages)
    url = f"{BASE_URL}/messages"
    headers = {
        "x-api-key": API_KEY,
        "anthropic-version": ANTHROPIC_VERSION,
        "Content-Type": "application/json",
    }
    payload = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "temperature": TEMPERATURE,
        "messages": convo,
        "stream": True,
    }
    if system:
        payload["system"] = system

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        async with client.stream("POST", url, headers=headers, json=payload) as resp:
            await _raise_for_status(resp)
            async for line in resp.aiter_lines():
                data = _iter_sse_data(line)
                if data is None:
                    continue
                try:
                    evt = json.loads(data)
                except json.JSONDecodeError:
                    continue
                # 文本增量只在 content_block_delta.text_delta 里
                if evt.get("type") == "content_block_delta":
                    delta = evt.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        piece = delta.get("text")
                        if piece:
                            yield piece
                elif evt.get("type") == "message_stop":
                    break


async def _complete_anthropic(
    messages: list[dict],
    temperature: float | None,
    max_tokens: int | None,
) -> str:
    system, convo = _split_system(messages)
    url = f"{BASE_URL}/messages"
    headers = {
        "x-api-key": API_KEY,
        "anthropic-version": ANTHROPIC_VERSION,
        "Content-Type": "application/json",
    }
    payload = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS if max_tokens is None else max_tokens,
        "temperature": TEMPERATURE if temperature is None else temperature,
        "messages": convo,
    }
    if system:
        payload["system"] = system

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(url, headers=headers, json=payload)
        await _raise_for_status(resp)
    body = resp.json()
    parts = []
    for block in body.get("content") or []:
        if block.get("type") == "text" and block.get("text"):
            parts.append(block["text"])
    return "".join(parts).strip()


async def _raise_for_status(resp: httpx.Response) -> None:
    if resp.status_code != 200:
        body = (await resp.aread()).decode("utf-8", "replace")
        raise RuntimeError(f"LLM 请求失败 {resp.status_code}: {body[:500]}")
