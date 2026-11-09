"""Verification report schema (forked from ``common_py.schemas.verification``).

The deterministic-gate output model. Forked standalone so the evaluators carry no
dependency on the dropped factory package.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class CheckKind(StrEnum):
    """Category of a single verification check."""

    LINT = "lint"
    TYPECHECK = "typecheck"
    TEST = "test"
    SECURITY = "security"
    PERF = "perf"
    CONSISTENCY = "consistency"


class Severity(StrEnum):
    """Severity of a failing check."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class Finding(BaseModel):
    """A single structured finding emitted by an evaluator."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    file: str
    line: int | None = None
    message: str
    severity: Severity
    rule_id: str | None = None


class CheckResult(BaseModel):
    """Outcome of one verification check (e.g., ruff, mypy, pytest)."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    kind: CheckKind
    name: str
    passed: bool
    severity: Severity = Severity.INFO
    details: str | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    findings: list[Finding] = Field(default_factory=list)


class VerificationReport(BaseModel):
    """Aggregate verification outcome for one run."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    task_id: str
    run_id: str
    completed_at: datetime
    checks: list[CheckResult] = Field(default_factory=list)
    requires_adr: bool = False
    blocks_merge: bool = False


__all__ = [
    "CheckKind",
    "CheckResult",
    "Finding",
    "Severity",
    "VerificationReport",
]
