"""流式 LLM 客户端，同时支持 OpenAI 与 Anthropic 两种协议。

通过环境变量 LLM_PROTOCOL 选择：
- openai    : OpenAI Chat Completions 协议 /v1/chat/completions
              （DeepSeek / 通义 / Kimi / 本地 vLLM / 兼容网关等）
- anthropic : Anthropic Messages 协议 /v1/messages

两种协议对上层暴露同一个 stream_chat(messages)，逐段 yield 文本增量。
"""

import json
import os
import asyncio
import time
from dataclasses import dataclass

import httpx

PROTOCOL = os.getenv("LLM_PROTOCOL", "openai").strip().lower()
BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
API_KEY = os.getenv("LLM_API_KEY", "")
MODEL = os.getenv("LLM_MODEL", "deepseek-v4-flash")
TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.9"))
MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "2048"))
ANTHROPIC_VERSION = os.getenv("ANTHROPIC_VERSION", "2023-06-01")
CONNECT_RETRY_DELAYS = (0.5, 1.5)

_TIMEOUT = httpx.Timeout(120.0, connect=15.0)


@dataclass(frozen=True)
class LLMConfig:
    protocol: str
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float | None = 120.0


DEFAULT_CONFIG = LLMConfig(PROTOCOL, BASE_URL, API_KEY, MODEL)


def config_from_env(prefix: str) -> LLMConfig:
    """Build an agent-specific config, falling back to the main LLM."""
    return LLMConfig(
        protocol=os.getenv(f"{prefix}_PROTOCOL", PROTOCOL).strip().lower(),
        base_url=os.getenv(f"{prefix}_BASE_URL", BASE_URL).rstrip("/"),
        api_key=os.getenv(f"{prefix}_API_KEY", API_KEY),
        model=os.getenv(f"{prefix}_MODEL", MODEL),
        timeout_seconds=float(os.getenv(f"{prefix}_TIMEOUT", "120")),
    )


async def stream_chat(
    messages: list[dict],
    request_type: str = "narrative",
    session_id: str | None = None,
):
    """向 LLM 发起流式对话，逐段 yield 文本增量。

    Args:
        messages: 统一的 OpenAI 风格消息列表，形如
            [{"role": "system"|"user"|"assistant", "content": "..."}]

    Yields:
        str: 模型输出的文本片段。
    """
    started = time.monotonic()
    output_chars = 0
    status = "success"
    error_type = ""
    try:
        if not API_KEY:
            raise RuntimeError("未配置 LLM_API_KEY，请复制 .env.example 为 .env 并填写。")
        if PROTOCOL == "anthropic":
            iterator = _stream_anthropic(messages)
        elif PROTOCOL == "openai":
            iterator = _stream_openai(messages)
        else:
            raise RuntimeError(f"未知的 LLM_PROTOCOL={PROTOCOL!r}，应为 openai 或 anthropic。")
        async for piece in iterator:
            output_chars += len(piece)
            yield piece
    except asyncio.CancelledError:
        status = "timeout"
        error_type = "CancelledError"
        raise
    except Exception as exc:
        status = "api_error"
        error_type = type(exc).__name__
        raise
    finally:
        _record_metric({
            "save_id": session_id,
            "request_type": request_type,
            "protocol": PROTOCOL,
            "model": MODEL,
            "status": status,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "input_chars": sum(len(str(message.get("content") or "")) for message in messages),
            "output_chars": output_chars,
            "error_type": error_type,
        })


async def complete_chat(
    messages: list[dict],
    temperature: float | None = None,
    max_tokens: int | None = None,
    config: LLMConfig | None = None,
    request_type: str = "background",
    session_id: str | None = None,
) -> str:
    """向 LLM 发起非流式对话，用于后台结构化任务。"""
    config = config or DEFAULT_CONFIG
    if not config.api_key:
        raise RuntimeError("未配置 LLM_API_KEY，请复制 .env.example 为 .env 并填写。")

    started = time.monotonic()
    text = ""
    usage = {}
    status = "success"
    error_type = ""
    try:
        if config.protocol == "anthropic":
            text, usage = await _complete_anthropic(messages, temperature, max_tokens, config)
        elif config.protocol == "openai":
            text, usage = await _complete_openai(messages, temperature, max_tokens, config)
        else:
            raise RuntimeError(f"未知的 LLM_PROTOCOL={config.protocol!r}，应为 openai 或 anthropic。")
        return text
    except asyncio.CancelledError:
        status = "timeout"
        error_type = "CancelledError"
        raise
    except Exception as exc:
        status = "api_error"
        error_type = type(exc).__name__
        raise
    finally:
        _record_metric({
            "save_id": session_id,
            "request_type": request_type,
            "protocol": config.protocol,
            "model": config.model,
            "status": status,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "input_chars": sum(len(str(message.get("content") or "")) for message in messages),
            "output_chars": len(text),
            "error_type": error_type,
            **_usage_metrics(usage),
        })


def _usage_metrics(usage: dict | None) -> dict:
    usage = usage if isinstance(usage, dict) else {}
    details = usage.get("prompt_tokens_details") or {}
    hit = usage.get("prompt_cache_hit_tokens")
    if hit is None:
        hit = details.get("cached_tokens")
    miss = usage.get("prompt_cache_miss_tokens")
    if miss is None and hit is not None and usage.get("prompt_tokens") is not None:
        miss = max(0, usage["prompt_tokens"] - hit)
    return {
        "prompt_tokens": usage.get("prompt_tokens") or usage.get("input_tokens"),
        "completion_tokens": usage.get("completion_tokens") or usage.get("output_tokens"),
        "cache_hit_tokens": hit,
        "cache_miss_tokens": miss,
    }


def _record_metric(metric: dict) -> None:
    try:
        import store
        store.record_llm_request_metric(metric)
    except Exception:
        pass


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
        for attempt in range(len(CONNECT_RETRY_DELAYS) + 1):
            yielded = False
            try:
                async with client.stream("POST", url, headers=headers, json=payload) as resp:
                    await _raise_for_status(resp)
                    async for line in resp.aiter_lines():
                        data = _iter_sse_data(line)
                        if data is None:
                            continue
                        if data == "[DONE]":
                            return
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        piece = (choices[0].get("delta") or {}).get("content")
                        if piece:
                            yielded = True
                            yield piece
                return
            except httpx.ConnectError:
                if yielded or attempt >= len(CONNECT_RETRY_DELAYS):
                    raise
                await asyncio.sleep(CONNECT_RETRY_DELAYS[attempt])


async def _complete_openai(
    messages: list[dict],
    temperature: float | None,
    max_tokens: int | None,
    config: LLMConfig,
) -> tuple[str, dict]:
    url = f"{config.base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": config.model,
        "messages": messages,
        "temperature": TEMPERATURE if temperature is None else temperature,
        "max_tokens": MAX_TOKENS if max_tokens is None else max_tokens,
        "stream": False,
    }

    timeout = _completion_timeout(config.timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(len(CONNECT_RETRY_DELAYS) + 1):
            try:
                resp = await client.post(url, headers=headers, json=payload)
                await _raise_for_status(resp)
                break
            except httpx.ConnectError:
                if attempt >= len(CONNECT_RETRY_DELAYS):
                    raise
                await asyncio.sleep(CONNECT_RETRY_DELAYS[attempt])
    body = resp.json()
    choices = body.get("choices") or []
    if not choices:
        return "", body.get("usage") or {}
    return ((choices[0].get("message") or {}).get("content") or "").strip(), body.get("usage") or {}


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
        for attempt in range(len(CONNECT_RETRY_DELAYS) + 1):
            yielded = False
            try:
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
                        if evt.get("type") == "content_block_delta":
                            delta = evt.get("delta") or {}
                            if delta.get("type") == "text_delta":
                                piece = delta.get("text")
                                if piece:
                                    yielded = True
                                    yield piece
                        elif evt.get("type") == "message_stop":
                            return
                return
            except httpx.ConnectError:
                if yielded or attempt >= len(CONNECT_RETRY_DELAYS):
                    raise
                await asyncio.sleep(CONNECT_RETRY_DELAYS[attempt])


async def _complete_anthropic(
    messages: list[dict],
    temperature: float | None,
    max_tokens: int | None,
    config: LLMConfig,
) -> tuple[str, dict]:
    system, convo = _split_system(messages)
    url = f"{config.base_url}/messages"
    headers = {
        "x-api-key": config.api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "Content-Type": "application/json",
    }
    payload = {
        "model": config.model,
        "max_tokens": MAX_TOKENS if max_tokens is None else max_tokens,
        "temperature": TEMPERATURE if temperature is None else temperature,
        "messages": convo,
    }
    if system:
        payload["system"] = system

    timeout = _completion_timeout(config.timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(len(CONNECT_RETRY_DELAYS) + 1):
            try:
                resp = await client.post(url, headers=headers, json=payload)
                await _raise_for_status(resp)
                break
            except httpx.ConnectError:
                if attempt >= len(CONNECT_RETRY_DELAYS):
                    raise
                await asyncio.sleep(CONNECT_RETRY_DELAYS[attempt])
    body = resp.json()
    parts = []
    for block in body.get("content") or []:
        if block.get("type") == "text" and block.get("text"):
            parts.append(block["text"])
    return "".join(parts).strip(), body.get("usage") or {}


def _completion_timeout(timeout_seconds: float | None) -> httpx.Timeout:
    """Allow background agents to wait indefinitely for response bytes."""
    if timeout_seconds is None:
        return httpx.Timeout(None, connect=15.0)
    return httpx.Timeout(timeout_seconds, connect=min(15.0, timeout_seconds))


async def _raise_for_status(resp: httpx.Response) -> None:
    if resp.status_code != 200:
        body = (await resp.aread()).decode("utf-8", "replace")
        raise RuntimeError(f"LLM 请求失败 {resp.status_code}: {body[:500]}")
