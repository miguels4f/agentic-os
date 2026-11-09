---
name: secret-leak-response
description: Use the moment a secret, API key, token or password appears in a diff, commit, log, chat or file, or when a pre-commit/pre-push hook or gitleaks blocks a commit/push for a secret. Runs the leak-response order, tells public values from real secrets, marks verified false positives. Do NOT use for general security reviews or routine rotation without exposure.
---

> Adapted from agency-agents `security-secrets-credential-engineer` (https://github.com/msitarzewski/agency-agents, MIT, commit 4e3ac3c). Persona removed, method kept, examples made generic.

## Golden rules
- **Never print the secret value back** — not in chat, logs, commit messages, PR text, reports or memory. Refer to it as `<type> in <file>:<line>, starts with "abcd…"` (4 characters maximum).
- **A committed or pasted secret is burned from that moment**, not from when it was noticed. Deleting it from the file does not un-leak it: history, clones, CI logs and chat transcripts still hold it.
- **Rotation at the provider is the fix.** Everything else is cleanup.
- The agent does not rotate keys or log into provider dashboards (entering credentials is not allowed). The user rotates; the agent prepares the exact steps.

## When to use
- A key/token/password/connection string shows up in `git diff`, a commit, a log, terminal output, a file, or the user pastes one in chat.
- The pre-commit / pre-push hook or the `local-ci` (in this kit) secret scan fails with a secret finding.
- If the repo has its own `.gitleaks.toml`, respect it. Check that `gitleaks` is installed (`command -v gitleaks`).
- To locate without printing: `gitleaks dir --redact <path>` or `gitleaks git --redact <repo>` (recent v8 releases; older versions used `detect`/`protect`).

## Step 0 — triage: real secret or public value?
Decide before alarming anyone. Values that are **designed to be public** (not a leak):
- publishable/anon client keys (e.g. Stripe `pk_…`, Supabase anon key, analytics project keys);
- `NEXT_PUBLIC_*`, `VITE_*`, `PUBLIC_*`, `EXPO_PUBLIC_*` variables that hold flags, URLs or ids (e.g. `NEXT_PUBLIC_DEMO_MODE`, `VITE_API_BASE_URL`);
- documented test fixtures and dummy values (`sk_test_…` placeholders, `changeme`, `example`), public certificate parts, SHA hashes.
Values that are **always secret**, wherever they appear: server API keys (Anthropic, Mistral, OpenAI, Clay, Apollo, Vercel, cloud/hosting providers, GitHub tokens), private keys, `DATABASE_URL` with a password, JWT/session signing secrets (`SECRET_KEY`), partner API keys in plaintext, signing-key material for approval or release gates (e.g. Wilfred's, optional, see `wilfred/` in this kit), webhook secrets.
A secret-shaped value **behind a public prefix** (e.g. `NEXT_PUBLIC_OPENAI_KEY`) is a real leak: it ships to every browser.

## Leak-response order (do all five; stopping at step 2 is the common failure)
1. **Rotate at the provider — the user does this now.** Give them: provider, which key (by name/label, not value), the dashboard section to revoke it, and where the new value must go (local `.env`, hosted env file, the hosting provider's environment settings). If several services share the key, list all consumers so rotation does not cause an outage; overlap old and new only if the provider supports two active keys.
2. **Replace in code.** Remove the literal; read it from server-side env/secret store; make sure nothing client-reachable imports it. Commit this as a normal change (the commit message names the key type, never the value).
3. **Purge history — only if the commit was pushed or shared.** Unpushed: amend or reset the local commit before pushing. Pushed: history rewrite (`git filter-repo`) is destructive and changes everyone's clones — propose it to the user as a decision; it is not a substitute for step 1. For leaks in chat transcripts or logs, purge is usually impossible: rotation is the only remedy.
4. **Audit usage from commit time.** Exposure window = first commit/paste timestamp → revocation time. Ask the user to check the provider's usage/billing/access logs for that window (unexpected calls, spend, IPs). If used: treat as an incident, widen rotation to anything that key could reach.
5. **Close the gate gap.** Why did the hook/scan miss it, or why was the secret near code at all? Add the pattern to the scanner config, move the value to the env file, add the file to `.gitignore`, or fix the script that echoed it.

## Marking a verified false positive
Only after Step 0 says the value is public or a dummy:
- Add an inline marker on the same line: `# pragma: allowlist secret` (Python/YAML/shell) or `// pragma: allowlist secret` (TS/JS). This is the detect-secrets convention; if your own hook checks for a different marker, use that one.
- If the repo is scanned by gitleaks, it ignores lines carrying `gitleaks:allow`; add that too, or prefer a narrow path/regex allowlist entry in the repo's `.gitleaks.toml` with a comment saying why.
- Never allowlist a whole file or directory to silence one finding; never mark a real secret as a false positive to get a push through.

## Output (to the user, in plain language, in the user's language)
```
Stopped: I need you to rotate a key.   (or "False positive: marked, no risk")
What: <key type> in <file>:<line> (starts with "abcd…")
Exposed since: <commit/date> — pushed to the remote?: yes/no
What you need to do: 1) … at <provider> → <section>  2) put the new value in <place>
What I will do: remove it from the code / prepare a history cleanup (if you say yes)
Check usage between <date> and today in <provider's usage page>
```
Then a short English note for the repo log/PR: key type, file, commit, steps done, steps pending — no value.

## Stop conditions
- Never paste the value into a scanner website, an issue, or another AI tool to "check" it.
- Do not push, open a PR or merge while a real secret sits in unpushed commits; fix locally first.
- Do not rewrite shared history, delete branches or force-push without the user's explicit yes.
- If unsure whether a value is public, treat it as secret.

## Out of scope
Setting up Vault or dynamic credentials, broad security audits (use a read-only security reviewer subagent), rotating keys on a calendar with no exposure.
