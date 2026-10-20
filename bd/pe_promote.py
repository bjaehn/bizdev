"""From portfolio company to account: the bridge into the existing fact research, fit gate and signal scanning. Paid, capped, explicit.

Collecting a portfolio (bd.pe_collect) never creates an account and never scans anything: that would widen the target list as a side effect.
Promotion does it on purpose and within the configured targeting: a portfolio company still owned (or pending, or unclear) and not shown to be outside
the configured geography goes through the same fact research and fit gate as any discovered company (state and counties, size floors, exclusions). Only
those that pass become accounts, and only then do the signal scanners look at them. Nothing here gives a company a score for being PE-owned."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date

from bd.config import Config
from bd.cost import estimate_cost
from bd.discover import Company, DiscoverSummary, Found, _known, _norm_name, research_and_add
from bd.enrich import Enricher
from bd.llm import Usage
from bd.pe_ownership import company_geography
from bd.verify import UrlVerifier

CONF_RANK = {"High": 0, "Medium": 1, "Low": 2}


def promotable(conn: sqlite3.Connection, cfg: Config, limit: int) -> list[tuple[sqlite3.Row, str]]:
    """(company, the firm it is listed under), best-supported first: still-held relationships, geography not out of scope, not already an account,
    candidate or exclusion."""
    names, domains = _known(conn)
    out = []
    for c in conn.execute("SELECT * FROM pe_companies WHERE account_id IS NULL ORDER BY name").fetchall():
        if _norm_name(c["name"]) in names or (c["domain"] and c["domain"] in domains):
            continue
        o = conn.execute("SELECT o.*, f.name AS firm_name FROM pe_ownership o JOIN pe_firms f ON f.id = o.firm_id WHERE o.company_id = ? AND f.status = 'active' "
                         "AND o.status != 'historical' ORDER BY CASE o.confidence WHEN 'High' THEN 0 WHEN 'Medium' THEN 1 ELSE 2 END, o.id LIMIT 1", (c["id"],)).fetchone()
        if o is None:
            continue
        g = company_geography(conn, c["id"], cfg)
        if g.status == "out_of_scope":
            continue
        out.append((c, o["firm_name"], g.status, CONF_RANK[o["confidence"]]))
    out.sort(key=lambda t: (t[2] != "in_scope", t[3], t[0]["name"].lower()))
    return [(c, firm) for c, firm, _g, _r in out[:limit]]


def run_promote(conn: sqlite3.Connection, cfg: Config, verifier: UrlVerifier, enricher: Enricher, as_of: date, model: str | None, limit: int,
                workers: int | None = None, max_cost: float | None = None, log: Callable[[str], None] = lambda s: None) -> DiscoverSummary:
    workers = max(1, workers or cfg.llm.concurrency)
    cap = cfg.llm.max_cost_per_run_usd if max_cost is None else max_cost
    out = DiscoverSummary()
    rows = promotable(conn, cfg, limit)
    out.segments = len(rows)
    out.found = len(rows)
    new: list[Found] = []
    for c, firm in rows:
        g = company_geography(conn, c["id"], cfg)
        src = conn.execute("SELECT e.url FROM pe_evidence e JOIN pe_ownership o ON o.id = e.subject_id AND e.subject_type = 'ownership' WHERE o.company_id = ? "
                           "AND e.verification = 'page_verified' ORDER BY e.id LIMIT 1", (c["id"],)).fetchone()
        if src is None:
            out.skipped_source += 1                      # no retrievable page backs it: not researched
            continue
        new.append(Found(Company(name=c["name"], domain=c["domain"], hq_city=g.city, hq_state=g.state, source_url=src["url"], source_type="secondary_report"),
                         f"PE: {firm}", None, None, False))

    def spend(u: Usage) -> float | None:
        cost = estimate_cost(u, model, cfg) if u.total_tokens else None
        out.tokens += u.total_tokens
        out.est_cost += cost or 0.0
        out.unpriced_calls += int(cost is None and u.total_tokens > 0)
        return cost

    if not new or (cap is not None and out.est_cost >= cap):
        return out
    ids = research_and_add(conn, cfg, verifier, enricher, new, as_of, workers, out, spend, log)
    with conn:
        for name, account_id in ids.items():
            if account_id is not None and not conn.execute("SELECT 1 FROM pe_companies WHERE account_id = ?", (account_id,)).fetchone():
                conn.execute("UPDATE pe_companies SET account_id = ? WHERE lower(name) = lower(?) AND account_id IS NULL", (account_id, name))
    out.budget_hit = cap is not None and out.est_cost >= cap
    return out
