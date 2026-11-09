# REVIEW · Comprehend the target

You are scoping a **read-only audit**. The engine already resolved the target into a concrete
file list (`.hw/<run_id>/review-target.json`). Your job here is only to understand WHAT is being
reviewed so the critics that follow are well-aimed. You write NOTHING to the reviewed repo.

## Process
1. Read `review-target.json` to see the resolved file list, the target kind (paths / whole-repo /
   diff range), and whether it was truncated.
2. Skim the target files enough to understand their purpose, structure, language/stack, and how
   they fit together. Do NOT change anything — this is read-only.
3. Write a short comprehension note to the `produces_path` (`comprehension.md`): what the target
   is, its main components, any obvious areas of risk worth the critics' attention, and (if the
   target was truncated) which parts are out of scope this run.
4. In the same note, say what was and was not seen (agency-agents wiring 2026-10-23, owner OK; method: specialized-workflow-architect):
   - **Files inspected:** the files you actually read (not just the resolved list).
   - **Blind spots:** modules the target calls or is called by that are NOT in
     `review-target.json`. The critics can only read the target files, so name these for the
     report and suggest a wider `--path` if they matter.
   - **Unverified assumptions:** anything you inferred (purpose, data flow, who calls what)
     without reading the code that proves it. Say "not verified", do not present it as fact.

## Output (`comprehension.md`)
A brief markdown note — a few paragraphs or bullets. This is orientation for the critic panel,
not a deliverable. Then run `hw complete-step --run-id <id> --step comprehend`.
