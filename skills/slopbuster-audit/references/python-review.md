# Python Backend Review Pack

Framework-agnostic review criteria for Python backends (FastAPI / Flask / Django / Litestar / Quart): layering, async correctness, external-call resilience, security, and observability.

---

## Review Mindset

Before touching any checklist, establish:

- **System intent** — what is being added or changed? Is the request path synchronous (HTTP), streaming (WebSocket/SSE), or deferred (job/worker)? Are AI agents involved?
- **Layer map** — which layers does the change touch (transport, domain/services, data access, external clients)? Is each responsibility in the right layer?
- **Growth assumption** — the review should hold under higher traffic, partial external failures, and malformed/malicious input. Code that only works on the happy path fails the review.

Be proportionate: judge against the project's own conventions and framework idioms, not a single mandated tree. The principles below are universal; the directory names are illustrative.

---

## Review Order

Work through the sections below in sequence.

1. Architecture Placement
2. Dependency & Import Direction
3. Responsibility & Boundary
4. API (HTTP)
5. Async Correctness
6. External Calls — Timeout / Retry / Failure
7. Input Validation
8. Security
9. Observability
10. Streaming (if present)
11. Jobs / Workers / Queues (if present)
12. Agent-Specific (if agents are involved)
13. Scalability Judgment

---

## 1. Architecture Placement

The goal is **thin transport, thick domain**: HTTP/WS handlers wire requests and shape responses; the real logic lives in domain/service code that has no knowledge of the transport.

Map the change onto whatever conventional layout the project uses. Common shapes:

- **FastAPI / Litestar style** — `app/` (or `src/<package>/`) with `api/` or `routers/` (transport), `core/` (config, settings, db wiring), `models/` (ORM/domain models), `schemas/` (request/response DTOs), `services/` or `crud/` (domain logic), `db/` (engine/session).
- **`src/<package>/` style** — a single importable package with submodules per concern.
- **Django style** — apps, each with `views`, `models`, `serializers`/`forms`, `services`, `urls`.

The DB engine/session belongs in a conventional spot such as `app/db/`, `core/db.py`, or a Django app's `models`/settings — not a bespoke `infra/database/` tree.

**Reject if any of the following:**
- A transport handler (router/view) carries substantial business logic instead of delegating to a service/domain layer
- Domain or service code imports the transport layer (e.g. importing the FastAPI app, `request`/`Response` objects, routers)
- External-service call logic is duplicated inline in handlers instead of living behind a client/gateway
- Human-run scripts or one-off tooling are imported by runtime code

---

## 2. Dependency & Import Direction

Imports should flow one direction: **transport → domain/services → data access / external clients**. Config and pure utilities may be imported anywhere.

**Red flags:**
- Circular imports across layers (or modules that only resolve via deferred/local imports to dodge a cycle)
- A domain module reaching into another module's internals rather than going through its public interface
- Raw `os.environ` / `os.getenv` scattered through the codebase instead of a single config/settings module

State explicitly: *"Import direction is valid"* or *"Import-direction violation: …"*

---

## 3. Responsibility & Boundary

For each function or class in the diff, establish its single responsibility and confirm it sits in exactly one layer.

**Common violations:**
- Router/view enforcing business rules that belong in a service
- Service code parsing raw HTTP request objects or building HTTP responses
- Domain code performing auth/token parsing
- An external client making inconsistent ad-hoc retry/timeout decisions per call site

If a unit has more than one architectural responsibility, request a refactor.

---

## 4. API (HTTP) Review

**Rules:**
- Input parsed/validated via the framework's schema layer (Pydantic models, DRF serializers, dataclasses, marshmallow) — not raw dicts read field-by-field
- Response shape consistent with the project's existing convention
- No blocking calls inside an async handler (see §5)
- Domain errors mapped to explicit, appropriate HTTP status codes

**Checklist:**
- Unknown/extra fields handled per project policy (rejected or ignored deliberately, not silently trusted)?
- Error responses safe — no stack traces, no secrets, no internal detail leaked to clients?
- A correlation/request id propagated where the project supports it?

**Reject if:**
- Raw unvalidated request data flows into domain logic
- Exceptions are swallowed silently (bare `except: pass`)
- Handlers return inconsistent ad-hoc shapes for the same logical endpoint

---

## 5. Async Correctness

Only relevant for async frameworks/handlers (FastAPI async routes, Litestar, Quart, asyncio workers). Sync WSGI apps (Flask, Django sync views) skip the event-loop concerns but still need bounded thread/connection use.

**Reject / red flags:**
- Blocking I/O (sync DB driver, `requests`, `time.sleep`, file/disk, subprocess) called directly inside an `async def` handler without offloading (`run_in_executor`, `anyio.to_thread`, an async driver, or a worker)
- CPU-bound work run inline on the event loop instead of a thread/process pool or worker
- Coroutines created but never awaited (a missing `await`), or fire-and-forget tasks with no reference held and no error handling (silent task GC / swallowed exceptions)
- Unbounded concurrency — fan-out that spawns one task per item with no semaphore/batching limit
- Shared mutable state mutated across `await` points without protection

State explicitly: *"Async usage is correct"* or list the hazards found.

---

## 6. External Calls — Timeout / Retry / Failure

For every outbound call in the diff (HTTP, DB, cache, queue, third-party SDK, another internal service), identify:
- **Timeout** — a connect/read timeout is configured; no call can hang indefinitely
- **Retry** — bounded attempts, with backoff where appropriate; retries restricted to idempotent operations
- **Failure handling** — errors are propagated, surfaced, or handled with an explicit fallback; failures are isolated so one slow dependency can't cascade

Connection pooling / reusable clients should be used rather than constructing a new client per call.

**Reject if:**
- Any external call has no timeout
- A retry loop is unbounded or can retry a non-idempotent write
- A failure can block or cascade indefinitely

---

## 7. Input Validation

- Untrusted input (request bodies, query/path params, headers, uploaded files, webhook payloads) is validated and coerced at the boundary before reaching domain logic
- Validation is strict about types and constraints, not "parse and hope"
- File uploads bound size and content type; pagination params are bounded
- Validated, typed objects (not raw dicts) cross the boundary into the domain

**Reject if** raw client input flows unchecked into queries, file paths, shell/subprocess calls, or deserialization.

---

## 8. Security

**Checklist:**
- Authentication enforced on all protected routes?
- Authorization checked in the domain/service layer (resource-level), not assumed from the transport layer alone?
- Client-provided identifiers (user id, tenant id, object id) verified against the authenticated principal — never trusted as-is?
- Secrets loaded from config/env, never hardcoded, never logged?
- Injection avoided — parameterized queries / ORM (no string-built SQL), no shell injection, safe deserialization?
- Rate limiting / abuse protection applied where the endpoint warrants it?
- Sensitive actions audit-logged where the project requires it?

**Reject if:**
- A security check is bypassed or missing on a protected path
- Client-provided IDs are trusted without an ownership/authorization check
- Secrets or tokens appear in logs or error responses
- Raw input is concatenated into SQL, shell commands, or file paths

---

## 9. Observability

**Required signals:**
- Structured logs at meaningful boundaries (request start/finish, external-call failures, domain errors)
- A correlation id (request id / job id / trace id) propagated through the call chain where supported
- Error logs carry enough context (operation, identifiers, cause) to diagnose without a debugger
- No noisy debug logging or secret-bearing payloads left in production paths

Ask: *"If this fails at 3 am, can the root cause be diagnosed from logs alone?"* If not, request improvements.

---

## 10. Streaming (OPTIONAL — only if WebSocket / SSE is present)

Review this section only when the change touches streaming. A service is **not** rejected for lacking streaming.

**Rules:**
- Authentication verified on connect (token validated, channel/topic access authorized)
- No heavy compute in the stream handler — long loops must `await` and yield
- Disconnects, heartbeats, and cleanup handled; resources released on drop

**Checklist:**
- Used for streaming/progress, not as a backdoor RPC channel?
- Messages typed/tagged (e.g. `progress` / `chunk` / `done` / `error`)?
- Backpressure and message-size limits considered?

**Reject if** the handler runs long loops without awaiting, never cleans up on disconnect, or skips connect-time auth.

---

## 11. Jobs / Workers / Queues (OPTIONAL — only if present)

Review this section only when the change introduces or touches background work (Celery, RQ, arq, Dramatiq, APScheduler, cron, custom workers). Do **not** reject a service for handling work inline when that work is genuinely short.

**Pattern for genuinely long-running tasks:**
1. The request enqueues the job and returns a handle (e.g. a job id)
2. A worker executes the job out of the request path
3. Progress/result is retrievable (poll or stream)

**Checklist:**
- Job state persisted and recoverable across restart?
- Retries bounded and explicit; dead-letter / give-up path defined?
- Each task has a timeout and a cancellation path?
- Worker resource use bounded (concurrency, memory, any GPU/CPU pinning) when relevant?

**Reject if:**
- A genuinely long-running or unbounded task is executed inline in the request path
- A task has no timeout and no cancellation
- A retry path can loop indefinitely

---

## 12. Agent-Specific Review (OPTIONAL — only if AI agents are involved)

Apply when the change wires LLM agents, tools, or prompts.

**Checklist:**
- Tools registered through a central registry rather than ad-hoc per call site?
- Prompts versioned and isolated from runtime control flow, not scattered inline across files?
- Agent capabilities constrained — no unrestricted tool/data access beyond what the task needs?
- Context/prompt assembly deterministic and auditable?
- External model calls covered by the §6 timeout/retry/failure rules?

**Reject if:**
- An agent can reach data-access or external-client layers directly, bypassing the domain boundary
- Prompt logic is duplicated/scattered with no single source of truth
- Tools are invoked without the metadata/validation the registry would enforce

---

## 13. Scalability Judgment

Answer explicitly:

1. Can this run safely under significantly higher traffic?
2. Can it coexist with other modules without tight coupling?
3. Can the deferred/heavy work be moved to a separate worker or service later without a rewrite?

If any answer is "no", explain why and propose a concrete fix.

---

## Absolute Reject Conditions

Immediate rejection — no exceptions — if any of the following are found:

- A security bypass or missing auth/authz on a protected path
- Blocking I/O or CPU-bound work on the async event loop with no offload
- An external call with no timeout
- A cross-layer import violation (transport logic in domain, or domain importing transport)
- A genuinely long-running task executed inline in the request path with no job/worker option

---

## Closing Standard

The review is not complete until the reviewer can honestly state:

> "This code will still be safe, maintainable, and extensible as the system grows, under partial failure, and against hostile input — regardless of which Python framework it runs on."
