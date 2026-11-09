# Third-party notices

Most of this kit is original work released under the MIT License (see `LICENSE`). A few
parts are adapted from other open-source projects. Their notices are reproduced here, as
their licenses require.

Tools that this kit only *links to* (and installs from their own source with
`third-party/install.sh`) are listed in [`third-party/README.md`](third-party/README.md);
none of their files are copied into this repository.

## agency-agents (MIT)

Source: <https://github.com/msitarzewski/agency-agents>, commit `4e3ac3c`.

The method of these skills was adapted from agency-agents prompts. The persona text was
removed, the method kept and the examples made generic:

| Skill in this kit | agency-agents source |
| --- | --- |
| `skills/workflow-spec` | `specialized-workflow-architect` |
| `skills/secret-leak-response` | `security-secrets-credential-engineer` |
| `skills/browser-test-hygiene` | `testing-test-automation-engineer` |
| `skills/ui-finish-gate` | `design-ui-finish-gate-reviewer` |
| `skills/accessibility-audit` | `testing-accessibility-auditor` |
| `skills/persona-walkthrough` | `design-persona-walkthrough` |
| `skills/money-flow-integrity` | `engineering-payments-billing-engineer` |
| `skills/automation-governance` | `automation-governance-architect` |

```text
MIT License

Copyright (c) 2025 AgentLand Contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## slopbuster-skill (Apache-2.0)

Source: <https://github.com/PerhapxinLab/slopbuster-skill>. Only the `slopbuster-audit`
skill is included, in `skills/slopbuster-audit/`, with its license file
(`skills/slopbuster-audit/LICENSE`). Change made: the local-rules block at the top of
`SKILL.md` was rewritten to be generic. The rest is unmodified.

## cybersecurity-skills (MIT)

Source: <https://github.com/briiirussell/cybersecurity-skills>, Copyright (c) 2026 Bri Russell.
Six audit skills are vendored unmodified inside Wilfred, in `wilfred/.agents/skills/`, with
their notice in `wilfred/.agents/skills/NOTICE-cybersecurity-skills.md`.

## Wilfred's runtime dependencies

Wilfred depends on Pydantic and cryptography at runtime, and on uv, Hatch, pytest, Ruff,
mypy and build for development. They are installed from their own distributions (see
`wilfred/pyproject.toml` and `wilfred/uv.lock`) under their own licenses; none is vendored.

## The kit map (`docs/map.html`)

`docs/map.html` was generated with Floorplan (<https://github.com/haydenschmitty/floorplan>,
MIT License, Copyright (c) 2026 Hayden Smith), and the file embeds its viewer code. It also
embeds, unmodified:

- elkjs, the layout engine (<https://github.com/kieler/elkjs>), under the Eclipse Public License
  2.0 (<https://www.eclipse.org/legal/epl-2.0/>);
- the Inter font (<https://github.com/rsms/inter>, Copyright 2016 The Inter Project Authors) and
  the JetBrains Mono font (<https://github.com/JetBrains/JetBrainsMono>, Copyright 2020 The
  JetBrains Mono Project Authors), both under the SIL Open Font License 1.1
  (<https://openfontlicense.org>).
