# Novelty Dossier: Recursive Lift / 2×2 Recursive Audit

## Proposed method

We propose an operational test for recursive self-improvement (RSI): whether a later system is not only better at tasks, but is better at producing the next useful self-modification.

Save an old agent/improver I0 and a later agent/improver I1, plus an old modification target T0 and later target T1. Under identical task evidence, tools, sampling budget, and hidden evaluation, run the full crossed design:

- I0 improves T0
- I1 improves T0
- I0 improves T1
- I1 improves T1

Here “improver” is the procedure that diagnoses failures and proposes/chooses modifications; “target” is the harness being modified. Estimate the improver main effect and improver×target interaction with paired tasks and repeated seeds. Define Recursive Lift as the later improver's improvement advantage over the old improver on matched targets. Add an equal-budget control comparing: one-generation broad search, a multi-generation chain always operated by I0, and a genuine recursive chain operated by each new generation.

## Core novelty claims

1. Crossed interventions separate improvement-operator ability from target quality/improvability; ordinary lineage scores conflate them.
2. Recursive Lift provides an empirical, falsifiable criterion for whether improvement ability itself increased.
3. An equal-total-budget restart/fixed-improver control separates recursion from extra search depth or Best-of-N sampling.
4. A negative result is meaningful: task score can rise monotonically while Recursive Lift is zero or negative.

## Verified closest work from arXiv official API

1. **Huxley-Gödel Machine: Human-Level Coding Agent Development by an Approximation of the Optimal Self-Improving Machine** — arXiv:2510.21614, first submitted 2025-10-24. It identifies a metaproductivity-performance mismatch and estimates a node's self-improvement potential by aggregating descendant benchmark performance. Closest conceptual neighbor. Apparent delta: descendant outcomes are observationally entangled with the target node, search path, and improvement operator; our crossed intervention holds targets fixed while swapping old/new improvers and estimates a causal operator effect.
2. **Self-Taught Optimizer (STOP): Recursively Self-Improving Code Generation** — arXiv:2310.02304, first submitted 2023-10-03. It lets an improver improve itself and compares resulting task outcomes. Apparent delta: no factorial swap of old/new improvers over matched old/new targets and no equal-budget restart test.
3. **Darwin Godel Machine: Open-Ended Evolution of Self-Improving Agents** — arXiv:2505.22954, first submitted 2025-05-29. It maintains a branching lineage and selects promising descendants. Apparent delta: focuses on search method and endpoint, not causal decomposition of who improves versus what is improved.
4. **Generalized Agent Iteration: One Formal Framework for Iterative Policy Improvement and Recursive Self-Improvement** — arXiv:2609.13406, first submitted 2026-09-11. It distinguishes whether the improving mechanism lies inside the agent and whether evaluation is externally grounded. Apparent delta: formal taxonomy, not the proposed low-cost crossed empirical estimator.
5. **Audit the Scaffold, Not the Checkpoint: A Stationarity Dichotomy for Recursive Self-Improvement in Agentic Coding** — arXiv:2609.34924, first submitted 2026-09-28. It characterizes saturation when reachable edits remain fixed and separates search, test-time training, and scaffold rewriting. Apparent delta: studies ceilings and saturation, not matched interventions estimating whether later improvers have higher causal efficacy.
6. **Recursive self-improvement of AI research agents** — arXiv:2609.26457, first submitted 2026-09-22. It reports seven successive agent improvements and held-out transfer. Apparent delta: successive endpoint gains do not by themselves identify whether each successor became better at the act of improving.
7. **On the Fragility of Self-Improving Agents: Variance, Task Order, and Underspecification** — arXiv:2608.18066, first submitted 2026-08-18. It studies variance and task-order sensitivity. Apparent delta: strengthens the need for paired seeds, but does not appear to introduce the 2×2 operator-target design.
8. **RRSI: Regularized Recursive Self-Improvement of Agent Harnesses** — arXiv:2609.24972, first submitted 2026-09-21. It reduces overfitting in harness evolution. Apparent delta: a method for more generalizable updates, not a causal criterion for recursion.

## Search limitations

The generic WebSearch plugin returned HTTP 401. Searches were instead performed through arXiv's official API using queries around recursive self-improvement, self-improving agents, metaproductivity, causal/counterfactual evaluation, improvement operator, acceleration, fixed budgets, and harness evolution. Therefore this is a recent targeted arXiv scan, not an exhaustive Google Scholar/Semantic Scholar search.

## Reviewer questions

1. Is this method materially novel, or already contained in a named paper above?
2. Is it a genuine operational definition of recursion, or only a rebranding of HGM's metaproductivity?
3. What exact experimental design is required for the delta to survive review?
4. Should the equal-budget frontier be part of the main method or only a baseline?
5. Give a calibrated novelty score and PROCEED / PROCEED WITH CAUTION / ABANDON verdict.

=== NOVELTY VERDICT LIMITS (these bound how you judge, never how widely you search) ===
Search exhaustively; judge calibrated. Two failures waste months equally:
passing an idea a published paper already contains, and killing a viable idea
because the territory has neighbors.
1. Proximity is information, not a verdict. Someone working nearby goes in the
   report; it is not by itself a reason to reject.
2. ABANDON has exactly one qualification: a specific published paper already
   contains this result — name that paper. No named paper, no ABANDON.
3. Crowded-but-deltaed is PROCEED: state the delta in one sentence a reviewer
   could verify. Thin or contested delta is PROCEED WITH CAUTION — say what
   would make it carry, not why it should die. CAUTION is not a safe middle:
   if you cannot name the specific thing that makes the delta thin, the
   verdict is PROCEED.
4. Concurrent or competing work is not a veto. That is a race — report it and
   let the user decide whether to run it.
5. A direct attack on a central problem is legitimate novelty when nobody has
   executed it well. "This area is hot" does not mean "this area is taken."
6. This check is an early gate, never the last one — more triage, pilots, or
   external review still stand between any idea and a paper, whatever order
   this run uses. A wrongly passed idea dies cheaply at one of them; a wrongly
   killed idea is never seen again. When torn between two verdicts, choose the
   more permissive one.
Say plainly when an idea clears the check. Do not manufacture overlap.
