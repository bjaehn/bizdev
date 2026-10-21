"""Multi-account scan runner: concurrency limit, cadence, cost logging, budget cap.

Network work (scanner.scan) runs on a bounded thread pool; every database write happens on the
calling thread as results come back, so SQLite is never touched from two threads. One failed
account never stops the run.
"""

from __future__ import annotations

import sqlite3
from collections import deque
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import date, timedelta

from bd.accounts import Account
from bd.budget import Reserve
from bd.config import Config
from bd.cost import estimate_cost
from bd.llm import Usage
from bd.postings import known_accessions, learn_board, record_filings, store_board_reading, store_postings
from bd.scanners.base import ScanOutcome, Scanner, store_signals
from bd.signal_coverage import SUCCESSFUL_RUN


@dataclass
class AccountRun:
    account: Account
    tokens: int = 0
    web_searches: int = 0
    est_cost: float | None = 0.0   # None once any call for this account was unpriced
    new_signals: int = 0
    known_signals: int = 0
    rejected: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class RunSummary:
    run_date: date
    accounts: dict[int, AccountRun] = field(default_factory=dict)
    skipped_not_due: list[Account] = field(default_factory=list)
    calls: int = 0            # (account, scanner) calls that finished (successfully or not)
    skipped_budget: int = 0   # (account, scanner) calls never started because of the cost cap
    budget_hit: bool = False
    est_cost: float = 0.0     # priced calls only
    unpriced_calls: int = 0
    tokens: int = 0
    new_signals: int = 0
    errors: int = 0


def accounts_with_signals(conn: sqlite3.Connection, accounts: list[Account], as_of: date, days: int) -> list[Account]:
    """The accounts among `accounts` that already have a stored signal (not a Low-confidence rumour) dated in the last `days` days."""
    if not accounts:
        return []
    ids = [a.id for a in accounts]
    since = (as_of - timedelta(days=days)).isoformat()
    have = {r[0] for r in conn.execute(
        f"SELECT DISTINCT account_id FROM signals WHERE confidence != 'Low' AND event_date >= ? AND account_id IN ({','.join('?' * len(ids))})",
        (since, *ids))}
    return [a for a in accounts if a.id in have]


def deep_due(conn: sqlite3.Connection, accounts: list[Account], as_of: date, cfg: Config, scanner_names: list[str]) -> list[Account]:
    """The accounts among `accounts` that are due their periodic deep look: no successful run of at least one of the extra scanners within
    scan.deep_every_weeks. A run that ended in an error (a failed search, a failed fetch) does not count as a look: it is tried again next time."""
    weeks = cfg.scan.deep_every_weeks
    if not weeks or not accounts or not scanner_names:
        return []
    since = (as_of - timedelta(days=weeks * 7 - cfg.scan.cadence_grace_days)).isoformat()
    ids = [a.id for a in accounts]
    marks = ",".join("?" * len(ids))
    due: set[int] = set()
    for name in scanner_names:
        looked = {r[0] for r in conn.execute(
            f"SELECT DISTINCT account_id FROM runs WHERE step = ? AND run_date >= ? AND {SUCCESSFUL_RUN} AND account_id IN ({marks})",
            (f"scan:{name}", since, *ids))}
        due |= {i for i in ids if i not in looked}
    return [a for a in accounts if a.id in due]


def is_due(acc: Account, as_of: date, cfg: Config, force: bool = False, quiet: bool = False) -> bool:
    """Is the account due for a scan? `quiet` (scanned before, no recent signal) stretches the wait to scan.quiet_every_weeks, never shortens it."""
    if force or not acc.last_scanned:
        return True
    weeks = cfg.scan.tiers[acc.tier].every_weeks
    if quiet and cfg.scan.quiet_every_weeks:
        weeks = max(weeks, cfg.scan.quiet_every_weeks)
    age = (as_of - date.fromisoformat(acc.last_scanned)).days
    return age >= weeks * 7 - cfg.scan.cadence_grace_days


def quiet_accounts(conn: sqlite3.Connection, accounts: list[Account], as_of: date, cfg: Config) -> set[int]:
    """Accounts scanned before that show no signal in the last scan.quiet_days days: they are checked less often."""
    if not cfg.scan.quiet_every_weeks:
        return set()
    scanned = [a for a in accounts if a.last_scanned]
    loud = {a.id for a in accounts_with_signals(conn, scanned, as_of, cfg.scan.quiet_days)}
    return {a.id for a in scanned if a.id not in loud}


def split_due(conn: sqlite3.Connection, accounts: list[Account], as_of: date, cfg: Config, force: bool = False) -> tuple[list[Account], list[Account]]:
    """(due, not due), by tier cadence and the quiet-company wait."""
    quiet = set() if force else quiet_accounts(conn, accounts, as_of, cfg)
    due, later = [], []
    for acc in accounts:
        (due if is_due(acc, as_of, cfg, force, acc.id in quiet) else later).append(acc)
    return due, later


def run_scans(conn: sqlite3.Connection, cfg: Config, scanners: list[Scanner], accounts: list[Account],
              as_of: date, model: str | None, workers: int | None = None, force: bool = False,
              max_cost: float | None = None, touch_last_scanned: bool = True,
              log: Callable[[str], None] = lambda s: None) -> RunSummary:
    workers = max(1, workers or cfg.llm.concurrency)
    cap = cfg.llm.max_cost_per_run_usd if max_cost is None else max_cost
    summary = RunSummary(run_date=as_of)

    due, summary.skipped_not_due = split_due(conn, accounts, as_of, cfg, force)

    # Free scans (no model call) go first and are never held back by the spending limit; paid calls follow in account order.
    queue: deque[tuple[Account, Scanner]] = deque(sorted(((a, s) for a in due for s in scanners if s.applies_to(a)), key=lambda p: not p[1].free))
    remaining = {a.id: sum(s.applies_to(a) for s in scanners) for a in due}
    failed: set[int] = set()
    reserve = Reserve(cfg.llm.call_reserve_usd)
    inflight: dict[Future[ScanOutcome], tuple[Account, Scanner]] = {}

    def paid_inflight() -> int:
        return sum(1 for _, s in inflight.values() if not s.free)

    def finish(acc: Account, sc: Scanner, out: ScanOutcome) -> None:
        run = summary.accounts.setdefault(acc.id, AccountRun(acc))
        ins, known = store_signals(conn, acc.id, out.signals, as_of)
        with conn:
            if out.postings and not out.error:
                store_postings(conn, acc.id, out.postings, as_of, acc.ats_provider if sc.name == "ats" else "search",
                               out.postings_complete)
            elif out.postings_complete and not out.error:        # a complete empty board: everything was taken down
                store_postings(conn, acc.id, [], as_of, acc.ats_provider or "", True)
            if out.board and not out.error:
                store_board_reading(conn, acc.id, as_of, out.board)
            if out.learned_board and not out.error:
                learn_board(conn, acc.id, out.learned_board)
            record_filings(conn, acc.id, sc.name, out.read_filings, as_of)
        cost = estimate_cost(out.usage, sc.model_name or model, cfg)
        reserve.note(cost, out.usage.web_searches > 0)
        if cost is None:
            run.est_cost = None
            summary.unpriced_calls += 1
        else:
            summary.est_cost += cost
            if run.est_cost is not None:
                run.est_cost += cost
        summary.calls += 1
        run.tokens += out.usage.total_tokens
        run.web_searches += out.usage.web_searches
        run.new_signals += ins
        run.known_signals += known
        run.rejected += len(out.rejections)
        if out.error:
            run.errors.append(f"{sc.name}: {out.error}")
            failed.add(acc.id)
            summary.errors += 1
        summary.tokens += out.usage.total_tokens
        summary.new_signals += ins
        notes = "; ".join(x for x in (out.error, ("rejected: " + out.rejection_summary()) if out.rejections else "") if x)
        with conn:
            conn.execute(
                "INSERT INTO runs(run_date,step,account_id,accounts_scanned,signals_found,tokens,est_cost,errors) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (as_of.isoformat(), f"scan:{sc.name}", acc.id, 1, ins, out.usage.total_tokens, cost, notes or None))
            remaining[acc.id] -= 1
            if touch_last_scanned and remaining[acc.id] == 0 and acc.id not in failed:
                conn.execute("UPDATE accounts SET last_scanned = ? WHERE id = ?", (as_of.isoformat(), acc.id))
        log(f"  {acc.name} [{sc.name}]: {ins} new, {known} known, {len(out.rejections)} rejected, "
            f"{out.usage.total_tokens:,} tokens, " + ("unpriced" if cost is None else f"${cost:.4f}")
            + (f"  ERROR {out.error}" if out.error else ""))

    with ThreadPoolExecutor(max_workers=workers) as ex:
        while queue or inflight:
            while queue and len(inflight) < workers and (queue[0][1].free or (
                    not summary.budget_hit and reserve.allows(summary.est_cost, paid_inflight(), cap))):
                acc, sc = queue.popleft()
                if sc.reads_filings:
                    inflight[ex.submit(sc.scan, acc, as_of, known_accessions(conn, acc.id, sc.name))] = (acc, sc)
                else:
                    inflight[ex.submit(sc.scan, acc, as_of)] = (acc, sc)
            if not inflight:
                break
            done, _ = wait(inflight, return_when=FIRST_COMPLETED)
            for fut in done:
                acc, sc = inflight.pop(fut)
                try:
                    out = fut.result()
                except Exception as e:  # a scanner bug must not abort the whole run
                    out = ScanOutcome(account=acc, scanner=sc.name, usage=Usage(),
                                      error=f"{type(e).__name__}: {e}")
                finish(acc, sc, out)
            if cap is not None and not summary.budget_hit and queue and not queue[0][1].free and (
                    summary.est_cost >= cap or not reserve.allows(summary.est_cost, paid_inflight(), cap)):
                summary.budget_hit = True
        summary.skipped_budget = len(queue)

    with conn:
        conn.execute(
            "INSERT INTO runs(run_date,step,account_id,accounts_scanned,signals_found,tokens,est_cost,errors) "
            "VALUES (?,?,NULL,?,?,?,?,?)",
            (as_of.isoformat(), "scan:total", len(summary.accounts), summary.new_signals, summary.tokens,
             None if summary.unpriced_calls and not summary.est_cost else round(summary.est_cost, 6),
             "; ".join(filter(None, [
                 f"{summary.errors} call errors" if summary.errors else "",
                 f"{summary.unpriced_calls} unpriced calls (model not in pricing)" if summary.unpriced_calls else "",
                 f"budget cap hit, {summary.skipped_budget} calls not started" if summary.budget_hit else ""])) or None))
    return summary
