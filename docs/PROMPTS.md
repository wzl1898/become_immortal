# 项目内提示词模板

通用提示词正文位于 `backend/prompt_templates/`，题材相关内容位于 `backend/story_cards/<id>/`，均以 UTF-8 Markdown 文件管理。`backend/prompts.py` 负责读取、校验变量和组合当前存档的故事卡规则；`game.py`、`constraints.py` 负责准备上下文、判断是否注入可选块，以及构造消息列表。完整世界包格式见 [故事卡编写说明](STORY_CARDS.md)。

## 按用途查找

| 目录 | 内容 |
| --- | --- |
| `guidance/conflict/` | 引导层：冲突引导的系统规则和输入 |
| `engine/event/` | 事件 Agent：系统规则、事件输入、下一事件输入、引导槽位内容 |
| `engine/causal/`、`engine/viewpoint/` | 因果与视角 Agent，包括模型不可用时的备用说明 |
| `engine/pacing/`、`engine/progression/` | 节奏与推进 Agent，包括工具结果、强制结算和审计反馈 |
| `engine/hook/`、`engine/payoff/` | 钩子与爽点 Agent，包括绑定失败后的重试要求 |
| `engine/skeleton/`、`engine/audit/` | 导演骨架生成和执行审计 |
| `engine/narrative/` | 剧情系统规则、开场、玩家行动、因果/视角上下文与导演骨架注入 |
| `observation/conflict/`、`observation/character/`、`observation/state/` | 冲突观察、人物设定和状态校准 |
| `memory/inquiry/`、`memory/extract/` | 世界记忆问询、搜索工具反馈和记忆提取 |
| `constraints/` | 开场、行动、问询的世界约束呈现模板 |
| `shared/` | 故事卡注入格式、动态状态字段、物品、记忆、历史种子与面板片段 |
| `legacy/director/` | 保留兼容的旧导演提示词；新生成链路不调用旧导演 |

`system.md` 是系统规则，`user.md` 是主要输入；其余文件按使用场景命名。可选内容采用独立片段，例如只有确实召回记忆时，才使用对应记忆模板。模板里的换行也是最终提示词的一部分。

## 编辑和变量

模板使用 Python 标准库 `string.Template`，无额外依赖。推荐以 `${变量名}` 表示槽位：

```text
【完整事件】
${event}

【最近一轮正文】
${recent_story}

【玩家本轮行动】
${action}
```

代码先筛选与序列化数据，再渲染：

```python
content = render_prompt(
    "engine/pacing/user",
    event=_stable_json(complete_event),
    recent_story=recent_story,
    action=action,
)
```

- 模板名称是相对于模板目录的路径，不带 `.md`；查找不依赖启动时的工作目录。
- 变量缺失、传入多余变量、模板文件不存在或模板语法错误都会报错，不会静默输出空槽位。
- JSON 的 `{}` 原样保留，不需要双写大括号。模板正文中的字面美元符号写成 `$$`。
- 变量值按纯文本插入一次，不执行表达式，也不递归解析其中的 `${...}`。玩家输入和 JSON 内容中的美元符号无需额外处理。
- 修改已有文案不需要改 Python。新增、删除或重命名槽位时，需要同步修改调用处传入的变量。
- 文件的前后空白会原样保留；`.gitattributes` 允许这些模板保留用于拼接的尾部空行。不要在模板里添加供维护者阅读、却不打算发送给模型的备注。

事件系统模板用 `${context}` 明确指定世界、记忆和引导的注入位置；因果系统模板也使用显式上下文槽位。注入不依赖 `# 输出` 等标题文字。`${story_rules}` 引用当前存档故事卡的背景和规则；事件、冲突引导和爽点 Agent 另外消费奖励规则，剧情 Agent 另外消费风格、状态字段和格式示例。放置顺序由各系统模板决定。

原 `shared/cultivation.md` 已移到 `story_cards/xiuxian/rules.md`。故事卡 Markdown 是纯文本，不解析 `${...}`；无需转义美元符号。

## 生效与存档

所有模板在进程启动时读取，运行期间使用同一份快照。修改模板后需要重启后端；普通的 uvicorn `--reload` 默认只监听 Python 文件，不保证 Markdown 修改会触发重启。

存档 `messages[0]` 中保存的剧情系统提示词继续用于该存档。修改 `engine/narrative/system.md` 后，新建存档使用新规则；已有存档不会被自动改写。其他 Agent 的通用模板在重启后用于后续调用，但题材规则始终取该存档保存的故事卡快照。修改故事卡内容或版本只影响之后创建的新局。

世界事实从故事卡加载并写入存档，存档中的位置、知识和机会状态独立维护。裁判判定、状态转移、记忆筛选、输出解析和模型调用仍由原有模块管理。模板负责模型指令及上下文的文本呈现，不执行数据库查询或业务逻辑。

## 验证

从项目根目录运行：

```bash
.venv/bin/python -m unittest discover -s backend/tests -t backend
```

`test_prompt_templates.py` 检查变量契约、字面文本保留、工作目录独立性，以及修改标题后上下文槽位仍然有效。`test_story_cards.py` 检查题材隔离、状态字段、奖励、存档快照和旧档迁移；既有 Agent 测试继续检查输入顺序与信息边界。
