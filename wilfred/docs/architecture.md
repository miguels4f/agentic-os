# Wilfred architecture

Wilfred provides software engineering and Agent Foundry through one installed
Python package. `cli.py` publishes the command interface; it does not authorize
actions from model claims. The historical `hyperwilfred_core` import path is kept
for compatibility and provenance.

## Software engineering

`engine.py` persists the run, scopes implementation packets and collects outcomes.
`guardrails.py` accepts or rejects changes based on their declared scope.
`regression.py` preserves meaningful baseline witnesses. `quality.py` gathers
independent reviews; its decision cannot replace requirement-level evidence.
Build, improve and review retain their packaged step instructions.

## Agent Foundry

The lifecycle remains Frame -> Classify -> Scout -> Blueprint -> Contract ->
Generate -> Evaluate -> Repair -> Package. Intake resolves material uncertainty
before choosing architecture. Explicit task-family contracts link the actual
inputs and outputs to the generated implementation and evaluation scenarios.

The deterministic family parses notes into structured proposed actions. Agent
families use bounded model/tool requests and a separately controlled host provider.
Separate agent contexts and observable handoffs must justify multiple agents.
Provider fixtures exercise control flow; live execution has separate evidence.

Generated source and configuration are versioned together. Scenario evidence binds
the executed source, manifest, inputs and outputs. A repair publishes only after its
failed and regression scenarios pass. Publication uses immutable artifacts with an
atomic state pointer, so incomplete staging cannot destroy accepted prior evidence.

## Authorization boundary

Generated programs are untrusted workers. Operating-system isolation limits their
filesystem, network and process access. Host model access and approval verification
stay outside that boundary; secrets and signing authority are not worker inputs.
Both product capabilities must use the shared approval verifier for protected
actions. A model can propose an action but cannot authorize it by emitting JSON.

This is host-side enforcement: generated runners expose neither arbitrary protected effects
nor a configurable human-gate workflow. Their adapter rejects specifications with arbitrary
transitions, human gates or custom tools, even if a deferral is supplied. Only the three bounded
families with a single terminal result state are executable; agent lookups are restricted to the
supplied documents. R6 evidence must distinguish an actually blocked host callback from separately
observed worker isolation. The demo's positive signature is an ephemeral test, not human consent.

An unrestricted host-user process is trusted infrastructure, not a protected worker.
The product does not claim to resist that user modifying the installed source.
Supported platforms and current observed coverage are in [verification](verification.md).

Security and data references: [threat model and privileged-route registry](threat-model.md); [canonical domain and event model](domain-model.md).
