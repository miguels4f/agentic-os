# Human-approvals policy switch

Wilfred asks a human to sign (with a passphrase-protected key) at several approval points. One
owner-controlled file turns that signature off for three lower-impact points and keeps it
everywhere else — in particular on every run gate (shipping) and on promoting a learning
proposal. Delete the file and every point needs a signature again.

## The switch

File: `~/Library/Application Support/Wilfred/policy.json`

```json
{"human_approvals": "auto-except-ship-and-promote", "set_by": "<owner name>", "since": "<ISO date>"}
```

The file must contain exactly these three keys, the exact value above, a non-empty `set_by` and an
ISO date in `since`, in at most 4 KiB. Anything else — file absent, unreadable, not JSON, another
value, an extra or missing key — means the switch is off and every approval point needs a
signature (fail-closed).

Check the current state with `hw policy-status` (read-only).

## What changes while the switch is on

| Approval point | Command | With the switch on |
|---|---|---|
| Accept a quality review that was not independent | `hw quality-override` | Approved by policy, no signature |
| Accept a live-demo finding at verify | `hw verify-override` | Approved by policy, no signature |
| Recorded review of a changed pre-existing test file | regression gate (`hw evaluate`) | Approved by policy, no signature |
| Every run gate: ship, merge, deploy, dependencies, secrets, migrations, ad hoc or attached to a step | `hw gate-open` / `hw gate-status` / `hw gate-approve` | **Signature required** |
| Promote a learning proposal to an IMPROVE run | `hw propose --id` + `hw promote` | **Signature required** |
| Attest that a review was independent | `hw record-subjective` / `hw record-critique` with `author=independent` | **Signature required** |

Run gates are never policy-eligible because the gate record that would tell a ship gate from a
minor one is not itself signed; an agent could relabel it.

The policy replaces only the signature. Every other check still runs and still blocks: the
deterministic gate, open blocker/major findings, the quality score threshold, requirement
traceability, artifact reproducibility, baseline tests that stop passing, and test files changed
with no recorded review.

A review accepted by policy is still reported as *not independent*. The policy never relabels it.

Note on the test-evolution point: the recorded review (`create_semantic_review_challenge`) is a
record the agent can write itself; before this switch, the reviewer's signature was the only thing
that gave it weight. With the switch on, an agent can change a pre-existing test and have that
change approved without anyone else looking. The ship gate lists every such approval for the
human who signs there.

## How a policy approval is recorded

- The engine writes `<gate>.decision.json` next to the challenge with `"approved_by":
  "policy:auto"`, `"human_signature": false` and the SHA-256 of the policy file. It carries no
  signature and never names a human.
- The override record shows `policy:auto` as the approver and a note with the same hash; the
  regression check details say `auto-approved by policy (no human signature)`.
- Each approval prints `Auto-approved by policy (no human signature): <action> [<run>/<gate>]` to
  standard error, and the run event log says `POLICY AUTO-APPROVAL (no human signature)`.
- The ship gate lists every policy approval of the run, including those granted after the gate
  was opened, so the human who signs the ship decision sees what was not signed.

A policy decision verifies only while the switch is on **and** the policy file is byte-identical
to the one that granted it. Removing or editing the file voids earlier policy decisions: the
override no longer shows in the banner, and a verify override or test-evolution review is
re-checked the next time the step runs. A pipeline step already marked done on the strength of a
policy approval stays done; nothing rewinds completed state.

## Security trade-off

The switch file is not signed. Any process running as the owner's user can create it, so once this
feature is installed the three policy-eligible points are only as protected as that file path.
Every run gate and the promote point stay protected by the signing key.
