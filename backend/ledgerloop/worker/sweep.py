"""One-shot sweep, then exit. The cron-job form of ``worker/sweeper.py``.

    python -m ledgerloop.worker.sweep

``Sweeper.run_forever`` is a long-lived loop that sleeps between passes, which needs
a process that stays up. A scheduled platform (Render Cron Jobs, Kubernetes CronJob,
systemd timer) supplies the schedule itself and expects the process to do one unit of
work and exit -- so this entry point is the same ``sweep_once`` with the sleeping
removed, and the worker runs with ``LEDGERLOOP_ENABLE_SWEEPER=false``.

Two properties the loop gets for free and a cron run has to arrange deliberately:

**It drains rather than doing exactly one pass.** ``sweep_once`` reads at most
``batch_limit`` stale rows per side. The resident loop just comes round again 30
seconds later; a cron run that stopped after one pass would leave the remainder for
five minutes and let a backlog outlive every schedule tick. So this repeats until a
pass resolves nothing, bounded by ``--max-passes`` so a pathological backlog cannot
run past the next scheduled start.

**Its exit code is the health signal.** Nothing scrapes an ephemeral container, so
the metrics this records die with the process; the platform's own run history is the
observable. A failed sweep must therefore exit non-zero -- exiting 0 on a caught
exception would make a silently broken sweeper indistinguishable from a healthy one,
and the sweeper is the component that decides what counts as an unreconciled break.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from ledgerloop.config import Settings, get_settings
from ledgerloop.db.session import build_engine, build_sessionmaker
from ledgerloop.observability.logging import configure_logging, get_logger
from ledgerloop.worker.sweeper import Sweeper

log = get_logger("ledgerloop.worker.sweep")

#: Enough to clear a large backlog, low enough that a run cannot outlast a 5-minute
#: schedule and overlap the next one. Hitting it is a signal, and it is logged as one.
DEFAULT_MAX_PASSES = 20


async def sweep(settings: Settings | None = None, max_passes: int = DEFAULT_MAX_PASSES) -> int:
    """Drain the stale-row backlog. Returns how many rows reached a terminal state."""
    settings = settings or get_settings()
    configure_logging(settings)

    engine = build_engine(settings)
    sessionmaker = build_sessionmaker(engine)
    sweeper = Sweeper(sessionmaker, settings)

    total = 0
    passes = 0
    drained = False
    try:
        log.info(
            "sweep.started",
            unmatched_after_s=settings.unmatched_after_s,
            max_passes=max_passes,
        )
        while passes < max_passes:
            passes += 1
            resolved = await sweeper.sweep_once()
            total += resolved
            if resolved == 0:
                # Nothing left past the window. Any row still pending is younger than
                # unmatched_after_s and is the matcher's to resolve, not ours.
                drained = True
                break

        if not drained:
            # Still finding work when the pass budget ran out: the backlog is growing
            # faster than the schedule drains it. The rows are safe -- they stay
            # pending and the next run picks them up -- but the schedule needs
            # tightening or the matcher needs scaling, and that is a human decision.
            log.warning(
                "sweep.backlog_not_drained",
                passes=passes,
                resolved=total,
                hint="raise --max-passes, shorten the schedule, or scale the matcher",
            )
        log.info("sweep.finished", resolved=total, passes=passes)
    finally:
        await engine.dispose()

    return total


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ledgerloop.worker.sweep",
        description="Run the unmatched sweeper once and exit. Intended for a cron schedule.",
    )
    parser.add_argument(
        "--max-passes",
        type=int,
        default=DEFAULT_MAX_PASSES,
        help=f"Stop after this many draining passes (default: {DEFAULT_MAX_PASSES}).",
    )
    args = parser.parse_args()

    try:
        asyncio.run(sweep(max_passes=args.max_passes))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 -- the exit code is the whole report
        # No re-raise: a traceback on stderr plus exit 1 is what a scheduler surfaces,
        # and an unhandled exception would add nothing an operator can use.
        log.error("sweep.failed", error=str(exc), exc_info=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
