from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _executable(environment: Path, name: str) -> Path:
    directory = "Scripts" if os.name == "nt" else "bin"
    suffix = ".exe" if os.name == "nt" else ""
    return environment / directory / f"{name}{suffix}"


def test_wheel_is_self_contained_and_exposes_both_commands(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(dist)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(dist.glob("wilfred-*.whl"))

    with zipfile.ZipFile(wheel) as archive:
        members = set(archive.namelist())
        entry_points = archive.read(
            next(name for name in members if name.endswith(".dist-info/entry_points.txt"))
        ).decode()
    assert "wilfred = hyperwilfred_core.cli:main" in entry_points
    assert "hw = hyperwilfred_core.cli:main" in entry_points
    assert "hyperwilfred_core/steps/improve/01_comprehend.md" in members
    assert "hyperwilfred_core/steps/quality/00_loop.md" in members
    assert "hyperwilfred_core/steps/review/01_comprehend.md" in members
    assert "hyperwilfred_core/skills/hyperwilfred-build/SKILL.md" in members
    assert "hyperwilfred_core/skills/hyperwilfred-review/SKILL.md" in members
    assert "hyperwilfred_core/templates/evidence_runner.py" in members
    assert "hyperwilfred_core/foundry_acceptance.py" in members

    environment = tmp_path / "clean-environment"
    uv = shutil.which("uv")
    assert uv is not None, "uv is required for the development and CI environment"
    subprocess.run(
        [uv, "venv", "--python", sys.executable, str(environment)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [uv, "pip", "install", "--python", str(_executable(environment, "python")), str(wheel)],
        check=True,
        capture_output=True,
        text=True,
    )

    outside_checkout = tmp_path / "outside-checkout"
    outside_checkout.mkdir()
    clean_env = os.environ.copy()
    clean_env.pop("PYTHONPATH", None)
    for command in ("wilfred", "hw"):
        completed = subprocess.run(
            [str(_executable(environment, command)), "--help"],
            cwd=outside_checkout,
            env=clean_env,
            check=True,
            capture_output=True,
            text=True,
        )
        assert "plan" in completed.stdout

    # Perform real work from the installed wheel outside the checkout with no PYTHONPATH.
    evidence_root = outside_checkout / "installed-proof"
    performed = subprocess.run(
        [str(_executable(environment, "python")), "-m", "hyperwilfred_core.foundry_acceptance",
         "--output-root", str(evidence_root), "--provider", "fixture"],
        cwd=outside_checkout, env=clean_env, check=True, capture_output=True, text=True, timeout=90,
    )
    summary = json.loads(performed.stdout)
    assert summary["architectures"] == {"notes_to_actions": "workflow",
            "evidence_answer": "single_agent", "reviewed_brief": "multi_agent"}
    assert summary["live_models_demonstrated"] is False  # Fixtures are never model evidence.
    task = json.loads((evidence_root / "workflow-proof.json").read_text())["execution"]
    assert task["output"]["actions"][0]["title"] == "Send summary"
    assert task["evidence"]["isolation"] == "macos-sandbox-exec-deny-default"

    subprocess.run(
        [
            str(_executable(environment, "python")),
            "-c",
            (
                "from importlib.resources import files; "
                "root = files('hyperwilfred_core'); "
                "assert (root / 'steps/improve/01_comprehend.md').read_text(); "
                "assert (root / 'skills/hyperwilfred-build/SKILL.md').read_text()"
            ),
        ],
        cwd=outside_checkout,
        env=clean_env,
        check=True,
        capture_output=True,
        text=True,
    )
