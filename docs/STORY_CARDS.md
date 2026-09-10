# 故事卡编写说明

故事卡是项目内的完整世界包。新建游戏时在页面选择一张卡，后端将其提示词、地图、固定人物、势力、物品和初始状态保存为该局的快照。不同故事卡可以使用相同实体 ID，各局的位置、知识与奖励进度独立。

## 文件结构

```text
backend/story_cards/<id>/
  card.json     # 元数据、状态字段、固定世界、初始状态
  premise.md    # 题材、背景和世界前提
  rules.md      # 能力体系、成长限制、人物设定边界
  style.md      # 该题材的语气、措辞和叙事风格
  rewards.md    # 奖励类型、来源与兑现要求
  opening.md    # 开局处境和场景约束
  example.md    # 与状态字段一致的输出示例
```

内置 `xiuxian` 保留原玄苍大陆世界，`orbital` 是轨道站科幻生存示例。添加新卡可复制 `orbital` 目录并修改 JSON 和 Markdown，无需登记 Python 路由或修改前端。

## 元数据和状态字段

`card.json` 必须包含 `id`、正整数 `version`、`name`、`genre`、`description`、`default_save_name`。ID 必须与目录名相同，以小写字母开头，只含小写字母、数字和下划线。

`prompts` 将上述六个正文名称映射到卡片目录内的 `.md` 文件，例如 `"rules": "rules.md"`。正文是纯文本，不渲染模板变量；目录穿越和外部路径不被允许。

`status_fields` 按显示顺序定义状态栏，例如：

```json
[
  {"key": "health", "label": "健康", "kind": "text", "initial": "良好"},
  {"key": "supplies", "label": "补给", "kind": "resources", "initial": "饮水 1 袋"},
  {"key": "equipment", "label": "装备", "kind": "equipment", "initial": "多功能维修工具（绝缘握柄，可测电压）"}
]
```

- `key` 是存档字段名，使用与卡片 ID 相同的命名格式；不得重复或使用 `turn`、`updated_at`。
- `label` 是剧情面板与页面显示标签，不得重复或包含冒号、换行。
- `kind=text` 只保存状态；`resources` 将带括号属性的关键物品录入物品库；`equipment` 将所有非空装备条目录入物品库。
- `initial` 是开局文本。请同步修改 `example.md` 中的 `《状态》` 面板，保持字段、顺序与语义一致。

状态解析、校准和读档显示均按这份定义工作。修仙卡的“灵力、法宝”和科幻卡的“体力、装备”不需要共享字段名。

## 固定世界

`world.name` 是世界名称，下列数组必须存在，无内容时用 `[]`。条目均有唯一 `id` 和 `name`；其余必需字段如下：

| 数组 | 内容与字段 |
| --- | --- |
| `regions` | 区域：`role`、`summary` |
| `locations` | 地点：`region_id`、`kind`、`summary`；可选 `parent_id` |
| `routes` | 路线：`from_location_id`、`to_location_id`、`difficulty`、`risk`、`summary` |
| `factions` | 势力：`region_id`、`kind`、`summary` |
| `characters` | 固定人物：`location_id`、`summary`、`visibility` |
| `items` | 固定物品：`location_id`、`summary`、`attrs`、`kind`、`visibility` |
| `rewards` | 奖励：`summary`、`reward_kind`、`source_location_id`；来源可以是地点或势力 ID |
| `opportunities` | 机会：`location_id`、`kind`、`clue`、`danger`、`default_state` |
| `special_locations` | 特殊场所：`entrance_location_id`、`kind`、`opening_rule`、`entry_limit` |
| `power_levels` | 能力层级：非负整数 `sort_order`、`rarity`、`prevalence`、`npc_rule` |

至少有一个区域和地点，所有引用必须指向该卡存在的实体。奖励可增加 `rank`、`category`、`primary_element`、`realm_cap`、`visibility` 和 `source_label` 等描述；`reward_kind` 可用 `art`、`equipment`、`identity` 等稳定标识。`power_levels` 用于人物能力分布和出场合理性，不要求是修仙境界。

可选映射：

- `location_aliases`：地点 ID → 地点别名数组。
- `site_aliases`：地点 ID → `{局部场景名: [别名]}`，用于确认同一地点内的位置变化。
- `faction_homes`：势力 ID → 驻地地点 ID。

固定人物与物品按当前地点或已知实体送入事件上下文。秘密应放在固定世界数据中并通过知识边界控制；不要在所有 Agent 都能看到的背景正文里无条件宣布玩家未知的秘密。`visibility` 是设定描述，不是独立的访问控制开关。

## 初始状态

`initial` 包含：

- `location`：`region_id`、`location_id`，可选 `site_name`、`location_state`、`lost_risk`。
- `time`：从 1 开始的 `day`、0–1439 的 `minute_of_day`、`season` 和 `calendar_label`。
- `knowledge`：已知实体列表，每项有 `knowledge_type`、`target_id`、`status`，可选 `reliability`、`source`、`notes`。状态为 `confirmed`、`known` 或 `rumored`。
- `inventory`：开局拥有的固定物品 ID 数组，需与状态字段的初始装备描述一致。

知识类型沿用存档协议：`location`、`route`、`faction`、`art`（任意奖励）、`opportunity`、`realm`（特殊场所）、`character`、`item`。世界上下文中的 `arts`、`realms`、`cultivation_demographics` 也是兼容字段，分别映射奖励、特殊场所、能力层级，并不强制题材为修仙。

## 生效与兼容

卡片目录和通用模板在后端启动时读取。修改后需重启后端，随后新建的游戏使用新内容。提高 `version` 方便区分版本，但不会自动迁移旧存档；旧存档继续使用自己的完整快照，删除原卡目录也不影响已创建的局。兼容旧客户端和迁移所需的默认 `xiuxian` 卡必须保留。

首次升级时，没有故事卡信息的旧档会从旧世界表补齐快照，保留原剧情、状态、当前位置和旧世界事实。旧 `world_*` 表保留用于兼容迁移，新游戏的世界读取以各局快照为准。

卡片选择接口只返回展示元数据和状态字段，不返回完整地图、隐藏人物或提示词。引导层、叙事引擎、观察层的职责和调用顺序保持原有划分；故事卡提供设定，不调度 Agent。

## 验证

```bash
.venv/bin/python -m unittest discover -s backend/tests -t backend
```

`test_story_cards.py` 覆盖卡片校验、界面接口、用户隔离、并发 Agent 的题材隔离、不同卡复用实体 ID、装备奖励、状态校准、旧档迁移和快照持久化。Agent 调用在测试中模拟；叙事质量仍需使用实际模型试玩评估。
