# Assumption Ledger — Stage Guidance System
<!-- ASK mode: never -->

| ID | Under-determined by the request | Chosen | Class | Source |
|----|--------------------------------|--------|-------|--------|
| A-001 | What happens when the remaining budget reaches 1 | The next generated event must directly settle the stage with success or explicit irreversible failure; it cannot add another intermediate clue | semantic | default |
| A-002 | Whether player diversion consumes stage event budget | Budget counts successful event creations under the active stage, even when player direction affects the event; ordinary turns do not consume it | semantic | default |
| A-003 | How stage completion is established | The narrative audit judges actual prose against the exact stage goal; event/planner declarations alone cannot complete it | semantic | default |
| A-004 | Shape of archived history | Keep active stage exactly `{goal,event_budget}`; archive adds `result`, `ended_turn`, and `evidence` outside the active stage schema | interface | default |
| A-005 | Relationship to the payoff Agent | Remove payoff Agent from the normal planning path; keep legacy fields/helpers only for persisted-save and focused-test compatibility | interface | default |
| A-006 | Invalid stage-agent output | Retry is not added; fall back to a broad card-grounded major goal with budget 4 so event generation remains available | semantic | default |

## Notes

- **A-001** — This gives the budget an enforceable meaning. Without it, the final event can still defer the promised result.
- **A-002** — The user specified an event budget, not a cooperation budget. Explicit abandonment/failure is represented by audited stage settlement rather than silently freezing the counter.
- **A-003** — The audit sees both prose and status panel. State reconciliation remains independent and may fail, so prose evidence is sufficient for stage lifecycle while state consistency is separately reconciled.
- **A-006** — The fallback is deliberately generic and grounded in current character/world context; it does not hard-code cultivation-only goals.
