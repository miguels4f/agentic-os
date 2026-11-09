# IMPROVE · Step 02 · Locate the change site(s) and scope the diff

Turn the objective + the comprehension map into a `plan.json` of atomic, **tightly scoped**
slices — the same `Plan`/`Slice` contract BUILD uses, so the parallel fan-out and the engine's
acceptance guardrail work unchanged. The difference from BUILD is that you are scoping a change
against existing code, so the scopes must be **narrow**: only the files each change truly needs.

## What to do
1. From `comprehension.md`, identify the exact file(s) each part of the change touches.
2. Split the work into slices that can be applied independently. For each slice declare:
   - `allowed_globs` — the MINIMAL set of paths it may write (e.g. `["src/auth/**"]`, not
     `["src/**"]`). The engine rejects any write outside this at acceptance time, so keep it
     tight to make the diff scoped.
   - `blocked_globs` — anything inside the allowed scope that must NOT be touched.
   - `depends_on` — other slice ids that must land first.
   - `max_files`, `max_changed_lines` — keep them small; IMPROVE edits should be surgical.
3. Slices with disjoint `allowed_globs` and no dependency run in the same wave (parallel).

## Write `plan.json`
```json
{
  "objective_id": "<objective.id>",
  "product_dir": "<the existing repo/target, relative to repo root>",
  "slices": [
    {
      "id": "slice-1",
      "title": "short imperative title",
      "description": "what to change and the acceptance for this slice",
      "acceptance_criteria": ["..."],
      "allowed_globs": ["src/feature/**"],
      "blocked_globs": [],
      "depends_on": [],
      "max_files": 4,
      "max_changed_lines": 120
    }
  ]
}
```

The engine validates this against the `Plan` schema before fan-out. Keep scopes minimal: a
narrow scope is what makes an IMPROVE change reviewable and reversible.

### Cut by RESPONSIBILITY, not by file count
- One slice = one responsibility, end to end: the code that decides a rule AND the place its
  result is published (a generated snapshot, an API response, a command string) belong in the
  same slice/packet. `max_files` is a **ceiling** to keep a slice reviewable — never a target to
  cut against. A slice cut to "2 files" that splits a rule from where it is published forces the
  next slice to **re-implement the rule** (in another language or layer) and the two copies drift
  (the pilot V2 failure: "recompute the allowed/blocked state in TypeScript").
- Single source of truth: the authoritative engine computes each verdict once and publishes it;
  every other layer (UI, CLI help, generated commands) only DISPLAYS it. Never write a slice that
  "recomputes", "mirrors" or "duplicates" an existing rule.
- `hw complete-step` runs the deterministic plan lint (`hw plan-lint` shows it): it WARNS on
  re-implementation wording and on a rule slice that cannot reach its artifact, and it BLOCKS when
  the objective's `risk_level` is high or critical.
- Declare generated artifacts on the slice that writes them: `"generated_artifacts": [{"path":
  "site/data/accounts.json", "command": "<regeneration command>", "volatile_fields":
  ["generated_at"]}]` (data files only; the path must sit inside the slice's `allowed_globs`).
  Only declared artifacts are exempt from the full-file-replacement guardrail.

### Contract checklist (put each applicable item in `acceptance_criteria`)
- [ ] **Fail-closed integrity pins** — governed config/catalogs (allow-lists, budgets, tool
      lists) carry a runtime hash/pin the product verifies at start; a missing/edited/tampered
      file REFUSES to run, with a tamper test.
- [ ] **Enforced caps, not display-only** — any command the product hands to a human or agent
      (paste commands, generated prompts) carries the caps as real flags (tool allow-list,
      `--max-budget-usd`, …) and a test asserts they are present.
- [ ] **Reproducible generated data** — generated artifacts are deterministic (no wall-clock
      dates, no absolute paths, stable ordering; take dates from git); regenerating in a fresh
      checkout yields the same bytes except declared volatile fields.
- [ ] **Every locked/blocked state is explained** — the user sees WHY an action is locked or
      blocked (reason + what unlocks it) without clicking or interacting.
- [ ] Every requirement/part ID in the shaping doc is covered by a criterion or declared in
      `out_of_scope` (the verify step blocks on a MISSING one).
- [ ] **Multi-step stateful flows name their branches** — a slice that implements a flow with
      more than one step or an external call (quote → book → cancel, payment, a job queue)
      lists in `acceptance_criteria` what happens on **timeout**, on a **retryable vs permanent
      failure** (how many retries, then what), on a **partial failure** (what was created and
      how it is cleaned up or undone), and on a **duplicate or concurrent request** (idempotent
      result or explicit conflict). One test per branch. A step that assumes another already ran
      names the mechanism that guarantees it (lock, transaction, existence check), never "it
      usually runs first". (agency-agents wiring 2026-10-23, owner OK; method: specialized-workflow-architect)
