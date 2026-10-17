"""Pipeline: where each lead stands with you, your notes, and a watchlist.

Plain bookkeeping. It never scans, scores, calls a model or posts anything; the score is only read, to remember what it
was when you made a decision (so outcomes can later be compared with what the score predicted). Every stage change is also
written to the audit log."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from bd.accounts import log_audit

STAGES = ("new", "reviewing", "pursuing", "passed", "won", "lost")
STAGE_LABELS = {"new": "New", "reviewing": "Reviewing", "pursuing": "Pursuing", "passed": "Passed", "won": "Won", "lost": "Lost"}
OPEN_STAGES = ("new", "reviewing", "pursuing")
MAX_NOTE = 2000


@dataclass(frozen=True)
class LeadState:
    stage: str = "new"
    watching: bool = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_state(conn: sqlite3.Connection, account_id: int) -> LeadState:
    r = conn.execute("SELECT stage, watching FROM lead_state WHERE account_id = ?", (account_id,)).fetchone()
    return LeadState(r["stage"], bool(r["watching"])) if r else LeadState()


def all_states(conn: sqlite3.Connection) -> dict[int, LeadState]:
    return {r["account_id"]: LeadState(r["stage"], bool(r["watching"]))
            for r in conn.execute("SELECT account_id, stage, watching FROM lead_state")}


def _ensure(conn: sqlite3.Connection, account_id: int) -> None:
    conn.execute("INSERT OR IGNORE INTO lead_state(account_id, stage, watching, updated) VALUES (?, 'new', 0, ?)", (account_id, _now()))


def _exists(conn: sqlite3.Connection, account_id: int) -> bool:
    return conn.execute("SELECT 1 FROM accounts WHERE id = ?", (account_id,)).fetchone() is not None


def latest_score(conn: sqlite3.Connection, account_id: int) -> int | None:
    r = conn.execute("SELECT score FROM scores WHERE account_id = ? ORDER BY run_date DESC LIMIT 1", (account_id,)).fetchone()
    return r["score"] if r else None


def set_stage(conn: sqlite3.Connection, account_id: int, stage: str, note: str = "", actor: str = "ui") -> bool:
    """Move a lead to `stage`. False if the account or stage is unknown or nothing changes (and no note was given)."""
    note = (note or "").strip()[:MAX_NOTE]
    if stage not in STAGES or not _exists(conn, account_id):
        return False
    old = get_state(conn, account_id).stage
    if old == stage and not note:
        return False
    with conn:
        _ensure(conn, account_id)
        conn.execute("UPDATE lead_state SET stage = ?, updated = ? WHERE account_id = ?", (stage, _now(), account_id))
        conn.execute("INSERT INTO lead_notes(account_id, ts, kind, stage, score_at, body) VALUES (?,?,?,?,?,?)",
                     (account_id, _now(), "stage", stage, latest_score(conn, account_id), note))
        if old != stage:
            log_audit(conn, actor, "stage", account_id, "stage", old, stage)
    return True


def add_note(conn: sqlite3.Connection, account_id: int, body: str) -> bool:
    body = (body or "").strip()[:MAX_NOTE]
    if not body or not _exists(conn, account_id):
        return False
    with conn:
        conn.execute("INSERT INTO lead_notes(account_id, ts, kind, stage, score_at, body) VALUES (?,?,?,?,?,?)",
                     (account_id, _now(), "note", None, None, body))
    return True


def set_watching(conn: sqlite3.Connection, account_id: int, on: bool) -> bool:
    if not _exists(conn, account_id):
        return False
    with conn:
        _ensure(conn, account_id)
        conn.execute("UPDATE lead_state SET watching = ?, updated = ? WHERE account_id = ?", (1 if on else 0, _now(), account_id))
    return True


def notes_for(conn: sqlite3.Connection, account_id: int, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM lead_notes WHERE account_id = ? ORDER BY id DESC LIMIT ?", (account_id, limit)).fetchall()


def stage_counts(conn: sqlite3.Connection) -> dict[str, int]:
    out = {s: 0 for s in STAGES}
    for r in conn.execute("SELECT stage, COUNT(*) AS n FROM lead_state GROUP BY stage"):
        out[r["stage"]] = r["n"]
    return out


@dataclass
class WatchChange:
    account_id: int
    name: str
    stage: str
    score: int | None
    band: str | None
    new_signals: list[sqlite3.Row]
    new_postings: int


def watchlist_changes(conn: sqlite3.Connection, since: date) -> list[WatchChange]:
    """For every watched account: signals found since `since` and job postings first seen since then. Accounts with nothing new
    are still listed (with empty lists) so the page shows the whole watchlist."""
    out: list[WatchChange] = []
    rows = conn.execute("SELECT a.id, a.name, s.stage FROM lead_state s JOIN accounts a ON a.id = s.account_id "
                        "WHERE s.watching = 1 ORDER BY a.name").fetchall()
    for r in rows:
        sigs = conn.execute("SELECT * FROM signals WHERE account_id = ? AND detected_date >= ? ORDER BY event_date DESC, id DESC",
                            (r["id"], since.isoformat())).fetchall()
        posts = conn.execute("SELECT COUNT(*) FROM postings WHERE account_id = ? AND open = 1 AND first_seen >= ?",
                             (r["id"], since.isoformat())).fetchone()[0]
        sc = conn.execute("SELECT score, band FROM scores WHERE account_id = ? ORDER BY run_date DESC LIMIT 1", (r["id"],)).fetchone()
        out.append(WatchChange(r["id"], r["name"], r["stage"], sc["score"] if sc else None, sc["band"] if sc else None, sigs, posts))
    out.sort(key=lambda w: (-len(w.new_signals), -w.new_postings, w.name))
    return out


def default_since(today: date | None = None, days: int = 7) -> date:
    return (today or date.today()) - timedelta(days=days)
