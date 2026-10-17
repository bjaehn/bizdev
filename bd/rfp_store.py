"""Storage and reading of open public RFPs (no model, network or scanner code, so the local UI can import it)."""

from __future__ import annotations

import json
import sqlite3
from datetime import date

from pydantic import BaseModel, field_validator

from bd.models import MAX_SUMMARY_WORDS


class RfpExtract(BaseModel):
    title: str
    agency: str
    due_date: date
    summary: str
    source_url: str
    service_lines: list[str] = []

    @field_validator("summary")
    @classmethod
    def _words(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v or len(v.split()) > MAX_SUMMARY_WORDS:
            raise ValueError(f"summary must be 1-{MAX_SUMMARY_WORDS} words")
        return v

    @field_validator("title", "agency")
    @classmethod
    def _clean(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("empty")
        return v[:200]


def store_rfps(conn: sqlite3.Connection, group: str, rfps: list[RfpExtract], seen_on: date) -> tuple[int, int]:
    """(new, already known). A known solicitation gets its due date and last-seen day refreshed (an addendum may move the deadline)."""
    new = known = 0
    with conn:
        for r in rfps:
            cur = conn.execute(
                "INSERT OR IGNORE INTO solicitations(url,title,agency,grp,due_date,summary,service_lines,first_seen,last_seen) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (r.source_url, r.title, r.agency, group, r.due_date.isoformat(), r.summary, json.dumps(r.service_lines),
                 seen_on.isoformat(), seen_on.isoformat()))
            if cur.rowcount:
                new += 1
            else:
                known += 1
                conn.execute("UPDATE solicitations SET due_date = ?, last_seen = ?, summary = ? WHERE url = ?",
                             (r.due_date.isoformat(), seen_on.isoformat(), r.summary, r.source_url))
    return new, known


def list_open(conn: sqlite3.Connection, today: date, include_closed: bool = False, service_line: str = "", grp: str = "") -> list[sqlite3.Row]:
    where, args = [], []
    if not include_closed:
        where.append("due_date >= ?")
        args.append(today.isoformat())
    if grp:
        where.append("grp = ?")
        args.append(grp)
    rows = conn.execute("SELECT * FROM solicitations" + (" WHERE " + " AND ".join(where) if where else "") +
                        " ORDER BY due_date, id", args).fetchall()
    if service_line:
        rows = [r for r in rows if service_line in json.loads(r["service_lines"])]
    return rows
