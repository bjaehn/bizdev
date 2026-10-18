"""Storage for plain-English questions (no model, network or query-running code, so the local UI can import it)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

MAX_HISTORY = 25


def clean_question(text: str, max_chars: int) -> str | None:
    """The question with whitespace collapsed, or None if it is empty or too long."""
    q = " ".join((text or "").split())
    return q if q and len(q) <= max_chars else None


NAME_COLUMNS = ("name", "company_name", "company", "account", "account_name")


def link_columns(columns: list[str]) -> tuple[int | None, int | None]:
    """(index of the account_id column, index of the column holding the company's name), or (None, None): the name cell of a row about
    a company links to that company's page. Only a column literally called account_id counts as an id."""
    if "account_id" not in columns:
        return None, None
    name = next((columns.index(n) for n in NAME_COLUMNS if n in columns), None)
    return (columns.index("account_id"), name) if name is not None else (None, None)


def add_question(conn: sqlite3.Connection, question: str) -> int:
    with conn:
        cur = conn.execute("INSERT INTO questions(asked, question, status) VALUES (?,?, 'pending')",
                           (datetime.now(timezone.utc).isoformat(timespec="seconds"), question))
    return cur.lastrowid


def recent(conn: sqlite3.Connection, limit: int = MAX_HISTORY) -> list[dict]:
    out = []
    for r in conn.execute("SELECT * FROM questions ORDER BY id DESC LIMIT ?", (limit,)):
        d = dict(r)
        d["columns"] = json.loads(r["columns_json"]) if r["columns_json"] else []
        d["rows"] = json.loads(r["rows_json"]) if r["rows_json"] else []
        d["id_col"], d["name_col"] = link_columns(d["columns"])
        out.append(d)
    return out
