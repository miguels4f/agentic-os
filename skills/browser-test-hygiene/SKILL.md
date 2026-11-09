---
name: browser-test-hygiene
description: Use when writing, fixing or reviewing Playwright browser tests — an existing e2e suite (e.g. `<your-app>/e2e/`) or a first browser suite for a new app — to keep them deterministic: no fixed sleeps, tests own their data, role-based selectors, 10 repeat runs, artifacts on failure. Do NOT use for unit or backend API tests, or for deciding overall test strategy.
---

> Adapted from agency-agents `testing-test-automation-engineer` (https://github.com/msitarzewski/agency-agents, MIT, commit 4e3ac3c). Persona removed, method kept, examples made generic.

# Browser test hygiene

Rules that make a browser test trustworthy: it passes or fails for one reason, every time. A test
that needs a retry to pass is a flaky test, and a flaky test is not done. Run tests locally (for
example through the `local-ci` skill in this kit) before a PR, whatever hosted CI you also use.

Overlap: a test-driven-development skill (write the test first), `local-ci` (replay checks
locally), a testing-strategy skill (what to test at which level). This skill only covers how a
browser test is written and stabilised.

## Where the suites are (verify before citing)

Find them before writing anything: `rg --files -g '*.spec.ts' -g 'playwright*.config.*'`.
Typical layout:

- `<your-app>/e2e/*.spec.ts` with `playwright.config.ts` (sometimes a second config for another
  environment, e.g. `playwright.staging.config.ts`), plus helpers such as `e2e/auth.ts` or a shared
  fixture file. Note the package scripts that run them (e.g. `npm run test:e2e`).
- Backend API e2e tests (Jest, pytest, etc.) are not browser tests — out of scope here.
- No Playwright in the project yet? Adding it is a new dependency: ask the user first.

## Rules

1. **No fixed waits.** No `waitForTimeout`, `sleep`, `setTimeout` in tests. Wait on a condition:
   a web-first assertion (`await expect(locator).toBeVisible()`), a URL, or a specific response
   (`page.waitForResponse(r => r.url().includes('/api/bookings') && r.ok())`).
2. **Tests own their data.** Each test creates what it needs, ideally through the API or a seed
   helper, with unique names (suffix with `test.info().testId` or a timestamp). Never depend on
   another test's leftovers or on "the demo user" being in a given state.
3. **Set up through the API, assert through the UI.** Log in once per worker (storage state) or
   seed a session; only the login test drives the login form.
4. **Select like a user.** `getByRole('button', { name: 'Book' })`, `getByLabel`, `getByText` for
   static copy. `data-testid` only when no role or label can reach the element. No CSS chains or
   `nth-child`.
5. **Browser tests only where the integration is the risk.** If an API or unit test can prove it,
   push it down. Keep browser tests for journeys: sign-in, checkout, quote → book, editor save.
6. **Artifacts on failure.** In the config: `trace: 'retain-on-failure'`,
   `screenshot: 'only-on-failure'`, `video: 'retain-on-failure'`, `forbidOnly: !!process.env.CI`.
   A failure must be explainable from `test-results/` without rerunning
   (`npx playwright show-trace <trace.zip>`).
7. **Retries measure, they do not fix.** Run with `retries: 0` locally. A pass-on-retry is a flake
   signal to root-cause, not a green result.
8. **Quarantine with a reason.** If a flake cannot be fixed now, mark it `test.fixme` with a comment
   naming the suspected cause and a tracking issue or task id from the user's tracker — never
   delete it silently.

## Method for a new or fixed test

1. Read the existing fixtures and helpers; reuse them before writing new ones.
2. Write the test following the rules above.
3. Run it alone: `npx playwright test <file> --repeat-each=10 --retries=0` (add `-c <other
   config>` if the spec belongs to a second config). All 10 must pass.
4. Run it with the rest of the suite in parallel (default `fullyParallel`) to expose shared state.
5. Grep the diff for forbidden patterns:
   `rg -n "waitForTimeout|setTimeout|nth-child|\.only\(" <changed spec files>`.
6. Hand off to `local-ci` (or your own local check runner) before the PR.

## Flake triage

| Symptom | Likely cause | Real fix |
|---|---|---|
| Passes locally, fails in CI / a slower run | timing race exposed by slower run | wait on condition / response, remove time waits |
| Fails only in parallel | shared user or record | per-test or per-worker data |
| ~1 in 20 element not found | animation or hydration race, weak selector | web-first assertion on final state, role selector |
| Breaks after an unrelated merge | hidden dependency on seed data | test creates its own data |
| Navigation timeout | third-party script blocking `load` | block third-party routes; wait for an app-ready signal |

## Output (when reviewing or fixing)

```markdown
Browser test hygiene — <files>
Repeat run: <n>/10 passed (command used)
Violations: <rule # — file:line — fix>
Flakes root-caused: <test — cause — fix>
Quarantined: <test — reason — issue/task id or "needs a tracking issue from the user">
```

## Stop / escalate

- The flake comes from the app (real race in the product) → report it as a product bug with the
  trace; do not paper over it in the test.
- Fixing needs a new dependency or a config change shared by the whole suite → ask first.

## Out of scope

CI pipeline YAML, visual regression suites, test strategy decisions.
