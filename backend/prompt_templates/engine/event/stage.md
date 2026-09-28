【阶段目标引导】
${stage}

硬规则：
- 当前事件必须实质推进 stage.goal，不能生成与阶段目标无关的随机事件。
- stage.event_budget 表示包含当前事件在内的剩余新事件预算。当前事件必须消耗其中 1 个预算，并让 core、benefit、end_condition 都能服务该目标。
- 当 stage.event_budget = 1 时，当前事件必须是最终收束事件：end_condition 必须要求正文直接给出 stage.goal 的明确成功，或明确且不可逆的失败；不得再设计中间线索、预告、铺垫、试探或“之后再确认”的结果。
- 当 stage.event_budget > 1 时，当前事件也必须给 stage.goal 带来可验证进展、代价、关系变化或路线判断，不能只留下模糊悬念。
