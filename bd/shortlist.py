"""Shortlist: which scored accounts may be posted this week.

Read-only. Applies, in order: posted band (Hot/Warm), the High-confidence-in-last-90-days gate,
the 4-week cooldown (unless a new family appeared or the score rose 15+), then the per-post cap.
Nothing is marked posted here - only a successful Teams post does that (mark_posted)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date

from bd.accounts import Account
from bd.config import Config
from bd.score_store import score_one
from bd.scoring import PriorPost, ScoreResult, cap_leads, cooldown_allows, cooldown_until, is_postable


class ShortlistError(Exception):
    pass


@dataclass(frozen=True)
class LeadSignal:
    id: int
    type: str
    event_date: date
    summary: str
    url: str
    source_type: str
    confidence: str
    role_or_person: str | None
    service_lines: tuple[str, ...]
    points: float


@dataclass
class Lead:
    account: Account
    result: ScoreResult
    signals: list[LeadSignal]   # strongest first; only signals that scored


@dataclass
class Skipped:
    account: Account
    score: int
    band: str
    reason: str


@dataclass
class Shortlist:
    run_date: date
    leads: list[Lead] = field(default_factory=list)
    overflow: list[Lead] = field(default_factory=list)   # postable but beyond the cap; not marked posted
    skipped: list[Skipped] = field(default_factory=list)


def lead_signals(sa, keep: int) -> list[LeadSignal]:
    """The strongest scored signals of a scored account (with their stored text and source), best first."""
    out = []
    for p in sorted((p for p in sa.result.signals if p.ref is not None), key=lambda p: (-p.points, p.event_date, p.type))[:keep]:
        row = sa.rows[p.ref]
        out.append(LeadSignal(
            id=row["id"], type=row["type"], event_date=date.fromisoformat(row["event_date"]),
            summary=row["summary"], url=row["source_url"], source_type=row["source_type"],
            confidence=row["confidence"], role_or_person=row["role_or_person"],
            service_lines=tuple(json.loads(row["service_lines"] or "[]")), points=p.points))
    return out


def _prior_post(conn: sqlite3.Connection, account_id: int, as_of: date) -> PriorPost | None:
    r = conn.execute(
        "SELECT score, components_json, cooldown_until FROM scores WHERE account_id = ? AND posted = 1 "
        "AND run_date <= ? ORDER BY run_date DESC LIMIT 1", (account_id, as_of.isoformat())).fetchone()
    if r is None or not r["cooldown_until"]:
        return None
    families = frozenset(json.loads(r["components_json"]).get("families", {}))
    return PriorPost(r["score"], families, date.fromisoformat(r["cooldown_until"]))


def build_shortlist(conn: sqlite3.Connection, cfg: Config, as_of: date) -> Shortlist:
    from bd.accounts import list_accounts

    stored = {r["account_id"]: r for r in conn.execute(
        "SELECT account_id, score, band FROM scores WHERE run_date = ?", (as_of.isoformat(),))}
    if not stored:
        raise ShortlistError(f"No scores for {as_of}. Run `bd score --as-of {as_of}` first.")
    sl = Shortlist(run_date=as_of)
    candidates: list[Lead] = []
    keep = max(cfg.card.max_buttons_per_lead, cfg.briefer.max_signals_in_prompt)

    for acc in list_accounts(conn, status="active"):
        if acc.id not in stored:
            continue
        sa = score_one(conn, cfg, acc, as_of)
        res = sa.result
        if (res.score, res.band) != (stored[acc.id]["score"], stored[acc.id]["band"]):
            raise ShortlistError(
                f"Stored score for {acc.name} ({stored[acc.id]['score']}) no longer matches its signals "
                f"({res.score}). Signals changed since `bd score`; re-run it.")
        skip = lambda reason: sl.skipped.append(Skipped(acc, res.score, res.band, reason))  # noqa: E731
        if res.band not in cfg.scoring.posted_bands:
            skip(f"band {res.band} is not posted")
            continue
        if not is_postable(res, sa.signals, as_of, cfg):
            skip(f"no High-confidence signal in the last {cfg.post_gate.require_high_confidence_within_days} days")
            continue
        prior = _prior_post(conn, acc.id, as_of)
        if not cooldown_allows(prior, res, as_of, cfg):
            skip(f"in cooldown until {prior.cooldown_until}")  # type: ignore[union-attr]
            continue
        ls = lead_signals(sa, keep)
        candidates.append(Lead(acc, res, ls))

    by_id = {lead.account.id: lead for lead in candidates}
    top = cap_leads([(lead.account.id, lead.result.score) for lead in candidates], cfg)
    top_ids = [i for i, _ in top]
    sl.leads = [by_id[i] for i in top_ids]
    sl.overflow = [by_id[i] for i, _ in sorted(((l.account.id, l.result.score) for l in candidates),
                                                key=lambda t: (-t[1], t[0])) if i not in set(top_ids)]
    return sl


def mark_posted(conn: sqlite3.Connection, leads: list[Lead], posted_on: date, cfg: Config) -> None:
    """Record a successful Teams post: posted = 1 and the cooldown start date. Only call this after
    the webhook accepted the card."""
    until = cooldown_until(posted_on, cfg).isoformat()
    with conn:
        for lead in leads:
            conn.execute("UPDATE scores SET posted = 1, cooldown_until = ? WHERE account_id = ? AND run_date = ?",
                         (until, lead.account.id, posted_on.isoformat()))
