# 问道 · 修仙 AI 文字奇缘

一个 AI 驱动的修仙文字冒险游戏：AI 生成开场，你输入想做的事，AI 续写剧情，如此循环，走一条逆天成仙的路。

## 玩法

- AI 生成一段开场，把你放进修仙世界的一角。
- 你在输入框自由输入任何行动（不是选项菜单）。
- AI 根据世界逻辑推进剧情，选择有真实后果。
- 网页端流式打字机效果，边生成边显示。「新的一世」可开新局。
- **自动存档**：每一手都写入 SQLite，重启不丢。点「存档」可查看所有历世、读档重放、改名或删除。启动时自动续上最近一局。
- **主角状态与物品记忆**：AI 每回合输出身体状态与关键物件，后端保存最新版主角状态并维护物品影子库；状态和近期相关物品会注入后续生成，冷物品收进折叠区，玩家提及时再召回。
- **世界记忆**：点「记忆」可查看长期剧情事实，也可打听主角此刻理应知道的背景；问答与每轮提取出的关键情节会按需召回，约束后续剧情。
- **长期爽点**：爽点 Agent 在多轮之间维护一个尚未触发的高价值机缘或突破，并按剧情为现有世界机缘动态关联现有功法；只有玩家满足明确条件且正文实际兑现后，异步审计才将其标记为已触发。

## 技术栈

- 后端：FastAPI + httpx，SSE 流式转发
- 前端：原生 HTML/CSS/JS，`fetch` 读 SSE 流
- 本地召回：fastembed + BGE-small-zh，用于冷物品语义召回；不可用时自动降级为只保留热物品
- 模型：同时支持 **OpenAI 协议**（DeepSeek / 通义 / Kimi / 本地 vLLM / 兼容网关等）和 **Anthropic 协议**（Anthropic 官方或兼容网关），用 `LLM_PROTOCOL` 切换

## 快速开始

```bash
# 1. 装依赖（需要 Python 3.10+，本机用 3.11）
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. 配置模型
cp .env.example .env
# 编辑 .env 填入 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL

# 3. 启动
cd backend
../.venv/bin/python -m uvicorn main:app --reload --port 8888 --timeout-graceful-shutdown 3
```

打开浏览器访问 http://127.0.0.1:8888 即可开玩。

> `--timeout-graceful-shutdown 3`：`--reload` 检测到改动重启时，最多等 3 秒就断开旧连接。
> 否则正在进行的 SSE 流式请求（开场/行动生成）会让重启卡在 "Waiting for connections to close"。
> 想连前端热更新一起开：在命令前加 `LIVE_RELOAD=1`（改前端文件浏览器自动刷新）。

## 配置项（.env）

| 变量 | 说明 | 示例 |
| --- | --- | --- |
| `LLM_PROTOCOL` | 接口协议：`openai` 或 `anthropic` | `openai` |
| `LLM_BASE_URL` | 服务地址（不含 `/chat/completions` 或 `/messages`） | `https://api.deepseek.com/v1` |
| `LLM_API_KEY` | API Key | `sk-...` |
| `LLM_MODEL` | 模型名 | `deepseek-v4-flash` / `claude-sonnet-5` |
| `LLM_TEMPERATURE` | 采样温度，叙事建议 0.8~1.0 | `0.9` |
| `LLM_MAX_TOKENS` | 单次回复最大 token | `2048` |
| `DIRECTOR_LLM_*` | 可选的独立导演模型配置；协议、地址、Key、模型默认继承主模型 | 未设置 |
| `DIRECTOR_EVENT_MAX_TOKENS` | 事件 Agent 结构化输出上限 | `350` |
| `DIRECTOR_PAYOFF_MAX_TOKENS` | 爽点 Agent 两字段输出上限 | `250` |
| `DIRECTOR_PACING_MAX_TOKENS` | 节奏 Agent 结构化输出上限 | `450` |
| `DIRECTOR_LLM_TIMEOUT` | 每个导演 Agent 的硬超时（秒） | `35` |
| `EMBED_ENABLED` | 是否启用冷物品语义召回；设 `0` 可关闭 | `1` |
| `EMBED_MODEL` | fastembed 模型名 | `BAAI/bge-small-zh-v1.5` |

旧配置 `DIRECTOR_LLM_MAX_TOKENS` 仍兼容：未设置三个专职 Agent 上限时，它会作为共同上限。

LLM 请求会写入 SQLite 的 `llm_request_metrics` 表，包含请求类型、模型、耗时、状态、字符量及供应商返回的缓存命中/未命中 token。剧情历史每 10 轮更新一次阶段摘要，并保留最近 16 轮原文。
| `EMBED_CACHE_DIR` | fastembed 模型缓存目录 | `backend/data/fastembed_cache` |

两种协议示例：

```bash
# OpenAI 协议（DeepSeek）
LLM_PROTOCOL=openai
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-v4-flash

# Anthropic 协议
LLM_PROTOCOL=anthropic
LLM_BASE_URL=https://api.anthropic.com/v1
LLM_MODEL=claude-sonnet-5
```

## 目录结构

```
backend/
  main.py      # FastAPI 入口，SSE 接口 + 静态前端挂载
  game.py      # 会话状态管理（内存态）
  llm.py       # OpenAI 兼容的流式客户端
  embed.py     # 本地 embedding 召回（可降级）
  prompts.py   # 修仙 Game Master 系统提示词
frontend/
  index.html   # 页面
  style.css    # 古风暗色主题
  app.js       # SSE 流式渲染 + 打字机效果
```

## 存档与持久化

- 存档落在 `backend/data/saves.db`（SQLite，已在 `.gitignore` 中忽略）。
- 每完成一手（行动 + 续写）自动写盘，进程重启不丢。
- 每个存档包含：`messages`（喂给 LLM 的上下文，按 40 轮截断省 token）、`transcript`（展示用完整剧情，只增不删）、`character_state`（主角当前状态快照）、`world_memory`（长期世界记忆）与 `inventory`（物品影子库）。旧库中的 `lore` 会自动迁移为 `qa` 类型世界记忆。
- 相关接口：`GET /api/saves` 列表、`GET /api/load` 读档、`GET /api/character-state` 主角状态、`POST /api/action` 行动续写、`POST /api/inquiry` 世界记忆问答、`GET /api/world-memory` 世界记忆列表、`POST /api/world-memory/delete` 删除世界记忆、`POST /api/rename` 改名、`POST /api/delete` 删除。旧 `/api/lore` 接口保留兼容。

## 物品召回

- 近期在正文中出现过的关键物品会保持为热物品，自动注入后续生成。
- 超过热窗口未被正文提及的物品会变冷，只在前端「其他随身物」中折叠展示。
- 玩家输入与冷物品语义相近时，后端用 fastembed 召回它，并重新注入本轮生成。
- `backend/data/fastembed_cache` 被 `.gitignore` 忽略；如果模型缓存不存在或 fastembed 加载失败，服务不会崩，只会跳过语义召回。可设置 `EMBED_ENABLED=0` 显式关闭。

## 说明与后续

- 上下文默认保留最近 40 轮（见 `game.py` 的 `MAX_TURNS`），防止过长。
- 提示词在 `backend/prompts.py`，想改题材/风格直接改这里。
- 多人 / 上线需另加鉴权与会话隔离，当前定位为本地单人游玩。
