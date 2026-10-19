"""Opportunities: every scored account in one ranked list, with the reasons behind its rank.

Read-only and deterministic: it re-reads what `bd score` stored plus the stored signals, postings and sponsor activity,
and explains each score by where the points came from (leadership, hiring, company events, private equity, technology,
procurement, filings, and the sponsor's own activity, shown separately). The Monday Teams card is the top of this list
that also passes the posting gates (Hot/Warm, a High-confidence signal in the last 90 days, not in cooldown, capped);
`status` says which of those an opportunity is. No model, scanner or posting code is imported here."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

from bd.accounts import Account, list_accounts
from bd.brief_core import fallback_brief
from bd.config import Config
from bd.insights import Insight, evidence_from_scored, insights_for
from bd.labels import FAMILY_LABELS, FIRM_TYPE_LABELS, TYPE_LABELS, fmt_date
from bd.pipeline import OPEN_STAGES, all_states
from bd.score_store import score_one
from bd.scoring import ScoreResult
from bd.shortlist import Lead, LeadSignal, ShortlistError, build_shortlist, lead_signals

STATUS_CARD, STATUS_OVER_CAP, STATUS_HELD, STATUS_TRACKING, STATUS_UNKNOWN = "card", "over_cap", "held", "tracking", "unknown"


@dataclass(frozen=True)
class Reason:
    kind: str            # family name, or "sponsor"
    label: str
    points: float
    headline: str        # the strongest signal in this group, with its date


@dataclass
class Opportunity:
    account: Account
    result: ScoreResult
    rank: int = 0
    prev_score: int | None = None
    is_new: bool = False                       # not scored (or below Watch) on the previous run
    reasons: list[Reason] = field(default_factory=list)
    signals: list[LeadSignal] = field(default_factory=list)
    why_now: str = ""
    why_source: str = ""
    service_line: str | None = None
    status: str = STATUS_UNKNOWN
    status_text: str = ""
    posted: bool = False
    open_postings: int = 0
    leadership_postings: int = 0
    firms: list[str] = field(default_factory=list)
    stage: str = "new"                         # where you have taken it (bd.pipeline)
    watching: bool = False
    client: bool = False                       # an existing SEI client (expansion), not a new logo
    themes: list[Insight] = field(default_factory=list)    # what the signals add up to (bd.insights), strongest first

    @property
    def theme(self) -> Insight | None:
        return self.themes[0] if self.themes else None

    @property
    def delta(self) -> int | None:
        return None if self.prev_score is None else self.result.score - self.prev_score


@dataclass
class OpportunityList:
    run_date: date
    prev_date: date | None
    items: list[Opportunity] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)     # accounts whose stored score no longer matches their signals


def score_dates(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT DISTINCT run_date FROM scores ORDER BY run_date DESC")]


def _reasons(result: ScoreResult) -> list[Reason]:
    out: list[Reason] = []
    for fam, fd in result.components.get("families", {}).items():
        sigs = sorted(fd["signals"], key=lambda s: (-s["points"], s["event_date"]))
        top = sigs[0]
        n = len(sigs)
        out.append(Reason(fam, FAMILY_LABELS.get(fam, fam), fd["total"],
                          f"{TYPE_LABELS.get(top['type'], top['type'])}, {fmt_date(date.fromisoformat(top['event_date']))}"
                          + (f" (+{n - 1} more)" if n > 1 else "")))
    sp = result.components.get("sponsor") or {}
    if sp.get("lift"):
        top = sp["signals"][0]
        out.append(Reason("sponsor", "Sponsor activity", sp["lift"],
                          f"{top['firm']} {FIRM_TYPE_LABELS.get(top['type'], top['type'])}, "
                          f"{fmt_date(date.fromisoformat(top['event_date']))}"))
    seq = result.components.get("sequences") or {}
    for m in seq.get("matches", []):
        out.append(Reason("sequence", "Pattern", m["bonus"],
                          f"{m['label']}: {TYPE_LABELS.get(m['first']['type'], m['first']['type'])} "
                          f"{fmt_date(date.fromisoformat(m['first']['event_date']))}, then "
                          f"{TYPE_LABELS.get(m['then']['type'], m['then']['type'])} {fmt_date(date.fromisoformat(m['then']['event_date']))}"))
    return sorted(out, key=lambda r: -r.points)


def build_opportunities(conn: sqlite3.Connection, cfg: Config, run_date: date, include_tracked_only: bool = False) -> OpportunityList:
    """All active accounts scored on `run_date` at or above the Watch band (or with any score, if
    `include_tracked_only`), ranked by score. Raises ShortlistError only if there are no scores for the date."""
    stored = {r["account_id"]: r for r in conn.execute("SELECT * FROM scores WHERE run_date = ?", (run_date.isoformat(),))}
    if not stored:
        raise ShortlistError(f"No scores for {run_date}. Run `bd score --as-of {run_date}` first.")
    prev_row = conn.execute("SELECT MAX(run_date) FROM scores WHERE run_date < ?", (run_date.isoformat(),)).fetchone()[0]
    prev_date = date.fromisoformat(prev_row) if prev_row else None
    prev = {r["account_id"]: r["score"] for r in conn.execute("SELECT account_id, score FROM scores WHERE run_date = ?",
                                                              (prev_date.isoformat(),))} if prev_date else {}
    out = OpportunityList(run_date, prev_date)
    keep = max(cfg.card.max_buttons_per_lead, cfg.briefer.max_signals_in_prompt)
    floor = 1 if include_tracked_only else cfg.scoring.bands.watch
    states = all_states(conn)
    briefs = {r["account_id"]: r for r in conn.execute("SELECT * FROM briefs WHERE run_date = ?", (run_date.isoformat(),))}

    window_start = (run_date - timedelta(days=cfg.hiring.window_days)).isoformat()
    lead_levels = tuple(cfg.hiring.leadership_levels)
    postings = {r["account_id"]: r for r in conn.execute(
        f"SELECT account_id, COUNT(*) AS n, SUM(seniority IN ({','.join('?' * len(lead_levels))})) AS leaders FROM postings "
        "WHERE open = 1 AND posted_date >= ? GROUP BY account_id", (*lead_levels, window_start))}
    firms: dict[int, list[str]] = {}
    for r in conn.execute("SELECT l.account_id, f.name FROM portfolio_links l JOIN pe_firms f ON f.id = l.firm_id "
                          "WHERE l.status = 'current' AND f.status = 'active' AND l.account_id IS NOT NULL ORDER BY f.name"):
        firms.setdefault(r["account_id"], []).append(r["name"])

    try:
        sl = build_shortlist(conn, cfg, run_date)
        on_card = {l.account.id for l in sl.leads}
        over = {l.account.id for l in sl.overflow}
        skipped = {s.account.id: s.reason for s in sl.skipped}
        gated = True
    except ShortlistError:
        on_card, over, skipped, gated = set(), set(), {}, False

    for acc in list_accounts(conn, status="active"):
        row = stored.get(acc.id)
        if row is None:
            continue
        sa = score_one(conn, cfg, acc, run_date)
        res = sa.result
        if (res.score, res.band) != (row["score"], row["band"]):
            out.stale.append(acc.name)
        if res.score < floor or not res.signals:
            continue
        sigs = lead_signals(sa, keep)
        o = Opportunity(acc, res, prev_score=prev.get(acc.id), signals=sigs, reasons=_reasons(res), posted=bool(row["posted"]),
                        themes=insights_for(evidence_from_scored(res, sa.rows), cfg.service_lines))
        o.is_new = bool(prev_date) and (acc.id not in prev or prev[acc.id] < cfg.scoring.bands.watch)
        if sigs:
            b = briefs.get(acc.id)
            fb = fallback_brief(Lead(acc, res, sigs), cfg=cfg)
            o.why_now, o.why_source = (b["why_now"], "written brief") if b else (fb.why_now, "plain source summary")
            o.service_line = (b["best_service_line"] if b else fb.best_service_line)
        p = postings.get(acc.id)
        o.open_postings, o.leadership_postings = (p["n"], p["leaders"] or 0) if p else (0, 0)
        o.firms = firms.get(acc.id, [])
        st = states.get(acc.id)
        if st:
            o.stage, o.watching, o.client = st.stage, st.watching, st.client
        if not gated or acc.name in out.stale:
            o.status, o.status_text = STATUS_UNKNOWN, "scores are out of date: re-run score"
        elif acc.id in on_card:
            o.status, o.status_text = STATUS_CARD, "on the Monday card"
        elif acc.id in over:
            o.status, o.status_text = STATUS_OVER_CAP, "postable, but over the card's cap"
        elif res.band in cfg.scoring.posted_bands:
            o.status, o.status_text = STATUS_HELD, skipped.get(acc.id, "held back")
        else:
            o.status, o.status_text = STATUS_TRACKING, f"{res.band}: tracked, below the posting bands"
        out.items.append(o)

    out.items.sort(key=lambda o: (-o.result.score, -sum(r.points for r in o.reasons), o.account.id))
    for i, o in enumerate(out.items, 1):
        o.rank = i
    return out


def filter_opportunities(items: list[Opportunity], band: str = "", family: str = "", service_line: str = "", pe_only: bool = False,
                         q: str = "", new_only: bool = False, status: str = "", sort: str = "score",
                         stage: str = "", client: str = "", industry: str = "") -> list[Opportunity]:
    q = q.strip().lower()
    out = [o for o in items
           if (not band or o.result.band == band)
           and (not family or any(r.kind == family for r in o.reasons))
           and (not service_line or o.service_line == service_line)
           and (not pe_only or o.account.ownership == "pe-backed")
           and (not q or q in o.account.name.lower())
           and (not new_only or o.is_new)
           and (not status or o.status == status)
           and (not stage or (o.stage in OPEN_STAGES if stage == "open" else o.stage == stage))
           and (client not in ("yes", "no") or o.client == (client == "yes"))
           and (not industry or (o.account.industry or "") == industry)]
    if sort == "change":
        out.sort(key=lambda o: (-(o.delta if o.delta is not None else o.result.score), o.rank))
    elif sort == "hiring":
        out.sort(key=lambda o: (-o.leadership_postings, -o.open_postings, o.rank))
    return out
