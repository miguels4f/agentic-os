# Limits and roadmap

## Current limits

- Three bounded task families ship; unknown objectives are blocked rather than approximated.
- Generated execution requires macOS isolation; other operating systems fail closed.
- Live agents require an explicit authenticated provider. Fixture execution is not model evidence.
- Model execution and content review do not prove unrestricted semantic quality or human approval.
- Repair fixes the known seeded empty-input source defect, not arbitrary broken programs.
- Atomic content-addressed versions survive tested process crashes, not concurrent host writers.
- Human signing keys are external; generated agents receive no private key or host credentials.
- Arbitrary transitions, generated human gates and custom tools are rejected even with deferral;
  protected-action authorization is enforced by a separate trusted-host callback API.
- The software-engineering engine and Agent Foundry share a CLI but are separate lifecycles.
- No public license, hosted service, security certification, or support promise exists.

## Roadmap, not commitments

Candidates include additional reviewed model adapters and OS isolation backends, more task families,
durable memory, online scouting, deployment, and schema migration. Each needs its own
security, cost, and quality evidence before moving from “optional” to “proven.”

No essential acceptance failure may be moved to this roadmap to declare delivery complete.
See [known limitations](known-limitations.md) and [six criteria](acceptance.md) for the current verdict.
