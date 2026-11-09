"""Executable acceptance demonstration; fixture evidence never implies live-model success."""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from . import approvals, engine, foundry, gates
from .foundry_agents import CodexCLIProvider, ScriptedProvider
from .foundry_execution import execute_bundle
from .foundry_families import FAMILIES
from .foundry_storage import atomic
from .schemas import BuildObjective

ANSWERS = {
    "actors": "A local operator reviews drafts",
    "trigger_frequency": "On operator request",
    "inputs": "Only the explicitly supplied bounded task input",
    "outputs": "Structured output conforming to the selected task contract",
    "current_reality": "The operator performs the task manually",
    "decisions": "Use the selected task's deterministic or evidence-based decision rule",
    "constraints": "Read-only input; no publishing; model calls only through explicit provider",
    "failure_impact": "An incorrect local draft must be rejected and corrected",
    "autonomy": "Create drafts locally; protected host actions require signed human approval",
    "success_evidence": "Execute normal, edge, adversarial and recovery scenarios",
    "non_goals": "No production deployment, messaging, financial transactions or publication",
}
OBJECTIVES = {
    "notes_to_actions": "Extract explicit actions, owners and dates from notes using fixed rules",
    "evidence_answer": "Answer a question by selecting and synthesizing supplied evidence",
    "reviewed_brief": "Write an evidence-based brief and have a separate reviewer check it",
}
SYNTHESIS = {"question": "How many days remain? Reply with only '<number> days remain.' "
                         "using the calculated number.", "documents": [
    {"id": "policy", "text": "The response deadline is 30 days after receipt."},
    {"id": "case", "text": "This request was received 7 days ago."},
]}


def require(condition: bool, claim: str) -> None:
    if not condition:
        raise RuntimeError(f"Evidence did not demonstrate: {claim}")


def write_json(path: Path, value: Any) -> None:
    atomic(path, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode())


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def artifact(project: Path, name: str) -> dict[str, Any]:
    state = json.loads((project / "state.json").read_text())
    entry = state["artifacts"][name]
    path = project / entry["path"]
    require(digest(path) == entry["sha256"], f"{name} is bound to stored digest")
    return json.loads(path.read_text())


def prepare(project: Path, family: str, *, seeded: bool = False) -> None:
    # No explicit family hint: the problem itself must select the supported architecture.
    created = foundry.create(project, OBJECTIVES[family])
    require(created.get("question") is not None, "ambiguous intake asks before generating")
    while (question := foundry.status(project).get("question")) is not None:
        field = question["field"]
        answer = ANSWERS[field]
        if field == "decisions":
            answer = str(FAMILIES[family]["rationale"])
        foundry.answer(project, answer, field=field)
    for _ in range(4):
        foundry.next_action(project)
    require(artifact(project, "topology")["kind"] == FAMILIES[family]["topology"],
            "task justification selects the expected architecture")
    require(foundry.validate(project)["valid"], "generated contracts validate")
    bundle = foundry.generate(project, seeded_defect=seeded)
    require(bool(bundle["manifest"]["architecture"]["rationale"]),
            "generated architecture includes its decision rationale")


def execute(project: Path, payload: dict, provider=None) -> dict:
    build = artifact(project, "build")
    return execute_bundle(project / build["directory"], payload,
                          expected_manifest_sha256=build["manifest_sha256"],
                          provider=provider).model_dump(mode="json")


def interrupted_evaluation(project: Path) -> dict:
    previous = (project / "state.json").read_bytes()
    command = [sys.executable, "-c", "import sys; from hyperwilfred_core import foundry; "
               "foundry.evaluate(sys.argv[1])", str(project)]
    env = {**os.environ, "FOUNDRY_CRASH_AT": "partial_file_written"}
    process = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60)
    require(process.returncode == 86, "child actually died during a half-written artifact")
    require((project / "state.json").read_bytes() == previous, "committed state survived")
    partial = [p for p in (project / "artifacts").iterdir() if p.name.startswith(".")]
    require(bool(partial), "interrupted partial write remains observable")
    resumed = foundry.resume(project)
    require(resumed["stage"] == "evaluate", "resume found the coherent previous version")
    return {"command": command, "crash_at": "partial_file_written", "exit_code": 86,
            "state_sha256": hashlib.sha256(previous).hexdigest(), "resumed": resumed,
            "partial_files": {p.name: {"sha256": digest(p), "bytes": p.stat().st_size}
                              for p in partial}}


def workflow(project: Path, *, interrupt: bool) -> dict:
    prepare(project, "notes_to_actions", seeded=True)
    first_build = artifact(project, "build")
    state = json.loads((project / "state.json").read_text())
    accepted = {n: state["artifacts"][n] for n in
                ("topology", "specification", "blueprint", "contracts", "validation")}
    crash = interrupted_evaluation(project) if interrupt else None
    failed = foundry.evaluate(project)
    require(not failed["accepted"] and not failed["scenarios"]["edge"]["passed"],
            "seeded runner defect fails an executed edge scenario")
    old_artifacts = {p: digest(p) for p in (project / "artifacts").glob("*.json")}
    repair = foundry.repair(project, "edge")
    require(set(repair["changes"]) == {"runner.py", "manifest.json"},
            "repair changes source and its manifest only")
    require(bool(repair["changes"]["runner.py"]["diff"]) and repair["rerun"]["accepted"],
            "source correction has a diff and a successful executed rerun")
    current = json.loads((project / "state.json").read_text())
    require(all(current["artifacts"][n] == a for n, a in accepted.items()),
            "accepted components preserved byte-for-byte")
    require(all(p.exists() and digest(p) == d for p, d in old_artifacts.items()),
            "all previous artifacts and evaluation evidence still exist unchanged")
    require(all(digest(project / first_build["directory"] / n) == d
                for n, d in first_build["file_sha256"].items()), "old code version retained")
    evaluation = foundry.evaluate(project)
    require(evaluation["accepted"], "repaired version passes all executed scenarios")
    task = execute(project, {"notes": "- Ana: Send summary by 2026-10-01"})
    require(task["output"]["actions"] == [{"owner": "Ana", "title": "Send summary",
            "due_date": "2026-10-01", "source_line": 1}], "generated code performs the task")
    package = foundry.package(project)
    require(foundry.resume(project)["next_action"] == "complete", "packaged revision resumes")
    state = json.loads((project / "state.json").read_text())
    return {"architecture": artifact(project, "topology"), "execution": task, "crash": crash,
            "repair": repair, "evaluation": evaluation, "first_build": first_build,
            "build": artifact(project, "build"), "package": package,
            "package_sha256": state["artifacts"]["package"]["sha256"],
            "retained_artifacts": {str(p.relative_to(project)): d
                                   for p, d in old_artifacts.items()}}


class SynthesisFixture(ScriptedProvider):
    """Protocol-only fixture: explicitly not evidence of model reasoning."""
    def __call__(self, request):
        context = request["context"]
        if request["role"] == "reviewer" or context.get("question") != SYNTHESIS["question"]:
            return super().__call__(request)
        retrieved = {d["id"] for d in context.get("lookup_results", [])}
        for identifier in ("policy", "case"):
            if identifier not in retrieved:
                return {"action": "lookup", "query": identifier}
        return {"action": "finish", "answer": "23 days remain.", "citations": ["policy", "case"]}


def agent(project: Path, family: str, provider) -> dict:
    prepare(project, family)
    task = execute(project, SYNTHESIS, provider)
    write_json(project / "acceptance-task.json", {"input": SYNTHESIS, "execution": task})
    output = task["output"]
    supported = {"23 days remain.", "23 days remaining.", "23 days remain", "23 days remaining",
                 "there are 23 days remaining.", "there are 23 days remaining",
                 "23 days remain until the response deadline.",
                 "23 days remain until the deadline."}
    require(task["accepted"] and output.get("answer", "").strip().casefold() in supported
            and set(output.get("citations", [])) == {"policy", "case"},
            "answer synthesizes two retrieved documents, not numbers supplied in the question")
    looked_up = {identifier for t in output.get("trace", []) if t.get("event") == "lookup"
                 for identifier in t.get("ids", [])}
    # Citation validation in the runner rejects ids that have not actually been retrieved.
    require({"policy", "case"} <= looked_up, "generated agent retrieved both documents")
    if family == "reviewed_brief":
        require(output.get("content_review", {}).get("approved") is True,
                "separate reviewer checked the writer's result")
    evaluation = foundry.evaluate(project, provider=provider)
    require(evaluation["accepted"], "agent passed all executed scenarios")
    foundry.package(project)
    return {"architecture": artifact(project, "topology"), "execution": task,
            "input": SYNTHESIS, "evaluation": evaluation, "build": artifact(project, "build")}


def approval_proof(root: Path) -> dict:
    """Only a harmless local callback; ephemeral signing key is explicitly a TEST FIXTURE."""
    engine.plan(repo_root=root, objective=BuildObjective(id="approval-demo", title="Approval",
                objective="Append a harmless marker only after signed authorization"),
                run_id="approval-demo")
    opened = gates.gate_open(repo_root=root, run_id="approval-demo", kind=gates.GateKind.MERGE,
                action="Append harmless acceptance marker", evidence=["synthetic-input-only"])
    gate_id = opened["gate"]["gate_id"]
    marker = root / "harmless-action.txt"
    def action():
        atomic(marker, b"Test-fixture authorized action\n")
        return "harmless marker written"
    def invoke(key=None):
        return gates.execute_protected_action(repo_root=root, run_id="approval-demo",
                    gate_id=gate_id, callback=action, public_key_path=key)
    rejected = []
    for claim in (None, {"human_approved": True, "by": "generated-agent"}):
        if claim is not None:
            gate_dir = root / ".hw/approval-demo/gates"
            write_json(gate_dir / f"{gate_id}.approved", claim)
        try:
            invoke()
        except gates.GateError as exc:
            rejected.append(str(exc))
        else:
            raise RuntimeError("Protected action executed without signed authorization")
        require(not marker.exists(), "no authorization/self-claim cannot execute host action")
    private = Ed25519PrivateKey.generate()
    public = root / "fixture-public-key.pem"
    atomic(public, private.public_key().public_bytes(serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo))
    payload = approvals.build_decision_payload(opened["challenge"], decision="approve",
                decided_by="TEST FIXTURE; NOT A HUMAN", note="Harmless local acceptance test",
                public_key_path=public)
    token = root / "fixture-signed-decision.json"
    write_json(token, {"payload": payload,
        "signature": base64.b64encode(private.sign(approvals.canonical_bytes(payload))).decode()})
    gates.gate_approve(repo_root=root, run_id="approval-demo", gate_id=gate_id,
                       decision=gates.GateDecision.APPROVE, token_file=token,
                       public_key_path=public)
    result = invoke(public)
    require(marker.exists(), "valid test signature releases only the harmless host action")
    try:
        invoke(public)
    except gates.GateError:
        pass
    else:
        raise RuntimeError("Protected callback replay was not rejected")
    return {"absent_and_self_claim_blocked": rejected, "callback": result,
            "signed_positive_is_test_fixture": True, "human_approval": "NOT REQUESTED",
            "marker_sha256": digest(marker), "replay_blocked": True}


def run(root: Path, mode: str = "offline") -> dict:
    require(not root.exists(), "output root is new; previous evidence is never overwritten")
    root.mkdir(parents=True)
    source = Path(__file__).parent
    versions = {str(p.relative_to(source)): digest(p) for p in sorted(source.rglob("*.py"))}
    write_json(root / "product-version.json", {"version": importlib.metadata.version("wilfred"),
                                               "source_sha256": versions})
    provider = CodexCLIProvider() if mode == "codex" else SynthesisFixture()
    try:
        first = workflow(root / "first/demo", interrupt=True)
        replay = workflow(root / "replay/demo", interrupt=False)
        require(first["build"] == replay["build"] and
                first["package_sha256"] == replay["package_sha256"],
                "two clean builds reproduce the same file and package hashes")
        write_json(root / "workflow-proof.json", first)
        write_json(root / "replay-proof.json", replay)
        approval = approval_proof(root / "approval")
        write_json(root / "approval-proof.json", approval)
        architectures = {"notes_to_actions": first["architecture"]["kind"]}
        if mode != "offline":
            for family in ("evidence_answer", "reviewed_brief"):
                proof = agent(root / family, family, provider)
                write_json(root / f"{family}-proof.json", proof)
                architectures[family] = proof["architecture"]["kind"]
        write_json(root / "provider-receipts.json", getattr(provider, "receipts", []))
        result = {"status": "demonstration_passed", "mode": mode,
            "architectures": architectures, "reproducible_build": True,
            "live_models_demonstrated": mode == "codex", "approval_fixture_only": True,
            "independent_review": "required separately; not self-certified",
            "output_root": str(root),
            "product_version_sha256": digest(root / "product-version.json")}
        write_json(root / "summary.json", result)
        return result
    except Exception as exc:
        write_json(root / "provider-receipts.json", getattr(provider, "receipts", []))
        write_json(root / "failure.json", {"error": str(exc), "mode": mode})
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path,
                        help="new directory; persists complete evidence")
    parser.add_argument("--provider", choices=("offline", "fixture", "codex"), default="offline")
    args = parser.parse_args(argv)
    root = args.output_root or Path(tempfile.mkdtemp(prefix="wilfred-proof-")) / "evidence"
    try:
        print(json.dumps(run(root.resolve(), args.provider), indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc), "output_root": str(root)}),
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
