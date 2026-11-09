# Automation prompt template — keep AGENTS.md current

Copy this prompt into a recurring automation (only after the user approves the schedule — see
`automation-governance`) or into a one-off session. Replace every `<placeholder>`; list only source
files that exist in the repo.

```text
Use the agents-maintainer skill (<path-to-skill>/SKILL.md) to maintain `<repo>/AGENTS.md`.

Task:
1. Read:
   - `<repo>/AGENTS.md`
   - `<repo>/README.md`
   - `<repo>/<app-dir>/README.md`          (one line per app or package covered)
   - `<repo>/<app-dir>/<manifest>`         (package.json, pyproject.toml, Cargo.toml, ...)
   - `<repo>/CHANGELOG.md`                 (if present)
2. Update `AGENTS.md` only if repository reality changed (structure, commands, workflow, priorities).
3. Keep edits minimal and verifiable.
4. If no change is needed, do not edit files and report `no_changes`.

Output format:
- decision: `updated` or `no_changes`
- files_inspected: list
- sections_changed: list (empty when no changes)
- summary: short bullet list
```
