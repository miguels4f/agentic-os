# Third-party skills — attribution

`owasp-audit/`, `api-audit/`, `dependency-audit/`, `secrets-audit/`, `crypto-audit/`, and
`finding-triage/` in this directory are vendored, unmodified, from:

- Source: https://github.com/briiirussell/cybersecurity-skills
- Author: Bri Russell
- License: MIT (Copyright (c) 2026 Bri Russell)

Vendored 2026-07-01 as part of HyperWilfred's security-lens upgrade. These six were selected out of
the source repo's ~30 skills as the subset directly applicable to a web/API/Node/contracts stack;
the rest (cloud/container/mobile/forensics/red-team/compliance-framework skills) were left out as
out of scope for this project's targets. Re-run the vendoring step against the upstream repo if a
newer version is wanted — these are a point-in-time copy, not a submodule.

See `security-methodology.md` (in `packages/hyperwilfred-core/src/hyperwilfred_core/steps/quality/`)
for the condensed field guide the engine's `security` critic lens actually loads; these six files
are the deep-dive reference it points to.
