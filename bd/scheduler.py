"""Built-in Monday 05:00 America/Chicago schedule for the hosted service.

The weekly run is the same job the UI's dry-run button runs, but with posting enabled (it posts only when
TEAMS_WEBHOOK_URL is set and BD_DRY_RUN is not 1 - see bd.jobs.sequence_for). A run is skipped if one already
happened for this week's Monday, and catches up if the service was down at 05:00 (up to CATCH_UP_HOURS later)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

from bd.jobs import JobError, JobRunner

CHICAGO = ZoneInfo("America/Chicago")
RUN_AT = dtime(5, 0)
CATCH_UP_HOURS = 12


def _slot(monday_date) -> datetime:
    return datetime.combine(monday_date, RUN_AT, tzinfo=CHICAGO)


def most_recent_slot(now: datetime) -> datetime:
    """The latest Monday 05:00 Chicago at or before `now`."""
    local = now.astimezone(CHICAGO)
    monday = local.date() - timedelta(days=local.weekday())
    slot = _slot(monday)
    return slot if slot <= local else _slot(monday - timedelta(days=7))


def next_run(now: datetime) -> datetime:
    """The next Monday 05:00 Chicago strictly after `now` (built from the local date, so DST is handled)."""
    base = most_recent_slot(now).astimezone(CHICAGO)
    return _slot(base.date() + timedelta(days=7))


def due(now: datetime, last_started_utc: str | None) -> datetime | None:
    """The slot to run now, if one is due: the latest slot is within the catch-up window and nothing has started
    since it. `last_started_utc` is the `created` time (UTC, 'YYYY-MM-DD HH:MM:SS') of the latest weekly job."""
    slot = most_recent_slot(now)
    if now.astimezone(CHICAGO) - slot > timedelta(hours=CATCH_UP_HOURS):
        return None
    if last_started_utc:
        last = datetime.strptime(last_started_utc, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        if last >= slot.astimezone(timezone.utc):
            return None
    return slot


class Scheduler:
    def __init__(self, runner: JobRunner, tick_seconds: float = 60.0,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.runner, self.tick, self.clock = runner, tick_seconds, clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def check(self) -> int | None:
        """Start the weekly job if due and nothing else is running. Returns the job id, or None."""
        last = self.runner.last_scheduled()
        if due(self.clock(), last.created if last else None) is None or self.runner.busy():
            return None
        try:
            return self.runner.submit("weekly", {}, scheduled=True)
        except JobError:
            return None

    def start(self) -> None:
        def loop() -> None:
            while not self._stop.wait(self.tick):
                try:
                    self.check()
                except Exception:
                    import traceback
                    traceback.print_exc()
        self._thread = threading.Thread(target=loop, daemon=True, name="scheduler")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
