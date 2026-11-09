"""Stage and verify repairs before publishing a new lifecycle state."""
from __future__ import annotations

import difflib
import hashlib
from pathlib import Path
from typing import Any

from .foundry_execution import FoundryExecutionBlocked, materialize_bundle
from .foundry_runtime import LocalPythonBundle
from .foundry_storage import fault, publish_build


def repair(project: str | Path, component: str, *, provider: Any = None) -> dict[str, Any]:
    # Import the lifecycle gates at call time to avoid an import cycle.
    from . import foundry as lifecycle
    from .foundry_runtime import repair_bundle_source

    root, state = lifecycle._load(project)
    if state["stage"] != lifecycle.FoundryStage.REPAIR:
        raise lifecycle.FoundryBlocked("repair is only available after a failed evaluation")
    evaluation = lifecycle._read_artifact(root, state, "evaluation")
    if component not in evaluation["scenarios"] or evaluation["scenarios"][component]["passed"]:
        raise lifecycle.FoundryBlocked("repair must target exactly one failed component")
    before_build = lifecycle._verify_build(root, state)
    old_bundle = LocalPythonBundle.model_validate(lifecycle._read_artifact(root, state, "bundle"))
    if component != "edge":
        raise lifecycle.FoundryBlocked("no supported source repair located for the failed scenario")
    try:
        repaired = repair_bundle_source(old_bundle)
    except ValueError as exc:
        raise lifecycle.FoundryBlocked(str(exc)) from exc
    before_files, after_files = old_bundle.files(), repaired.files()
    changed = sorted(name for name in before_files if before_files[name] != after_files[name])
    if changed != ["manifest.json", "runner.py"]:
        raise lifecycle.FoundryBlocked("repair would modify unrelated generated components")
    diffs = {}
    for name in changed:
        before, after = before_files[name], after_files[name]
        diffs[name] = {
            "before_sha256": hashlib.sha256(before.encode()).hexdigest(),
            "after_sha256": hashlib.sha256(after.encode()).hexdigest(),
            "before": before, "after": after,
            "diff": "".join(difflib.unified_diff(
                before.splitlines(keepends=True), after.splitlines(keepends=True),
                fromfile=f"a/{name}", tofile=f"b/{name}")),
        }
    try:
        evidence = publish_build(root, repaired, materialize_bundle)
    except (FoundryExecutionBlocked, ValueError) as exc:
        raise lifecycle.FoundryBlocked(str(exc)) from exc
    old_bundle_record = dict(state["artifacts"]["bundle"])
    old_build_record = dict(state["artifacts"]["build"])
    lifecycle._artifact(root, state, "bundle", repaired)
    lifecycle._artifact(root, state, "build", evidence)
    rerun = lifecycle._executed_evaluation(root, state, provider=provider)
    if not rerun["accepted"]:
        raise lifecycle.FoundryBlocked("repair did not pass the failed and regression scenarios")
    fault("repair_verified")
    repairs = state.setdefault("repairs", [])
    record = {
        "component": component, "target_file": f"{evidence['directory']}/runner.py",
        "before_sha256": diffs["runner.py"]["before_sha256"],
        "after_sha256": diffs["runner.py"]["after_sha256"], "changes": diffs,
        "bundle_before_sha256": old_bundle_record["sha256"],
        "bundle_after_sha256": state["artifacts"]["bundle"]["sha256"],
        "previous_bundle": old_bundle_record, "previous_build": old_build_record,
        "previous_directory": before_build["directory"],
        "rerun": rerun, "at": lifecycle._now(),
    }
    repairs.append(record)
    lifecycle._artifact(root, state, f"repair-{len(repairs)}", record)
    if lifecycle.FoundryStage.REPAIR.value not in state["completed"]:
        state["completed"].append(lifecycle.FoundryStage.REPAIR.value)
    state["stage"] = lifecycle.FoundryStage.EVALUATE.value
    lifecycle._save(root, state)
    lifecycle._event(root, "repaired", component=component,
                     before_sha256=record["before_sha256"], after_sha256=record["after_sha256"])
    return record
