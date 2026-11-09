"""Deterministic severity floor — a rule in code, never critic judgement.

The pilot runs recorded a UI/engine divergence (the page offered an action the engine blocks) as
"minor". Some defect classes are never minor: a personal-data leak, a gate/permission bypass, and
a divergence between a displayed/offered action and the authoritative check. Any finding or issue
carrying one of these classes is raised to at least ``major`` (so it feeds the patcher and cannot
be deferred as minor). The class comes from an explicit ``tags`` entry OR, as an engine-side
backstop for an un-tagged finding, from a small fixed set of phrases in its title/description.
"""

from __future__ import annotations

import re

from hyperwilfred_core.schemas import CriticFinding, CriticSeverity, SubjectiveIssue

PERSONAL_DATA_LEAK = "personal_data_leak"
GATE_BYPASS = "gate_bypass"
UI_ENGINE_DIVERGENCE = "ui_engine_divergence"

FLOOR_TAGS: frozenset[str] = frozenset({PERSONAL_DATA_LEAK, GATE_BYPASS, UI_ENGINE_DIVERGENCE})

_ALIASES: dict[str, str] = {
    "pii": PERSONAL_DATA_LEAK,
    "privacy": PERSONAL_DATA_LEAK,
    "privacy_leak": PERSONAL_DATA_LEAK,
    "permission_bypass": GATE_BYPASS,
    "gate_bypass": GATE_BYPASS,
    "divergence": UI_ENGINE_DIVERGENCE,
    "ui_divergence": UI_ENGINE_DIVERGENCE,
    "engine_divergence": UI_ENGINE_DIVERGENCE,
}

_PHRASES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        PERSONAL_DATA_LEAK,
        re.compile(
            r"\bpii\b|personal (data|information)|privacy leak|data leak|"
            r"(leaks?|exposes?|reveals?|discloses?)\s+(\w+['’]?s?\s+){0,3}"
            r"(names?|phones?|phone numbers?|emails?|uuids?|identifiers?)",
            re.IGNORECASE,
        ),
    ),
    (
        GATE_BYPASS,
        re.compile(
            r"(bypass\w*|circumvent\w*|evad\w+)\W+(\w+\W+){0,2}"
            r"(gate|permission|approval|allow-?list|guard\w*)|"
            r"do[ _-]?not[ _-]?contact|"
            r"(gate|permission|approval)s?\W+(\w+\W+){0,2}(bypass\w*|skipped|ignored)",
            re.IGNORECASE,
        ),
    ),
    (
        UI_ENGINE_DIVERGENCE,
        re.compile(
            # "diverges" alone is ordinary review vocabulary ("docstring diverges from the code");
            # it only counts when a UI surface and the authoritative engine are both named.
            r"\b(ui|page|client|display|screen|frontend|button|view)\b[^.;\n]{0,80}diverg\w+"
            r"[^.;\n]{0,80}\b(engine|gate|backend|authoritative|rule)|"
            r"\b(engine|gate|backend|authoritative|rule)\b[^.;\n]{0,80}diverg\w+"
            r"[^.;\n]{0,80}\b(ui|page|client|display|screen|frontend|button|view)\b|"
            r"while\s+the\s+(engine|gate)\s+(blocks|refuses|rejects)|"
            r"disagree\w*\s+with\s+(the\s+)?(engine|gate|authoritative)|"
            r"offers?\s+.{0,60}(engine|gate|check)\s+(blocks|rejects|refuses)",
            re.IGNORECASE,
        ),
    ),
)


def normalise_tag(tag: str) -> str:
    key = re.sub(r"[\s\-]+", "_", tag.strip().lower())
    return _ALIASES.get(key, key)


# A negation cancels ONLY the phrase it governs: the few words right before a match, inside the
# same sentence ("no divergence found", "does not leak"). A negation elsewhere in the finding
# ("… not covered by any leak test") must not switch the whole backstop off.
_NEGATION_WORD = re.compile(r"\b(?:no|not|without|never|none|zero|n't)\b", re.IGNORECASE)
_SENTENCE_BREAK = re.compile(r"[.;!?\n]")
_NEGATION_WINDOW = 40


def _negated(text: str, start: int) -> bool:
    window = text[max(0, start - _NEGATION_WINDOW):start]
    breaks = list(_SENTENCE_BREAK.finditer(window))
    if breaks:
        window = window[breaks[-1].end():]
    return bool(_NEGATION_WORD.search(window))


def floor_classes(tags: list[str], text: str) -> list[str]:
    """The floor classes a finding falls in (explicit tags first, then the phrase backstop).

    The phrase backstop does not fire on a negated statement ("no divergence found").
    """
    found = [t for t in dict.fromkeys(normalise_tag(t) for t in tags) if t in FLOOR_TAGS]
    for cls, pattern in _PHRASES:
        if cls in found:
            continue
        if any(not _negated(text, m.start()) for m in pattern.finditer(text)):
            found.append(cls)
    return found


def _floored(severity: CriticSeverity, classes: list[str]) -> CriticSeverity:
    if classes and severity is CriticSeverity.MINOR:
        return CriticSeverity.MAJOR
    return severity


def floor_finding(finding: CriticFinding) -> CriticFinding:
    """Return *finding* raised to >= major if it is in a floor class (idempotent, pure)."""
    classes = floor_classes(finding.tags, f"{finding.title}\n{finding.description}")
    new = _floored(finding.severity, classes)
    if new is finding.severity:
        return finding
    return finding.model_copy(
        update={
            "severity": new,
            "severity_raised_from": finding.severity_raised_from or finding.severity,
            "tags": list(dict.fromkeys([*finding.tags, *classes])),
        }
    )


def floor_issue(issue: SubjectiveIssue) -> SubjectiveIssue:
    """The same floor for a subjective-evaluator issue."""
    classes = floor_classes(issue.tags, f"{issue.criterion}\n{issue.description}")
    new = _floored(issue.severity, classes)
    if new is issue.severity:
        return issue
    return issue.model_copy(
        update={
            "severity": new,
            "severity_raised_from": issue.severity_raised_from or issue.severity,
            "tags": list(dict.fromkeys([*issue.tags, *classes])),
        }
    )


__all__ = [
    "FLOOR_TAGS",
    "GATE_BYPASS",
    "PERSONAL_DATA_LEAK",
    "UI_ENGINE_DIVERGENCE",
    "floor_classes",
    "floor_finding",
    "floor_issue",
    "normalise_tag",
]
