# Repository Audit — Core Detection Reference

This reference defines what to look for and how to inspect it. Output format and report structure live in SKILL.md.

---

## Audit Scope: Every File, Changed or Not

The audit is whole-repository, not diff-based. Every source file is read regardless of whether it was recently modified. Files that were never touched after initial generation are often the worst offenders — they get skipped by incremental review and accumulate slop silently. Walk the tree completely.

Read **every file, end to end** — no exceptions. Do not trust any memory, index, changelog, or prior summary of "what changed" to decide what to skip; those are exactly what let the untouched files rot. Coverage is the goal: a partial pass misses the one file that mattered. If the repo is too large for one pass, work in stages (see SKILL.md), but every file gets read.

---

## How to Inspect — read and reason, not grep

The single biggest reason audits miss real bugs: searching for keywords instead of understanding code. Grepping for `TODO`, `eval`, `password`, `any`, or a list of "common mistakes" finds the easy, obvious 10% — and real defects almost never live there. They live in the interaction *between* functions, in the path the data actually takes, in the edge case the happy path skips, in the assumption one function makes that its caller violates.

So:

- **Read the logic, don't pattern-match it.** Understand what each unit is supposed to do, then check whether it actually does that under all inputs — not just whether it contains a suspicious token.
- **Trace flow across files.** Follow control and data flow from entry point through to the data layer, reading each hop. A bug is usually a mismatch between two correct-looking pieces.
- **Hunt the unhappy paths.** Empty inputs, nulls, concurrency, partial failures, ordering, re-entrancy, error branches that are never exercised. The happy path is where bugs aren't.
- Keyword search is a way to *find candidates to read*, never a substitute for reading.

---

## Detection Catalogue

### Dead Weight

Unreachable or never-referenced code that ships to production but does nothing. Look for:

- Exported functions, classes, or constants with no import site anywhere in the repo
- Imported symbols that are never used in the importing file
- Entire modules or files that nothing depends on
- Variables assigned but never read
- Conditional branches whose guard can never be true given the surrounding logic
- Commented-out code blocks left as permanent residents

A symbol is only safe to call dead if you have confirmed there is no dynamic or runtime reference to it (reflection, `eval`, string-keyed dispatch). Note those cases rather than silently marking them dead.

---

### Fake Work

Code that simulates behaviour without delivering it, inflating apparent progress.

**Mock / stub / fake implementations** — hardcoded return values, `return null`, `throw new Error("not implemented")`, or bodies consisting only of a TODO comment. These are legitimate in test files; flag them only when they appear in production paths.

**TODO explosion** — TODOs that represent deferred design decisions or missing functionality, not minor polish. A TODO in a critical path means the feature is partially shipped. Count and surface them; a large concentration is a health signal in itself.

**Hallucinated architecture** — scaffolding that was generated to look like a complete system but connects to nothing: interfaces with a single unused implementation, event buses with no subscribers, plugin registries that never load a plugin, service classes instantiated nowhere. The tell is plausible-looking structure whose wires are all dangling.

---

### Duplication

Logic that exists in more than one place and will diverge under maintenance. Look for:

- Near-identical function bodies across different files or modules
- Copy-pasted validation, transformation, or mapping logic with minor literal differences
- Multiple utilities that do the same thing under different names
- Data-shape definitions (types, schemas, interfaces) declared in parallel across modules instead of shared from a single source

When duplication spans module boundaries, note which boundary is the right home for the canonical version.

---

### AI Slop

Patterns that are the fingerprint of AI-generated code that was accepted without review.

**Generic naming** — identifiers like `handleData`, `processItem`, `doStuff`, `helper`, `utils`, `manager`, `service` (as a catch-all suffix) that carry no semantic content. A name should tell you what the thing is, not just that it exists.

**Over-engineering** — abstraction layers added in anticipation of requirements that do not exist. Strategy patterns with one strategy, factory functions producing a single type, config-driven behaviour with a single config value, plugin systems with one built-in plugin.

**Wrapper hell** — functions or classes whose entire body delegates to one other call with no transformation, validation, or error handling added. Pure pass-through wrappers that exist because an AI wanted a "service layer."

**Abstraction hell** — so many indirection layers between a call site and actual work that tracing execution requires jumping through five or more files. Indirection is warranted when it carries meaningful isolation; indirection that only adds distance is debt.

---

### Architecture

**Boundary violations** — code in one module reaching directly into the internals of another (importing from deep private paths, reading another module's database table, mutating another service's state). Detect by tracing import paths and data ownership.

**Dependency direction** — in a layered or domain-driven structure, dependencies should flow one way. Flag when a lower-level module imports from a higher-level one, or when a domain module imports from an infrastructure module in the reverse of the intended direction.

**Circular dependencies** — A imports B which imports A, whether direct or through a chain. These prevent clean module initialization, complicate testing, and are a structural smell regardless of whether the runtime tolerates them.

**Responsibility leaks** — a module doing work that belongs to a different concern: a UI component performing data persistence, a repository class containing business rules, a utility library owning application state. Identify what the module claims to be and what it actually does.

---

### Correctness & Logic

Defects in what the code actually computes — the class that keyword search never finds. Read the logic and check it against intent.

- Off-by-one, inverted or wrong conditionals, mishandled boundary values.
- Edge cases the happy path skips: empty/null/missing inputs, zero/negative, very large values, unexpected types.
- Error handling that is wrong, not just missing: swallowed exceptions, errors logged-and-continued, `catch` that hides the failure, wrong fallback values.
- Async/concurrency correctness: unawaited promises/futures, races, shared mutable state, ordering assumptions, missing cancellation, deadlocks.
- State and data transforms: incorrect mapping, lost updates, mutation of shared structures, stale reads, off-spec serialization.
- Contract mismatches: a function's assumptions that its callers violate; a return shape that consumers misread.

For each, state the input or sequence that triggers the wrong behavior — a logic finding without a triggering case is a guess.

---

### Security

Trace untrusted input from where it enters to where it acts. Security bugs live along that path, not at a keyword — read the flow.

- **Injection & untrusted input reaching a sink:** SQL/NoSQL/command/template/LDAP injection, XSS, SSRF, path traversal, unsafe deserialization, prototype pollution — anywhere external input reaches a sink without validation, escaping, or parameterization.
- **AuthN / AuthZ:** endpoints missing authentication; authorization checks missing or done at the wrong layer; **IDOR** — trusting client-supplied IDs (user_id, job_id, file path) without an ownership check; privilege-escalation paths.
- **Secrets & sensitive data:** hardcoded credentials/keys/tokens; secrets in logs or error messages; PII or tokens returned in responses or written to logs without redaction.
- **Crypto & tokens:** home-rolled or weak crypto, fast/unsalted password hashing, predictable IDs or tokens, weak randomness for security purposes.
- **Resource abuse / DoS:** missing rate limits, no request/payload size limits, unbounded loops or allocations driven by input, missing timeouts that enable exhaustion.
- **Configuration & defaults:** insecure defaults, debug mode or stack traces leaking in production, permissive CORS, disabled security headers, over-broad permissions or scopes.
- **Dependencies:** known-vulnerable, abandoned, or unpinned dependencies.

Rate each by exploitability and blast radius; the worst go in Critical.

---

### Performance

**N+1 queries** — a loop that issues a database or network call per iteration, where a single batched call would suffice. Trace call sites inside loops or `.map` / `.forEach` chains that touch I/O.

**Missing cache** — repeated identical fetches or computations within a request lifecycle or across requests where the result is stable. Look for repeated calls to the same external endpoint or expensive computation with the same inputs.

**Sequential awaits that could parallelize** — `await a(); await b();` where a and b have no data dependency on each other. These should be `Promise.all([a(), b()])` or equivalent. The cost grows linearly; parallelizing them collapses latency to the longest of the two.

**Redundant compute** — the same derived value computed multiple times in the same scope rather than computed once and reused.

**Bottleneck paths** — synchronous blocking work on a hot path (large sort, regex on every request, unindexed query in a loop). Flag and explain the impact, not just the presence.

---

### Maintainability

**File too large** — a file that mixes multiple concerns or contains hundreds of lines of logic is a navigation and merge hazard. There is no universal line-count threshold; the signal is when the file contains things that are conceptually separable. When flagging, propose a concrete split (see below).

**Function too large** — a function that requires scrolling to read, or one whose body could be narrated as "first it does X, then it does Y, then it does Z" — each clause is a candidate for extraction.

**Class doing too much** — a class with a large number of unrelated public methods, or one whose constructor receives many dependencies, is likely several responsibilities bundled together.

**Excessive nesting** — deeply nested conditionals or loops that make the control flow hard to follow at a glance. Early returns and guard clauses flatten nesting without changing behaviour.

**Poor folder layout** — files that belong together scattered across the tree; files that belong apart co-located; no clear pattern for where a new file of a given type should go. Evaluate whether the folder structure communicates the architecture.

#### Proposing Concrete Splits

When a file, function, or class is flagged for size or responsibility, the finding must include a specific proposal: which file it should split into, what each part contains, and why that boundary is the right one. "This file is too large" without a split plan is noise. "Extract the retry logic into `retry.ts`, the serialization into `codec.ts`, leaving `client.ts` as the entry point that composes them" is actionable.

---

## Comment Cleanup Pass

After the structural analysis, read every comment in every file with the question: does this comment tell a reader something the code cannot tell them?

**Keep** comments that explain why a decision was made — a non-obvious algorithm choice, a workaround for a known external constraint, a deliberate tradeoff. These carry information that cannot be recovered from the code alone.

**Remove** everything else:

- Narration that restates what the next line does in prose
- Section dividers and banners that only add visual noise
- Commented-out code
- Change history and attribution ("fixed by X on date Y")
- Back-and-forth process notes from a development session ("tried this, reverted because...")
- Placeholder or aspirational comments that describe intended future behaviour as if it is present
- TODO comments that have already been resolved but not removed

The goal is to leave the code in its cleanest possible state: every comment earns its presence by adding understanding that the code itself cannot provide.
