"""What the PE screens show, as plain data. No HTML here: the pages render these, and the tests read them.

Everything displayed is a stored, sourced fact, a count, or a hypothesis labelled as one. A sponsor is never given a score: it gets the number of distinct
supported opportunities among its holdings and how complete the reading of its portfolio is."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime

from bd.config import Config
from bd.opportunity_model import (CATEGORY_ORDER, CONF_RANK, Assessment, FirmSummary, Holding, assess_company, firm_summary, phrase, rank_holdings)
from bd.pe_ownership import company_geography, company_places, evidence_for, is_stale, source_is_stale

SECTIONS = (("current", "Current holdings", "The firm's own page lists them as current, or a completion notice states the closing. Not a check that nothing has happened since."),
            ("pending", "Pending: announced, not completed", "An announcement exists and no source states that it closed."),
            ("unclear", "Ownership unclear", "A source lists the company, but not whether the investment is current, or nothing states what happened to it. These are leads, not holdings."),
            ("historical", "Historical", "A source states an exit, or the firm's own page lists it among past investments."))
GEO = (("", "All"), ("in_scope", "In configured geography"), ("unknown", "Geography not verified"), ("out_of_scope", "Outside configured geography"))


@dataclass
class FirmRow:
    firm: sqlite3.Row
    sources: list[dict] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    prospect_now: int = 0
    validate_first: int = 0
    assessed: int = 0
    reading: str = "never read"            # a plain sentence about the portfolio reading
    reading_class: str = ""                # good | warm | bad | "" (for the badge)


def source_dicts(conn: sqlite3.Connection, firm_id: int, today: date, cfg: Config) -> list[dict]:
    out = []
    for s in conn.execute("SELECT * FROM pe_sources WHERE firm_id = ? ORDER BY id", (firm_id,)):
        out.append(dict(s, stale=source_is_stale(s, today, cfg), snapshots=conn.execute("SELECT COUNT(*) FROM pe_snapshots WHERE source_id = ?", (s["id"],)).fetchone()[0]))
    return out


def reading_sentence(sources: list[dict]) -> tuple[str, str]:
    if not sources:
        return "no portfolio page registered", ""
    bad = [s for s in sources if s["status"] in ("failed", "blocked")]
    never = [s for s in sources if s["status"] == "never"]
    if bad:
        s = bad[0]
        when = f"; last good reading {s['last_success'][:10]}" if s["last_success"] else "; never read successfully"
        return f"{s['status']}: {s['last_error']}{when}", "bad"
    if never:
        return "registered, not read yet", "warm"
    stale = [s for s in sources if s["stale"]]
    if stale:
        return f"stale: last read {stale[0]['last_success'][:10]}", "warm"
    if all(s["coverage"] == "complete" for s in sources):
        return f"read {max(s['last_success'] for s in sources)[:10]}, coverage complete", "good"
    return f"read {max(s['last_success'] for s in sources)[:10]}, coverage {'partial' if any(s['coverage'] == 'partial' for s in sources) else 'unknown'}", "warm"


def firm_directory(conn: sqlite3.Connection, cfg: Config, today: date) -> list[FirmRow]:
    rows = []
    for f in conn.execute("SELECT * FROM pe_firms ORDER BY status, lower(name)").fetchall():
        r = FirmRow(f)
        r.sources = source_dicts(conn, f["id"], today, cfg)
        r.counts = {s: 0 for s, *_ in SECTIONS}
        for c in conn.execute("SELECT status, COUNT(*) AS n FROM pe_ownership WHERE firm_id = ? GROUP BY status", (f["id"],)):
            r.counts[c["status"]] = c["n"]
        if f["status"] == "active" and sum(r.counts.values()):
            s = firm_summary(conn, cfg, f["id"], today)
            r.prospect_now, r.validate_first, r.assessed = s.by_category.get("Prospect now", 0), s.by_category.get("Validate first", 0), s.assessed
        r.reading, r.reading_class = reading_sentence(r.sources)
        rows.append(r)
    return rows


@dataclass
class RunLine:
    run: sqlite3.Row
    outcomes: list[dict]


def run_history(conn: sqlite3.Connection, limit: int = 8) -> list[RunLine]:
    return [RunLine(r, json.loads(r["detail"]) if r["detail"] else []) for r in conn.execute("SELECT * FROM pe_runs ORDER BY id DESC LIMIT ?", (limit,))]


def collection_status(conn: sqlite3.Connection, cfg: Config, today: date) -> dict:
    srcs = conn.execute("SELECT * FROM pe_sources s WHERE EXISTS (SELECT 1 FROM pe_firms f WHERE f.id = s.firm_id AND f.status = 'active')").fetchall()
    n_runs = conn.execute("SELECT COUNT(*) FROM pe_runs WHERE kind = 'collect' AND finished IS NOT NULL").fetchone()[0]
    return {
        "firms": conn.execute("SELECT COUNT(*) FROM pe_firms WHERE status = 'active'").fetchone()[0],
        "sources": len(srcs), "ok": sum(s["status"] == "ok" for s in srcs), "failed": sum(s["status"] in ("failed", "blocked") for s in srcs),
        "never": sum(s["status"] == "never" for s in srcs), "partial": sum(s["status"] == "ok" and s["coverage"] != "complete" for s in srcs),
        "stale": sum(source_is_stale(s, today, cfg) for s in srcs if s["status"] != "never"),
        "relationships": conn.execute("SELECT COUNT(*) FROM pe_ownership").fetchone()[0],
        "runs": n_runs,
        "history": "No earlier reading exists to compare with: changes will be shown from the second reading on." if n_runs < 2 else None,
        "last": run_history(conn, 1)[0] if n_runs else None,
    }


# ---- one firm ----------------------------------------------------------------------------------------------------------------------------------
@dataclass
class HoldingRow:
    holding: Holding
    stale: bool
    other_sponsors: list[str]
    evidence_count: int
    verified_count: int
    log_last: sqlite3.Row | None


@dataclass
class Pattern:
    category: str
    holdings: list[tuple[str, int, str, str]]        # (company name, company id, fact summary, source url)
    text: str


@dataclass
class FirmDetail:
    firm: sqlite3.Row
    summary: FirmSummary
    sources: list[dict]
    sections: list[tuple[str, str, str, list[HoldingRow]]]
    hidden_out_of_scope: int
    geo_counts: dict[str, int]
    patterns: list[Pattern]
    add_ons: list[dict]
    changes: list[dict]
    history_note: str | None
    reading: str
    reading_class: str
    scope_note: str
    firm_signals: list[sqlite3.Row]


def cross_portfolio_patterns(holdings: list[Holding]) -> list[Pattern]:
    """A pattern is named only when at least two holdings each carry their own supported hypothesis in the same consulting category (Medium confidence or
    better, a fact with its own source). It never says the companies share systems, problems or a sponsor-wide program."""
    by_cat: dict[str, list[Holding]] = {}
    for h in holdings:
        a = h.assessment
        if a.category in ("Prospect now", "Validate first", "Nurture") and CONF_RANK[a.confidence] >= CONF_RANK["Medium"] and a.hypotheses and a.facts:
            by_cat.setdefault(a.hypotheses[0].category, []).append(h)
    out = []
    for cat, hs in sorted(by_cat.items()):
        if len(hs) < 2:
            continue
        rows = [(h.company["name"], h.company["id"], h.assessment.facts[0].summary, h.assessment.facts[0].url) for h in hs]
        names = ", ".join(r[0] for r in rows)
        out.append(Pattern(cat, rows, f"Hypothesis: {len(hs)} holdings ({names}) each have their own sourced evidence pointing to {phrase(cat)}. The evidence concerns each company "
                                      "separately. It does not show shared systems, a common problem, centralized purchasing or a sponsor-wide program."))
    return out


def firm_detail(conn: sqlite3.Connection, cfg: Config, firm_id: int, today: date, geo: str = "") -> FirmDetail | None:
    f = conn.execute("SELECT * FROM pe_firms WHERE id = ?", (firm_id,)).fetchone()
    if f is None:
        return None
    all_holdings = rank_holdings(conn, cfg, firm_id, today, statuses=("current", "pending", "unclear", "historical"))
    geo_counts = {k: sum(h.geography == k for h in all_holdings) for k, _ in GEO if k}
    geo = geo if geo in dict(GEO) else ""
    shown = [h for h in all_holdings if (h.geography == geo if geo else h.geography != "out_of_scope")]
    hidden = 0 if geo else geo_counts["out_of_scope"]
    rows: dict[str, list[HoldingRow]] = {s: [] for s, *_ in SECTIONS}
    for h in shown:
        ev = evidence_for(conn, "ownership", h.ownership["id"])
        others = [r["name"] for r in conn.execute("SELECT fi.name FROM pe_ownership o JOIN pe_firms fi ON fi.id = o.firm_id WHERE o.company_id = ? AND o.firm_id != ? ORDER BY fi.name",
                                                  (h.company["id"], firm_id))]
        log = conn.execute("SELECT * FROM pe_ownership_log WHERE ownership_id = ? ORDER BY id DESC LIMIT 1", (h.ownership["id"],)).fetchone()
        rows[h.ownership["status"]].append(HoldingRow(h, is_stale(h.ownership, today, cfg), others, len(ev), sum(e["verification"] == "page_verified" for e in ev), log))
    sections = [(k, title, blurb, rows[k]) for k, title, blurb in SECTIONS if rows[k]]
    sources = source_dicts(conn, firm_id, today, cfg)
    patterns = cross_portfolio_patterns([h for h in all_holdings if h.ownership["status"] in ("current", "pending")])
    ids = [h.company["id"] for h in all_holdings]
    adds = []
    if ids:
        q = ",".join("?" * len(ids))
        for lk in conn.execute(f"SELECT l.*, p.name AS parent, c.name AS child, p.id AS parent_id, c.id AS child_id FROM pe_company_links l JOIN pe_companies p ON p.id = l.parent_company_id "
                               f"JOIN pe_companies c ON c.id = l.child_company_id WHERE l.parent_company_id IN ({q}) ORDER BY l.announced_date DESC, l.id DESC", ids):
            ev = conn.execute("SELECT * FROM pe_evidence WHERE subject_type = 'link' AND subject_id = ? AND verification = 'page_verified' ORDER BY id LIMIT 1", (lk["id"],)).fetchone()
            parent_a = next((h.assessment for h in all_holdings if h.company["id"] == lk["parent_id"]), None)
            hyp = next((x for x in (parent_a.hypotheses if parent_a else []) if x.category == "Acquisition integration and separation"), None)
            adds.append({"link": lk, "evidence": ev, "hypothesis": hyp.text if hyp else None})
    changes, note = firm_changes(conn, firm_id)
    sigs = conn.execute("SELECT * FROM firm_signals WHERE firm_id = ? ORDER BY event_date DESC, id DESC", (firm_id,)).fetchall()
    reading, rclass = reading_sentence(sources)
    sc = cfg.pe_portfolio.scope_cities
    scope = (f"Geography follows the configured scope: {cfg.fit_gate.state}, counties {', '.join(cfg.fit_gate.counties)}. A company's place counts only when a retrieved page shows its "
             f"headquarters; the sponsor's location never does. " + (f"Cities treated as in scope: {', '.join(sc)}." if sc else "Counties are not mapped to cities and no scope cities are "
                                                                                                                              "configured (pe_portfolio.scope_cities), so Illinois companies stay 'not verified' until fact research confirms them."))
    return FirmDetail(f, firm_summary(conn, cfg, firm_id, today, all_holdings), sources, sections, hidden, geo_counts, patterns, adds, changes, note, reading, rclass, scope, sigs)


def firm_changes(conn: sqlite3.Connection, firm_id: int) -> tuple[list[dict], str | None]:
    """Per source of this firm: what was added and removed between the last two readings, from the stored run records. None-note means there was a comparison."""
    src_ids = {r["id"] for r in conn.execute("SELECT id FROM pe_sources WHERE firm_id = ?", (firm_id,))}
    out: list[dict] = []
    for run in run_history(conn, 20):
        for o in run.outcomes:
            if o["source_id"] in src_ids and o["status"] == "ok":
                out.append(dict(o, when=run.run["started"], run_id=run.run["id"]))
    if not out:
        return [], "This firm's portfolio page has not been read yet, so there is nothing to compare."
    latest = out[0]
    if latest["added"] is None:
        return [], f"This is the first reading of this firm's page (run #{latest['run_id']}, {latest['entries']} entries): no earlier reading exists, so no changes can be shown yet."
    return out[:3], None


# ---- one relationship, one company -----------------------------------------------------------------------------------------------------------------
@dataclass
class OwnershipDetail:
    o: sqlite3.Row
    firm: sqlite3.Row
    company: sqlite3.Row
    fund: sqlite3.Row | None
    evidence: list[sqlite3.Row]
    log: list[sqlite3.Row]
    others: list[sqlite3.Row]
    stale: bool
    geography: str
    geography_reason: str
    places: list[sqlite3.Row]
    assessment: Assessment
    aliases: list[str]


def ownership_detail(conn: sqlite3.Connection, cfg: Config, ownership_id: int, today: date) -> OwnershipDetail | None:
    o = conn.execute("SELECT * FROM pe_ownership WHERE id = ?", (ownership_id,)).fetchone()
    if o is None:
        return None
    g = company_geography(conn, o["company_id"], cfg)
    return OwnershipDetail(
        o, conn.execute("SELECT * FROM pe_firms WHERE id = ?", (o["firm_id"],)).fetchone(), conn.execute("SELECT * FROM pe_companies WHERE id = ?", (o["company_id"],)).fetchone(),
        conn.execute("SELECT * FROM pe_funds WHERE id = ?", (o["fund_id"],)).fetchone() if o["fund_id"] else None, evidence_for(conn, "ownership", ownership_id),
        conn.execute("SELECT * FROM pe_ownership_log WHERE ownership_id = ? ORDER BY id", (ownership_id,)).fetchall(),
        conn.execute("SELECT o.*, f.name AS firm_name FROM pe_ownership o JOIN pe_firms f ON f.id = o.firm_id WHERE o.company_id = ? AND o.id != ?", (o["company_id"], ownership_id)).fetchall(),
        is_stale(o, today, cfg), g.status, g.reason, company_places(conn, o["company_id"]), assess_company(conn, cfg, o["company_id"], today),
        [r["alias"] for r in conn.execute("SELECT alias FROM pe_aliases WHERE entity_type = 'company' AND entity_id = ? ORDER BY alias", (o["company_id"],))])


@dataclass
class CompanyPe:
    company: sqlite3.Row
    relationships: list[dict]
    assessment: Assessment
    geography: str
    geography_reason: str
    places: list[sqlite3.Row]
    add_ons: list[sqlite3.Row]
    is_account: bool
    aliases: list[str]


def company_pe(conn: sqlite3.Connection, cfg: Config, today: date, company_id: int | None = None, account_id: int | None = None) -> CompanyPe | None:
    if company_id is None and account_id is not None:
        row = conn.execute("SELECT id FROM pe_companies WHERE account_id = ?", (account_id,)).fetchone()
        company_id = row["id"] if row else None
    if company_id is None:
        return None
    co = conn.execute("SELECT * FROM pe_companies WHERE id = ?", (company_id,)).fetchone()
    if co is None:
        return None
    rels = []
    for o in conn.execute("SELECT o.*, f.name AS firm_name, f.id AS firm_id_ FROM pe_ownership o JOIN pe_firms f ON f.id = o.firm_id WHERE o.company_id = ? "
                          "ORDER BY CASE o.status WHEN 'current' THEN 0 WHEN 'pending' THEN 1 WHEN 'unclear' THEN 2 ELSE 3 END, f.name", (company_id,)):
        ev = evidence_for(conn, "ownership", o["id"])
        rels.append({"o": o, "stale": is_stale(o, today, cfg), "evidence": ev})
    g = company_geography(conn, company_id, cfg)
    adds = conn.execute("SELECT l.*, p.name AS parent, c.name AS child FROM pe_company_links l JOIN pe_companies p ON p.id = l.parent_company_id "
                        "JOIN pe_companies c ON c.id = l.child_company_id WHERE l.parent_company_id = ? OR l.child_company_id = ? ORDER BY l.id", (company_id, company_id)).fetchall()
    return CompanyPe(co, rels, assess_company(conn, cfg, company_id, today), g.status, g.reason, company_places(conn, company_id), adds, bool(co["account_id"]),
                     [r["alias"] for r in conn.execute("SELECT alias FROM pe_aliases WHERE entity_type = 'company' AND entity_id = ? ORDER BY alias", (company_id,))])


def run_detail(conn: sqlite3.Connection, run_id: int) -> RunLine | None:
    r = conn.execute("SELECT * FROM pe_runs WHERE id = ?", (run_id,)).fetchone()
    return RunLine(r, json.loads(r["detail"]) if r["detail"] else []) if r else None


def fmt_ts(s: str | None) -> str:
    """'2026-10-20T14:03:11+00:00' -> '2026-10-20 14:03 UTC'. A value that is not a timestamp is returned as it is."""
    if not s:
        return ""
    try:
        return datetime.fromisoformat(s).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return s
