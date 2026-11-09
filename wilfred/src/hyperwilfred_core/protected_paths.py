"""Sensitive / irreversible path classifier — the read-side guardrail.

FORKED AND CLEANED from ``common_py.factory.protected_paths`` (recycle-table "core"). Lifted
as standalone source — it imports NOTHING from ``common_py.factory.*`` (that package eagerly
drags the dropped Linear/Symphony spine back in; the CI grep + the AST import-guard test fail
the build if a real ``common_py`` import ever reappears here).

What it does, and what it does NOT:

* It CLASSIFIES a set of changed paths into sensitive categories (auth / payments / migrations
  / secrets / contracts / compliance / infra). A match means "a human must look at this before
  it ships" — it feeds the human-review routing (which :class:`GateKind` to open), it does NOT
  itself block a write. The hard write-time block is the PreToolUse hook + the engine acceptance
  gate; the irreversible-action block is the disk-file HITL gate. This module is the SENSITIVITY
  signal those mechanisms route on, exactly as the architecture's recycle note describes
  ("the read-side guardrail … keep it; build write-time enforcement + rollback around it").

Pure stdlib + pydantic. Runtime-neutral. No git, no subprocess, no LLM.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable, Mapping

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "DEFAULT_PROTECTED_PATHS",
    "IRREVERSIBLE_CATEGORIES",
    "ProtectedPathConfig",
    "ProtectedPathGate",
    "ProtectedPathMatch",
    "ProtectedPathResult",
    "category_to_gate_kind",
]


# The category → glob map. Kebab/snake categories are stable identifiers the HITL gate routes
# on. Mirrors the forked source; ``contracts`` covers smart-contract / chain config (relevant
# to a payments product), kept because it is genuinely a "human must review" surface.
DEFAULT_PROTECTED_PATHS: dict[str, tuple[str, ...]] = {
    "auth": (
        "**/auth/**",
        "**/authentication/**",
        "**/authorization/**",
        "**/oauth/**",
        "**/jwt/**",
        "**/session/**",
        "**/sessions/**",
        "**/permission*/**",
        "**/rbac/**",
    ),
    "payments": (
        "**/billing/**",
        "**/payment*/**",
        "**/checkout/**",
        "**/stripe/**",
        "**/invoice*/**",
        "**/subscription*/**",
    ),
    "migrations": (
        "**/migrations/**",
        "**/migration*/**",
        "**/prisma/migrations/**",
        "**/schema.prisma",
        "**/alembic/**",
    ),
    "secrets": (
        ".env",
        ".env.*",
        "**/.env",
        "**/.env.*",
        "**/secret*/**",
        "**/credential*/**",
        "**/*secret*",
        "**/*credential*",
        "**/*private-key*",
        # SSH / TLS keys + key material (the hook's global-blocked list covers these at write-time;
        # the classifier needs them too so a key change routes to a human gate).
        "**/id_rsa*",
        "**/id_ed25519*",
        "**/id_ecdsa*",
        "**/id_dsa*",
        "**/*.pem",
        "**/*.key",
        "**/*.p12",
        "**/*.pfx",
        "**/*.keystore",
        "**/.npmrc",
        "**/.pypirc",
    ),
    "contracts": (
        "**/contracts/**",
        "**/*.sol",
        "**/hardhat.config.*",
        "**/foundry.toml",
    ),
    "compliance": (
        "**/compliance/**",
        "**/privacy/**",
        "**/legal/**",
        "**/policy/**",
        "**/policies/**",
        "**/gdpr/**",
        "**/kyc/**",
        "**/aml/**",
    ),
    "infra": (
        "**/.github/workflows/**",
        "**/.gitlab-ci.yml",
        "**/Jenkinsfile",
        "**/Dockerfile",
        "**/Dockerfile.*",
        "**/docker-compose*.yml",
        "**/docker-compose*.yaml",
        "**/terraform/**",
        "**/*.tf",
        "**/k8s/**",
        "**/kubernetes/**",
        "**/kubeconfig",
        "**/helm/**",
        "**/deploy*/**",
        "**/infra/**",
    ),
}


# Which sensitive categories are also IRREVERSIBLE (touching them is a human-gated action, not
# just a human-reviewed one). Used to pick the :class:`GateKind` for the disk-file HITL gate.
IRREVERSIBLE_CATEGORIES: frozenset[str] = frozenset(
    {"secrets", "migrations", "payments", "infra"}
)


def category_to_gate_kind(category: str) -> str:
    """Map a protected-path *category* to the HITL :class:`GateKind` value to open.

    Returns one of the irreversible gate kinds (``deploy`` / ``deps`` / ``secrets`` /
    ``migration``) — the routing the architecture's "human gates for irreversible actions"
    envelope uses. A non-irreversible category (auth / contracts / compliance) maps to the
    generic ``secrets``-style review kind, because a sensitive-but-reversible change still
    deserves a human look; the engine never decides this silently.
    """
    mapping = {
        "secrets": "secrets",
        "migrations": "migration",
        "payments": "deploy",
        "infra": "deploy",
        "auth": "secrets",
        "contracts": "deploy",
        "compliance": "secrets",
    }
    return mapping.get(category, "secrets")


class ProtectedPathMatch(BaseModel):
    """One protected-path rule matched by one changed path."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    path: str
    category: str
    pattern: str


class ProtectedPathResult(BaseModel):
    """Human-review decision for a proposed set of changed paths."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    requires_human_review: bool
    matches: list[ProtectedPathMatch] = Field(default_factory=list)

    @property
    def categories(self) -> list[str]:
        """The distinct sensitive categories hit, in first-seen order (stable)."""
        seen: list[str] = []
        for m in self.matches:
            if m.category not in seen:
                seen.append(m.category)
        return seen

    @property
    def irreversible(self) -> bool:
        """True iff any matched category is irreversible (a human-GATED action)."""
        return any(c in IRREVERSIBLE_CATEGORIES for c in self.categories)


class ProtectedPathConfig(BaseModel):
    """Protected-path rules, with optional repo-specific overlays."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    protected_paths: dict[str, tuple[str, ...]] = Field(default_factory=dict)

    @classmethod
    def default(cls) -> ProtectedPathConfig:
        """Return the built-in protected-path config."""
        return cls(protected_paths=dict(DEFAULT_PROTECTED_PATHS))

    @classmethod
    def with_overlay(
        cls,
        overlay: Mapping[str, Iterable[str]] | None = None,
    ) -> ProtectedPathConfig:
        """Return default rules extended with repo-specific protected patterns."""
        protected_paths = {key: tuple(value) for key, value in DEFAULT_PROTECTED_PATHS.items()}
        if overlay:
            for category, patterns in overlay.items():
                protected_paths[category] = (
                    *protected_paths.get(category, ()),
                    *tuple(patterns),
                )
        return cls(protected_paths=protected_paths)


class ProtectedPathGate:
    """Decide whether changed paths need explicit human review/approval."""

    def __init__(self, config: ProtectedPathConfig | None = None) -> None:
        self.config = config or ProtectedPathConfig.default()

    def check_paths(self, paths: Iterable[str]) -> ProtectedPathResult:
        """Check changed paths against the protected-path rules."""
        matches: list[ProtectedPathMatch] = []

        for raw_path in paths:
            path = _normalize_path(raw_path)
            if not path:
                continue

            for category, patterns in self.config.protected_paths.items():
                for pattern in patterns:
                    normalized_pattern = _normalize_path(pattern)
                    if _matches(path, normalized_pattern):
                        matches.append(
                            ProtectedPathMatch(
                                path=path,
                                category=category,
                                pattern=normalized_pattern,
                            )
                        )
                        break

        return ProtectedPathResult(
            requires_human_review=bool(matches),
            matches=matches,
        )


def _normalize_path(path: str) -> str:
    normalized = path.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _matches(path: str, pattern: str) -> bool:
    if fnmatch.fnmatchcase(path, pattern):
        return True
    if pattern.startswith("**/"):
        return fnmatch.fnmatchcase(path, pattern[3:])
    return False
