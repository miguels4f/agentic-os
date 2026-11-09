# State-claims verification — field guide

> Origin incident (2026-07-08, pilot-app): a delegated docs-sync session read the repo state at
> session start, produced documents asserting "no MFA code exists on main today / PR #69 is an
> open draft", and committed them — in the same changeset as a sibling file that recorded the
> SAME PR as merged. The PR had merged mid-session. Nothing stopped the commit: the claims were
> stale (verified at start, not at write time) and the changeset was internally contradictory.
> This guide encodes the rule that would have caught it. It applies to every artifact a run
> writes or reviews — docs, reports, checklists, READMEs, plans, findings.

## 1. What is a state claim

A **state claim** is any statement in an artifact that asserts the CURRENT state of a repo,
system, or deployment — something that can drift while a session runs. Recognize them by shape:

- Merge/branch state: "PR #N is merged / open / draft", "X landed on main", "branch Y is stale".
- Existence: "no X code exists", "file/module/route Y is present", "dependency Z is installed".
- Doneness: "Block N is DONE / pending / blocked", "feature X is implemented", "TODO: …".
- Quantities: "N tests green", "~230 routes", "coverage is X%".
- Deployment/config: "deployed at address 0x…", "region is Frankfurt", "MFA is enabled",
  "CI runs gitleaks".
- Freshness markers: "as of <date>", "today", "currently", "still".

Opinions, designs, and history ("we decided…", "in June we…") are NOT state claims. A claim
about the past that is dated and closed ("#43 merged 2026-07-02") is history once verified;
"#69 is still open" is a live state claim every time it is written.

## 2. The three rules

**Rule 1 — verify at write time, not at session start.** The repo you observed when the session
began is a snapshot, not the truth. Long sessions race against other sessions, merges, and
deploys. Every state claim in an artifact you are about to finalize must be re-verified against
the CURRENT state (fresh `git log` / file read / command output) immediately before the artifact
is written or staged — not recalled from earlier context. If you cannot re-verify a claim, say
so in the artifact ("not re-verified") instead of asserting it.

**Rule 2 — a changeset must agree with itself.** Before finalizing, cross-read every artifact in
the same changeset (and the unchanged files they cite). Two artifacts asserting contradictory
states — one says merged, the other says draft; one says DONE, the other says pending — is a
**blocker**, never a style issue. The fix is to re-verify against the current state and align
both to it; committing either side unverified is wrong even if one happens to be right.

**Rule 3 — contradiction with your premises = stop.** If the state you just observed contradicts
the task's own premises or instructions (the packet says "X is unmerged" but `git log` shows it
merged), do NOT silently pick a side, and do NOT proceed with the half that still makes sense.
Stop, record what you observed vs. what was claimed, and escalate to the human gate. A task
built on a stale premise usually has more wrong with it than the one contradiction you noticed.

## 3. Verification recipes

Cheap, deterministic checks — prefer these over recall:

- Is commit/PR X on main? `git log --oneline --grep "#N" main` ·
  `git merge-base --is-ancestor <sha> main && echo yes`
- Does code X exist? `grep -rn "<symbol>" <paths>` (a zero-hit grep is evidence; an unread
  memory is not) · `ls <path>`
- Dependency present? grep the manifest (`package.json`, `pyproject.toml`), not the lockfile
  comment.
- Counts ("N tests", "N routes"): re-run the counting command; never carry a count forward from
  a previous document.
- Deployment/config: read the actual config file at HEAD (`git show main:<path>` if the working
  tree may be dirty).
- PR/issue state: query the forge (`gh pr view N --json state,mergedAt`), not the plan document
  that mentions it.

## 4. Disposition

For each state-claim finding, state the disposition in prose inside `description`/`suggestion`
(the `CriticFinding` schema has no disposition field):

- **Stale** — claim was true once, is false at HEAD → severity `major` (or `blocker` if the
  artifact is outward-facing: customer docs, security scope, compliance).
- **Contradictory** — two artifacts in the changeset disagree → `blocker`.
- **Unverifiable** — claim asserts state the critic cannot check (external system, credentials)
  → `minor`, suggestion: mark it "not re-verified" in the artifact.
- **Verified** — checked against HEAD and true → no finding.
