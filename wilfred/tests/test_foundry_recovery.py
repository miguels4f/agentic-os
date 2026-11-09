"""Kill real writers at publication boundaries, then continue the same project."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_foundry_e2e import OBJECTIVE, _answer_every_question, _artifact, _stored

from hyperwilfred_core import foundry


def ready(project: Path, operation: str) -> None:
    foundry.create(project, OBJECTIVE)
    _answer_every_question(project)
    for _ in range(4):
        foundry.next_action(project)
    foundry.validate(project)
    if operation != "generate":
        foundry.generate(project, seeded_defect=True)
    if operation == "repair":
        assert not foundry.evaluate(project)["accepted"]


def immutable_bytes(project: Path) -> dict[Path, bytes]:
    return {path: path.read_bytes() for pattern in
            ("artifacts/*.json", "revisions/*.json", "generated-*/*")
            for path in project.glob(pattern) if path.is_file()}


BOUNDARIES = [
    (operation, point) for operation in ("generate", "repair")
    for point in ("partial_file_written", "file_written", "bundle_file", "build_published",
                  "artifact_published", "state_revision", "state_committed")
] + [("repair", "repair_verified")] + [
    ("evaluate", point) for point in ("partial_file_written", "file_written", "artifact_published",
                                     "state_revision", "state_committed")
]


@pytest.mark.parametrize(("operation", "point"), BOUNDARIES)
def test_process_death_preserves_revisions_and_resumes(
    tmp_path: Path, operation: str, point: str,
) -> None:
    project = tmp_path / "recoverable"
    ready(project, operation)
    previous = immutable_bytes(project)
    previous_state = (project / "state.json").read_bytes()
    arguments = {"seeded_defect": True} if operation == "generate" else (
        {"component": "edge"} if operation == "repair" else {})
    child = subprocess.run(
        [sys.executable, "-c", "import json,sys; from hyperwilfred_core import foundry; "
         "getattr(foundry,sys.argv[2])(sys.argv[1], **json.loads(sys.argv[3]))",
         str(project), operation, json.dumps(arguments)],
        env={**os.environ, "FOUNDRY_CRASH_AT": point}, capture_output=True,
        timeout=30, check=False,
    )
    assert child.returncode == 86, child.stderr.decode()
    partial_writes = {}
    if point == "partial_file_written":
        partial_writes = {path: path.read_bytes() for path in project.rglob(".*") if path.is_file()}
        assert len(partial_writes) == 1, "writer must leave exactly one partial temporary file"
        assert all(partial_writes.values()), "partial file must contain bytes flushed before death"
    assert all(path.read_bytes() == content for path, content in previous.items())
    if point != "state_committed":
        assert (project / "state.json").read_bytes() == previous_state
    resumed = foundry.resume(project)
    expected = {"generate": "evaluate", "evaluate": "repair", "repair": "evaluate"}
    assert resumed["stage"] == (expected[operation] if point == "state_committed" else operation)
    if point == "bundle_file":
        assert list(project.glob(".staging-*/bundle/*")), "writer must die during file publication"
    while foundry.status(project)["next_action"] != "complete":
        stage = foundry.status(project)["stage"]
        if stage == "generate":
            foundry.generate(project, seeded_defect=True)
        elif stage == "evaluate":
            foundry.evaluate(project)
        elif stage == "repair":
            foundry.repair(project, "edge")
        else:
            foundry.package(project)
    assert foundry.resume(project)["next_action"] == "complete"
    assert all(path.read_bytes() == content for path, content in previous.items())
    assert len(list(project.glob("generated-*"))) == 2
    for temporary, partial in partial_writes.items():
        intended_name = temporary.name[1:].rsplit(".", 1)[0]
        completed_files = [path.read_bytes() for path in project.rglob(intended_name)
                           if path.is_file()]
        assert any(0 < len(partial) == len(complete) // 2 < len(complete)
                   and partial == complete[:len(partial)] for complete in completed_files)
    for path in project.glob("revisions/*.json"):
        assert hashlib.sha256(path.read_bytes()).hexdigest() == path.stem
        for record in json.loads(path.read_bytes())["artifacts"].values():
            digest = hashlib.sha256((project / record["path"]).read_bytes()).hexdigest()
            assert digest == record["sha256"]
    repair = _stored(project)["repairs"][0]
    assert set(repair["changes"]) == {"runner.py", "manifest.json"}
    assert repair["rerun"]["accepted"] and _artifact(project, "evaluation")["accepted"]


def test_failed_repair_rerun_never_replaces_current_state(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "rejected-repair"
    ready(project, "repair")
    previous = immutable_bytes(project)
    state = (project / "state.json").read_bytes()
    monkeypatch.setattr(foundry, "_executed_evaluation",
                        lambda *args, **kwargs: {"accepted": False})
    with pytest.raises(foundry.FoundryBlocked, match="regression scenarios"):
        foundry.repair(project, "edge")
    assert (project / "state.json").read_bytes() == state
    assert all(path.read_bytes() == content for path, content in previous.items())
    assert foundry.resume(project)["stage"] == "repair"


def test_software_ledger_preserves_previous_record_during_partial_write(tmp_path: Path) -> None:
    from hyperwilfred_core.run_store import atomic_write_text

    target = tmp_path / "software-evidence.txt"
    atomic_write_text(target, "previous accepted evidence")
    child = subprocess.run(
        [sys.executable, "-c", "import sys; from pathlib import Path; "
         "from hyperwilfred_core.run_store import atomic_write_text; "
         "atomic_write_text(Path(sys.argv[1]), 'x' * 101)", str(target)],
        env={**os.environ, "FOUNDRY_CRASH_AT": "partial_file_written"},
        capture_output=True, timeout=10, check=False,
    )
    assert child.returncode == 86, child.stderr.decode()
    assert target.read_text() == "previous accepted evidence"
    partials = list(tmp_path.glob(".software-evidence.txt.*"))
    assert len(partials) == 1 and partials[0].read_bytes() == b"x" * 50
    atomic_write_text(target, "x" * 101)
    assert target.read_bytes() == b"x" * 101
