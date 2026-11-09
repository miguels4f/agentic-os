"""Materialize and exercise Foundry bundles in bounded local subprocesses."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .foundry_agents import ModelProvider
from .foundry_models import CanonicalModel
from .foundry_runtime import RUNTIME_VERSION, LocalPythonBundle
from .foundry_sandbox import sandbox_command
from .foundry_scenarios import scenario_passed, scenario_payloads
from .foundry_storage import atomic, fault
from .templates.provider_broker import run_broker


class FoundryExecutionBlocked(RuntimeError):
    """Execution stopped with an actionable, operator-facing explanation."""


class ExecutionResult(CanonicalModel):
    accepted: bool
    exit_code: int
    output: dict[str, Any]
    stderr: str = ""
    evidence: dict[str, Any]


class ScenarioResult(CanonicalModel):
    scenario: str
    passed: bool
    executions: list[ExecutionResult]
    evidence: dict[str, Any]


def materialize_bundle(bundle: LocalPythonBundle, destination: str | Path) -> dict[str, Any]:
    """Write a deterministic bundle without replacing an existing directory."""
    root = Path(destination)
    if root.exists():
        raise FoundryExecutionBlocked(
            f"Why: destination already exists: {root}. "
            "Unlock: choose a new empty path or remove the old test bundle explicitly."
        )
    root.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    files = bundle.files()
    for name, content in files.items():
        target = root / name
        if target.parent != root or target.is_symlink():
            raise FoundryExecutionBlocked(
                f"Why: unsafe bundle path {name!r}. Unlock: use flat, ordinary bundle filenames."
            )
        atomic(target, content.encode())
        target.chmod(0o600)
        fault("bundle_file")
    digests = _file_digests(root)
    expected = bundle.manifest["file_sha256"]
    if any(digests.get(name) != digest for name, digest in expected.items()):
        raise FoundryExecutionBlocked(
            "Why: materialized files do not match the hash-pinned bundle manifest. "
            "Unlock: regenerate into a clean destination and do not edit generated files."
        )
    manifest_sha256 = hashlib.sha256(files["manifest.json"].encode()).hexdigest()
    return _bundle_evidence(root, expected_manifest_sha256=manifest_sha256)


def execute_bundle(
    bundle_directory: str | Path, payload: Mapping[str, Any], *, expected_manifest_sha256: str,
    timeout_seconds: float = 180, provider: ModelProvider | None = None,
) -> ExecutionResult:
    """Execute one JSON request using the bundle's own Python runner."""
    root = Path(bundle_directory).resolve()
    evidence = _bundle_evidence(root, expected_manifest_sha256=expected_manifest_sha256)
    runner = root / "runner.py"
    if not runner.is_file():
        raise FoundryExecutionBlocked(
            "Why: runner.py is missing. Unlock: materialize a complete bundle before execution."
        )
    encoded = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True)
    if len(encoded.encode()) > 196608:
        raise FoundryExecutionBlocked("Why: input exceeds 192 KiB. Unlock: reduce supplied data.")
    if provider is not None and (not isinstance(provider.provider_id, str)
                                 or not isinstance(provider.is_fixture, bool)):
        raise FoundryExecutionBlocked("Why: provider provenance is missing. "
                                      "Unlock: use an explicit, labelled host provider.")
    try:
        exit_code, output, stderr, calls = run_broker(
            sandbox_command(root), root, encoded, provider, timeout_seconds,
        )
    except (RuntimeError, ValueError, OSError) as exc:
        raise FoundryExecutionBlocked(
            f"Why: isolated bundle execution stopped: {exc}. "
            "Unlock: check the supported OS, explicit provider and bounded input."
        ) from exc
    if not isinstance(output, dict):
        raise FoundryExecutionBlocked(
            "Why: runner output must be one JSON object. Unlock: regenerate the reviewed runtime."
        )
    run_evidence = {
        **evidence,
        "input_sha256": _digest(encoded),
        "output_sha256": _digest(json.dumps(output, ensure_ascii=False, sort_keys=True)),
        "isolation": "macos-sandbox-exec-deny-default", "model_calls": calls,
        "model_evidence": "fixture" if any(c["is_fixture"] for c in calls)
        else "live" if calls else "no-model",
    }
    return ExecutionResult(
        accepted=exit_code == 0 and output.get("accepted") is True,
        exit_code=exit_code, output=output, stderr=stderr,
        evidence=run_evidence,
    )


def run_scenarios(bundle_directory: str | Path, *,
                  expected_manifest_sha256: str,
                  provider: ModelProvider | None = None) -> dict[str, ScenarioResult]:
    """Run the required normal, edge, adversarial, and recovery scenarios."""
    root = Path(bundle_directory).resolve()
    _bundle_evidence(root, expected_manifest_sha256=expected_manifest_sha256)
    family = json.loads((root / "manifest.json").read_text())["task_family"]
    fixtures = scenario_payloads(family)
    results: dict[str, ScenarioResult] = {}
    for name, payloads in fixtures.items():
        executions = [execute_bundle(bundle_directory, payload,
                       expected_manifest_sha256=expected_manifest_sha256,
                       provider=provider) for payload in payloads]
        passed = scenario_passed(family, name, executions)
        evidence = {"bundle": executions[-1].evidence, "task_family": family,
                    "execution_output_sha256": [x.evidence["output_sha256"] for x in executions]}
        results[name] = ScenarioResult(
            scenario=name, passed=passed, executions=executions, evidence=evidence
        )
    return results


def _bundle_evidence(root: Path, *, expected_manifest_sha256: str) -> dict[str, Any]:
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise FoundryExecutionBlocked(
            "Why: manifest.json is missing. Unlock: materialize the generated bundle first."
        )
    manifest_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    if manifest_digest != expected_manifest_sha256:
        raise FoundryExecutionBlocked(
            "Why: manifest does not match trusted build evidence. "
            "Unlock: discard the modified copy and materialize the original bundle again."
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("runtime_version") != RUNTIME_VERSION:
        raise FoundryExecutionBlocked(
            f"Why: runtime version is not {RUNTIME_VERSION!r}. "
            "Unlock: regenerate the bundle with the current Foundry adapter."
        )
    actual = _file_digests(root)
    declared = manifest.get("files")
    expected = manifest.get("file_sha256")
    actual_names = set(actual) - {"manifest.json"}
    if (not isinstance(declared, list) or not isinstance(expected, dict)
            or set(declared) != actual_names or set(expected) != actual_names):
        raise FoundryExecutionBlocked(
            "Why: bundle manifest does not declare the exact generated file set. "
            "Unlock: regenerate the complete bundle from trusted build evidence."
        )
    if any(actual.get(name) != digest for name, digest in expected.items()):
        raise FoundryExecutionBlocked(
            "Why: bundle digest verification failed. "
            "Unlock: discard the modified copy and materialize the original bundle again."
        )
    return {"bundle_id": manifest["bundle_id"], "runtime_version": RUNTIME_VERSION,
            "manifest_sha256": actual["manifest.json"], "file_sha256": actual}


def _file_digests(root: Path) -> dict[str, str]:
    if any(path.is_symlink() or not path.is_file() for path in root.iterdir()):
        raise FoundryExecutionBlocked("Why: bundle contains symlinks or untracked directories. "
                                      "Unlock: regenerate a flat bundle of ordinary files.")
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.iterdir()) if path.is_file()}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


__all__ = [
    "ExecutionResult", "FoundryExecutionBlocked", "ScenarioResult",
    "execute_bundle", "materialize_bundle", "run_scenarios",
]
