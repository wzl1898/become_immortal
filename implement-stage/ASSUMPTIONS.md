# Assumption Ledger — Stage Guidance System
<!-- ASK mode: never -->

| ID | Under-determined by the request | Chosen | Class | Source |
|----|--------------------------------|--------|-------|--------|
| A-001 | What happens when the remaining budget reaches 1 | The next generated event must directly settle the stage with success or explicit irreversible failure; it cannot add another intermediate clue | semantic | default |
| A-002 | Whether player diversion consumes stage event budget | Budget counts successful event creations under the active stage, even when player direction affects the event; ordinary turns do not consume it | semantic | default |
| A-003 | How stage completion is established | The last event ending is the deterministic stage boundary; the existing narrative audit only classifies the ended stage as success or fail from actual prose | semantic | user |
| A-004 | Shape of archived history | Keep active stage exactly `{goal,event_budget}`; archive adds `result`, `ended_turn`, and `evidence` outside the active stage schema | interface | default |
| A-005 | Relationship to the payoff Agent | Remove payoff Agent from the normal planning path; keep legacy fields/helpers only for persisted-save and focused-test compatibility | interface | default |
| A-006 | Invalid stage-agent output | Retry is not added; fall back to a broad card-grounded major goal with budget 4 so event generation remains available | semantic | default |
| A-007 | Where stage information appears in the existing UI | Add it at the top of the existing Director drawer rather than creating a new drawer or exposing it in the story pane | interface | default |
| A-008 | How remaining budget is described to players | Show the numeric remaining new-event budget plus a plain status: `>1` progressing, `1` next event is final, `0` final event in progress | semantic | default |
| A-009 | How much stage history is initially visible | Use a native collapsed details section containing all history returned by the backend, with result, ending turn, and evidence | interface | default |
| A-010 | What to show while no stage is active | If stage state is present but null, show “等待生成下一阶段”; do not expose generation IDs or other lifecycle internals | interface | default |

## Notes

- **A-001** — This gives the budget an enforceable meaning. Without it, the final event can still defer the promised result.
- **A-002** — The user specified an event budget, not a cooperation budget. Explicit abandonment/failure is represented by audited stage settlement rather than silently freezing the counter.
- **A-003** — The audit sees actual prose and only classifies `success` or `fail` after the final event has ended. Event state determines the boundary; state reconciliation remains independent.
- **A-006** — The fallback is deliberately generic and grounded in current character/world context; it does not hard-code cultivation-only goals.
