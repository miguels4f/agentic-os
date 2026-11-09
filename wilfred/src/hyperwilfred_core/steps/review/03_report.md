# REVIEW · Synthesize the findings report

The critics have run and their findings are on disk. This step aggregates them into a single
structured `ReviewReport` — the engine does this deterministically (no LLM), so you only invoke
it and report the result.

## Process
1. `hw review-report --run-id <id>`. The engine reads every recorded critic findings JSON,
   flattens the findings, groups them by severity, computes the severity/lens counts and the
   overall verdict (`changes_requested` if any blocker/major, `advisory` if only minor, else
   `pass`), and writes both `review-report.json` and a human-readable `review-report.md` under
   `.hw/<run_id>/`. It is the `produces_path` for this step. It also records who wrote each
   critique (`critic_authors`: independent / adapter / missing). If any lens is not independent,
   a `pass` or `advisory` result becomes `pending_independent_review` (the findings-only verdict
   stays in `severity_verdict`) and the report opens with "NOT AN INDEPENDENT REVIEW".
2. `hw complete-step --run-id <id> --step report`. The engine validates the report artifact
   exists before the run completes.
3. Hand the founder the verdict + `review-report.md`. If the verdict is
   `pending_independent_review`, say so first, in plain words: the critiques were written by the
   session that ran the review, not by independent reviewers, so "no problems found" is not
   established. The findings can also drive an IMPROVE run
   later (review → improve continuity): point `hw plan --mode improve` at the same target with the
   report's findings as acceptance criteria.

## Note
REVIEW ends here — there is NO ship gate, NO patcher, NO deploy. It is a standalone read-only
audit. Acting on the findings is a separate, explicit decision.
