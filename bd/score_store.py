"""Load stored signals, score accounts with the deterministic engine, persist to `scores`.

Only accounts that passed the fit gate (status = active) are scored; held and excluded accounts
are never scored. Low-confidence signals are stored but contribute nothing (see scoring.py)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date

from bd.accounts import Account, list_accounts
from bd.config import Config
from bd.models import ScoringSignal
from bd.scoring import ScoreResult, score_account


@dataclass
class ScoredAccount:
    account: Account
    rows: dict[int, sqlite3.Row]          # signals.id -> row (summary, url, ...)
    signals: list[ScoringSignal]
    result: ScoreResult


def load_signal_rows(conn: sqlite3.Connection, account_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM signals WHERE account_id = ? ORDER BY event_date DESC, id", (account_id,)).fetchall()


def to_scoring_signal(row: sqlite3.Row) -> ScoringSignal:
    return ScoringSignal(
        type=row["type"],
        event_date=date.fromisoformat(row["event_date"]),
        confidence=row["confidence"],
        service_lines=tuple(json.loads(row["service_lines"] or "[]")),
        ref=row["id"],
    )


_RANK = {"Low": 0, "Medium": 1, "High": 2}


def dedupe_same_event(signals: list[ScoringSignal]) -> list[ScoringSignal]:
    """One event reported by several domains counts once: same type and event_date collapse to the
    highest-confidence source (lowest signal id on a tie). The extra rows stay stored for audit."""
    best: dict[tuple[str, date], ScoringSignal] = {}
    for s in signals:
        key = (s.type, s.event_date)
        cur = best.get(key)
        if cur is None or (_RANK[s.confidence], -(s.ref or 0)) > (_RANK[cur.confidence], -(cur.ref or 0)):
            best[key] = s
    return list(best.values())


def score_one(conn: sqlite3.Connection, cfg: Config, account: Account, as_of: date) -> ScoredAccount:
    rows = load_signal_rows(conn, account.id)
    signals = dedupe_same_event([to_scoring_signal(r) for r in rows])
    return ScoredAccount(account, {r["id"]: r for r in rows}, signals, score_account(signals, as_of, cfg))


def score_accounts(conn: sqlite3.Connection, cfg: Config, as_of: date) -> list[ScoredAccount]:
    """Score every active account as of `as_of` and upsert into `scores`. Re-scoring the same day
    refreshes score/band/components but never clears `posted` or `cooldown_until`."""
    out = [score_one(conn, cfg, acc, as_of) for acc in list_accounts(conn, status="active")]
    with conn:
        for sa in out:
            r = sa.result
            conn.execute(
                "INSERT INTO scores(account_id, run_date, score, band, components_json, rationale) "
                "VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(account_id, run_date) DO UPDATE SET score=excluded.score, band=excluded.band, "
                "components_json=excluded.components_json, rationale=excluded.rationale",
                (sa.account.id, as_of.isoformat(), r.score, r.band, r.components_json(), r.rationale),
            )
    return out
