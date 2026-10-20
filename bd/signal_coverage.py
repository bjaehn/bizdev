"""How much of the list has actually been scanned for signals, and what that found. Shown on the Admin Home so a thin list is explained by
a number you can act on (companies not scanned yet, updates it takes to reach them) and never by missing company facts.

Read-only and deterministic: counts from the accounts and signals tables and the fit gate's own verdicts. No model or network code."""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

from bd.accounts import list_accounts
from bd.config import Config
from bd.feed import GROUPS, load_items
from bd.fit_gate import check_fit, load_exclusions

WINDOW_DAYS = 90
SUCCESSFUL_RUN = "(errors IS NULL OR errors LIKE 'rejected:%')"      # a `runs` row for a scan that actually ran: rejections are an answer, an error is not
DEEP_WINDOW_WEEKS = 8                                                 # how far back the deep-scan measurement looks


@dataclass
class DeepLooks:
    """What the periodic deep technology and procurement scan has done, from the `runs` table: how many companies it has covered in the cycle, what it added
    and what it cost. Nothing here is estimated by a model; it is counts of what ran and what was stored."""
    cycle_weeks: int = 0                  # scan.deep_every_weeks (0 = the deep scan runs only where a signal shows)
    window_weeks: int = DEEP_WINDOW_WEEKS
    looked: int = 0                       # companies on the list with a successful deep scan inside the cycle
    due: int = 0                          # companies on the list with none: due one the next time they are scanned
    runs: int = 0                         # successful deep scans in the window
    errored: int = 0                      # deep scans in the window that ended in an error (not counted as a look)
    new_signals: int = 0                  # signals those scans stored that nothing had stored before
    companies_with_new: int = 0
    cost: float = 0.0                     # estimated dollars of the deep scans in the window (priced runs only)
    unpriced: int = 0                     # runs in the window with no price


@dataclass
class LeadershipLooks:
    """Leadership signals stored in the window, by where they came from: SEC 8-K filings read by code (free, Item 5.02) or searches. Counts of what was stored."""
    window_days: int = WINDOW_DAYS
    from_filings: int = 0
    from_search: int = 0
    companies: int = 0                    # companies with at least one, from either
    companies_from_filings: int = 0       # companies whose only leadership signals came from filings (a search had not found them)
    filing_companies: int = 0             # companies on the list with a CIK: the ones 8-K filings can speak for


@dataclass
class SignalCoverage:
    on_list: int = 0                                          # companies scanned for signals (everything not known out of scope)
    out_of_scope: int = 0
    scanned: int = 0                                          # scanned at least once by the paid scan
    unscanned: int = 0
    unverified: int = 0                                       # on the list with location or size unconfirmed: scanned all the same
    with_signals: int = 0                                     # companies with at least one signal in the window
    by_group: list[tuple[str, int]] = field(default_factory=list)      # (label, signals in the window), in feed order
    per_update: int = 0
    updates_needed: int = 0                                   # presses of Update my list to scan every company not yet scanned
    last_scanned: str | None = None
    deep: DeepLooks = field(default_factory=DeepLooks)
    leadership: LeadershipLooks = field(default_factory=LeadershipLooks)


def deep_looks(conn: sqlite3.Connection, cfg: Config, today: date, on_list_ids: list[int], scanners: tuple[str, ...]) -> DeepLooks:
    d = DeepLooks(cycle_weeks=cfg.scan.deep_every_weeks)
    if not scanners:
        return d
    steps = [f"scan:{n}" for n in scanners]
    sm = ",".join("?" * len(steps))
    since = (today - timedelta(days=DEEP_WINDOW_WEEKS * 7)).isoformat()
    row = conn.execute(f"SELECT COUNT(*) AS n, COALESCE(SUM(signals_found), 0) AS sig, COUNT(DISTINCT CASE WHEN signals_found > 0 THEN account_id END) AS co, "
                       f"COALESCE(SUM(est_cost), 0) AS cost, SUM(est_cost IS NULL) AS unpriced FROM runs WHERE step IN ({sm}) AND run_date >= ? "
                       f"AND {SUCCESSFUL_RUN}", (*steps, since)).fetchone()
    d.runs, d.new_signals, d.companies_with_new, d.cost, d.unpriced = row["n"], row["sig"], row["co"], row["cost"] or 0.0, row["unpriced"] or 0
    row = conn.execute(f"SELECT COUNT(*) AS n, COALESCE(SUM(est_cost), 0) AS cost FROM runs WHERE step IN ({sm}) AND run_date >= ? AND NOT {SUCCESSFUL_RUN}", (*steps, since)).fetchone()
    d.errored = row["n"]
    d.cost += row["cost"] or 0.0                       # an errored call still spent tokens
    if d.cycle_weeks and on_list_ids:
        cycle_since = (today - timedelta(days=d.cycle_weeks * 7 - cfg.scan.cadence_grace_days)).isoformat()
        im = ",".join("?" * len(on_list_ids))
        looked = {r[0] for r in conn.execute(f"SELECT DISTINCT account_id FROM runs WHERE step IN ({sm}) AND run_date >= ? AND {SUCCESSFUL_RUN} AND account_id IN ({im})",
                                              (*steps, cycle_since, *on_list_ids))}
        d.looked = len(looked)
        d.due = len(on_list_ids) - d.looked
    return d


def leadership_looks(conn: sqlite3.Connection, cfg: Config, today: date) -> LeadershipLooks:
    types = cfg.scoring.families.get("leadership", [])
    out = LeadershipLooks()
    out.filing_companies = conn.execute("SELECT COUNT(*) FROM accounts WHERE status != 'excluded' AND cik IS NOT NULL AND trim(cik) != ''").fetchone()[0]
    if not types:
        return out
    since = (today - timedelta(days=WINDOW_DAYS)).isoformat()
    marks = ",".join("?" * len(types))
    rows = conn.execute(f"SELECT s.account_id, s.source_type FROM signals s JOIN accounts a ON a.id = s.account_id WHERE a.status != 'excluded' AND s.type IN ({marks}) "
                        f"AND s.confidence != 'Low' AND s.event_date >= ?", (*types, since)).fetchall()
    by: dict[int, set[bool]] = {}
    for r in rows:
        filing = r["source_type"] == "sec_filing"
        out.from_filings += filing
        out.from_search += not filing
        by.setdefault(r["account_id"], set()).add(filing)
    out.companies = len(by)
    out.companies_from_filings = sum(v == {True} for v in by.values())
    return out


def signal_coverage(conn: sqlite3.Connection, cfg: Config, today: date, update: dict[str, int],
                    deep_scanners: tuple[str, ...] = ("tech_procurement",)) -> SignalCoverage:
    c = SignalCoverage(per_update=int(update.get("scan", 0)))
    exclusions = load_exclusions(conn)
    ids: list[int] = []
    for a in list_accounts(conn, status=None):
        if a.status == "excluded":
            c.out_of_scope += 1
            continue
        ids.append(a.id)
        c.on_list += 1
        c.scanned += bool(a.last_scanned)
        c.unverified += check_fit(a, exclusions, cfg).unverified
        if a.last_scanned and (c.last_scanned is None or a.last_scanned > c.last_scanned):
            c.last_scanned = a.last_scanned
    c.unscanned = c.on_list - c.scanned
    items = load_items(conn, today, WINDOW_DAYS)
    c.with_signals = len({i.account_id for i in items})
    c.by_group = [(label, sum(i.group == key for i in items)) for key, label in GROUPS]
    c.updates_needed = math.ceil(c.unscanned / c.per_update) if c.unscanned and c.per_update else 0
    c.deep = deep_looks(conn, cfg, today, ids, deep_scanners)
    c.leadership = leadership_looks(conn, cfg, today)
    return c
