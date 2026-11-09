# Wilfred

Wilfred is a local, evidence-driven engineering system with two capabilities:

1. build, improve, and review software through bounded work packets and hard quality gates;
2. design and build executable agents and deterministic workflows through Agent Foundry.

The same core provides persistence, scope controls, human approvals, evaluation,
repair, and traceability. Agent Foundry is a Wilfred capability, not a separate
product.

## What is proven today

Agent Foundry supports three bounded problems: explicit notes-to-actions parsing (a workflow),
evidence-grounded answers (one agent), and a reviewed brief (writer and reviewer with separate
contexts). Unsupported goals stop instead of silently becoming a notes parser.

Run the offline proof, which performs real work, detects and patches defective generated
source, survives a half-written file, and retains both versions and their evidence:

```bash
uv run python -m hyperwilfred_core.foundry_acceptance --output-root /tmp/wilfred-offline-proof
```

To demonstrate actual single-agent and multi-agent reasoning, use `--provider codex` with an
authenticated Codex CLI. This makes real model calls and may consume account usage. Both agents
must retrieve two separate documents and derive the answer; a test fixture is never substituted
for a missing provider. `--provider fixture` tests the protocol only, not model reasoning.

The output directory contains task outputs, model-call traces, source fingerprints, executed
scenario evidence, repair diffs and crash records. A passing demo is not a self-certification:
see [acceptance](docs/acceptance.md) and [what was verified in this public copy](docs/verification.md).

## Development installation

Python 3.11+, macOS with `/usr/bin/sandbox-exec`, and uv are required for generated execution.
Other operating systems fail closed: no unsandboxed fallback. The host brokers model calls;
generated workers cannot read host credentials, write host files, fork children, or use sockets.

```bash
uv sync --group dev
uv run wilfred --help
uv run python examples/demo.py
uv run pytest -q
```

`wilfred` is the primary command. `hw` remains available as a compatibility alias for
existing workflows.

`tests/test_installation.py` builds a wheel, installs it in a fresh environment, clears
`PYTHONPATH`, and executes all three task families outside the checkout (model protocol fixtures).
Production signing keys are not included. Protected actions require an externally signed,
challenge-bound decision; the demo's positive signature is an ephemeral test fixture only.

## Documentation

- [Agent Foundry overview](docs/agent-foundry/README.md)
- [Architecture](docs/agent-foundry/architecture.md)
- [Command reference](docs/agent-foundry/cli.md)
- [Demo narration](docs/demo.md)
- [Limits and roadmap](docs/limits-and-roadmap.md)
- [Contributing](CONTRIBUTING.md)
- [Historical provenance and extraction decisions](PROVENANCE.md)

## Your own signing keys

Protected actions (ship, merge, deploy, dependency and migration gates, promoting a learning,
independent-review attestations) need a human signature. No key ships with this kit; create
your own once, in your own terminal:

```bash
uv run python tools/wilfred-signer.py init --kind approval   # gates and promote
uv run python tools/wilfred-signer.py init --kind review     # review attestations
```

Each command writes a public key and a passphrase-encrypted private key to
`~/Library/Application Support/Wilfred/`. When an agent opens a gate, sign it with
`uv run python tools/wilfred-signer.py sign <challenge.json>` and hand the agent the token path
it prints. Agents cannot sign: the passphrase is read only from an interactive terminal.

## License

MIT, see [LICENSE](LICENSE). Part of the agentic-os kit; see [provenance](PROVENANCE.md).
