# 问道 · 修仙 AI 文字奇缘

一个 AI 驱动的修仙文字冒险游戏：AI 生成开场，你输入想做的事，AI 续写剧情，如此循环，走一条逆天成仙的路。

## 玩法

- AI 生成一段开场，把你放进修仙世界的一角。
- 你在输入框自由输入任何行动（不是选项菜单）。
- AI 根据世界逻辑推进剧情，选择有真实后果。
- 网页端流式打字机效果，边生成边显示。「重开一世」可开新局。

## 技术栈

- 后端：FastAPI + httpx，SSE 流式转发
- 前端：原生 HTML/CSS/JS，`fetch` 读 SSE 流
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
../.venv/bin/python -m uvicorn main:app --reload --port 8000
```

打开浏览器访问 http://127.0.0.1:8000 即可开玩。

## 配置项（.env）

| 变量 | 说明 | 示例 |
| --- | --- | --- |
| `LLM_PROTOCOL` | 接口协议：`openai` 或 `anthropic` | `openai` |
| `LLM_BASE_URL` | 服务地址（不含 `/chat/completions` 或 `/messages`） | `https://api.deepseek.com/v1` |
| `LLM_API_KEY` | API Key | `sk-...` |
| `LLM_MODEL` | 模型名 | `deepseek-chat` / `claude-sonnet-5` |
| `LLM_TEMPERATURE` | 采样温度，叙事建议 0.8~1.0 | `0.9` |
| `LLM_MAX_TOKENS` | 单次回复最大 token | `2048` |

两种协议示例：

```bash
# OpenAI 协议（DeepSeek）
LLM_PROTOCOL=openai
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat

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
  prompts.py   # 修仙 Game Master 系统提示词
frontend/
  index.html   # 页面
  style.css    # 古风暗色主题
  app.js       # SSE 流式渲染 + 打字机效果
```

## 说明与后续

- 会话存在内存里，重启进程即清空；要持久化可把 `game.py` 换成 Redis/DB。
- 上下文默认保留最近 40 轮（见 `game.py` 的 `MAX_TURNS`），防止过长。
- 提示词在 `backend/prompts.py`，想改题材/风格直接改这里。
