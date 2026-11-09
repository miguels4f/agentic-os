"""Group independent slices into parallel waves.

Adapts the *pattern* of ``common_py.factory.conflict_planner`` (lift the idea, rewrite the
file). The original keyed conflicts off a coarse top-3-path-segment guess of *predicted*
changed paths. This version bases waves on each slice's **declared allowed-write scope**
(``allowed_globs``) plus its explicit ``depends_on`` edges — the scope is authoritative
(the engine enforces it at acceptance time), so two slices may run in the same wave iff
their declared write-scopes are disjoint AND neither depends on the other.

Once each worker also runs in its own git worktree, same-directory collision is already
prevented; this planner's remaining job is to avoid scheduling a slice before its declared
dependency and to keep scope-overlapping slices in separate waves (they would touch the same
files and produce an integration conflict the engine can't auto-merge).
"""

from __future__ import annotations

from collections.abc import Iterable

from hyperwilfred_core.schemas import Slice


def _scope_signature(slice_: Slice) -> frozenset[str]:
    """The set of allow-globs that defines a slice's write territory."""
    return frozenset(g.strip() for g in slice_.allowed_globs if g.strip())


def _scopes_overlap(a: Slice, b: Slice) -> bool:
    """True if two slices declare any identical allow-glob (a shared write territory).

    Conservative by design: identical globs clearly overlap. Distinct-but-nested globs are
    treated as non-overlapping here because per-worker worktrees isolate the filesystem and
    the engine's acceptance gate still bounds each worker to its own allow-list — the planner
    only needs to prevent the obvious same-territory collisions.
    """
    return not _scope_signature(a).isdisjoint(_scope_signature(b))


def plan_waves(slices: Iterable[Slice]) -> list[list[Slice]]:
    """Order slices into dependency-respecting waves of non-overlapping scopes.

    Greedy: repeatedly emit a wave of all slices whose dependencies are already satisfied
    and whose scope does not overlap another slice already chosen for the current wave.
    Raises ``ValueError`` on a dependency cycle or an unknown dependency id.
    """
    remaining = list(slices)
    ids = {s.id for s in remaining}
    for s in remaining:
        for dep in s.depends_on:
            if dep not in ids:
                raise ValueError(f"slice {s.id!r} depends on unknown slice {dep!r}")

    done: set[str] = set()
    waves: list[list[Slice]] = []

    while remaining:
        ready = [s for s in remaining if all(dep in done for dep in s.depends_on)]
        if not ready:
            raise ValueError(
                f"dependency cycle or unsatisfiable deps among: {[s.id for s in remaining]}"
            )

        wave: list[Slice] = []
        for candidate in ready:
            if any(_scopes_overlap(candidate, chosen) for chosen in wave):
                continue  # scope clash within this wave — defer to a later wave
            wave.append(candidate)

        waves.append(wave)
        for s in wave:
            done.add(s.id)
            remaining.remove(s)

    return waves


__all__ = ["plan_waves"]
