# Stage Guidance System

## Target
Add a guidance-layer stage state `{goal, event_budget}`. A stage Agent dynamically chooses a major, persistent goal from protagonist growth, revenge/gratitude and promises, faction/social identity, recent narrative direction, world facts, and stage history. Every newly generated event consumes the stage goal and remaining budget; successful event creation decrements the budget, and the final budgeted event must settle the stage. Narrative audit confirms completion, archives the stage, and clears it for regeneration.

## Inputs
Existing save/director state, character state, world slice, memories, recent narrative, prior stage history.

## Outputs
`director_state.stage` with exactly `goal` and `event_budget`; `director_state.stage_history` with completion metadata; stage and audit agent traces.

## Success command
`cd backend && ../.venv/bin/python -m unittest tests.test_director`

## Base commit
`59335d13589790601bace143a0231b9f62fb4296`

## Scope cuts
No frontend UI and no general workflow DSL. Legacy payoff helpers remain for save/test compatibility but leave the normal planning path.
