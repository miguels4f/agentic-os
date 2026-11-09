# Acceptance (public copy)

The original acceptance record of this engine cites evidence folders from private runs
(model-call receipts, run archives and independent reviews of real projects). Those folders are
not part of this public copy, so the record is not included either.

What you can check yourself in this copy:

```bash
uv sync --group dev
uv run pytest -q                 # the full test suite
uv run python -m hyperwilfred_core.foundry_acceptance --output-root /tmp/wilfred-offline-proof
```

The offline proof performs real work without any model or network: it runs the three Agent
Foundry task families, detects and patches a defective generated source, survives a
half-written file, and keeps both versions with their evidence. See
[verification](verification.md) for what was observed when this copy was published.
