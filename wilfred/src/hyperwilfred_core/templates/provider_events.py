"""Host-only CLI event classification, with diagnostics that never retain raw messages."""
from __future__ import annotations

import hashlib
import json


class ProviderResponseError(RuntimeError):
    def __init__(self, diagnostic):
        self.diagnostic = diagnostic
        super().__init__("Codex provider rejected: " + json.dumps(diagnostic, sort_keys=True))


def read_provider_result(completed, output_path):
    known_events = {"thread.started", "turn.started", "turn.completed", "turn.failed",
                    "item.started", "item.updated", "item.completed", "error"}
    known_items = {"agent_message", "reasoning", "error", "command_execution", "mcp_tool_call",
                   "web_search", "file_change", "collab_tool_call", "tool_call", "function_call"}
    diagnostic = {"returncode": completed.returncode, "event_kinds": [], "item_kinds": [],
                  "warnings": [], "warning_count": 0}
    def reject(reason):
        raise ProviderResponseError({**diagnostic, "reason": reason, "accepted": False})
    try:
        events = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    except (ValueError, TypeError):
        reject("invalid_event_stream")
    if any(not isinstance(event, dict) for event in events):
        reject("invalid_event_stream")
    diagnostic["event_kinds"] = sorted({
        event.get("type") if event.get("type") in known_events else "unknown" for event in events})
    items = [event["item"] for event in events if "item" in event]
    if any(not isinstance(item, dict) for item in items):
        reject("invalid_item")
    diagnostic["item_kinds"] = sorted({
        item.get("type") if item.get("type") in known_items else "unknown" for item in items})
    for item in items:
        if item.get("type") == "error":
            diagnostic["warning_count"] += 1
            if len(diagnostic["warnings"]) < 16:
                message = str(item.get("message", ""))
                diagnostic["warnings"].append({"kind": "error", "message_chars": len(message),
                    "message_sha256": hashlib.sha256(message.encode()).hexdigest()})
    if completed.returncode:
        reject("nonzero_exit")
    if any(item.get("type") not in {"agent_message", "reasoning", "error"} for item in items):
        reject("external_or_unknown_tool_item")
    if ("turn.completed" not in diagnostic["event_kinds"] or
            any(event.get("type") in {"turn.failed", "error"} for event in events)):
        reject("turn_not_successfully_completed")
    if not output_path.is_file():
        reject("missing_response")
    try:
        result = json.loads(output_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        reject("invalid_response_json")
    fields = {"action", "query", "answer", "citations", "feedback"}
    if (not isinstance(result, dict) or set(result) != fields
            or any(not isinstance(result[key], str) for key in fields - {"citations"})
            or result["action"] not in {"lookup", "finish", "approve", "reject"}
            or not isinstance(result["citations"], list)
            or any(not isinstance(citation, str) for citation in result["citations"])):
        reject("invalid_response_schema")
    usage = []
    for event in events:
        if isinstance(event.get("usage"), dict):
            usage.append({key: value for key, value in event["usage"].items()
                          if key in {"input_tokens", "cached_input_tokens", "output_tokens",
                                     "reasoning_output_tokens"}
                          and isinstance(value, int) and not isinstance(value, bool)
                          and value >= 0})
    return result, {**diagnostic, "accepted": True, "external_tool_calls": 0, "usage": usage}
