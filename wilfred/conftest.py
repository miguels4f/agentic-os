"""Suite-wide isolation from the host's Wilfred human-approvals policy switch.

The owner may turn the switch on for real work on this machine
(``~/Library/Application Support/Wilfred/policy.json``). The tests must behave the same either
way, so every test sees a policy path that does not exist unless it installs its own.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_host_policy_switch(tmp_path_factory, monkeypatch):
    from hyperwilfred_core import approvals

    absent = tmp_path_factory.getbasetemp() / "no-host-policy" / "policy.json"
    monkeypatch.setattr(approvals, "DEFAULT_POLICY_PATH", absent)
