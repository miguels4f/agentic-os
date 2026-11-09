from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_public_demo_proves_every_promised_stage(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    env = {**os.environ, "PYTHONPATH": str(root / "src")}
    result = subprocess.run(
        [sys.executable, str(root / "examples" / "demo.py"), "--output-root",
         str(tmp_path / "proof")], cwd=root, env=env,
        text=True, capture_output=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout)
    assert evidence["status"] == "demonstration_passed"
    assert evidence["architectures"] == {"notes_to_actions": "workflow"}
    assert evidence["live_models_demonstrated"] is False
    proof = json.loads((tmp_path / "proof/workflow-proof.json").read_text())
    assert all(proof["package"]["evaluation_results"].values())
    assert proof["crash"]["exit_code"] == 86
    assert set(proof["repair"]["changes"]) == {"runner.py", "manifest.json"}
    assert proof["execution"]["output"]["actions"][0]["title"] == "Send summary"
