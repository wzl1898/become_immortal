# 项目 CLI 与 Agent 测试

项目根目录提供 `./story`，使用项目 `.venv` 中的 Python。也可以执行 `.venv/bin/python backend/cli.py`。CLI 从任何工作目录都可调用；远程操作复用网页使用的 HTTP/SSE 接口，不直接修改 SQLite，也不在 CLI 中复制游戏逻辑。

## 快速开始

```bash
# 默认服务为 127.0.0.1:8888
./story health
./story cards list --pretty

# 为测试使用单独的 userid
export STORY_USER_ID=agent-cli-test
./story saves create --card orbital --name "CLI 测试" > /tmp/new-story.json
sid=$(python3 -c 'import json; print(json.load(open("/tmp/new-story.json"))["session_id"])')

./story play opening "$sid" --wait
./story play action "$sid" "核对记录，询问异常出现的时间" --wait
./story play inquiry "$sid" "我认识哪些人？"
./story saves export "$sid" --pretty --output /tmp/story-report.json
```

`saves create` 只创建存档。`play opening/action/inquiry`、`state reconcile` 和场景中的相应步骤会调用服务配置的真实模型。CLI 不会自动选择下一轮行动；Agent 可以读输出后决定下一条命令。

## 命令覆盖

所有命令均支持 `--help`。公共选项可以放在命令前后：`--url`、`--user`、`--timeout`、`--pretty`、`--output/-o`。

| 命令 | 对应功能 |
| --- | --- |
| `health` | 检查服务及 userid，健康请求最多等待 8 秒 |
| `cards list` | 服务端可选故事卡 |
| `cards show ID` | 本地完整故事卡，包含提示词与隐藏世界设定 |
| `cards validate [PATH]` | 校验本地全部卡，或指定目录/card.json |
| `saves list` | 当前用户存档列表 |
| `saves create --card ID [--name NAME]` | 选择故事卡并创建空存档 |
| `saves load SID` | 读档：完整剧情、状态、物品、记忆、世界与导演信息 |
| `saves rename SID NAME` | 存档改名 |
| `saves delete SID` | 永久删除指定存档，无交互确认 |
| `saves export SID [--trace-limit N]` | 导出完整剧情与当前状态，加最近诊断记录；建议配合 `--output` |
| `play opening SID` | 生成开场 |
| `play action SID TEXT` | 行动续写 |
| `play inquiry SID TEXT` | 世界记忆问询，不推进剧情 |
| `state show SID` | 主角状态 |
| `state reconcile SID` | 从最新正文校准状态 |
| `state inventory SID` | 完整物品影子库，包括冷物品 |
| `world show SID` | 位置、时间和主角已知的世界信息 |
| `memory list SID` | 世界记忆 |
| `memory delete SID INDEX` | 按刚读取的列表下标删除一条记忆，下标从 0 开始 |
| `director show SID` | 引导、观察、事件、因果、视角、钩子、爽点、执行审计等导演状态 |
| `traces SID [--turn N] [--content] [--after CURSOR] [--limit N]` | Agent Trace 与增量游标；指定回合时自动带正文 |
| `metrics SID [--limit N]` | 模型请求状态和耗时 |
| `tokens SID` | 模型 token 总量与 Agent 分组统计 |
| `jobs show SID` | 当前存档正在运行的后台任务 |
| `jobs wait SID [--wait-timeout SECONDS] [--interval SECONDS]` | 有界等待后台任务结束 |
| `prompts list` | 本地通用模板与必需变量清单 |
| `prompts show NAME` | 模板原文 |
| `prompts render NAME --vars JSON` | 严格变量替换；支持 `--vars-file PATH` 或 `-` 读取 stdin |
| `prompts render NAME --system --card ID` | 组合指定卡的系统提示词；只接受可选的 `context` 变量 |
| `api schema` | OpenAPI 契约，供 Agent 发现接口与参数 |
| `api request METHOD /api/PATH` | 直接调用 JSON 接口；支持 `--query KEY=VALUE`、`--body JSON`、`--body-file PATH` |
| `run FILE` | 顺序执行 JSON 场景，断言失败立即停止 |
| `serve [--port 8888] [--data-dir DIR] [--no-embed]` | 前台启动无 reload 后端，Ctrl+C 停止 |
| `test [--pattern 'test_*.py']` | 运行项目 unittest 测试 |

旧 `/api/lore` 等 JSON 兼容路由可通过 `api request` 调用；旧 GET 生成接口由对应 `play` 命令覆盖。`api request` 会在发送前拒绝 SSE 生成路径，避免已经执行行动后才发现不能解析结果。

页面的语音输入最终提交的是行动文本，CLI 通过文本参数或 stdin 提交同样的内容；抽屉、打字机和布局等显示效果仍属于浏览器测试范围。

## 输入、输出和失败判定

```bash
./story play action "$sid" --file /tmp/action.txt --wait
printf '%s\n' '查看记录中的 $() 和 `符号`' | ./story play action "$sid" --file -
./story play opening "$sid" --stream --wait > /tmp/opening.jsonl
./story prompts render engine/narrative/system --system --card orbital --output /tmp/system.json
```

- 默认 stdout 为单个 JSON；生成成功时包含 `session_id`、完整 `text` 和 SSE `done` 的 `result`。只有收到 `done` 才返回成功。
- `--stream` 逐条输出 `{"event":"delta","data":{"text":"…"}}` 形式的 JSON Lines，保留 `stage`、`delta`、`done` 和 `error`。加 `--wait` 时最后还有 `idle` 事件。流式模式不使用 JSON 缩进。
- `--output` 将结果写到指定文件，结束时替换目标文件；失败时写入错误对象。流式实时消费请使用 stdout 重定向，而非 `--output`。
- 参数错误、HTTP 错误和生成错误在 stderr 输出 `{"error":{"type":"…","message":"…"}}`。默认非流式失败不会把部分正文输出成成功结果。
- `serve` 与 `test` 的日志也写 stderr；这两类命令的 stderr 包含人类可读诊断。
- CLI 不自动重试写操作。连接中断或等待超时时，已经完成的写入不会回滚，应先 `saves load SID`、`jobs show SID`、`traces SID` 确认状态，再决定是否重试。
- `--timeout` 默认 120 秒，是 HTTP 读取超时；`--wait-timeout` 默认 120 秒，是后台等待的总时限。
- HTTP 客户端忽略机器的代理环境变量。内网服务请先通过 `lumier-relay` 建立本地转发，再将 `--url` 指向本地端口。

| 退出码 | 含义 |
| --- | --- |
| `0` | 命令完成 |
| `2` | 参数、输入文件、JSON 或模板配置错误 |
| `3` | HTTP 非成功响应，错误对象包含 `status` |
| `4` | 连接/网络/HTTP 读取超时 |
| `5` | 模型错误、无效 SSE，或缺少 `done` 的不完整流 |
| `6` | 后台任务等待超时 |
| `7` | 场景断言失败或项目测试失败 |
| `130` | 用户中断 |

`jobs` 观察记忆提取、冲突观察、因果生成、下一事件生成和执行审计等后台任务，不改变它们的调度。`idle` 仅表示当前服务进程内没有该存档的后台任务，不表示模型成功或剧情正确；仍需检查审计、Trace 和测试断言。该状态接口用于单进程本地服务；重启后不会恢复未完成任务。请串行操作同一存档，避免其他客户端同时提交新回合。

## 场景测试

完整示例位于 `examples/cli/orbital-smoke.json`：

```bash
./story run examples/cli/orbital-smoke.json \
  --user agent-cli-test --pretty --output /tmp/orbital-run.json
```

场景使用 `card` 创建新局，或使用 `sid` 继续已有局，二者必须且只能指定一个。`name` 可选。

```json
{
  "card": "orbital",
  "steps": [
    {"command": "assert", "path": "/story_card/id", "equals": "orbital"},
    {"command": "opening"},
    {"command": "action", "text": "核对手边的记录"},
    {"command": "assert", "path": "/transcript/1/text", "equals": "核对手边的记录"},
    {"command": "checkpoint"}
  ]
}
```

步骤支持 `opening`、`action`、`inquiry`、`reconcile`、`checkpoint` 和 `assert`。行动和问询使用 `text`。生成后自动等待后台任务；检查点和断言前也会等待。断言从 `/api/load` 结果读取，路径采用 JSON Pointer，支持对象、数组以及 `~0`、`~1` 转义；必须且只能指定 `equals`、`contains` 或布尔值 `exists`。

执行前先校验所有步骤。执行中在首个错误或断言失败处停止，错误报告包含 `session_id`、已完成步骤和 `failed_step`，保留存档便于调查。成功报告附带最终存档与最近诊断数据；这是一份功能测试报告，叙事质量仍需要 Agent 或人工评审。

## 隔离验证

```bash
# 终端一：独立目录和端口；此命令仍使用真实模型配置
./story serve --port 8901 --data-dir /tmp/story-agent-test --no-embed

# 终端二：所有测试命令指向隔离服务
export STORY_URL=http://127.0.0.1:8901
export STORY_USER_ID=agent-test
./story run examples/cli/orbital-smoke.json --output /tmp/story-agent-report.json

# 不调用真实模型的自动化测试
./story test
./story test --pattern test_cli.py
```

CLI 集成测试会自动启动临时 TCP 服务并使用临时 SQLite，只替换模型回复。测试实际执行 CLI 子进程、HTTP/SSE、游戏逻辑与持久化，并验证所有公开业务 API 都有命令覆盖；不会操作真实用户的存档。
