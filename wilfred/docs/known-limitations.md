# Known limitations and essential gaps

[Acceptance](acceptance.md) records essential requirements and observed status.
An unmet essential requirement is an incomplete delivery, never a roadmap item.

Runtime `309ee570` implements corrections for both iteration-1 findings: step-completion
ownership and damaged approval metadata. [Fresh execution evidence](evidence/2026-09-23-authority/)
is available; [independent requirement contrast](evidence/2026-09-23-authority/independent/requirements-review.json)
demonstrates all six requirements for the bounded local product. Global quality/verification
remain pending. These corrections were essential work, not features deferred to the roadmap.

The concrete external blocker is reviewer account usage: two final independent reports
(security and maintainability) are missing. New local-model, Claude/Codex SDK, conversational
RAG and live Assist integration requests are [captured separately](agent-foundry/extensions-frame.md)
and are not implemented or proven by this candidate. No savings claim is made for Assist.

The [independent prior-runtime review](evidence/2026-09-23-final/independent/final-acceptance-reprobe.json)
demonstrates R1–R6 for runtime `7000d4e`, after correction of the seven original findings and
the subsequently discovered multiple-gate continuation defect. Overall local delivery still
awaits the final global quality/verification records. No runtime defect is silently deferred:
the old failing observations and the corrected version's actual re-probes remain available.

Historical context: for runtime
`60ca5cf092e9bd5572ea6368e209051576debd2f`, the [bounded acceptance review](remediation/independent-review.md)
demonstrated R1–R6 in its observed cases. Broader independent quality probes subsequently
found seven major defects, including incompatible executable contracts and stale authorization.
The [bounded correction record](remediation/final-hardening.md) identifies every required fix;
none is deferred to the roadmap or waived by the earlier demonstrations.
The following boundaries describe the implemented product; they do not waive any failed essential
criterion or authorize deferring it until later.

## Supported tasks and evaluation

Support is bounded to explicit task families. Unsupported goals must stop with an
actionable error. Generic arbitrary-agent generation is not claimed. Fixtures are
not live model execution; live calls require a configured provider and carry its
ordinary usage and service-availability constraints.

The three task families are explicit action extraction, evidence-grounded answers and a reviewed
brief. The deterministic classifier recognizes supported objective patterns or requires an explicit
supported choice; it is not a general architecture optimizer for arbitrary businesses. Writer and
reviewer use separate contexts but can use the same underlying model, so their agreement is not
external certification. Their content review is distinct from the independent delivery review.

Scenario evaluation checks bounded known cases and conservative whole-answer contracts. Correct
unrecognized paraphrases can fail closed; the scenario format is stated explicitly without giving
the answer. This is not a general semantic evaluator. Source repair fixes the seeded empty-input
defect and preserves accepted components; arbitrary new defects require scoped engineering work.

## Runtime and authorization

The local adapter does not implement arbitrary state transitions, generated human gates or
custom component tools. Such specifications are blocked, including deferral attempts. Signed
approval protects trusted-host callbacks outside the generated worker; it does not establish a
general generated approval workflow. R6 review must inspect the blocked host action and the
worker-isolation evidence separately, without treating a test signature as human consent.

An unrestricted host-user process can edit local code and state. Authorization must
separate generated execution from host signing authority. Labels, bare files and
self-declared authors are not authentication. Unavailable operating-system isolation
must block execution, not silently downgrade.

Generated execution currently requires local macOS `/usr/bin/sandbox-exec`; Linux and Windows
have no supported isolation backend and do not run workers unsandboxed. Host provider calls are
outside the worker sandbox and must remain controlled by the operator. Authentication and private
human signing keys stay on the trusted host, outside worker reach; no production signing key is
created by the demonstration. Model calls can incur account usage and fail due to service limits.

Approval consumption and renewal require POSIX file-lock support; an unsupported platform
fails closed at that operation without preventing software-only CLI imports. This delivery
was exercised on macOS, not certified across operating systems.

R6 proves a negative invariant over the actual host callback and isolated worker: no valid external
signature means no protected callback execution, and the worker cannot substitute a self-claim.
There is no arbitrary generated external-effect or human-gate API. The positive signature fixture
tests verification and replay prevention only; no real human authorization, publication or deploy
has occurred. An uncertain callback attempt remains recorded rather than being silently retried.

## Persistence and distribution

Immutable content-addressed artifacts, prior state revisions and code versions are retained.
Recovery tests kill real processes during partial writes and publication boundaries, including
repair. They do not prove multi-writer safety, physical power-loss recovery, filesystem corruption
tolerance, remote replication or a durable distributed service. Concurrent writers for one project
are not supported. Schema changes require explicit compatibility/migration work, not inference.

Assist remains optional and disabled. publishing integration, publication, deployment, durable
cross-session model memory and a web interface are outside this delivery. The public
license decision remains with the repository owner.

No hosted service, public release, support commitment or security certification is claimed.
The public evidence archive contains synthetic inputs and outputs; it is not permission to publish
real client material. Selecting a license and authorizing external publication are owner decisions,
separate from local technical completion.
