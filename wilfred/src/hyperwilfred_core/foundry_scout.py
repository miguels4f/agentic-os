"""Safe, provider-neutral scouting for reusable agent-system components.

Scouting is deliberately read-only: providers return descriptions and this module only
classifies them.  It never downloads, executes, or copies candidate code.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Protocol, runtime_checkable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from pydantic import Field

from .foundry_models import CanonicalModel


class ReuseClass(StrEnum):
    """The only outcomes a scout may recommend."""

    REFERENCE_ONLY = "reference_only"
    EXTERNAL_DEPENDENCY = "external_dependency"
    LICENSED_ADAPTATION = "licensed_adaptation"
    REJECTED = "rejected"


class ScoutRequest(CanonicalModel):
    problem: str
    capabilities: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    runtimes: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class SearchQuery(CanonicalModel):
    text: str
    dimension: str
    value: str
    tags: list[str] = Field(default_factory=list)


def expand_queries(request: ScoutRequest) -> list[SearchQuery]:
    """Expand each discovery dimension into stable, de-duplicated queries."""
    dimensions = (
        ("problem", [request.problem]),
        ("capability", request.capabilities),
        ("artifact", request.artifacts),
        ("language", request.languages),
        ("runtime", request.runtimes),
        ("tag", request.tags),
    )
    suffix = " ".join(request.tags)
    queries: list[SearchQuery] = []
    seen: set[tuple[str, str]] = set()
    for dimension, values in dimensions:
        for raw_value in values:
            value = raw_value.strip()
            if not value or (dimension, value.casefold()) in seen:
                continue
            seen.add((dimension, value.casefold()))
            text = " ".join(part for part in (value, suffix) if part).strip()
            queries.append(
                SearchQuery(text=text, dimension=dimension, value=value, tags=request.tags)
            )
    return queries


class CandidateChecks(CanonicalModel):
    """Evidence gates. ``None`` means that a required check was not established."""

    provenance: bool | None = None
    maintained: bool | None = None
    security: bool | None = None
    tests: bool | None = None
    compatible: bool | None = None
    primary_source_correct: bool | None = None


class CandidateDossier(CanonicalModel):
    candidate_id: str
    name: str
    url: str
    provenance: list[str] = Field(default_factory=list)
    version_or_commit: str | None = None
    license: str | None = None
    summary: str = ""
    tags: list[str] = Field(default_factory=list)
    preferred_class: ReuseClass = ReuseClass.REFERENCE_ONLY
    checks: CandidateChecks = Field(default_factory=CandidateChecks)
    provider: str = "unknown"


class ProviderResult(CanonicalModel):
    candidates: list[CandidateDossier] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


@runtime_checkable
class SearchProvider(Protocol):
    """Minimal extension contract for local, hosted, or future search backends."""

    def search(self, query: SearchQuery) -> ProviderResult: ...


class InMemorySearchProvider:
    """Deterministic provider intended for network-free tests and fixtures."""

    def __init__(self, candidates: Iterable[CandidateDossier]) -> None:
        self._candidates = tuple(candidates)

    def search(self, query: SearchQuery) -> ProviderResult:
        terms = {query.value.casefold(), *(tag.casefold() for tag in query.tags)}
        matches = []
        for candidate in self._candidates:
            haystack = " ".join(
                (candidate.name, candidate.summary, " ".join(candidate.tags))
            ).casefold()
            if any(term in haystack for term in terms):
                matches.append(candidate)
        return ProviderResult(candidates=matches)


class GitHubSearchProvider:
    """Optional anonymous GitHub repository search using only the standard library."""

    endpoint = "https://api.github.com/search/repositories"

    def __init__(self, *, timeout: float = 5.0, per_page: int = 10) -> None:
        self.timeout = timeout
        self.per_page = max(1, min(per_page, 100))

    def search(self, query: SearchQuery) -> ProviderResult:
        url = f"{self.endpoint}?{urlencode({'q': query.text, 'per_page': self.per_page})}"
        request = Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "hyperwilfred-foundry-scout/1",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                payload = json.loads(response.read().decode("utf-8"))
            items = payload.get("items", []) if isinstance(payload, dict) else []
            if not isinstance(items, list):
                return ProviderResult(warnings=["github returned a malformed items field"])
            candidates = [self._candidate(item) for item in items if isinstance(item, dict)]
            return ProviderResult(candidates=candidates)
        except HTTPError as exc:
            detail = "rate limited" if exc.code in {403, 429} else f"HTTP {exc.code}"
            return ProviderResult(warnings=[f"github search unavailable: {detail}"])
        except (URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError) as exc:
            return ProviderResult(warnings=[f"github search unavailable: {type(exc).__name__}"])

    @staticmethod
    def _candidate(item: Mapping[str, object]) -> CandidateDossier:
        license_data = item.get("license")
        spdx = license_data.get("spdx_id") if isinstance(license_data, dict) else None
        url = str(item.get("html_url") or "")
        full_name = str(item.get("full_name") or item.get("name") or url)
        topics = item.get("topics")
        tags = [str(value) for value in topics] if isinstance(topics, list) else []
        return CandidateDossier(
            candidate_id=str(item.get("id") or url),
            name=full_name,
            url=url,
            provenance=[url] if url else [],
            # Repository search does not prove what immutable commit was reviewed.
            version_or_commit=None,
            license=str(spdx) if spdx and spdx != "NOASSERTION" else None,
            summary=str(item.get("description") or ""),
            tags=tags,
            provider="github",
            checks=CandidateChecks(
                provenance=bool(url),
                maintained=not bool(item.get("archived", False)),
                primary_source_correct=bool(url),
                # Search metadata cannot establish these gates; fail closed later.
                security=None,
                tests=None,
                compatible=None,
            ),
        )


class ProviderRegistry:
    """Small provider-neutral registry; registering does not trigger a search."""

    def __init__(self) -> None:
        self._providers: dict[str, SearchProvider] = {}

    def register(self, name: str, provider: SearchProvider) -> None:
        if not name.strip():
            raise ValueError("provider name must not be empty")
        if not isinstance(provider, SearchProvider):
            raise TypeError("provider must implement SearchProvider")
        self._providers[name] = provider

    def get(self, name: str) -> SearchProvider:
        try:
            return self._providers[name]
        except KeyError as exc:
            raise KeyError(f"unknown search provider: {name}") from exc

    def items(self) -> tuple[tuple[str, SearchProvider], ...]:
        return tuple(sorted(self._providers.items()))


class ReusePolicy(CanonicalModel):
    allowed_licenses: set[str] = Field(
        default_factory=lambda: {"Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "MIT"}
    )
    minimum_provenance_sources: int = Field(default=1, ge=1)
    require_version_or_commit: bool = True
    required_checks: tuple[str, ...] = (
        "provenance",
        "maintained",
        "security",
        "tests",
        "compatible",
        "primary_source_correct",
    )


class ReuseDecision(CanonicalModel):
    candidate_id: str
    candidate_url: str
    provenance: list[str]
    version_or_commit: str | None
    classification: ReuseClass
    rationale: list[str]


def decide_reuse(candidate: CandidateDossier, policy: ReusePolicy | None = None) -> ReuseDecision:
    """Apply the deterministic, fail-closed reuse floor to one dossier."""
    policy = policy or ReusePolicy()
    failures: list[str] = []
    if not candidate.url:
        failures.append("missing candidate URL")
    else:
        parsed_url = urlparse(candidate.url)
        if parsed_url.scheme.casefold() != "https" or not parsed_url.hostname:
            failures.append("candidate URL must use HTTPS with a host")
    if len(candidate.provenance) < policy.minimum_provenance_sources:
        failures.append(
            "insufficient provenance: "
            f"{len(candidate.provenance)}/{policy.minimum_provenance_sources} sources"
        )
    if policy.require_version_or_commit and not candidate.version_or_commit:
        failures.append("missing reviewed version or commit")
    if not candidate.license:
        failures.append("missing license")
    elif candidate.license not in policy.allowed_licenses:
        failures.append(f"incompatible license: {candidate.license}")
    check_values = candidate.checks.model_dump()
    for check in policy.required_checks:
        if check not in check_values:
            failures.append(f"unknown required check: {check}")
        elif check_values[check] is not True:
            failures.append(f"required check failed or missing: {check}")
    classification = ReuseClass.REJECTED if failures else candidate.preferred_class
    rationale = failures or [f"all reuse gates passed for {classification.value}"]
    return ReuseDecision(
        candidate_id=candidate.candidate_id,
        candidate_url=candidate.url,
        provenance=candidate.provenance,
        version_or_commit=candidate.version_or_commit,
        classification=classification,
        rationale=rationale,
    )


class ScoutReport(CanonicalModel):
    queries: list[SearchQuery]
    dossiers: list[CandidateDossier]
    decisions: list[ReuseDecision]
    warnings: list[str] = Field(default_factory=list)


def scout(
    request: ScoutRequest,
    providers: Iterable[SearchProvider],
    *,
    policy: ReusePolicy | None = None,
) -> ScoutReport:
    """Search without side effects, de-duplicate dossiers, then evaluate them."""
    queries = expand_queries(request)
    by_id: dict[str, CandidateDossier] = {}
    warnings: list[str] = []
    for provider in providers:
        for query in queries:
            try:
                result = provider.search(query)
            except Exception as exc:  # A third-party provider must not corrupt accumulated results.
                warnings.append(f"provider failed for {query.text!r}: {type(exc).__name__}")
                continue
            warnings.extend(result.warnings)
            for candidate in result.candidates:
                by_id.setdefault(candidate.candidate_id, candidate)
    dossiers = [by_id[key] for key in sorted(by_id)]
    decisions = [decide_reuse(candidate, policy) for candidate in dossiers]
    return ScoutReport(queries=queries, dossiers=dossiers, decisions=decisions, warnings=warnings)


__all__ = [
    "CandidateChecks",
    "CandidateDossier",
    "GitHubSearchProvider",
    "GitHubProvider",
    "InMemoryProvider",
    "InMemorySearchProvider",
    "ProviderRegistry",
    "ProviderResult",
    "ReuseClass",
    "ReuseDecision",
    "ReusePolicy",
    "ScoutReport",
    "ScoutRequest",
    "SearchRequest",
    "SearchProvider",
    "SearchQuery",
    "decide_reuse",
    "expand_queries",
    "scout",
]

# Concise aliases for callers that do not need the backend qualifier.
InMemoryProvider = InMemorySearchProvider
GitHubProvider = GitHubSearchProvider
SearchRequest = ScoutRequest
