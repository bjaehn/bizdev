"""Fit gate: decide whether an account is out of scope. Signals are what matter, so only a *known* reason stops a scan.

Verdicts: pass | fail | unknown. A company is scanned and scored unless it is known to be out of scope; an `unknown` verdict
(location or size not confirmed) does not block anything - the company is scanned, and its brief carries an "unverified" tag.
Location and size are facts entered by a person or read from a source (accounts.chicagoland / revenue / employees / ownership);
this module never infers them.

  excluded        -> fail   (name/domain in exclusions, or an excluded industry such as cannabis)
  location no     -> fail
  size below floor-> fail   (all applicable measures known and all below)
  anything unverifiable (and nothing failed) -> unknown (still scanned)
  else            -> pass
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from bd.accounts import Account, is_valid_domain, normalize_domain
from bd.config import Config, SizeFloor
from bd.urls import host_in

# "hold_review" stays a legal status in the schema for older databases, but the gate no longer produces it.
STATUS_FOR = {"pass": "active", "fail": "excluded", "unknown": "active"}


@dataclass(frozen=True)
class Exclusion:
    name: str
    domain: str | None
    reason: str


@dataclass(frozen=True)
class FitResult:
    verdict: str
    reasons: tuple[str, ...]

    @property
    def status(self) -> str:
        return STATUS_FOR[self.verdict]

    @property
    def unverified(self) -> bool:
        """Scanned all the same, but location or size has not been confirmed."""
        return self.verdict == "unknown"


def _size_verdict(revenue: float | None, employees: int | None, floor: SizeFloor) -> str:
    tests: list[bool | None] = []
    if floor.revenue_usd is not None:
        tests.append(None if revenue is None else revenue >= floor.revenue_usd)
    if floor.employees is not None:
        tests.append(None if employees is None else employees >= floor.employees)
    if any(t is True for t in tests):
        return "pass"
    if tests and all(t is False for t in tests):
        return "fail"
    return "unknown"


def check_fit(account: Account, exclusions: list[Exclusion], cfg: Config) -> FitResult:
    g = cfg.fit_gate
    fails: list[str] = []
    unknowns: list[str] = []

    for ex in exclusions:
        if ex.name.strip().lower() == account.name.strip().lower() or (
                ex.domain and account.domain and host_in(account.domain, [ex.domain])):
            fails.append(f"excluded: {ex.reason}")
    industry = (account.industry or "").lower()
    for bad in g.excluded_industries:
        if bad.lower() in industry:
            fails.append(f"excluded industry: {bad}")

    if account.chicagoland == "no":
        fails.append("not in Chicagoland")
    elif account.chicagoland != "yes":
        unknowns.append("Chicagoland status unverified")

    default, pe = g.size_floor["default"], g.size_floor["pe-backed"]
    if account.ownership == "pe-backed":
        size = _size_verdict(account.revenue, account.employees, pe)
    elif account.ownership in ("public", "private"):
        size = _size_verdict(account.revenue, account.employees, default)
    else:  # ownership unknown: could still be PE-backed, which has the lower floor
        d = _size_verdict(account.revenue, account.employees, default)
        p = _size_verdict(account.revenue, account.employees, pe)
        size = "pass" if d == "pass" else "fail" if d == "fail" and p == "fail" else "unknown"
    if size == "fail":
        fails.append("below size floor")
    elif size == "unknown":
        unknowns.append("size unverified (revenue/employees/ownership)")

    if fails:
        return FitResult("fail", tuple(fails))
    if unknowns:
        return FitResult("unknown", tuple(unknowns))
    return FitResult("pass", ())


def load_exclusions(conn: sqlite3.Connection) -> list[Exclusion]:
    return [Exclusion(r["name"], r["domain"], r["reason"])
            for r in conn.execute("SELECT name, domain, reason FROM exclusions")]


@dataclass
class GateSummary:
    passed: list[Account] = field(default_factory=list)
    unverified: list[tuple[Account, FitResult]] = field(default_factory=list)       # scanned, with a fact still unconfirmed
    excluded: list[tuple[Account, FitResult]] = field(default_factory=list)
    changed: int = 0


def apply_fit_gate(conn: sqlite3.Connection, cfg: Config) -> GateSummary:
    """Recompute every account's status from its data and the exclusions table, so filling in a
    missing fact (or adding an exclusion) takes effect on the next run."""
    from bd.accounts import list_accounts

    exclusions = load_exclusions(conn)
    summary = GateSummary()
    for acc in list_accounts(conn, status=None):   # load once; statuses change as we go
        res = check_fit(acc, exclusions, cfg)
        if res.status != acc.status:
            with conn:
                conn.execute("UPDATE accounts SET status = ? WHERE id = ?", (res.status, acc.id))
            summary.changed += 1
        if res.verdict == "pass":
            summary.passed.append(acc)
        elif res.verdict == "unknown":
            summary.unverified.append((acc, res))
        else:
            summary.excluded.append((acc, res))
    return summary


def seed_exclusions(conn: sqlite3.Connection, path: str | Path) -> tuple[int, list[str]]:
    """Load exclusions CSV (name,domain,reason). All-or-nothing. Returns (inserted, errors)."""


    reasons = {"client", "do_not_pursue", "cannabis", "conflict"}
    rows, errs = [], []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if [c.strip() for c in (reader.fieldnames or [])] != ["name", "domain", "reason"]:
            return 0, ["exclusions header must be exactly: name,domain,reason"]
        for n, r in enumerate(reader, start=2):
            name, dom, reason = (r["name"] or "").strip(), (r["domain"] or "").strip(), (r["reason"] or "").strip()
            if not name:
                errs.append(f"row {n}: name is required")
            if reason not in reasons:
                errs.append(f"row {n}: reason must be one of {sorted(reasons)}")
            nd = normalize_domain(dom) if dom else None
            if dom and not is_valid_domain(nd or ""):
                errs.append(f"row {n}: domain {dom!r} is not a valid host name")
            rows.append((name, nd, reason))
    if errs:
        return 0, errs
    inserted = 0
    with conn:
        for name, dom, reason in rows:
            dup = conn.execute("SELECT 1 FROM exclusions WHERE lower(name)=lower(?) AND reason=?",
                               (name, reason)).fetchone()
            if not dup:
                conn.execute("INSERT INTO exclusions(name, domain, reason) VALUES (?,?,?)", (name, dom, reason))
                inserted += 1
    return inserted, []
