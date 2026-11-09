---
name: annotated-system-map
description: Inspect an existing software project or completed build, reconstruct how its important parts work and interact, and create plain-language annotated SVG illustrations plus a written walkthrough. Use after a substantial build, for architecture explanations, onboarding, agent handoffs, harness/workflow analysis, or whenever a non-technical reader needs to understand both the system journey and its implementation contracts. Also use when someone wants a navigable HTML page explaining a system's architecture and the reasons behind its design decisions, for example to prepare a technical interview, an onboarding or a due-diligence review. Do not use for speculative architecture that has not been implemented unless the user explicitly asks for a proposed design.
---

# Annotated System Map

Turn a real codebase into a trustworthy visual explanation that a smart non-engineer can follow.

## Outcome

Produce three artifacts in the project unless the user chooses another location:

- `docs/system-map.svg`: a simple hand-drawn-style overview for orientation.
- `docs/system-map-details.svg`: a deeper architecture map when the system has meaningful internal contracts, agent handoffs, permissions, or guardrails.
- `docs/system-map.md`: a companion explaining the journey, architecture matrix, important branches, uncertainty, and evidence.

For a genuinely simple system, the overview and detail map may be the same SVG. For an agentic harness, workflow engine, distributed system, or multi-stage pipeline, always create both levels.

If the repository already has a documentation convention, follow it instead of forcing `docs/`. Do not overwrite an existing system map without preserving or updating it deliberately.

When the request is for a navigable HTML page, or for the reasons behind design choices, the deliverable is instead one self-contained `.html` file: see **HTML explainer mode** below.

## Investigate before drawing

1. Read the project's instructions and existing architecture or product documentation.
2. Identify the user-visible entry points and the primary journey the user wants explained. If no journey is specified, choose the most important end-to-end flow and state that choice.
3. Trace the flow through the code and configuration. Follow concrete calls, events, data transformations, storage, external services, and returned results.
4. Keep a compact evidence ledger: each important claim should point to a real file, symbol, route, configuration item, or test.
5. Distinguish three things explicitly:
   - **Observed:** verified in code or configuration.
   - **Inferred:** strongly implied but not directly demonstrated.
   - **Unknown:** cannot be established from the available project.

Do not present filenames as architecture. Convert implementation details into responsibilities and outcomes, then use filenames only as supporting evidence.

## Trace architecture contracts

For every meaningful block, establish these fields from evidence:

- **Executor:** the code module, runtime adapter, skill, step instruction, human, or external service that performs the work. Do not call everything a skill; name the actual mechanism.
- **Input contract:** the minimum structured information it receives, including schema or artifact names when present.
- **Output contract:** the artifact, state transition, result schema, or decision it must produce.
- **Restrictions:** allowed tools or paths, blocked operations, budgets, sandbox boundaries, approval requirements, and failure behavior.
- **Handoff:** what event triggers the next block, where the payload is stored, who validates it, and what happens when validation fails.
- **Evidence:** the file and symbol, command, schema, test, or configuration that proves the claim.

Follow a representative payload from the first entry point to the final outcome. A list of components is not enough: show what crosses every boundary and who owns acceptance at that boundary.

## Choose the story

Prefer one coherent left-to-right or top-to-bottom story over a complete inventory. Show:

- who or what starts the flow;
- the main stages in the order they happen;
- where information is stored or transformed;
- important decisions, retries, or failure paths;
- external systems and trust boundaries;
- what the user receives at the end.

Keep the main map to roughly 5–9 nodes. If the system needs more, create an overview plus separate detail maps rather than shrinking everything into one unreadable canvas.

### Architecture-detail mode

Use architecture-detail mode when the user asks how a harness, agent pipeline, orchestration system, or multi-service workflow works. Keep the overview at 5–9 nodes, then make a second map whose blocks expose the executor, input/output contracts, restrictions, and handoff. Split the detail map by phase or layer if a single readable canvas cannot hold it.

## Write for a non-technical reader

- Use plain-language labels such as “Checks the payment” rather than internal class names.
- Add a short “why this matters” annotation where a step affects cost, risk, privacy, reliability, or the customer experience.
- Translate every unavoidable technical term the first time it appears.
- Keep each node label short enough to scan. Put nuance in annotations or the companion document.
- Never invent certainty. Mark inferred or unknown details visibly.

## Build the illustration

Read [references/map-spec.md](references/map-spec.md) before creating a map specification.

Create one JSON specification per map, then render each with:

```bash
python3 <skill-directory>/scripts/render_map.py <spec.json> <output.svg>
```

The renderer uses only Python's standard library. It creates exact, selectable text and a sketch-like SVG without relying on an image model, so names and sequences remain accurate.

Detailed nodes may include a `details` list. Use it for the architecture fields above. The renderer refuses a node whose declared height cannot contain its details; increase the node or canvas rather than accepting clipped text.

Use visual semantics consistently:

- blue: user-facing or entry-point steps;
- cream: internal processing;
- green: successful output or storage;
- purple: external service;
- red: risk, failure, or manual intervention;
- dashed edge: optional, asynchronous, inferred, or failure path—label which one.

After rendering, inspect the SVG. Check for overlap, clipped text, crossed arrows, illegible labels, and misleading visual order. Adjust the specification and render again when needed.

## Companion walkthrough

The Markdown companion should contain:

1. **What this system does** — two or three sentences.
2. **The journey** — numbered steps matching the visual.
3. **What can go wrong** — only material failure paths.
4. **What is uncertain** — inferred or unknown behavior.
5. **Architecture matrix** — one row per block with executor, input, output, restrictions, handoff, and evidence.
6. **Evidence** — compact bullets linking each major claim to the relevant project file and symbol or line when practical.

Do not dump a full file tree, raw call graph, or long code excerpts.

## HTML explainer mode

The deliverable is one self-contained, navigable page: a clickable journey diagram, one contract card per block, the design decisions with their reasons and costs, controls, stack, measured numbers, honest limitations, likely questions and a glossary. It suits a reader who must understand **and defend** the system.

1. **Pin the version without writing to the project.** Compare the checkout's `HEAD` with the remote default branch (`git ls-remote origin HEAD`). Describe the default branch unless the user names another; if the checkout is behind, read that branch from a shallow clone in a temp folder (`git clone --depth 1 <remote-url> <tmp>`). State the repo, branch, commit and commit date in `subtitle`. In a monorepo, cite the last commit that touched the application's folder.
2. **Investigate** using *Investigate before drawing* and *Trace architecture contracts* above. The SVG steps (*Choose the story* layout rules, *Build the illustration*, the map specification, opening the SVG) apply only if the user also asked for SVG maps. Add a **decision ledger**: for each major choice, the choice, the alternatives that were available, the stated reason with its source (ADR, doc, commit, PR), and the trade-off. For several systems, make one page per system and run one read-only investigation subagent per system in parallel and ask each for blocks with contracts, 8–15 decisions, controls, stack, numbers with source and date, and known limitations.
3. **Write a content module** defining `DOC`, following [references/explainer-content.md](references/explainer-content.md); a short worked example for an invented small app is [references/example-explainer-content.py](references/example-explainer-content.py). Keep it in a scratch location, never in the user's repository.
4. **Build:**

   ```bash
   python3 <skill-directory>/scripts/build_explainer.py <content.py> <output.html>
   ```

   It refuses content that breaks the required structure, listing every problem. It warns, without blocking, about diagram labels that are too long, rows with more than 5 boxes, a pitch outside 40–110 words and a decision count outside 5–15. Fix both kinds before delivering.
5. **Verify in a browser.** Browser tools cannot drive a `file://` page, so serve the output folder over local HTTP: the runtime's preview or dev-server tool, or `python3 -m http.server` started from a temporary copy when the project must stay untouched (on macOS, a Python without access to `~/Documents` fails at start-up; a temp folder avoids it). A preview tool that needs a config file inside the project counts as writing to it. Check that every diagram box opens its card, "Expand all" and "Collapse all" work, dark mode is legible (toggle and system setting), there is no horizontal scroll at 375 px and about 930 px width, and no stray `*`, `**` or backticks outside code. For a quick static look, a headless Chrome screenshot also works: `"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --screenshot=/tmp/page.png --window-size=1400,1800 file:///path/page.html` (write the image to a temp folder).
6. **Save** where the user asks. Personal preparation material usually belongs outside the repository; otherwise use the project's documentation folder.

## Finish

Open or display the SVG, or the HTML page in explainer mode, when the environment supports it. Summarize the system in one plain-language paragraph and mention any uncertainty that materially changes the explanation.
