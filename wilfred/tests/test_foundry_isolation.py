"""Real macOS policy probes; no mocked subprocess or Python-only isolation claims."""
import hashlib
import json
import socket
import threading
import time

import pytest
from test_foundry_contract_hardening import runtime_design

from hyperwilfred_core.foundry_agents import ScriptedProvider
from hyperwilfred_core.foundry_execution import (
    FoundryExecutionBlocked,
    execute_bundle,
    materialize_bundle,
)
from hyperwilfred_core.foundry_runtime import LocalPythonManifestAdapter
from hyperwilfred_core.foundry_sandbox import sandbox_command


def materialize_probe(tmp_path, source):
    bundle = LocalPythonManifestAdapter().build_bundle(runtime_design()[0])
    manifest = json.loads(json.dumps(bundle.manifest))
    manifest["file_sha256"]["runner.py"] = hashlib.sha256(source.encode()).hexdigest()
    bundle = bundle.model_copy(update={"source_files": {"runner.py": source}, "manifest": manifest})
    root = tmp_path / "bundle"
    return root, materialize_bundle(bundle, root)


def test_os_blocks_host_secrets_gate_writes_network_and_child_processes(tmp_path):
    host_secret, gate = tmp_path / "host-auth-fixture.json", tmp_path / "approval.json"
    host_secret.write_text("synthetic signing authority; fixture only")
    gate.write_text("pending")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        source = f'''import json, os, pathlib, socket, subprocess
blocked = []
operations = {{
    "host_secret": lambda: pathlib.Path({str(host_secret)!r}).read_text(),
    "gate_write": lambda: pathlib.Path({str(gate)!r}).write_text("self-approved"),
    "bundle_write": lambda: pathlib.Path(__file__).write_text("tampered"),
    "network": lambda: socket.create_connection(("127.0.0.1", {port}), timeout=1),
    "child_process": lambda: subprocess.run(["/bin/sh", "-c", "true"], check=True),
}}
for name, operation in operations.items():
    try:
        operation()
    except (PermissionError, OSError):
        blocked.append(name)
print(json.dumps({{"accepted": True, "blocked": blocked,
                  "approval_env_present": "WILFRED_APPROVAL_PUBLIC_KEY" in os.environ}}))
'''
        root, evidence = materialize_probe(tmp_path, source)
        result = execute_bundle(root, {}, expected_manifest_sha256=evidence["manifest_sha256"])
    assert set(result.output["blocked"]) == {
        "host_secret", "gate_write", "bundle_write", "network", "child_process"}
    assert not result.output["approval_env_present"]
    assert gate.read_text() == "pending"
    assert result.evidence["isolation"] == "macos-sandbox-exec-deny-default"


def test_unavailable_os_is_fail_closed(monkeypatch, tmp_path):
    monkeypatch.setattr("hyperwilfred_core.foundry_sandbox.platform.system", lambda: "Linux")
    with pytest.raises(RuntimeError, match="isolation is mandatory"):
        sandbox_command(tmp_path)


def test_framework_python_runs_the_real_interpreter_not_the_respawning_stub(monkeypatch, tmp_path):
    app = tmp_path / "Resources/Python.app/Contents/MacOS/Python"
    app.parent.mkdir(parents=True)
    app.write_text("")
    monkeypatch.setattr("hyperwilfred_core.foundry_sandbox.sys.base_prefix", str(tmp_path))
    command = sandbox_command(tmp_path / "bundle")
    assert command[3] == str(app.resolve())
    assert f'(allow process-exec (literal "{app.resolve()}"))' in command[2]


def test_nonresponsive_worker_is_killed_at_deadline(tmp_path):
    root, evidence = materialize_probe(tmp_path, "while True: pass\n")
    with pytest.raises(FoundryExecutionBlocked, match="deadline"):
        execute_bundle(root, {}, expected_manifest_sha256=evidence["manifest_sha256"],
                       timeout_seconds=0.2)


def test_symlink_content_is_not_a_trusted_bundle_member(tmp_path):
    root, evidence = materialize_probe(tmp_path, "print('{}')\n")
    (root / "linked").symlink_to(tmp_path / "outside")
    with pytest.raises(FoundryExecutionBlocked, match="symlinks"):
        execute_bundle(root, {}, expected_manifest_sha256=evidence["manifest_sha256"])


def test_hanging_host_provider_cannot_hold_generated_worker_open(tmp_path):
    class HangingProvider(ScriptedProvider):
        def __call__(self, request):
            threading.Event().wait()
    source = '''import json, sys
print(json.dumps({"model_request": {"role": "answerer", "context": {}}}), flush=True)
sys.stdin.readline()
sys.stdin.readline()
'''
    root, evidence = materialize_probe(tmp_path, source)
    started = time.monotonic()
    with pytest.raises(FoundryExecutionBlocked, match="deadline"):
        execute_bundle(root, {}, provider=HangingProvider(), timeout_seconds=0.2,
                       expected_manifest_sha256=evidence["manifest_sha256"])
    assert time.monotonic() - started < 1
