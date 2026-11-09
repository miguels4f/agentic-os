# AGENTS.md — Wilfred

Scope: this folder (`wilfred/` inside the agentic-os kit). Codex reads this file directly;
Claude Code reads it through `CLAUDE.md`.

## What this is

Wilfred: the `wilfred` / `hw` engine (BUILD, IMPROVE and REVIEW runs with hard quality gates)
and Agent Foundry (design and build executable agents and deterministic workflows). Python 3.11+,
uv, macOS (generated workers are sandboxed with `sandbox-exec`).

## Commands

```bash
uv sync --group dev          # install (CI uses: uv sync --locked --group dev)
uv run pytest -q             # full suite
uv run ruff check .          # lint
uv run mypy src              # type check
uv build --wheel --out-dir dist && uv run pytest tests/test_installation.py -q   # wheel smoke
```

These are the steps of `.github/workflows/ci.yml`. To run them locally on the exact commit,
use the kit's `local-ci` skill and `bin/local-ci` script.

## Signatures and approvals (non-negotiable)

- Run gates (ship, merge, deploy, dependencies, credentials, migrations), promoting a learning
  proposal, and attesting a review as independent need the owner's signature with a
  passphrase-protected Ed25519 key. Agents open gates; the human signs in their own terminal
  with `tools/wilfred-signer.py`. No key ships with this kit: each user creates their own
  (`uv run python tools/wilfred-signer.py init`).
- Never forge, replay, fabricate or bypass a signature, a decision token, a gate record or an
  approval file. Never write to `~/Library/Application Support/Wilfred/` (keys, `policy.json`).
- The policy switch (`~/Library/Application Support/Wilfred/policy.json`, see
  `docs/human-approvals-policy.md`) removes the signature only from `quality-override`,
  `verify-override` and the changed-test review. Check it with `wilfred policy-status`
  (read-only). Ship gates and promote stay signed whatever its state.
- A change to what makes a run pass (new or changed evaluator, gate registry, regression rules)
  is a signed change: do not merge it unattended.

## Run state and reports

- `.hw/` is run state (events, gates, evidence). It is git-ignored here and `wilfred plan` adds
  it to the host repo's local exclude; never commit it anywhere.
- Review and audit reports go under `docs/reviews/<name>-<YYYYMMDD>/report.md` of the repository
  being reviewed.
- Security debt and the route registry live in `docs/threat-model.md`; update §5 when routes
  that read or write state change.
