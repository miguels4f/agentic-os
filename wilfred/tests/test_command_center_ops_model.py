"""Command center: the snapshot model for data read from ``<workspace>/ops``."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from hyperwilfred_core.command_center.model import (
    OPS_BLOCKED_STATUS,
    OPS_CLOSED_STATUSES,
    SCHEMA_VERSION,
    AttentionItem,
    AttentionSubject,
    Generator,
    Ops,
    OpsProposal,
    OpsTask,
    Snapshot,
    Source,
)

BLOB = "https://github.com/example/ops/blob/main/"


def _snapshot(**overrides) -> Snapshot:
    data = {
        "generated_at": "2026-10-14T12:00:00Z",
        "workspace": "/ws",
        "generator": Generator(wilfred_version="0.1.0"),
    }
    data.update(overrides)
    return Snapshot(**data)


def test_a_snapshot_written_before_ops_existed_still_loads() -> None:
    old = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": "2026-10-09T12:00:00Z",
        "workspace": "/ws",
        "generator": {"wilfred_version": "0.1.0"},
    }
    assert Snapshot.model_validate_json(json.dumps(old)).ops is None


def test_the_ops_section_round_trips_and_keeps_unknown_apart_from_none() -> None:
    ops = Ops(
        root="/ws/ops",
        bin="/home/.local/bin/ops",
        github_blob=BLOB,
        open_tasks=[OpsTask(id="T-0001", path="tasks/T-0001-a.md", title="A", status="Blocked")],
        proposals=[
            OpsProposal(id="P-0003", path="proposals/P-0003.json", kind="set",
                        target="sales/acme.md", fields=["status"])
        ],
        unapproved_edits=None,
        commands=["/home/.local/bin/ops export --json --root /ws/ops"],
    )
    again = Snapshot.model_validate_json(_snapshot(ops=ops).model_dump_json())
    assert again.ops == ops
    assert again.ops.unapproved_edits is None and again.ops.proposals is not None
    assert OPS_BLOCKED_STATUS == "Blocked"
    assert sorted(OPS_CLOSED_STATUSES) == ["Complete", "Deferred"]


def test_unknown_keys_in_the_ops_section_are_refused() -> None:
    with pytest.raises(ValidationError):
        Ops.model_validate({"root": "/ws/ops", "bin": "/b/ops", "sales": []})
    with pytest.raises(ValidationError):
        Ops.model_validate({"root": "/ws/ops", "bin": "/b/ops", "unapproved_sales": []})
    with pytest.raises(ValidationError):
        OpsProposal.model_validate({
            "id": "P-1", "path": "proposals/P-1.json", "kind": "set",
            "target": "sales/x.md", "changes": {"status": {}},
        })


@pytest.mark.parametrize(
    "address",
    [
        "javascript:alert(1)",
        "http://github.com/x",
        "file:///etc/passwd",
        "//github.com/x",
        " https://github.com/x",
        "data:text/html,x",
    ],
)
def test_a_url_source_must_be_an_https_address(address) -> None:
    with pytest.raises(ValidationError):
        Source(kind="url", path=address)


def test_https_sources_and_the_three_ops_kinds_are_accepted() -> None:
    link = Source(kind="url", path=BLOB + "tasks/T-0001-a.md")
    for kind in ("ops_pending_proposal", "ops_overdue_task", "ops_blocked_task"):
        item = AttentionItem(
            id=f"{kind}:tasks/T-0001-a.md", kind=kind, title_es="t", why_es="w", risk="bajo",
            default_es="d", if_ignored_es="i",
            subject=AttentionSubject(type="ops", id="tasks/T-0001-a.md"), sources=[link],
        )
        assert item.sources[0].kind == "url"


def test_a_hand_edited_snapshot_with_a_script_link_is_refused_on_load() -> None:
    item = {
        "id": "x", "kind": "ops_blocked_task", "title_es": "t", "why_es": "w", "risk": "bajo",
        "default_es": "d", "if_ignored_es": "i", "subject": {"type": "ops", "id": "x"},
        "sources": [{"kind": "url", "path": "javascript:alert(1)"}],
    }
    data = json.loads(_snapshot().model_dump_json())
    data["attention"] = [item]
    with pytest.raises(ValidationError):
        Snapshot.model_validate_json(json.dumps(data))
