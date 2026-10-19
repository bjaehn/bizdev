"""Does the combined sweep find what the separate scanners already found, and for how much less?

A side-by-side test that stores nothing. The sweep runs on a few companies that already have signals from the separate leadership, press, events
and technology scanners; what it finds is set beside what those scanners stored for the same company in the same window, and its cost beside what
those four calls cost the last time they ran. The sweep takes over the default scan only if you read this and decide it does.

The stored signals are the baseline, so this measures recall against what is already known and also counts what the sweep found that was not stored
(`new`). It cannot tell whether a `new` signal is right; each one still passed the same acceptance rules (a URL the search returned and that loads)
and is listed with its source for you to open."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta

from bd.accounts import Account
from bd.budget import Reserve
from bd.config import Config
from bd.cost import estimate_cost
from bd.scanners.base import AcceptedSignal, Scanner
from bd.scanners.sweep import SweepScanner
from bd.urls import normalize_url

REPLACED_STEPS = ("scan:leadership", "scan:press", "scan:events", "scan:tech_procurement")     # the calls the sweep stands in for


@dataclass
class Stored:
    type: str
    event_date: str
    url: str


@dataclass
class CompareRow:
    account: Account
    stored: list[Stored] = field(default_factory=list)
    found: list[AcceptedSignal] = field(default_factory=list)
    matched: int = 0              # found signals that are among the stored ones (same source page, or same type and date)
    new: int = 0                  # found, and not stored by the separate scanners
    missed: list[Stored] = field(default_factory=list)       # stored, and the sweep did not find
    cost: float | None = None     # None: the model has no price in config.yaml
    searches: int = 0
    prior_cost: float | None = None      # what the separate scanners cost the last time they ran for this company
    error: str | None = None
    skipped: bool = False         # never started: the spending limit


def stored_signals(conn: sqlite3.Connection, account_id: int, since: str) -> list[Stored]:
    marks = ",".join("?" * len(SweepScanner.allowed_types))
    return [Stored(r["type"], r["event_date"], r["source_url"]) for r in conn.execute(
        f"SELECT type, event_date, source_url FROM signals WHERE account_id = ? AND event_date >= ? AND type IN ({marks}) "
        "ORDER BY event_date DESC, id DESC", (account_id, since, *sorted(SweepScanner.allowed_types)))]


def prior_cost(conn: sqlite3.Connection, account_id: int) -> float | None:
    """What the four replaced scanners cost for this company on the most recent day any of them ran; None if never run or unpriced."""
    marks = ",".join("?" * len(REPLACED_STEPS))
    day = conn.execute(f"SELECT MAX(run_date) FROM runs WHERE account_id = ? AND step IN ({marks}) AND est_cost IS NOT NULL",
                       (account_id, *REPLACED_STEPS)).fetchone()[0]
    if not day:
        return None
    return conn.execute(f"SELECT SUM(est_cost) FROM runs WHERE account_id = ? AND run_date = ? AND step IN ({marks}) AND est_cost IS NOT NULL",
                        (account_id, day, *REPLACED_STEPS)).fetchone()[0]


def pick_accounts(conn: sqlite3.Connection, accounts: list[Account], as_of: date, window_days: int, count: int) -> list[Account]:
    """The companies with the most stored signals of the kinds the sweep covers (a test needs something to find), most first."""
    since = (as_of - timedelta(days=window_days)).isoformat()
    n = {a.id: len(stored_signals(conn, a.id, since)) for a in accounts}
    return sorted((a for a in accounts if n[a.id]), key=lambda a: (-n[a.id], a.name))[:count]


def _same(found: AcceptedSignal, stored: Stored) -> bool:
    e = found.extract
    return normalize_url(e.source_url) == normalize_url(stored.url) or (e.type == stored.type and e.event_date.isoformat() == stored.event_date)


def compare_account(conn: sqlite3.Connection, cfg: Config, scanner: Scanner, account: Account, as_of: date, model: str | None) -> CompareRow:
    since = (as_of - timedelta(days=cfg.scan.window_days)).isoformat()
    row = CompareRow(account, stored=stored_signals(conn, account.id, since), prior_cost=prior_cost(conn, account.id))
    out = scanner.scan(account, as_of)
    row.found, row.error, row.searches = out.signals, out.error, out.usage.web_searches
    row.cost = estimate_cost(out.usage, scanner.model_name or model, cfg)
    row.matched = sum(any(_same(f, s) for s in row.stored) for f in row.found)
    row.new = len(row.found) - row.matched
    row.missed = [s for s in row.stored if not any(_same(f, s) for f in row.found)]
    return row


def run_compare(conn: sqlite3.Connection, cfg: Config, scanner: Scanner, accounts: list[Account], as_of: date, model: str | None,
                max_cost: float | None = None, log: Callable[[str], None] = lambda s: None) -> list[CompareRow]:
    """One sweep per company, one after another, stopping before the spending limit. Nothing is written to the database."""
    rows: list[CompareRow] = []
    reserve, spent = Reserve(cfg.llm.call_reserve_usd), 0.0
    for acc in accounts:
        if not reserve.allows(spent, 0, max_cost):
            rows.append(CompareRow(acc, skipped=True))
            continue
        row = compare_account(conn, cfg, scanner, acc, as_of, model)
        rows.append(row)
        spent += row.cost or 0.0
        reserve.note(row.cost, row.searches > 0)
        log(f"  {acc.name}: {len(row.found)} found, {row.searches} searches" + (f", ${row.cost:.4f}" if row.cost is not None else ", unpriced")
            + (f"  ERROR {row.error}" if row.error else ""))
    return rows


def _money(x: float | None) -> str:
    return "unknown" if x is None else f"${x:.2f}"


def report(rows: list[CompareRow], model: str | None) -> list[str]:
    """The side-by-side in plain words."""
    done = [r for r in rows if not r.skipped and not r.error]
    lines = [f"Combined sweep on {model or 'the default model'}, nothing stored. Each line: signals the separate scanners had stored for the company "
             "against what the sweep found now.", ""]
    for r in rows:
        if r.skipped:
            lines.append(f"{r.account.name}: not run (spending limit)")
        elif r.error:
            lines.append(f"{r.account.name}: the sweep failed ({r.error})")
        else:
            lines.append(f"{r.account.name}: stored {len(r.stored)} · sweep found {len(r.found)} ({r.matched} the same, {r.new} not stored before) · "
                         f"missed {len(r.missed)} · sweep {_money(r.cost)} against {_money(r.prior_cost)} for the separate scanners last time")
            for f in r.found:
                if not any(_same(f, s) for s in r.stored):
                    e = f.extract
                    lines.append(f"    new: {e.event_date} {e.type}: {e.summary} <{e.source_url}>")
            for s in r.missed[:5]:
                lines.append(f"    missed: {s.event_date} {s.type} <{s.url}>")
    if not done:
        return lines + ["", "No company finished, so there is nothing to compare."]
    stored, found = sum(len(r.stored) for r in done), sum(len(r.found) for r in done)
    matched, new = sum(r.matched for r in done), sum(r.new for r in done)
    priced = [r for r in done if r.cost is not None and r.prior_cost is not None]        # only companies with a cost on both sides can be compared
    cost, past = sum(r.cost for r in priced), sum(r.prior_cost for r in priced)
    lines += ["", f"Together: stored {stored}, sweep found {found} ({matched} the same, {new} not stored before), missed {stored - matched}."]
    if priced:
        lines.append(f"Cost for the {len(priced)} compan{'y' if len(priced) == 1 else 'ies'} with a past cost: sweep {_money(cost)} against {_money(past)} for the separate scanners.")
    else:
        lines.append("No past cost on file to compare with (the separate scanners have not run for these companies since costs were logged).")
    enough = "at least as many signals as were stored" if found >= stored else "fewer signals than were stored"
    cheaper = " and cost less." if priced and cost < past else (" and did not cost less." if priced else ".")
    lines.append(f"The sweep found {enough}{cheaper} Open the 'new' sources to judge whether they are real signals. "
                 "Stored counts build up over several runs, so read this as a guide, not a measurement.")
    return lines
