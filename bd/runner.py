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
from bd.config import Config
from bd.cost import estimate_cost
from bd.llm import Usage
from bd.scanners.base import ScanOutcome, Scanner, store_signals


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
    skipped_budget: int = 0   # (account, scanner) calls never started because of the cost cap
    budget_hit: bool = False
    est_cost: float = 0.0     # priced calls only
    unpriced_calls: int = 0
    tokens: int = 0
    new_signals: int = 0
    errors: int = 0


def is_due(acc: Account, as_of: date, cfg: Config, force: bool = False) -> bool:
    if force or not acc.last_scanned:
        return True
    weeks = cfg.scan.tiers[acc.tier].every_weeks
    age = (as_of - date.fromisoformat(acc.last_scanned)).days
    return age >= weeks * 7 - cfg.scan.cadence_grace_days


def run_scans(conn: sqlite3.Connection, cfg: Config, scanners: list[Scanner], accounts: list[Account],
              as_of: date, model: str | None, workers: int | None = None, force: bool = False,
              max_cost: float | None = None,
              log: Callable[[str], None] = lambda s: None) -> RunSummary:
    workers = max(1, workers or cfg.llm.concurrency)
    cap = cfg.llm.max_cost_per_run_usd if max_cost is None else max_cost
    summary = RunSummary(run_date=as_of)

    due = []
    for acc in accounts:
        (due if is_due(acc, as_of, cfg, force) else summary.skipped_not_due).append(acc)

    queue: deque[tuple[Account, Scanner]] = deque((a, s) for a in due for s in scanners)
    remaining = {a.id: len(scanners) for a in due}
    failed: set[int] = set()
    inflight: dict[Future[ScanOutcome], tuple[Account, Scanner]] = {}

    def finish(acc: Account, sc: Scanner, out: ScanOutcome) -> None:
        run = summary.accounts.setdefault(acc.id, AccountRun(acc))
        ins, known = store_signals(conn, acc.id, out.signals, as_of)
        cost = estimate_cost(out.usage, model, cfg)
        if cost is None:
            run.est_cost = None
            summary.unpriced_calls += 1
        else:
            summary.est_cost += cost
            if run.est_cost is not None:
                run.est_cost += cost
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
            if remaining[acc.id] == 0 and acc.id not in failed:
                conn.execute("UPDATE accounts SET last_scanned = ? WHERE id = ?", (as_of.isoformat(), acc.id))
        log(f"  {acc.name} [{sc.name}]: {ins} new, {known} known, {len(out.rejections)} rejected, "
            f"{out.usage.total_tokens:,} tokens, " + ("unpriced" if cost is None else f"${cost:.4f}")
            + (f"  ERROR {out.error}" if out.error else ""))

    with ThreadPoolExecutor(max_workers=workers) as ex:
        while queue or inflight:
            while queue and len(inflight) < workers and not summary.budget_hit:
                acc, sc = queue.popleft()
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
            if cap is not None and summary.est_cost >= cap and not summary.budget_hit:
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
