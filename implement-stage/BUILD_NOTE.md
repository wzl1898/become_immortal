# Build Note — Stage Guidance System

| Rung | Feature | Acceptance check (ONE command) | Tier | Status |
|------|---------|-------------------------------|------|--------|
| F0 | stage lifecycle spine: normalize, generate, store, archive | `cd backend && ../.venv/bin/python -m unittest tests.test_director.StageGuidanceTests` | MUST | ✅ |
| F1 | dynamic stage Agent context and validated output | `cd backend && ../.venv/bin/python -m unittest tests.test_director.StageGuidanceTests` | MUST | ✅ |
| F2 | inject stage into event generation and decrement on successful creation | `cd backend && ../.venv/bin/python -m unittest tests.test_director.StageGuidanceTests` | MUST | ✅ |
| F3 | audit actual narrative, archive completion, retire payoff normal path | `cd backend && ../.venv/bin/python -m unittest tests.test_director.StageGuidanceTests` | MUST | ✅ |
| F4 | regression, temporary-service validation, docs | `cd backend && ../.venv/bin/python -m unittest discover -s tests` | MUST | ✅ |

## Run record
- F0–F3: `python -m unittest tests.test_director.StageGuidanceTests` → exit 0, 5 focused tests.
- F4 regression: `python -m unittest discover -s tests` → exit 0, 149 tests.
- F4 live HTTP smoke on port 8901 → exit 0; generated stage `获得《引气诀》`, persisted budget 3 after first event, event snapshot budget 4, `stage_guidance` trace present, `director_payoff` trace absent.

## Deferred
- Frontend stage visualization — not requested.
- Rich structured prerequisites — active stage schema is intentionally limited to `goal` and `event_budget`.

## Blockers
