# CLI usage and reproducible demo

Run from the repository after `uv sync --group dev`. Use `uv run wilfred`; the legacy
`hw` alias remains available. Every command emits one JSON object.

## Command examples

```bash
PROJECT=/tmp/foundry-demo
uv run wilfred foundry create --project "$PROJECT" \
  --objective "Turn workshop notes into a concise action list" --mode guided

# Answer the current question. --field is optional, but when supplied it must match.
uv run wilfred foundry answer --project "$PROJECT" \
  --field failure_impact --value "A reviewer corrects an incomplete draft"

# Continue one action. `next` is the alias for `next-action`.
uv run wilfred foundry next --project "$PROJECT"
uv run wilfred foundry status --project "$PROJECT"
uv run wilfred foundry validate --project "$PROJECT"
uv run wilfred foundry generate --project "$PROJECT"
uv run wilfred foundry execute --project "$PROJECT" --input-file notes.json
uv run wilfred foundry evaluate --project "$PROJECT"
uv run wilfred foundry repair --project "$PROJECT" --component edge
uv run wilfred foundry package --project "$PROJECT"
uv run wilfred foundry resume --project "$PROJECT"
```

`answer --value` accepts text or JSON. Use `--value-file PATH` for a JSON file. `create` also supports `--objective-file PATH`. At the scout stage, `next --scout-fixture PATH` accepts either a candidate list or `{"candidates": [...]}`; omitting it records a valid empty scout report.

`--defer-capability` does not turn a metadata declaration into an implementation. Specifications
with arbitrary transitions, human gates or custom tools are rejected even with this option.
Required unsupported behavior cannot be silently omitted to produce an accepted package.

Evaluation uses only subprocess evidence. Repair supports only the seeded empty-input defect;
neither command accepts caller-supplied result, replacement, or regression files.

`create --task-family notes_to_actions|evidence_answer|reviewed_brief` is an explicit supported
contract choice. Without it, unambiguous supported objectives select the family automatically;
unknown objectives stop. `execute`, `evaluate`, and `repair` accept `--provider codex` for agent
families. Missing providers stop; no fixture is selected silently. `execute` reads the persisted
trusted build fingerprint before running generated code against `--input-file` JSON.

Protected engine actions use `gate-open` to create an expiring challenge. An external trusted
human signer must sign it. Submit with `gate-approve --run-id ID --gate-id GATE --decision approve
--token-file SIGNED.json`. `--by` is metadata, not authorization; `.approved` markers are ignored.
The trusted host public key lives at `~/Library/Application Support/Wilfred/approval-public-key.pem`,
where `~` is the home in the operating system account record (the `HOME` variable is ignored) and
the file and its directory must be owned by that account or by root;
Wilfred does not create a production private key or grant itself human authority.
These are trusted-host APIs; generated runners do not implement arbitrary human-gate workflows
or protected external effects. The demo tests a real harmless host callback blocked by the verifier
and separately tests the generated worker's inability to self-authorize or access host state.

## Reproducible demo

```bash
uv run python -m hyperwilfred_core.foundry_acceptance --output-root /tmp/new-offline-proof
# Requires authenticated Codex CLI; uses real model calls and account usage:
uv run python -m hyperwilfred_core.foundry_acceptance --provider codex --output-root /tmp/new-live-proof
```

The offline command covers adaptive intake, topology choice, generation, real local
execution, a seeded failure, bounded repair, process interruption/resume, and a second
clean run with matching hashes. It exits nonzero if a promised stage lacks evidence.
Offline mode proves the workflow only. `--provider fixture` adds protocol-only agent tests.
`--provider codex` executes both agents and scenarios using real model calls. Human authorization
is never claimed: only an explicitly labelled ephemeral signature test is performed.
All modes preserve their audit directory and fail rather than overwrite it. See [the narration](../demo.md).
