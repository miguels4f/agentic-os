---
name: local-first-mvp
description: Use when the user wants a project's MVP to run on local, open-source models on their own machine to avoid paying for model APIs during development — "plan the MVP with local models", "can we run X with a local model?", "audit this project for local models", "which local models do we have?", "rerun the test with the local model", "compare small local models to cut inference cost". Do NOT use to design the production or server deployment (decided later, once the MVP is polished), or for a plain code audit with no cost or model question.
---

# Local-first MVP

Goal: decide, with measurements, which parts of a project can run free on the user's machine and
which still need a paid model, without locking the project to any provider.

## Fixed frame (do not ask again)

- Motive: cost during MVP development. Server hosting or a paid API is decided later, once the MVP
  is polished; keep that switch cheap with a provider-agnostic adapter (one interface, one config
  value to change the backend).
- Hardware: your machine. Check its RAM / unified memory and GPU once and write it down; every model
  choice must fit with room left for the app. Ollama and LM Studio are the local runtimes.
- Paid models only for the final, human-approved output, behind a spend gate (for example: cheap
  drafts or previews are approved by a person before any expensive generation runs).
- Models propose; deterministic code decides. Go/no-go decisions and spend gates stay in code,
  never in a model's answer.

## Steps

1. **Inventory before downloading.** Run `ollama list` and `ollama ps`, check LM Studio's model list
   and free disk (`df -h ~`). Download a model only when no installed one fits, and say how many GB
   it takes before downloading: disks fill up quickly.

2. **Map the AI steps from the code** (code is truth): for each step write its input, output, how
   often it runs, the quality bar and whether the data is sensitive.

3. **Put every step in one of four lanes**, cheapest first:
   1. Deterministic code: rules, parsers, schemas, validators, gates. This is the default.
   2. Bounded decision (classify, route, yes/no, score): a small local classifier model, or a small
      instruction model constrained to a fixed label set. Use a cheap hosted model only when the
      data may leave the machine and the local candidates fail the evaluation.
   3. Local generative model with a harness: retrieval over the project's own reference documents,
      a structured output schema with a validator, retries, short prompts, guardrails.
   4. Paid model: only the final approved output, after a spend gate and the user's approval.

4. **Choose at most two local candidates per lane** that fit in your machine's memory with room
   left for the app; note size, context length and speed.

5. **Measure.** Build a small evaluation set from the project's real cases with known answers (in a
   regulated domain, write the questions from the reference sources). Run each candidate and record
   accuracy, latency and cost per step; tag each model call with its step so you can count calls and
   cost per step afterwards. Run heavy evaluations when the machine is free (overnight or early
   morning; a scheduled run only with the user's OK — see `automation-governance`). Before starting,
   check that the local runtime is up and that no other evaluation is running.

6. **Write the deliverables in English** in the project repo:
   - a dated assessment such as `docs/audits/<YYYY-MM-DD>-local-models-assessment.md`: lanes table,
     chosen models, evaluation results, risks;
   - a dated plan such as `docs/plans/<YYYY-MM-DD>-local-first-mvp.md`: phases with exit
     criteria;
   - an ADR when the boundary between deterministic code and models is new for this project;
   - one simple architecture diagram: local frontend, backend, local models, and the paid API only
     at the final step.

7. **Summarise for the user in plain language (in the user's language)**: what runs free on the
   machine, what still costs money and when, the quality risk of small models and how the harness
   offsets it, and an estimated monthly cost during the MVP. Offer a Phase 1 prompt for a fresh
   session (`handoff-prompt`).
