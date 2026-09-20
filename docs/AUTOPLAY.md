# 自动化游玩

`auto_play.py` 驱动服务自动玩 N 个回合，并为每回合每个 Agent 产出 trace 报告。

它是**外部脚手架**：只通过 HTTP/SSE 与服务交互，不 import `game`/`store`/`llm`，
因此不会进入叙事引擎的 Agent 拓扑，也不会在被 `--reload` 监视的后端文件里触发重启。

## 快速开始

```bash
# 1. 先起服务（见 README）
cd backend && ../.venv/bin/python -m uvicorn main:app --reload --port 8888 --timeout-graceful-shutdown 3

# 2. 跑 3 回合冒烟
python auto_play.py --config examples/autoplay/smoke.json

# 只看配置和成本，不调用模型
python auto_play.py --config examples/autoplay/smoke.json --dry-run

# 命令行覆盖
python auto_play.py --card orbital --turns 5 --user autoplay -y
```

报告默认写到 `reports/autoplay-<时间戳>.html`，用浏览器直接打开即可，不需要服务。

## 流程

```
新建存档 -> 生成开场 -> 循环 N 回合：
    读最新剧情 -> 玩家 Agent 生成行动 -> POST /api/action
    -> 等后台任务结束 -> GET /api/agent-traces 抓全量 trace
-> 产出静态自包含 HTML 报告
```

每回合**严格串行**：同一存档不要并发提交新回合（与 `docs/CLI.md` 的约定一致）。

**必须等后台任务结束再抓 trace**：`store.reap_stale_agent_traces` 会把超过 180 秒
仍为 `running` 的 trace 标成 `timeout`，不等就会把正常调用误读成失败。

## 配置

JSON 文件，全部字段可选：

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `base_url` | `http://127.0.0.1:8888` | 服务地址 |
| `user_id` | `autoplay` | 受服务端 `_USER_ID_RE` 约束 |
| `card` | `xiuxian` | 故事卡 id |
| `save_name` | `自动游玩` | 存档名 |
| `turns` | `5` | 游玩回合数 |
| `on_turn_failure` | `stop` | `stop` 停止并保留存档；`continue` 记录后继续 |
| `wait_timeout` | `180` | 等待后台任务的秒数 |
| `output` | `reports/autoplay-{ts}.html` | 报告路径，`{ts}` 为时间戳 |
| `player_agent.model` | `null` | 玩家 Agent 模型，`null` 继承 `LLM_MODEL` |
| `player_agent.temperature` | `0.9` | 采样温度 |
| `player_agent.max_tokens` | `200` | 输出上限 |
| `player_agent.timeout` | `60` | 请求超时（秒） |

命令行参数 `--url --user --card --name --turns --output --on-turn-failure` 覆盖同名配置。
`--resume SID` 在已有存档上继续，`-y/--yes` 跳过成本确认，`--dry-run` 只校验配置。

### 玩家 Agent 的模型配置

玩家 Agent 默认继承主模型的 `LLM_*` 环境变量（`auto_play.py` 会读项目根目录的
`.env`）。要用独立模型，设置 `PLAYER_LLM_*`：

```bash
export PLAYER_LLM_BASE_URL=https://api.deepseek.com/v1
export PLAYER_LLM_API_KEY=sk-...
export PLAYER_LLM_MODEL=deepseek-v4-flash
```

它的调用**不进项目 trace**——trace 记录的是叙事引擎的 Agent，玩家 Agent 属于框架外部。

## 失败语义

| 情况 | 行为 |
| --- | --- |
| 单个 Agent 失败 | 记录并继续。叙事引擎本身有 fallback，报告按状态着色 |
| 整回合生成失败（SSE error / 无 done / 正文为空） | 按 `on_turn_failure`：默认停止并保留存档 |
| 玩家 Agent 失败 | 同上，算整回合失败 |
| 等待后台任务超时 | 记录后仍继续抓取已有 trace |

`state_reconcile` 在真实存档里 0% 成功，属于已知问题，**先跳过修复**。报告把它单独
归组、标为灰色、不计入健康度统计，避免满屏红色掩盖其他 Agent 的真实问题。

## 报告

单个自包含 HTML，全量数据内联，可直接归档分享。

- 左侧按回合分组，每回合列出该回合所有 Agent，带状态色点与耗时
- 右侧显示选中 Agent 的完整输入消息与原始输出，长文本可折叠
- 数据放在 `<script type="application/json">` 里**懒渲染**：切到某个 Agent 才把正文
  注入 DOM，避免几十条 trace 一次性铺开导致浏览器卡顿。这不削减任何数据。

### 消息去重

逐字节相同的消息内容全局只存一份，各位置按 sha256 引用；**凡是本回合实际变化的
内容全部原样保留，不做截断**。报告里仍能看到每个 Agent 的完整视野，只是不再重复
贴 N 遍相同的系统提示词。

实测真实存档（215 回合、1689 条 trace）：10,985,946 → 7,530,412 字符，省 31.5%；
其中 `narrative` 省 57.2%，`director_payoff_retry` 省 99.8%。冒烟运行（3 回合）省 22.2%。

判定按内容哈希动态进行，不写死消息索引——同一个 Agent 在不同回合的消息条数会浮动。

## 成本

每回合会调用 8~14 个 Agent。`turns: 5` 约 50 次叙事引擎请求 + 5 次玩家 Agent 请求。
启动时会打印预估并要求确认；非交互环境自动继续，`-y` 显式跳过。

## 隔离验证

改后端代码时不要碰用户正在跑的 8888。另起一个不带 `--reload` 的实例：

```bash
./story serve --port 8901 --data-dir /tmp/story-agent-test --no-embed
python auto_play.py --url http://127.0.0.1:8901 --user autoplay-smoke --turns 3
```

## 已知环境问题

`no_proxy` 里的方括号 IPv6 条目（如 `[::1]`）会让 httpx 把它当成端口解析，抛
`InvalidURL: Invalid port ':1]'`，导致所有 LLM 请求在建 client 时就失败。
`backend/llm.py` 和本框架都会在启动时清掉这类条目，同时保留代理可用。
