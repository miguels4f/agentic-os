# Simulation — user-journey war-game

A behavioral audit, not a static one. Instead of reading code looking for slop, you role-play a
real user and walk each flow step by step, reasoning about what actually happens at every step.
The lens is the user, not the file — but every conclusion is grounded by tracing the real code
path behind the step, so this is war-gaming, not imagination.

## Set up the journeys

1. Identify the real users and their primary jobs-to-be-done. Infer them from the product
   surface: routes/pages, navigation, API endpoints, CLI commands, forms, entry points. If the
   intended user or goal is unclear, enumerate the main entry points and pick the critical
   journeys (the ones most users hit, and the ones where failure hurts most).
2. For each journey, write the concrete ordered steps a user takes from intent to outcome —
   in user terms ("open the page", "submit the form", "wait for the result"), not code terms.

## Walk each step

For every step, simulate honestly:
- **What the user does** and what they expect to happen.
- **What the system actually does** — follow the real code path (handler → service → data →
  response → UI update). Trace it through the routes/handlers to the data layer and back.
- **What could go wrong** at this step.

Surface anything in these classes, tagging each finding:
- **redundant** — a step that adds no value: ceremony, a confirmation nobody needs, data the
  user must re-enter, a screen that could be merged or skipped.
- **breaks** — it errors, dead-ends, loses state, mishandles refresh / back / double-submit,
  races, leaves the user stuck, or has an unhandled empty / loading / error state.
- **confusing** — unclear what to do next, surprising or inconsistent result, misleading label,
  no feedback that the action worked.
- **friction** — too many clicks, slow with no indication, avoidable round-trips.
- **edge / abuse** — invalid or hostile input, unauthorized access, concurrent use, offline or
  partial-dependency failure, very large or empty data.

Push on the unhappy paths deliberately — happy-path-only is where this audit earns its keep.

## Output

Per journey:
- The step-by-step walk, with each step's expected vs actual behavior.
- A list of findings, each tagged by class above, with severity and a **concrete fix** (remove
  the step, handle the empty state, add feedback, collapse two screens, …), citing the
  `file:line` of the code path responsible.

Then a short summary: the worst journeys, ranked, and the single highest-leverage fix per
journey. Don't pad — a journey that holds up cleanly gets one line saying so.

## Persist (required)

Simulation has **its own folder** (each SlopBuster kind does: `audit/`, `plan/`, `docs/`,
`simulation/`). Write the markdown to `.slopbuster/simulation/<YYYY-MM-DD>.md` and a machine-readable
JSON sidecar to `.slopbuster/simulation/<YYYY-MM-DD>.json` that the desktop app reads. The md and
json must be complete and consistent. If the app passes a versioned target dir, write both there
instead.

Simulation keeps its own flat-JSON sidecar (it is a single behavioral report, not a per-category
split). Write `<date>.json` as (camelCase):

```json
{
  "summary": "worst journeys ranked + highest-leverage fix",
  "journeys": [
    { "name": "Checkout", "steps": ["open cart", "enter address", "pay"] }
  ],
  "findings": [
    {
      "journey": "Checkout",
      "tag": "redundant|breaks|confusing|friction|edge_abuse",
      "category": "correctness|architecture|performance|maintainability|ai_slop_debt|security",
      "severity": "critical|high|medium|low|info",
      "title": "short",
      "description": "expected vs actual behavior at this step",
      "file": "rel/path or null",
      "line": 12,
      "suggestedFix": "remove the step / handle the empty state / collapse two screens / …",
      "effort": "S|M|L",
      "status": "open",
      "fingerprint": "stable-hash"
    }
  ]
}
```

`tag` maps the finding classes above into a stable token; `journey` ties the finding back to the
walked journey so the app can group by journey. `fingerprint` is a stable hash of
`category|file|line|title` (join with `|`, hash the string) so the app can diff across versions.
`status` is always `"open"` on emit; the app owns later transitions.
