# Output contract — scores master + signature docs + per-category files

The audit is **not** one giant document. It is a structured `scores.json` master, two whole-audit
docs (`overview.md`, `fix-plan.md`), and **one markdown file per category**. The desktop app reads
`scores.json` first to render the master-detail UI (left = the scores overview with a dashboard
gauge and severity filter, right = the selected category's md), then loads a category's md, the
overview, or the fix plan on demand. This keeps any single document small enough to read and edit.

Categories are **flexible** — adapt them to the project (see SKILL.md). Default set:
Security, Correctness, Architecture, Performance, Maintainability, AI-Slop/Debt. Each category you
keep is one `scores.json` entry **and** one md file.

JSON is **camelCase** on the wire. Do not add fields not listed here and do not rename them.

## Files

Each kind of SlopBuster artifact lives in **its own folder**, one versioned subdir per run:

```
.slopbuster/
  audit/<version>/   scores.json + overview.md + fix-plan.md + one md per category   ← this skill
  plan/<version>/    plan.json   + one md per facet
  docs/<version>/    docs.json   + one md per section
```

Audit writes into a dated subdir of `.slopbuster/audit/`:

```
.slopbuster/audit/<YYYY-MM-DD>/
  scores.json     ← structured master (app reads first)
  overview.md     ← whole-audit executive outline + how-to-prompt-your-agent guidance
  fix-plan.md     ← prioritized, dependency-ordered remediation plan
  security.md     ← full findings for the Security category only
  correctness.md
  architecture.md
  …               ← one md per category, filename = the category's `doc` field
```

The app may pass an explicit versioned target dir (e.g. `.slopbuster/audit/v3-<date>/`); when it
does, write `scores.json` and the category md files into that dir instead.

## `scores.json` — the structured master

```json
{
  "overallGrade": "A+|A|B|C|D|F",
  "numeric": 61,
  "summary": "<2–3 SHORT sentences — a teaser under the gauge, NOT a paragraph; full summary in overview.md>",
  "meta": {
    "generatedBy": "<the tool + model that produced this report, e.g. 'claude-code / claude-opus-4-8', or 'cursor', 'copilot', 'aider' — whatever is running. Never blank>",
    "author": "<git author name — `git log -1 --format=%an`>",
    "email": "<git author email — `git log -1 --format=%ae`>",
    "commit": "<`git rev-parse HEAD`>",
    "branch": "<`git rev-parse --abbrev-ref HEAD`>",
    "generatedAt": "<ISO-8601 timestamp — `date`>"
  },
  "overviewDoc": "overview.md",
  "fixPlanDoc": "fix-plan.md",
  "categories": [
    {
      "key": "security",
      "label": "Security",
      "grade": "D",
      "numeric": 58,
      "summary": "<1–2 line status of this category>",
      "doc": "security.md",
      "findingsCount": 18,
      "severityCounts": { "critical": 1, "high": 6, "medium": 11, "low": 0, "info": 0 },
      "findings": [
        {
          "n": 1,
          "severity": "critical",
          "title": "...",
          "file": "crates/...",
          "line": 647,
          "effort": "S",
          "what": "what's wrong and why it matters (prose)",
          "fix": "the concrete fix (prose)"
        }
      ]
    }
  ]
}
```

### Field rules

- **`overallGrade`** (`Grade`) — the single capped overall grade. Not an average: a failing
  Security or Correctness category caps it.
- **`numeric`** — overall 0..100 score that drives the dashboard gauge. Capped the same way as
  `overallGrade` (a failing Security or Correctness category pulls it down), not an average.
- **`summary`** — **2–3 short sentences max**, a teaser shown under the dashboard gauge. This spot is
  not for a wall of text — the full executive summary belongs in `overview.md`, not here.
- **`meta`** — attribution for the report, so the team cloud can track **who produced it and with
  what tool** (the generator is not always Claude Code). Populate every field by running `git` and
  naming the running tool/model. `generatedBy` must never be blank. Required on every report so a
  dashboard can attribute quality to an author and a generator over time.
- **`overviewDoc`** — filename of the whole-audit overview doc (`overview.md`), relative to the
  manifest dir.
- **`fixPlanDoc`** — filename of the whole-audit fix plan (`fix-plan.md`), relative to the manifest
  dir.
- **`categories[]`** — **one entry per category actually written** (flexible set, no fixed length,
  no stubs). Order by severity, worst first.
  - **`key`** — stable slug for the category (e.g. `security`, `ai_slop_debt`, `data_integrity`).
    Used by the app to track a category across audit versions; keep it stable for the same concept.
  - **`label`** — human title shown in the UI (e.g. `Security`, `AI Slop / Debt`).
  - **`grade`** (`Grade`) — that category's sub-grade on the `A+ A B C D F` scale.
  - **`numeric`** — 0..100 score for the category.
  - **`summary`** — 1–2 line status of the category, shown in the overview row.
  - **`doc`** — the md filename for this category, relative to the manifest dir (e.g.
    `security.md`). The app loads this file when the row is selected.
  - **`findingsCount`** — number of findings in that category. Must equal `findings.length` and the
    count of findings in the md file.
  - **`severityCounts`** — per-severity tallies (`critical`, `high`, `medium`, `low`, `info`) that
    drive the UI's severity filter. Every bucket must equal what `findings` actually contains, and
    the buckets must sum to `findingsCount`.
  - **`findings[]`** — the **complete** structured finding set for the category, the same findings
    as the category md (not a sample). Each entry:
    - **`n`** — 1-based index within the category, severity-ordered (critical first). Matches the
      `1. 2. 3.` numbering in the category md.
    - **`severity`** — one of `critical | high | medium | low | info`.
    - **`title`** — short headline for the finding.
    - **`file`** — repo-relative path the finding lives at.
    - **`line`** — line number, or `null` for an architecture-wide finding with no single line.
    - **`effort`** — rough fix size, one of `S | M | L`, or `null`.
    - **`what`** — **markdown**: what is wrong and why it matters. Use short paragraphs and, where it
      helps, a bullet list — not one dense block. The app renders this as markdown.
    - **`fix`** — **markdown**: the concrete fix, written as a **numbered, step-by-step list** the
      reader can follow (`1.` `2.` `3.`), each step an imperative action; add an inline ` ```code``` `
      snippet or a short bullet sub-list where it makes the step unambiguous. Never a single dense
      sentence — a developer who is scared of a wall of text should see clear, actionable steps. The
      app renders this as markdown.

## Per-category md files

One file per `categories[]` entry, named by its `doc` field. Each is written in its **own** Write
call so no single document holds the whole audit. The file is the **readable export** of the same
findings the category's structured `findings[]` carries — the two must agree. It holds the
**complete** finding set for that category only — severity-ordered (critical → info) and **numbered
`1. 2. 3.` to match each finding's `n`** in `scores.json` — each finding with:

- the `file:line` it lives at (or an architecture-wide note when no single line applies),
- what is wrong and why it matters,
- the concrete fix to make, with a rough effort size (S / M / L).

Be exhaustive: include the minor findings (a dead import, a generic name) — the md is the complete
record, not a summary. `findingsCount` in the manifest must equal the number of findings here, and
each numbered finding must correspond to the structured entry with the same `n`.

## `overview.md` — the signature export

One whole-audit document, named by `overviewDoc`. Two parts:

- **Executive outline** — what the system is, the headline verdict, the worst areas, in prose. The
  read-aloud summary of the whole audit.
- **How to prompt your AI agent / how to improve** — forward-looking guidance for vibe coders who
  build by prompting an AI. Generalize the recurring problems found into the rules, constraints, and
  checklists they should paste into future prompts so these *classes* of issue don't recur. This is
  the highest-value section: prevention at prompt time, not a restatement of findings.

## `fix-plan.md` — the remediation plan

One whole-audit document, named by `fixPlanDoc`. A prioritized, dependency-ordered remediation plan:
numbered steps grouped into phases (quick wins → structural), ordered so prerequisites come before
the work that depends on them, each step citing `file:line` and an effort size (S / M / L). It is
the audit's remediation output — what to fix and in what order — not a from-scratch planning
artifact.

## Fields the app adds, NOT emitted by the skill

Version identity and per-finding ids are assigned by the app when it ingests the audit directory —
the skill emits only `scores.json`, `overview.md`, `fix-plan.md`, and the category md files
described above.
