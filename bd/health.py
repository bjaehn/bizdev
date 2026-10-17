"""Data health: is the app finding things, how much does it cost, and where are the gaps?

Read-only, deterministic, no model or network code: it only reads the tables the pipeline already fills (accounts, signals,
runs, scores). It answers the questions you ask after a run: which scanners produced anything, which companies are still held
and why, how fresh the data is, and what a scanned company costs."""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta

from bd.accounts import list_accounts
from bd.config import Config
from bd.fit_gate import check_fit, load_exclusions


@dataclass
class ScannerYield:
    name: str
    calls: int = 0
    accounts: int = 0
    signals: int = 0
    tokens: int = 0
    cost: float = 0.0
    unpriced: int = 0
    problems: int = 0                       # calls whose note is an error (not just "rejected: ...")
    notes: list[str] = field(default_factory=list)   # most common notes, shortest useful form

    @property
    def cost_per_signal(self) -> float | None:
        return self.cost / self.signals if self.signals else None

    @property
    def verdict(self) -> str:
        if not self.calls:
            return "never ran"
        if self.problems >= max(1, self.calls // 2):
            return "failing"
        if not self.signals:
            return "ran, found nothing"
        return "producing"


@dataclass
class Health:
    as_of: date
    days: int
    accounts_total: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    by_origin: dict[str, int] = field(default_factory=dict)
    unreviewed: int = 0
    hold_reasons: list[tuple[str, int]] = field(default_factory=list)
    scan_age: dict[str, int] = field(default_factory=dict)       # never / week / month / older, among active accounts
    with_cik: int = 0
    with_domain: int = 0
    scanners: list[ScannerYield] = field(default_factory=list)
    steps: list[tuple[str, int, float, int]] = field(default_factory=list)      # step, calls, cost, unpriced calls
    cost_total: float = 0.0
    cost_per_scanned_account: float | None = None
    accounts_scanned: int = 0
    by_family: list[tuple[str, int]] = field(default_factory=list)
    by_source: list[tuple[str, int]] = field(default_factory=list)
    signals_90d: int = 0
    high_share: float | None = None
    last_scan: str | None = None
    last_score: str | None = None
    last_enrich: str | None = None
    last_rfps: str | None = None
    daily_cost: list[tuple[str, float]] = field(default_factory=list)


def _last(conn: sqlite3.Connection, like: str) -> str | None:
    return conn.execute("SELECT MAX(run_date) FROM runs WHERE step LIKE ?", (like,)).fetchone()[0]


def _short(note: str, n: int = 110) -> str:
    note = " ".join(note.split())
    return note if len(note) <= n else note[: n - 1] + "…"


def build_health(conn: sqlite3.Connection, cfg: Config, as_of: date, days: int = 30) -> Health:
    h = Health(as_of=as_of, days=days)
    since = (as_of - timedelta(days=days)).isoformat()

    accounts = list_accounts(conn, status=None)
    h.accounts_total = len(accounts)
    h.by_status = dict(Counter(a.status for a in accounts))
    h.by_origin = dict(Counter(a.origin for a in accounts))
    h.unreviewed = sum(1 for a in accounts if not a.reviewed)
    live = [a for a in accounts if a.status != "excluded"]
    h.with_cik = sum(1 for a in live if a.cik)
    h.with_domain = sum(1 for a in live if a.domain)

    exclusions = load_exclusions(conn)
    reasons: Counter[str] = Counter()
    for a in accounts:
        if a.status == "hold_review":
            for r in check_fit(a, exclusions, cfg).reasons:
                reasons[r] += 1
    h.hold_reasons = reasons.most_common()

    age = Counter()
    for a in accounts:
        if a.status != "active":
            continue
        if not a.last_scanned:
            age["never"] += 1
        else:
            d = (as_of - date.fromisoformat(a.last_scanned)).days
            age["week" if d <= 7 else "month" if d <= 30 else "older"] += 1
    h.scan_age = {k: age.get(k, 0) for k in ("never", "week", "month", "older")}

    # ---- scanner yield over the window ----
    rows = conn.execute("SELECT step, account_id, signals_found, tokens, est_cost, errors FROM runs "
                        "WHERE step LIKE 'scan:%' AND step != 'scan:total' AND account_id IS NOT NULL AND run_date >= ?", (since,)).fetchall()
    ys: dict[str, ScannerYield] = {}
    seen: dict[str, set[int]] = {}
    notes: dict[str, Counter[str]] = {}
    for r in rows:
        name = r["step"].split(":", 1)[1]
        y = ys.setdefault(name, ScannerYield(name))
        y.calls += 1
        y.signals += r["signals_found"] or 0
        y.tokens += r["tokens"] or 0
        if r["est_cost"] is None:
            y.unpriced += 1
        else:
            y.cost += r["est_cost"]
        seen.setdefault(name, set()).add(r["account_id"])
        note = r["errors"]
        if note:
            if not note.startswith("rejected:"):
                y.problems += 1
            notes.setdefault(name, Counter())[_short(note)] += 1
    for name, y in ys.items():
        y.accounts = len(seen[name])
        y.notes = [n for n, _ in notes.get(name, Counter()).most_common(3)]
    # scanners that exist but never ran in the window are listed too, so a missing one is visible
    from bd.jobs import SCANNER_NAMES
    for n in SCANNER_NAMES:
        ys.setdefault(n, ScannerYield(n))
    h.scanners = sorted(ys.values(), key=lambda y: (-y.signals, -y.calls, y.name))
    h.accounts_scanned = len({a for s in seen.values() for a in s})

    # ---- cost ----
    for r in conn.execute("SELECT CASE WHEN step LIKE 'scan:%' THEN 'scan' ELSE step END AS s, COUNT(*) n, SUM(est_cost) c, "
                          "SUM(est_cost IS NULL) u FROM runs WHERE run_date >= ? AND step != 'scan:total' AND (account_id IS NOT NULL OR step IN "
                          "('discover','rfps','pe_firm','pe_portfolio')) GROUP BY s ORDER BY c DESC", (since,)):
        h.steps.append((r["s"], r["n"], r["c"] or 0.0, r["u"] or 0))
    h.cost_total = sum(c for _, _, c, _ in h.steps)
    scan_cost = sum(c for s, _, c, _ in h.steps if s == "scan")
    h.cost_per_scanned_account = scan_cost / h.accounts_scanned if h.accounts_scanned else None
    h.daily_cost = [(r[0], r[1] or 0.0) for r in conn.execute(
        "SELECT run_date, SUM(est_cost) FROM runs WHERE run_date >= ? AND step != 'scan:total' GROUP BY run_date ORDER BY run_date DESC LIMIT 10",
        (since,))]

    # ---- signals ----
    cut = (as_of - timedelta(days=90)).isoformat()
    h.by_family = [(r[0], r[1]) for r in conn.execute("SELECT family, COUNT(*) FROM signals WHERE detected_date >= ? GROUP BY family ORDER BY 2 DESC", (cut,))]
    h.by_source = [(r[0], r[1]) for r in conn.execute("SELECT source_type, COUNT(*) FROM signals WHERE detected_date >= ? GROUP BY source_type ORDER BY 2 DESC", (cut,))]
    h.signals_90d = sum(n for _, n in h.by_family)
    if h.signals_90d:
        high = conn.execute("SELECT COUNT(*) FROM signals WHERE detected_date >= ? AND confidence = 'High'", (cut,)).fetchone()[0]
        h.high_share = high / h.signals_90d

    h.last_scan = _last(conn, "scan:%")
    h.last_enrich = _last(conn, "enrich")
    h.last_rfps = _last(conn, "rfps")
    h.last_score = conn.execute("SELECT MAX(run_date) FROM scores").fetchone()[0]
    return h
