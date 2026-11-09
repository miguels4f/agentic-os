"""Env-guarded durable execution for HyperWilfred build waves.

Forked-and-cleaned from the sw-agent ``durable.py`` PATTERN (the "rent durability, keep the
moat" template), re-granularized from its cron-*session* unit to the HyperWilfred build-*wave*
unit. ZERO ``common_py`` imports and zero sw-agent imports — this is standalone source.

Two durability layers exist in HyperWilfred and they compose:

* **The pure path (always available, no external dependency):** artifact-scan resume
  (:mod:`hyperwilfred_core.resume`). Every step writes its artifact to disk atomically; on
  restart the engine scans for the first missing/invalid artifact and re-runs from there. This
  works with NO database and NO network — it is the default and the fallback.
* **The durable path (engaged ONLY when configured):** a DBOS-backed workflow that persists each
  completed wave to Postgres, so a process killed mid-run resumes the *wave sequence* from the
  last committed wave without re-deciding completed waves. Durability is opt-in via the
  environment; if DBOS is not configured (or its package is absent), HyperWilfred silently uses
  the pure path. **DBOS is never a hard dependency.**

The unit of durable work — :func:`run_wave_step` — is plain Python (no DBOS decorators, no DB),
so it stays unit-testable without Postgres. The durable wrapper merely wraps it in DBOS steps
when DBOS is live. Like the rest of the engine this module calls NO LLM and spawns NOTHING: the
caller supplies an ``execute`` callback (the adapter's job) and this module only sequences it
durably.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field

_log = logging.getLogger(__name__)

# Module-level guard so DBOS is launched at most once per process.
_dbos_launched = False


class DBOSNotConfigured(RuntimeError):
    """Raised when the durable runner is explicitly requested but DBOS is not configured."""


def dbos_configured() -> bool:
    """True iff a DBOS database endpoint is configured via the environment.

    Presence of an env var is the single signal (mirrors how ai-common guards Traceloop). No
    connection is attempted here, so calling this is free and side-effect-free. ``HW_DURABLE``
    must be truthy AND a DBOS database endpoint must be set — durability is doubly opt-in so a
    stray ``DBOS_*`` var in the environment never silently changes HyperWilfred's behaviour.
    """
    endpoint = bool(os.getenv("DBOS_DATABASE_URL") or os.getenv("DBOS_SYSTEM_DATABASE"))
    enabled = os.getenv("HW_DURABLE", "").strip().lower() in {"1", "true", "yes", "on"}
    return endpoint and enabled


@dataclass
class WaveStepResult:
    """Outcome of running one build wave through the pipeline."""

    wave_id: str
    proceeded: bool
    accepted: bool
    detail: str = ""


@dataclass
class WaveRunReport:
    """The outcome of running an ordered sequence of waves."""

    durable: bool  # True iff the DBOS durable path actually ran (vs. the pure fallback)
    results: list[WaveStepResult] = field(default_factory=list)

    @property
    def all_accepted(self) -> bool:
        return bool(self.results) and all(r.accepted for r in self.results)


# ---------------------------------------------------------------------------
# Pure wave step — the durable unit of work (no DBOS, no DB)
# ---------------------------------------------------------------------------


def run_wave_step(wave_id: str, execute: Callable[[str], bool]) -> WaveStepResult:
    """Run one build wave: call the adapter's ``execute`` and record acceptance. Pure, DB-free.

    Args:
        wave_id: the identifier of the wave to run (e.g. ``"wave-0"``).
        execute: caller-supplied callback that does the real work for the wave (dispatch its
            packets, run the engine acceptance gate per packet) and returns whether the WHOLE
            wave was accepted. The engine — not this function — owns what a wave contains.

    Returns:
        A :class:`WaveStepResult`. ``proceeded`` is always True here (the gate-halt decision is
        made by the caller across waves, in :func:`run_waves_in_process`).
    """
    accepted = bool(execute(wave_id))
    return WaveStepResult(
        wave_id=wave_id,
        proceeded=True,
        accepted=accepted,
        detail="accepted" if accepted else "rejected — chain halts",
    )


def run_waves_in_process(
    wave_ids: list[str], execute: Callable[[str], bool]
) -> WaveRunReport:
    """Run waves in order in-process (the pure, non-durable reference runner).

    Stops at the first wave that is not accepted (no cascade past a failed wave — the downstream
    waves simply have not had their turn). This is the same control flow the DBOS path makes
    durable, but plain and synchronous, with no external dependency.
    """
    results: list[WaveStepResult] = []
    for wave_id in wave_ids:
        result = run_wave_step(wave_id, execute)
        results.append(result)
        if not result.accepted:
            break
    return WaveRunReport(durable=False, results=results)


# ---------------------------------------------------------------------------
# DBOS durable wrapper (lazy, guarded — never imported/launched unless live)
# ---------------------------------------------------------------------------


def _launch_dbos() -> bool:
    """Lazily configure + launch DBOS, exactly once. Strict no-op if unconfigured.

    Returns True iff DBOS is live for this process. Any import or launch failure is swallowed
    (logged at debug) and returns False — durability must NEVER break a run; the pure path always
    remains usable.
    """
    global _dbos_launched
    if _dbos_launched:
        return True
    if not dbos_configured():
        return False
    try:
        from dbos import DBOS, DBOSConfig  # noqa: PLC0415  (lazy by design)

        config: DBOSConfig = {
            "name": os.getenv("DBOS_APP_NAME", "hyperwilfred"),
            "database_url": os.getenv("DBOS_DATABASE_URL")
            or os.getenv("DBOS_SYSTEM_DATABASE", ""),
        }
        DBOS(config=config)
        DBOS.launch()
        _dbos_launched = True
        return True
    except Exception as exc:  # pragma: no cover - env/Postgres-dependent
        _log.debug("DBOS launch skipped (%s); pure orchestration unaffected.", exc)
        return False


def run_waves_durable(
    wave_ids: list[str], execute: Callable[[str], bool]
) -> WaveRunReport:
    """Run the wave sequence as a DBOS durable workflow.

    Each wave is wrapped as a DBOS *step* so the sequence survives a crash and resumes from the
    last committed wave on restart. Requires DBOS configured (``HW_DURABLE`` truthy AND a
    reachable Postgres endpoint). When DBOS is NOT configured this raises
    :class:`DBOSNotConfigured`; callers that want a graceful offline fallback should use
    :func:`run_waves` (which branches automatically) instead of calling this directly.
    """
    if not _launch_dbos():
        raise DBOSNotConfigured(
            "DBOS is not configured. Set HW_DURABLE=1 and DBOS_DATABASE_URL (or "
            "DBOS_SYSTEM_DATABASE) to a reachable Postgres instance, or use run_waves() / "
            "run_waves_in_process() for the offline, non-durable runner."
        )

    from dbos import DBOS  # noqa: PLC0415  (only reached when DBOS is live)

    @DBOS.step()
    def _durable_wave_step(wave_id: str) -> WaveStepResult:
        return run_wave_step(wave_id, execute)

    @DBOS.workflow()
    def _durable_pipeline() -> list[WaveStepResult]:
        results: list[WaveStepResult] = []
        for wave_id in wave_ids:
            result = _durable_wave_step(wave_id)
            results.append(result)
            if not result.accepted:
                break
        return results

    return WaveRunReport(durable=True, results=_durable_pipeline())


def run_waves(
    wave_ids: list[str],
    execute: Callable[[str], bool],
    *,
    prefer_durable: bool = True,
) -> WaveRunReport:
    """Run the wave sequence, using DBOS durability ONLY when configured, else the pure path.

    This is the single entry point a driver should call. It engages the durable workflow when
    (and only when) ``prefer_durable`` and DBOS is configured AND launchable; in every other
    case — durability disabled, DBOS unconfigured, the ``dbos`` package absent, or a launch
    failure — it transparently runs the pure in-process path. The pure path ALWAYS works with no
    external dependency, so a run never fails for lack of durability.
    """
    if prefer_durable and dbos_configured() and _launch_dbos():
        try:
            return run_waves_durable(wave_ids, execute)
        except DBOSNotConfigured:  # pragma: no cover - race: env changed mid-call
            pass
    return run_waves_in_process(wave_ids, execute)


__all__ = [
    "DBOSNotConfigured",
    "WaveRunReport",
    "WaveStepResult",
    "dbos_configured",
    "run_wave_step",
    "run_waves",
    "run_waves_durable",
    "run_waves_in_process",
]
