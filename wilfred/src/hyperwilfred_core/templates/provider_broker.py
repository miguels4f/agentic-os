"""Host-only bounded JSON-line broker; this module is never placed in a generated bundle."""
from __future__ import annotations

import hashlib
import json
import os
import selectors
import subprocess
import time

from ..foundry_agents import invoke_provider


def run_broker(command, root, encoded, provider, timeout_seconds):
    """Nonblocking pipes keep malformed or silent generated workers under a deadline."""
    env = {"PATH": "/usr/bin:/bin", "PYTHONIOENCODING": "utf-8", "FOUNDRY_NETWORK": "disabled"}
    calls, buffers = [], {"stdout": b"", "stderr": b""}
    output, pending = None, (encoded + "\n").encode()
    started = time.monotonic()
    with subprocess.Popen(command, cwd=root, env=env, stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        selector = selectors.DefaultSelector()
        for name in ("stdin", "stdout", "stderr"):
            stream = getattr(process, name)
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_WRITE if name == "stdin"
                              else selectors.EVENT_READ, name)
        try:
            while selector.get_map():
                if time.monotonic() - started > timeout_seconds:
                    raise RuntimeError("generated worker exceeded execution deadline")
                for key, _ in selector.select(0.1):
                    name = key.data
                    if name == "stdin":
                        try:
                            pending = pending[os.write(key.fd, pending):]
                        except BrokenPipeError:
                            pending = b""
                        if not pending:
                            selector.unregister(key.fileobj)
                        continue
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    buffers[name] += chunk
                    if len(buffers[name]) > 262144:
                        raise RuntimeError("generated worker exceeded output bound")
                    if name != "stdout":
                        continue
                    while b"\n" in buffers[name]:
                        line, buffers[name] = buffers[name].split(b"\n", 1)
                        message = json.loads(line)
                        if not isinstance(message, dict) or output is not None:
                            raise RuntimeError("worker must return one final JSON object")
                        if "model_request" not in message:
                            output = message
                            continue
                        request = message["model_request"]
                        if pending:
                            # The protocol is strictly request → reply → request. A worker that
                            # asks again before reading its reply would otherwise re-register
                            # stdin (KeyError: an unstructured host crash) after a second,
                            # already-billed provider call whose predecessor is lost.
                            raise RuntimeError(
                                "worker sent a model request before reading the previous reply"
                            )
                        if (provider is None or len(calls) >= 12 or not isinstance(request, dict)
                                or request.get("role") not in {"answerer", "writer", "reviewer"}
                                or not isinstance(request.get("context"), dict)):
                            raise RuntimeError("model request has no authorized bounded provider")
                        reply = invoke_provider(provider, request,
                                                timeout_seconds - (time.monotonic() - started))
                        if not isinstance(reply, dict):
                            raise RuntimeError("model response must be an object")
                        pending = (json.dumps(reply) + "\n").encode()
                        if len(pending) > 16384:
                            raise RuntimeError("model response exceeded bound")
                        calls.append({"role": request["role"], "provider_id": provider.provider_id,
                                      "is_fixture": provider.is_fixture,
                                      "request_sha256": hashlib.sha256(line).hexdigest(),
                                      "response_sha256": hashlib.sha256(pending).hexdigest()})
                        selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
            code = process.wait(timeout=max(0.1, timeout_seconds - (time.monotonic() - started)))
            if output is None or buffers["stdout"].strip():
                reason = buffers["stderr"].decode(errors="replace")[:1000]
                raise RuntimeError(f"runner returned no complete JSON (exit {code}): {reason}")
            return code, output, buffers["stderr"].decode(errors="replace"), calls
        finally:
            selector.close()
            if process.poll() is None:
                process.kill()
                process.wait()
