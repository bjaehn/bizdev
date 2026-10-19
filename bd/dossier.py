"""One-page company summary for meeting prep: why now, score and where it came from, signals with their sources, facts, job postings,
sponsor, your stage and notes. Read-only, built from stored data and the same opportunity logic the Opportunities page uses."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from bd.accounts import Account
from bd.config import Config
from bd.fit_gate import check_fit, load_exclusions
from bd.opportunities import Opportunity, build_opportunities, score_dates
from bd.pipeline import get_state, notes_for
from bd.shortlist import ShortlistError
from bd.timeline import legend, timeline_svg


def build_dossier(conn: sqlite3.Connection, cfg: Config, a: Account, today: date) -> dict:
    sigs = [dict(r) for r in conn.execute("SELECT * FROM signals WHERE account_id = ? ORDER BY event_date DESC, id DESC", (a.id,))]
    opp: Opportunity | None = None
    run_date = None
    dates = score_dates(conn)
    if dates:
        run_date = dates[0]
        try:
            opp = next((o for o in build_opportunities(conn, cfg, date.fromisoformat(run_date), include_tracked_only=True).items
                        if o.account.id == a.id), None)
        except ShortlistError:
            opp = None
    window = (today - timedelta(days=cfg.hiring.window_days)).isoformat()
    lead = tuple(cfg.hiring.leadership_levels)
    post = conn.execute(f"SELECT COUNT(*) n, COALESCE(SUM(seniority IN ({','.join('?' * len(lead))})), 0) leaders FROM postings "
                        "WHERE account_id = ? AND open = 1 AND posted_date >= ?", (*lead, a.id, window)).fetchone()
    firms = [r["name"] for r in conn.execute(
        "SELECT f.name FROM portfolio_links l JOIN pe_firms f ON f.id = l.firm_id WHERE l.account_id = ? AND l.status = 'current' "
        "AND f.status = 'active' ORDER BY f.name", (a.id,))]
    st = get_state(conn, a.id)
    fit = check_fit(a, load_exclusions(conn), cfg)
    return {
        "a": a, "today": today, "opp": opp, "run_date": run_date, "signals": sigs, "postings": post["n"], "leadership_postings": post["leaders"],
        "firms": firms, "state": st, "notes": notes_for(conn, a.id, 5), "fit": fit,
        "timeline": timeline_svg(sigs, today), "legend": legend(sigs), "outside_window": sum(
            1 for s in sigs if s["event_date"] < (today - timedelta(days=180)).isoformat()),
    }
