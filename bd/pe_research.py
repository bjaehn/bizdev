"""Capped private-equity research, run inside Update after the paid signal scan: what is left of the update's spending limit, up to the PE limit, and not a cent more.

Three paid steps, in the order that makes the picture fullest for the money:
  1. deals       one search per firm for acquisitions, add-ons and exits; code retrieves each page and keeps only what the page supports (bd.pe_deals);
  2. companies   portfolio companies a fetched page backs are researched for location and size, go through the ordinary fit gate and, if they pass (an
                 unverified size is accepted for a company shown to be in Chicagoland), become accounts that the next Update scans (bd.pe_promote);
  3. portfolios  a firm whose portfolio page was not read in full is searched for its other Chicagoland companies (bd.pe_firms).
Each step may use a share of what is left when it starts (40% of the budget for deals, 60% of the rest for companies, all of the rest for portfolios), so one
step cannot starve the next, and unspent shares roll forward. Calls are started in batches only while the spending gate (bd.budget.Reserve) says the limit would
hold after every call in the batch finished, and the average cost of a call is learned from the calls already made. Spend is read from the `runs` rows the steps
already write, which is also what the Update's own limit counts. When it stops, the report says why (done, the PE limit, or the Update's limit) and what it left:
a stop is never silent."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone

from bd.budget import Reserve
from bd.config import Config
from bd.discover import DiscoverSummary
from bd.pe_deals import DealResearcher, run_deals
from bd.pe_firms import PortfolioFinder, run_portfolio
from bd.pe_promote import promotable, run_promote

DEALS_EVERY_DAYS = 28            # a firm's deals are searched again after this long
PORTFOLIO_EVERY_DAYS = 28
BATCH_MAX = 8                    # calls started together
SHARES = {"deals": 0.4, "companies": 0.6, "portfolios": 1.0}     # the fraction of what is left that a step may use when it starts


def research_cap(pe_budget: float, update_left: float | None) -> tuple[float, str]:
    """(the cap for this run, which limit sets it): the PE limit, or what is left of the Update's own limit when that is smaller."""
    if update_left is None or pe_budget <= update_left:
        return float(pe_budget), "pe_cap"
    return max(float(update_left), 0.0), "update_limit"


@dataclass
class StepResult:
    step: str
    did: int = 0                     # firms or companies researched
    left: int = 0                    # still waiting after this run
    spent: float = 0.0
    added: int = 0                   # companies added to the scan list (companies step)
    note: str = ""


@dataclass
class ResearchReport:
    cap: float
    binder: str                      # pe_cap | update_limit: which limit the cap came from
    spent: float = 0.0
    stopped_by: str = "done"         # done | pe_cap | update_limit
    steps: list[StepResult] = field(default_factory=list)

    def line(self) -> str:
        why = {"done": "everything due was researched", "pe_cap": f"stopped at the ${self.cap:.2f} PE research limit",
               "update_limit": f"stopped at what was left of the Update's spending limit (${self.cap:.2f})"}[self.stopped_by]
        parts = []
        for s in self.steps:
            bit = f"{s.step}: {s.did} researched"
            if s.step == "companies":
                bit += f" ({s.added} added to the scan list)"
            parts.append(bit + (f", {s.left} left" if s.left else ""))
        return f"PE research spent ${self.spent:.2f}; {why}. " + "; ".join(parts) + "."

    def to_json(self) -> str:
        return json.dumps({"cap": round(self.cap, 4), "binder": self.binder, "spent": round(self.spent, 4), "stopped_by": self.stopped_by,
                           "steps": [asdict(s) for s in self.steps]})


class Spend:
    """What this run has spent, from the `runs` rows the paid steps write (the same rows the Update's limit counts)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.start = self.mark()

    def mark(self) -> int:
        return self.conn.execute("SELECT COALESCE(MAX(id), 0) FROM runs").fetchone()[0]

    def total(self) -> float:
        return float(self.conn.execute("SELECT COALESCE(SUM(est_cost), 0) FROM runs WHERE id > ? AND step != 'scan:total'", (self.start,)).fetchone()[0])

    def costs(self, after: int, steps: tuple[str, ...]) -> list[float]:
        q = ",".join("?" * len(steps))
        return [r[0] for r in self.conn.execute(f"SELECT est_cost FROM runs WHERE id > ? AND step IN ({q}) AND est_cost IS NOT NULL", (after, *steps))]


def fit_calls(spend: Spend, reserve: Reserve, cap: float, most: int) -> int:
    """How many calls may start together now: the number the gate allows, so the limit would hold after every one of them finished."""
    n = 0
    while n < most and reserve.allows(spend.total(), n, cap):
        n += 1
    return n


# ---- who is due ---------------------------------------------------------------------------------------------------------------------------------------
_SCOPE = ("(COALESCE(f.scope, '') != 'investor' OR EXISTS (SELECT 1 FROM pe_ownership o JOIN pe_companies c ON c.id = o.company_id "
          "WHERE o.firm_id = f.id AND c.account_id IS NOT NULL))")           # an investor-only firm is researched only when it holds a company on the scan list


def deal_firms(conn: sqlite3.Connection, as_of: date) -> list[sqlite3.Row]:
    """Active firms whose deals have not been searched lately: Chicago firms first, then firms that still hold a transaction lead no page has confirmed."""
    cutoff = (as_of - timedelta(days=DEALS_EVERY_DAYS)).isoformat()
    return conn.execute(
        f"SELECT f.*, EXISTS (SELECT 1 FROM pe_ownership o JOIN pe_evidence e ON e.subject_type = 'ownership' AND e.subject_id = o.id AND e.source_type = 'workbook' "
        f"AND e.claim = 'announced' WHERE o.firm_id = f.id AND o.status = 'unclear') AS open_lead FROM pe_firms f "
        f"WHERE f.status = 'active' AND {_SCOPE} AND (f.last_deals IS NULL OR f.last_deals < ?) "
        f"ORDER BY (f.scope = 'chicago') DESC, open_lead DESC, f.last_deals IS NOT NULL, f.last_deals, f.id", (cutoff,)).fetchall()


def portfolio_firms(conn: sqlite3.Connection, as_of: date) -> list[sqlite3.Row]:
    """Active firms not searched lately whose own portfolio page has not been read in full: Chicago firms first."""
    cutoff = (as_of - timedelta(days=PORTFOLIO_EVERY_DAYS)).isoformat()
    return conn.execute(
        f"SELECT f.* FROM pe_firms f WHERE f.status = 'active' AND {_SCOPE} AND (f.last_portfolio IS NULL OR f.last_portfolio < ?) "
        f"AND NOT EXISTS (SELECT 1 FROM pe_sources s WHERE s.firm_id = f.id AND s.status = 'ok' AND s.coverage = 'complete') "
        f"ORDER BY (f.scope = 'chicago') DESC, f.last_portfolio IS NOT NULL, f.last_portfolio, f.id", (cutoff,)).fetchall()


# ---- the run ------------------------------------------------------------------------------------------------------------------------------------------
def run_research(conn: sqlite3.Connection, cfg: Config, deals: DealResearcher, finder: PortfolioFinder, verifier, enricher, model: str | None, as_of: date,
                 pe_budget: float, update_left: float | None, workers: int | None = None, log: Callable[[str], None] = lambda s: None) -> ResearchReport:
    cap, binder = research_cap(pe_budget, update_left)
    rep = ResearchReport(cap, binder)
    spend, reserve = Spend(conn), Reserve()

    def phase_cap(name: str) -> float:
        s = spend.total()
        return s + max(cap - s, 0.0) * SHARES[name]

    # 1. deals
    step = StepResult("deals")
    due = deal_firms(conn, as_of)
    pcap = phase_cap("deals")
    log(f"Deals: {len(due)} firm(s) due; this step may spend up to ${pcap - spend.total():.2f} of the ${cap - spend.total():.2f} left.")
    start = spend.total()
    while due:
        n = fit_calls(spend, reserve, pcap, min(BATCH_MAX, cfg.pe_firms.max_firms_per_run, len(due)))
        if n == 0:
            break
        batch, due = due[:n], due[n:]
        mark = spend.mark()
        run_deals(conn, cfg, deals, batch, as_of, model, workers, max_cost=max(pcap - spend.total(), 0.0), log=log)
        for c in spend.costs(mark, ("pe_deals",)):
            reserve.note(c)
        step.did += len(batch)
    step.left, step.spent = len(due), spend.total() - start
    rep.steps.append(step)

    # 2. companies a fetched page backs
    step = StepResult("companies")
    pcap = phase_cap("companies")
    start = spend.total()
    log(f"Companies: {len(promotable(conn, cfg, 10_000, researchable_only=True))} backed by a fetched page and not yet researched; up to ${pcap - spend.total():.2f}.")
    for _ in range(20):
        n = fit_calls(spend, reserve, pcap, BATCH_MAX)
        rows = promotable(conn, cfg, n, researchable_only=True) if n else []
        if not rows:
            break
        mark, before = spend.mark(), conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        out: DiscoverSummary = run_promote(conn, cfg, verifier, enricher, as_of, model, len(rows), workers, max_cost=max(pcap - spend.total(), 0.0), log=log)
        for c in spend.costs(mark, ("discover_enrich",)):
            reserve.note(c)
        step.did += out.researched
        step.added += len(out.added)
        if not out.researched and conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == before:
            break                                                    # nothing was researched: do not ask for the same companies again
    step.left, step.spent = len(promotable(conn, cfg, 10_000, researchable_only=True)), spend.total() - start
    rep.steps.append(step)

    # 3. portfolios not read in full
    step = StepResult("portfolios")
    due = portfolio_firms(conn, as_of)
    pcap = phase_cap("portfolios")
    start = spend.total()
    log(f"Portfolios: {len(due)} firm(s) whose portfolio page was not read in full; up to ${pcap - spend.total():.2f}.")
    while due:
        n = fit_calls(spend, reserve, pcap, 1 + cfg.discovery.max_candidates_per_run)
        if n < 2:                                                     # the find call and at least one company to research, or the find call buys nothing
            break
        firm, due = due[0], due[1:]
        capped = cfg.model_copy(update={"discovery": cfg.discovery.model_copy(update={"max_candidates_per_run": n - 1})})
        mark = spend.mark()
        run_portfolio(conn, capped, finder, enricher, [firm], as_of, model, workers, max_cost=max(pcap - spend.total(), 0.0), log=log)
        for c in spend.costs(mark, ("pe_portfolio", "discover_enrich")):
            reserve.note(c)
        step.did += 1
    step.left, step.spent = len(due), spend.total() - start
    rep.steps.append(step)

    rep.spent = spend.total()
    if not any(s.left for s in rep.steps):
        rep.stopped_by = "done"
    else:
        rep.stopped_by = binder
    return rep


# ---- the record ---------------------------------------------------------------------------------------------------------------------------------------
def record_run(conn: sqlite3.Connection, rep: ResearchReport, started: str) -> int:
    """One `pe_runs` row of kind 'research': the cap, what was spent, why it stopped and what each step left. The page and the job note read it."""
    with conn:
        return conn.execute("INSERT INTO pe_runs(kind, started, finished, summary, detail) VALUES ('research', ?, ?, ?, ?)",
                            (started, datetime.now(timezone.utc).isoformat(timespec="seconds"), rep.line(), rep.to_json())).lastrowid
