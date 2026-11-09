# Rust Backend Review Pack

This is the Rust backend review reference for the slopbuster-audit skill, covering HTTP APIs, WebSockets, jobs/workers/queues, infra/resource governance, security, observability, and agent-specific concerns.

---

## 0) Mandatory Review Mindset

Before reading a single line of code, the reviewer must do three things:

**1. Reconstruct System Intent**
- What feature or module is this change adding or modifying?
- Is it agent-based, non-agent, or hybrid?
- Is it sync (HTTP), streaming (WebSocket), or async (job/worker)?

**2. Map the Change to Architecture**
Identify which layers and crates are involved:
- `api_server` — transport layer only
- `modules` — domain logic
- `services` — cross-module reusable services
- `clients` — external call wrappers
- `infra` — workers, queues, DB, security primitives

Confirm each piece of logic is in the correct crate.

**3. Assume the System Will Grow**
- More modules and workers will be added
- CPU/GPU workloads will coexist
- External services (LLM, DB, vector DB) will partially fail
- Inputs will be malformed or adversarial

Code that only works on the happy path is a review failure.

---

## 1) Review Order

Complete these steps in strict sequence — do not skip:

1. Architecture Placement Review
2. Dependency & Crate Import Review
3. Responsibility & Boundary Review
4. API (HTTP) Review
5. WebSocket Review (if applicable)
6. Job / Worker / Queue Review (if applicable)
7. Infra & Resource Governance Review
8. Timeout / Retry / Failure Review
9. Security Review
10. Observability Review
11. Agent-Specific Review (if applicable)
12. Final Scalability & Maintainability Judgment

---

## 2) Architecture Placement Review (Hard Gate)

For every new or modified file, answer: is this file in the correct crate/layer?

| Crate/Layer | Allowed Contents |
|---|---|
| `api_server` | Transport wiring and routing only |
| `modules` | Domain logic |
| `services` | Cross-module reusable services |
| `clients` | External call wrappers |
| `infra` | Workers, queues, DB, security primitives |
| `utils` | Pure, stateless helpers |
| `bin` / scripts | Entry points only — must not be imported by libraries |

### Immediate Reject Conditions

Reject the PR immediately if any of the following are true:

- A handler or router calls a client directly without going through a service
- A `modules` crate depends on the `api_server` crate (transport coupling)
- Business logic appears in `bin/main.rs`
- Worker logic appears in `api_server`
- A runtime library imports from `bin` / scripts

---

## 3) Dependency & Crate Import Review

### Required Dependency Direction

Only this import graph is permitted:

```
api_server -> services -> modules -> clients
infra -> modules / services / clients
utils / config -> anywhere
bin -> links everything (never imported in reverse)
```

### Red Flags

- Circular crate dependencies
- A `modules` crate directly importing another module's internals rather than its public service API
- `env::var` calls outside `config` / `settings`
- Heavy side effects in `lib.rs` at startup (e.g., connecting to DB or loading a model during crate initialization)

The reviewer must state one of:
- "Import graph is valid"
- "Import graph violation found: ..."

---

## 4) Responsibility & Boundary Review

For every public function and struct, answer:

1. What is its single responsibility?
2. Which layer does it belong to: transport, domain, or infra?
3. Is there any responsibility leak across boundaries?

Common violations:
- A handler enforcing business rules
- A service parsing raw HTTP or WebSocket frames
- A module performing auth-token parsing
- A client deciding its own retry policy (inconsistent with the rest of the system)

If a single function carries responsibilities from more than one architectural layer, request a refactor.

---

## 5) API (HTTP) Review

Mandatory rules:
- Input validation uses transport DTOs (serde + a validation layer); unknown fields are rejected by default
- Responses are wrapped in a standard response envelope
- Handlers must not perform blocking CPU or GPU work
- Domain errors map to HTTP status codes in one centralized place
- `request_id` must propagate through services and clients

### Reject Conditions

- Handler uses raw maps/dicts instead of typed schemas
- Blocking work runs directly in a handler (missing `spawn_blocking` or job dispatch)
- Silent error swallowing (`Result`/`Err` discarded without logging or propagation)

---

## 6) WebSocket Review

WebSocket-specific rules:
- Authentication (token verification) happens at connect time
- Handlers do not perform heavy computation
- Handlers only subscribe to a stream service or pubsub and forward events
- Disconnect cleanup and heartbeat (ping/pong) are implemented
- Backpressure strategy is defined (channel buffer limits, slow-consumer handling)

### Reject Conditions

- A WebSocket handler calls an external API or LLM directly
- A long loop inside a handler has no `await` points
- No disconnect cleanup or message size limit
- Channel access control is missing (`job_id` / `session_id` ownership not verified)

---

## 7) Jobs / Workers / Queue Review

The required pattern for any long-running task:

1. API receives request → enqueues job → returns `job_id`
2. Worker executes the job
3. Progress, logs, and output stream over WebSocket

### Checklist

- Job definitions live in `modules/jobs/definitions`
- Task handlers live in `infra/queue/tasks`
- Job state is persisted and recoverable across restarts
- Retry is bounded; a cancellation path exists
- Job routing correctly declares `requires_gpu` and `expected_runtime`

### Reject Conditions

- An API server executes a long-running task inline
- GPU-heavy logic runs in the HTTP process
- A job has no timeout, no cancel path, or unbounded retries

---

## 8) Infra & Resource Governance Review

**CPU:** CPU-bound work must not block the Tokio reactor. Use `spawn_blocking` or a dedicated process pool.

**GPU:**
- If Rust manages GPUs: one process per card, or an explicit sharing strategy is documented.
- If GPU workloads run in Python: the Rust side must treat the Python model server as an external inference service, governed by timeouts and circuit breakers. It must not behave as if the GPU is local.

**Networking:** connection pooling, per-call timeouts, and circuit breakers / failure isolation must all be present.

The reviewer must state one of:
- "Resource governance acceptable"
- A list of specific risks and recommended mitigations

---

## 9) Timeout / Retry / Failure Review

The reviewer must enumerate:
- Every external call (LLM, Python model server, DB, vector DB, cache, third-party APIs)
- The timeout configuration for each (connect, read, total)
- The retry policy (idempotent operations only?)
- The failure handling strategy (fallback, graceful degradation, cancellation, quarantine)

### Reject Conditions

- Any external call without a timeout
- Any retry loop that can run indefinitely
- Failures that can cascade (e.g., stalling a worker or blocking the event loop)

---

## 10) Security Review

### Checklist

- Authentication enforced on all HTTP and WebSocket endpoints
- Authorization checked at the correct layer (router guard or service boundary — not inside domain logic)
- Rate limiting applied to HTTP and WebSocket
- Input validation is strict; unknown fields are rejected
- Secrets and tokens are never written to logs (redaction in place)
- Sensitive actions produce audit log entries

**WebSocket-specific:**
- Token verified on connect
- Channel access controlled by verified `job_id` / `session_id`
- Message size limit and heartbeat configured

### Reject Conditions

- Trusting a client-supplied `user_id`, `job_id`, or `session_id` without server-side verification
- Logging tokens or secrets
- WebSocket endpoints without authentication

---

## 11) Observability Review

Mandatory requirements:
- Structured (JSON) logs
- `request_id` and `job_id` propagated through the entire call chain
- Meaningful error logs that include external-call latency and timeout context
- No noisy debug logs on production code paths

The reviewer must be able to answer "yes" to: if this system breaks at 3 AM, can we pinpoint which external call stalled and which `job_id` was affected using only the logs and traces?

If the answer is no, request improvements before approving.

---

## 12) Agent-Specific Review

Apply this section only when the changed module uses agents.

### Checklist

- All tools are registered through the tool registry with complete metadata
- Prompts, personas, and context are versioned and isolated
- Each persona has an explicit tool policy (unrestricted tool access is not permitted)
- The context builder is deterministic and its output can be explained

### Reject Conditions

- Ad-hoc tool calls with no registry metadata
- Prompt logic scattered across multiple files or layers
- An agent can directly call infra or clients, bypassing policy or timeout enforcement

---

## 13) Scalability & Future-Proofing Judgment

The reviewer must explicitly answer all four questions:

1. Is this safe at 10x current traffic?
2. Does this coexist with other modules without coupling?
3. Is it straightforward to split this into a separate machine or service in the future?
4. Can agents extend this in the future without breaking existing rules?

Any "no" answer requires a written explanation and a concrete remediation path.

---

## 14) Absolute Reject Conditions

Stop review immediately and issue a REJECT verdict if any of the following are present:

- Security bypass of any kind
- Blocking CPU or GPU work in the API server process
- Any external call without a timeout
- Cross-layer dependency violations
- Long-running tasks executed inline without a job/worker

---

## 15) Closing Standard

A review is not complete until the reviewer can honestly state:

> "This code remains safe, maintainable, and extensible at 5x system scale, distributed across multiple machines, with portions operated by agents."

If that statement cannot be made, the review fails.
