# Contributing

Wilfred is published as part of the agentic-os kit under the MIT License. Open an issue or a
pull request on the kit's repository. The kit's maintainer also refreshes this folder from a
private original every week, so a change may need to be ported there too; say in the pull
request what you changed and why.

## Local checks

```bash
uv sync --group dev
uv run ruff check .
uv run mypy src
uv run pytest -q
uv run python examples/demo.py
git diff --check
```

Keep the core provider-neutral and network-free by default. Integrations must be optional,
document permissions and failure behavior, and never present fixture evidence as review.
Changes need focused tests and updated claims. Never commit keys, tokens, private runs, client
data, generated environments, or caches.

Generated execution tests require macOS isolation; do not skip them to make another OS green.
The installed-wheel test executes task outputs outside the source tree, not just CLI help.
Existing regression tests may only evolve with reviewed before/after semantic evidence; changing
their hashes or resetting the baseline does not waive regression protection. Use synthetic data
for public acceptance records, and preserve live-model versus fixture provenance explicitly.
