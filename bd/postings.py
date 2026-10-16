"""Job postings and learned job boards: storage rules. No LLM, no network."""

from __future__ import annotations

import sqlite3
from datetime import date

from bd.accounts import log_audit


def store_postings(conn: sqlite3.Connection, account_id: int, postings: list, seen: date, source: str,
                   complete: bool) -> tuple[int, int]:
    """Upsert the postings seen today (open). When `complete` (the board's full list), postings of this source that are
    no longer listed are marked closed. Returns (new, closed). The caller owns the transaction."""
    new = closed = 0
    for p in postings:
        existed = conn.execute("SELECT 1 FROM postings WHERE account_id = ? AND url = ?", (account_id, p.url)).fetchone()
        conn.execute(
            "INSERT INTO postings(account_id,url,title,posted_date,theme,seniority,interim,source,first_seen,last_seen,open) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,1) ON CONFLICT(account_id, url) DO UPDATE SET last_seen = excluded.last_seen, open = 1, "
            "theme = excluded.theme, seniority = excluded.seniority, interim = excluded.interim",
            (account_id, p.url, p.title, p.posted_date.isoformat(), p.theme, p.seniority, int(p.interim), source,
             seen.isoformat(), seen.isoformat()))
        new += 0 if existed else 1
    if complete:
        cur = conn.execute("UPDATE postings SET open = 0 WHERE account_id = ? AND source = ? AND open = 1 AND last_seen < ?",
                           (account_id, source, seen.isoformat()))
        closed = cur.rowcount
    return new, closed


def learn_board(conn: sqlite3.Connection, account_id: int, board: tuple[str, str], actor: str = "scan") -> bool:
    """Remember the account's job board once (never overwrites a board a person or an earlier scan set)."""
    cur = conn.execute("UPDATE accounts SET ats_provider = ?, ats_board = ? WHERE id = ? AND ats_board IS NULL",
                       (board[0], board[1], account_id))
    if cur.rowcount:
        log_audit(conn, actor, "update", account_id, "ats_board", None, f"{board[0]}:{board[1]}")
    return bool(cur.rowcount)


def known_accessions(conn: sqlite3.Connection, account_id: int, scanner: str) -> frozenset[str]:
    return frozenset(r[0] for r in conn.execute("SELECT accession FROM filings_read WHERE account_id = ? AND scanner = ?",
                                                (account_id, scanner)))


def record_filings(conn: sqlite3.Connection, account_id: int, scanner: str, accessions: list[str], day: date) -> None:
    for a in accessions:
        conn.execute("INSERT OR IGNORE INTO filings_read(account_id, scanner, accession, read_date) VALUES (?,?,?,?)",
                     (account_id, scanner, a, day.isoformat()))
