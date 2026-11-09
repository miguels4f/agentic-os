# HTML explainer — content contract

`scripts/build_explainer.py` turns a Python module that defines `DOC = {...}` into one self-contained, navigable HTML page (sidebar, clickable flow diagram, collapsible cards, light/dark, phone-friendly). Worked example (an invented notes-to-tasks app): `references/example-explainer-content.py`.

## Fields

Required keys are marked ●. Everything else is optional; empty sections are not rendered.

| Key | Type | Content |
|---|---|---|
| ● `title` | str | Page title, e.g. "Payments Service — Architecture Explainer" |
| ● `subtitle` | HTML str | One line: what it is + the repo, branch, commit and commit date it describes |
| ● `pitch` | str | 60–100 words the reader could say aloud; end with the core design rule |
| `pitch_simple` | str | The pitch in one plain sentence, in the reader's language |
| `context` | list of HTML str | Short paragraphs: modes, roles, entry points, and **which architecture this is** when the repo holds more than one |
| ● `journey_intro` | HTML str | Which journey the diagram shows and how to read its rows |
| ● `flow_rows` | list of lists of block ids | The main journey, ≤5 boxes per row, in order |
| `side` | list of `{id, short, one}` | Side paths shown dashed under the flow, with their own `short` and `one`; each `id` needs a block, and a block appears either in `flow_rows` or in `side`, not both |
| ● `blocks` | list of dicts | See below |
| ● `decisions` | list of dicts | See below; aim for 8–15 |
| `controls` | list of (control, what it does, evidence) | Safety and reliability controls |
| `stack` | list of (layer, choice) | Technologies with versions |
| `numbers` | list of (item, value, source) | Measured or recorded numbers (see writing rules); the source cell carries the date and any "not re-run" label |
| `limits` | list of str | Honest gaps; required in practice whenever known gaps exist |
| `fit_title` + `fit` | str + list of str | How the system maps to the reader's goal (a job role, onboarding, due diligence) |
| `qa` | list of `{q, a, tip?}` | Likely questions with ready answers |
| `glossary` | list of (term, plain definition) | Every term a non-engineer would stumble on |
| `about` | HTML str | Sources, commit, date, path conventions, what to re-measure before quoting |
| `labels` | dict | Overrides any heading, tag, column name or button (keys: `DEFAULT_LABELS` in the build script), e.g. to write the whole page in Spanish. Keys you don't override stay in English, so translate them all; `blocks_intro` and `decisions_intro` accept HTML. The older `simple_label`, `say_label` and `tip_label` keys still work |
| `lang` | str | HTML `lang`, default `en` |

**Block** — ● `id`, ● `kind`, ● `short` (≤3 words, shown in the diagram), ● `one` (≤10 words, shown in the diagram), ● `title`, ● `details` = list of `(label, value)` pairs using the contract labels Executor, Input contract, Output contract, Restrictions, Handoff, Evidence; optional `step` (number in the flow; a letter such as A or B for side paths, shown in their boxes), `badge` (`O` Observed, `I` Inferred, `U` Unknown; default `O`), `simple` (one plain sentence), `why` (why it matters for cost, risk or the customer).

Kinds set the colour: `entry` = where a person or caller starts, or a human decision or approval step; `process` = an internal step, including checks and validation; `storage` = where results persist; `external` = a third-party service, a separate runtime such as a model server, or an optional, legacy or planned component; `risk` = an automated control whose main job is to block or contain (authentication, guardrails, automated gates).

**Decision** — ● `title`, ● `choice`, ● `alt` (alternatives actually available), ● `why` (the stated reason, quoting and citing its source), ● `tradeoff` (what it costs); optional `badge` (default `O`), `simple`, `say` (one sentence the reader could say aloud).

Every field not marked "HTML str" is a text field: escaped, supporting `**bold**`, `*italics*` and `` `code` ``. Fields marked "HTML str" are trusted: they also accept tags.

## Writing rules

- **One reason per decision, with its source.** Cite the ADR, doc, commit or PR where the reason is stated. With no stated reason, give the most plausible engineering reason and set `badge: "I"`; with nothing to go on, `badge: "U"`.
- **Re-verify every count and status claim you can check read-only** ("merged", "closed", "N tests", "all findings fixed"). Prefer the exact figure ("12 of 13 with a test") over "all".
- **Numbers say where they come from.** A number is either measured by you now or quoted from the project's own recorded runs; either way give source and date, and label anything not re-run ("recorded 2026-10-05", "counted, not run"). Estimates (cost, effort) go in a decision or a Q&A answer, labelled as estimates.
- **One badge per card.** When a card mixes certainty, use the weakest badge that applies and mark the uncertain part inline, e.g. "(Inferred)".
- **Name the architecture.** If legacy and current designs share a repo, say in `context` which one the page describes and how the other relates.
- **Limitations before challenges.** List known gaps plainly; a reader who must defend the system needs them more than the strengths.
- **Plain language layer.** `simple`, `pitch_simple` and the glossary are for a smart non-engineer; translate every term on first use.
- **Language.** Write the body in the user's documentation language (English unless asked otherwise) and the `simple` lines, `pitch_simple` and `tip` in the reader's own language. For a fully translated page, set `lang` and `labels`.
- **Audience fields.** The `pitch` is always written so it could be said aloud. Add per-decision `say` lines only when the reader will present or defend the system (interview, pitch, review). `fit` states what the system means for the reader's goal; set `fit_title` to match, e.g. "How this maps to the role" or "What this means for you".
- **Diagram text is tiny.** Keep `short` and `one` within the limits above and the whole diagram to about 15 boxes including side paths; nuance goes in the card.
