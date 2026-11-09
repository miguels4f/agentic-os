# Provenance

Wilfred was extracted from a private historical repository without copying its full history
or unrelated project data. This public copy was made from the private product repository on
2026-11-09, again without history, with private names scrubbed and private run evidence left out
(see the kit's `tools/sync.py`).

## Imported baseline

- Historical base: `origin/main` at `a905a58217f188e2bb20f08302630504a66d7174`.
- Agent Foundry source: PR #26 head at
  `2c4fc6f2ca357c1376a0718fe7a98ee5b932c39c`.
- Imported scope: `packages/hyperwilfred-core`, its build/review adapter instructions,
  the Agent Foundry documentation, and the two architecture documents needed to
  explain the retained software-engineering engine.
- Import date: 2026-09-23.

The original commit identifiers remain the authoritative attribution trail. This
repository intentionally omits client files, secrets, virtual environments, dependency
caches, private run directories, and unrelated historical packages.

## Selection record

| Historical component | Decision | Reason |
| --- | --- | --- |
| `hyperwilfred-core` | Incorporate and adapt | It contains the standalone build, improve, review, guardrail, evaluation, resume, and Foundry engines. |
| Agent Foundry PR #26 | Incorporate and complete | It supplies the typed lifecycle and tests, but its runner, evaluation, and repair were incomplete. |
| Build/review step resources | Incorporate | They are runtime inputs loaded by the installed engine. |
| `sw-agent` | Reference only | Its useful scheduling concepts are already represented by the core engine; the package is mostly a legacy skill wrapper. |
| `guardrails` | Reference only | The enforceable guardrails used by current workflows already live in the core package. |
| `common` | Exclude | The extracted core deliberately has no runtime import from it. |
| `legacy` | Exclude | It is a separate consulting pipeline with client-oriented assets and a much larger dependency surface. |
| TypeScript workspace | Exclude | It is not required by the retained Python engine or the first Foundry runtime. |
| Historical `.hw` runs, environments, caches, and `node_modules` | Exclude | They are private execution data or reproducible local state, not product source. |

The repository owner chose the MIT License for publication (see `LICENSE`).

## Third-party notice

Runtime dependencies are Pydantic (typed contracts) and cryptography (signed approvals).
Development tooling
includes uv, Hatch, pytest, Ruff, mypy, and build. Their names identify upstream projects;
they do not imply endorsement. Consult `pyproject.toml` and `uv.lock` for the exact resolved
dependency set and each upstream distribution for its license terms.

The network-free demo does not download, copy, or execute any scouted candidate. Example
candidate URLs use the reserved `.invalid` domain and are evidence fixtures only.

## Vendored security-audit skills

Added 2026-10-01. These are project skills (instruction files for assistants working in this
checkout) under `.agents/skills/`. They are not packaged into the wheel and are not loaded by
the engine.

| Skill | Upstream source | License | Imported from | Modification |
| --- | --- | --- | --- | --- |
| `owasp-audit` | `https://github.com/briiirussell/cybersecurity-skills` | MIT (Copyright (c) 2026 Bri Russell) | historical repository commit `c980663`, `.claude/skills/owasp-audit/SKILL.md` | Vendored unmodified |
| `secrets-audit` | `https://github.com/briiirussell/cybersecurity-skills` | MIT (Copyright (c) 2026 Bri Russell) | historical repository commit `c980663`, `.claude/skills/secrets-audit/SKILL.md` | Vendored unmodified |
| `crypto-audit` | `https://github.com/briiirussell/cybersecurity-skills` | MIT (Copyright (c) 2026 Bri Russell) | historical repository commit `c980663`, `.claude/skills/crypto-audit/SKILL.md` | Vendored unmodified |
| `api-audit` | `https://github.com/briiirussell/cybersecurity-skills` | MIT (Copyright (c) 2026 Bri Russell) | historical repository commit `c980663`, `.claude/skills/api-audit/SKILL.md` | Vendored unmodified |
| `dependency-audit` | `https://github.com/briiirussell/cybersecurity-skills` | MIT (Copyright (c) 2026 Bri Russell) | historical repository commit `c980663`, `.claude/skills/dependency-audit/SKILL.md` | Vendored unmodified |
| `finding-triage` | `https://github.com/briiirussell/cybersecurity-skills` | MIT (Copyright (c) 2026 Bri Russell) | historical repository commit `c980663`, `.claude/skills/finding-triage/SKILL.md` | Vendored unmodified |

The attribution notice `.agents/skills/NOTICE-cybersecurity-skills.md` comes from the same
commit, also unmodified. Each file is byte-identical to its blob on the historical branch
`feature/hyperwilfred-security-lens`.
