---
shaping: true
---

# Model backends, conversational retrieval and bounded orchestration — frame

## Source

> el workflow que crea agentes, debe contemplar crear un agente con modelos open source, instalados en local, así como usar el claude sdk para api de claude o codex. tambien tiene que poder montar un RAG en conversación con el usuario cuando tenga sentido crearlo. igual tiene sentido lanzarlo en otra sesion. este seríami prompt, pero mejoralo y damelo para que lo copie. 'Build an AI assistant that searches documents, finds acurate answers, verifies them, and cites sources.'

> me gustaría que integrasemos en el workflow que crea agentes autonomos la posibilidad de integrar un agente como Assist, diseñando workflows adaptados a Assist para dirigir a los otros modelos y ahorrar tokens y reducir latencia

## Problem and outcome

The factory must help the user choose and actually execute suitable model backends and document
retrieval, not merely emit provider names or prompts. A generated document assistant must search
an approved corpus, ground claims in retrievable source passages, verify citations and abstain
when evidence is absent or contradictory. Configuration is negotiated in the interview.

Assist is requested as an optional bounded routing decision layer, not as an automatic authority
or a presumed saving. Total task token use, cost and latency must include routing, retrieval,
verification, retries and fallback. A smaller routing call can still make the entire task slower.

## Existing work to reuse, not overclaim

The existing design source is
the original shaping document (private, not included),
especially R0, R4, R6, R7 and C4/C7. Preserve its identifiers when evolving the design.
Read the adjacent `spike-assist-decision-layer.md`; its product claims need current primary-source
verification before implementation, not repetition as established fact.

Current `foundry_agents.py` provides a live Codex CLI adapter and an explicit test fixture.
It does not establish Claude SDK, Codex SDK or local-model support. These are distinct adapters;
do not assume Anthropic's SDK runs Codex. Determine supported official interfaces separately.
Current generated agents look up supplied documents; this does not establish a persistent RAG
index with ingestion, versioning, incremental updates and access-control-aware retrieval.

`foundry_runtime.py` already declares `JevLane`, `decide_bounded` and `benchmark_assist`.
These are extension contracts/helpers, not an implemented live Assist integration. The current
benchmark defaults allow cost/latency ratios up to 1.5; passing that helper does not prove a
reduction. A new savings claim needs explicit stricter targets and paired, task-matched evidence.

## Required design investigations

- Adaptive interview: corpus, file formats, updates, sensitivity, users/access, target quality,
  latency, hardware and budget; ask high-impact unanswered questions, not a fixed survey.
- Select direct context, simple search or RAG with a stated reason. Include a small-corpus case
  where RAG is unnecessary. Do not add a vector database merely because the request says AI.
- Local inference: inspect hardware and installed runtimes/models; verify license (open weights
  is not automatically open source), storage, memory and quantization. No silent cloud fallback
  for local-only/private data. Surface missing model/download/credential requirements precisely.
- Official Claude and Codex interfaces: research current SDK/API/runtime/authentication contracts
  separately. Preserve shared timeouts, cancellation, usage accounting, permissions and evidence.
- Retrieval: versioned ingestion/chunks/index, provenance and page/section spans, authorized-only
  search, updated/deleted documents, grounded answers, contradiction handling and abstention.
- Optional Assist: inspect actual current provider/API/license; route only among approved choices.
  Keep deterministic rules first and human authority outside every model. Low confidence,
  invalid output or outage must use an explicit policy without changing privacy or permissions.
- Paired benchmarks with identical cases and quality floors; report end-to-end token use, cost,
  p50/p95 latency, accuracy/citation support, errors and fallback rates. Distinguish measured
  provider tokens from estimates across different tokenizers. Include routing overhead and
  warm/cold local inference. Keep Assist off when evidence is absent or targets fail.

## Acceptance and boundaries

This is a frame for a new implementation, not a selected shape or completed feature. Complete
the current base review first. Then use Wilfred shaping, scoped implementation and independent
review. Demonstrate actual local inference, actual Claude and Codex paths when authorized and
available, real ingestion/retrieval/citation verification, and Assist-on versus Assist-off behavior.
Fixture execution cannot substitute for any missing live path. If a provider or review is blocked,
keep that exact requirement open; do not claim complete integration.

Retain the mandatory six criteria: justified architecture, clean installation and real execution,
executed version-bound evaluation, real source repair preserving accepted components, interrupted
write recovery with old evidence, and externally authorized protected actions. No publication,
deployment, historical-repository edits or publishing integration is authorized here.
