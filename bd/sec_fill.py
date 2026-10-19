"""bd sec-fill: fill what the SEC itself can tell us about a company, with no model call and no cost.

Two things, both read by code from public SEC data (so each comes with the URL it was read from):
  * a company with no CIK on file, not recorded as private or PE-backed, is looked up in the SEC's list of listed companies by exact
    name (bd.sec_ids); a unique match becomes its CIK;
  * a company with a CIK that lacks revenue, public ownership or a headquarters city gets them from its company-facts and
    submissions files (bd.sec_facts).
Changes go through the same writer as every other edit (validation, audit row per field, fit gate re-run), as actor "sec". A value
already on file is never overwritten. Companies the SEC cannot answer for are left exactly as they were."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

from bd.accounts import Account, list_accounts, log_audit
from bd.config import Config
from bd.curate import apply_selected
from bd.edgar import EdgarClient, EdgarError, facts_url, submissions_url
from bd.sec_facts import read as read_sec_facts
from bd.sec_ids import TICKERS_URL, Index, build_index

ACTOR = "sec"
STEP = "sec_fill"


@dataclass
class Filled:
    account: Account
    facts: list[str] = field(default_factory=list)       # columns written
    cik_from: str = ""                                   # "SEC Name (TICKER)" when the CIK came from the name match
    notes: list[str] = field(default_factory=list)       # what could not be read, or why nothing was filled
    status_before: str = ""
    status_after: str = ""


@dataclass
class FillSummary:
    skipped: str = ""                                    # why nothing ran at all
    candidates: int = 0                                  # companies the SEC could add something for
    read: int = 0                                        # SEC records fetched (the number the per-run cap limits)
    filled: list[Filled] = field(default_factory=list)
    unmatched: list[tuple[str, str]] = field(default_factory=list)   # (name, reason) for companies with no unique SEC match
    passed_gate: int = 0                                 # companies that moved from on hold to ready because of this run
    not_reached: int = 0                                 # candidates left for the next run by the per-run cap
    errors: int = 0


def wants_lookup(a: Account) -> bool:
    """No CIK on file and not recorded as private or PE-backed (a company the SEC would not list)."""
    return not a.cik and a.ownership in (None, "public")


def wants_read(a: Account) -> bool:
    return bool(a.cik) and (a.revenue is None or not a.ownership or not a.hq_city)


def cik_cleared(conn: sqlite3.Connection) -> set[int]:
    """Accounts where a person removed the CIK (a wrong match, say). The SEC list is never used for them again, or the next run would
    put the same wrong CIK back."""
    return {r[0] for r in conn.execute("SELECT DISTINCT account_id FROM audit_log WHERE field = 'cik' AND action = 'update' "
                                       "AND old IS NOT NULL AND new IS NULL AND account_id IS NOT NULL")}


def select(conn: sqlite3.Connection, only: str | None = None) -> list[Account]:
    """Companies worth a look, those on hold first (that is where the SEC data changes something), then the ones tried longest ago."""
    cleared = cik_cleared(conn)
    accts = [a for a in list_accounts(conn, status=None)
             if a.status != "excluded" and ((wants_lookup(a) and a.id not in cleared) or wants_read(a))]
    if only:
        accts = [a for a in accts if only.lower() in a.name.lower()]
    last = {r["account_id"]: r["d"] for r in conn.execute(
        "SELECT account_id, MAX(run_date) AS d FROM runs WHERE step = ? AND account_id IS NOT NULL GROUP BY account_id", (STEP,))}
    accts.sort(key=lambda a: (a.status != "hold_review", a.id in last, last.get(a.id, ""), a.id))
    return accts


def load_index(edgar: EdgarClient) -> tuple[Index | None, str]:
    """(index, note). The list is fetched once per run; trouble is reported, never raised."""
    try:
        index = build_index(edgar.company_tickers())
    except EdgarError as e:
        return None, f"the SEC's company list could not be read ({e})"
    if not index.size:
        return None, "the SEC's company list had no usable entries (its layout may have changed)"
    return index, ""


def _record(conn: sqlite3.Connection, acc: Account, as_of: date, note: str | None) -> None:
    with conn:
        conn.execute("INSERT INTO runs(run_date,step,account_id,accounts_scanned,signals_found,tokens,est_cost,errors) "
                     "VALUES (?,?,?,?,?,?,?,?)", (as_of.isoformat(), STEP, acc.id, 1, 0, 0, 0.0, note))


def fill_one(conn: sqlite3.Connection, cfg: Config, edgar: EdgarClient, index: Index | None, acc: Account, as_of: date,
             summary: FillSummary) -> Filled | None:
    out = Filled(acc, status_before=acc.status)
    cik, picks, evidence = acc.cik, {}, {}

    if not cik:
        if index is None:
            return None
        m = index.lookup(acc.name)
        if m.listing is None:
            summary.unmatched.append((acc.name, m.reason))
            return None
        cik = m.listing.cik
        picks["cik"], evidence["cik"] = cik, TICKERS_URL
        out.cik_from = m.listing.label

    need_rev = acc.revenue is None
    subs = facts = None
    try:
        subs = edgar.submissions(cik)
    except EdgarError as e:
        out.notes.append(f"SEC record not read ({e})")
        summary.errors += 1
    if need_rev:
        try:
            facts = edgar.company_facts(cik)
        except EdgarError as e:
            out.notes.append(f"SEC financial data not read ({e})")
            summary.errors += 1
    summary.read += 1
    sec = read_sec_facts(subs, facts, as_of, need_revenue=need_rev)
    out.notes += sec.notes
    if sec.revenue is not None and need_rev:
        picks["revenue"], evidence["revenue"] = f"{sec.revenue:.0f}", facts_url(cik)
        out.notes.append(sec.revenue_note)
    if sec.public and not acc.ownership:
        picks["ownership"], evidence["ownership"] = "public", submissions_url(cik)
    if sec.hq and not acc.hq_city:
        picks["hq_city"], evidence["hq_city"] = sec.hq, submissions_url(cik)

    if not picks:
        return out if out.notes else None
    res = apply_selected(conn, cfg, acc.id, picks, ACTOR)
    if not res.ok:
        out.notes.append("not saved: " + "; ".join(res.errors))
        summary.errors += 1
        return out
    out.facts = [c for c, _, _ in res.changed if c in picks]
    out.status_after = res.fit.status if res.fit else acc.status
    with conn:
        for col in out.facts:
            if col == "cik":
                log_audit(conn, ACTOR, "evidence", acc.id, col, None, f"{out.cik_from}: {evidence[col]}")
            else:
                log_audit(conn, ACTOR, "evidence", acc.id, col, None, evidence[col])
    return out


def run_sec_fill(conn: sqlite3.Connection, cfg: Config, edgar: EdgarClient, as_of: date, limit: int | None = None,
                 only: str | None = None, log: Callable[[str], None] = lambda s: None) -> FillSummary:
    summary = FillSummary()
    accts = select(conn, only)
    summary.candidates = len(accts)
    if not accts:
        return summary
    index = None
    if any(wants_lookup(a) for a in accts):
        index, note = load_index(edgar)
        if note:
            log(f"  {note}; companies with a CIK on file are still read.")
    cap = max(1, limit or cfg.sec_fill.max_accounts_per_run)
    for acc in accts:
        # A company with no CIK and no match costs nothing to check (the list is in memory); only an SEC record read counts toward the cap.
        if summary.read >= cap and (acc.cik or (index is not None and index.lookup(acc.name).listing is not None)):
            summary.not_reached += 1
            continue
        before = summary.read
        try:
            got = fill_one(conn, cfg, edgar, index, acc, as_of, summary)
        except Exception as e:     # one company must not stop the rest
            summary.errors += 1
            _record(conn, acc, as_of, f"{type(e).__name__}: {e}"[:200])
            log(f"  {acc.name}: ERROR {type(e).__name__}: {e}")
            continue
        if summary.read > before:           # the SEC was asked about this company: remember it, so the next run starts with others
            _record(conn, acc, as_of, "; ".join(got.notes)[:300] if got and got.notes else None)
        if got is None:
            continue
        ready = got.status_before != "active" and got.status_after == "active"
        if got.facts:
            summary.filled.append(got)
            summary.passed_gate += ready
            log(f"  {acc.name}: filled {', '.join(got.facts)}"
                + (f" (CIK matched to SEC filer {got.cik_from})" if got.cik_from else "")
                + (" - now passes the fit gate" if ready else "")
                + (f" [{'; '.join(got.notes)}]" if got.notes else ""))
        elif got.notes:
            log(f"  {acc.name}: nothing filled [{'; '.join(got.notes)}]")
    return summary
