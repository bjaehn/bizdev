"""Find a company on the list by name, for "what about Company X?". Read-only: no model, network or scanner code.

A hit says where the company stands (ready to scan or waiting for facts, and why), its latest score and its newest signal, so the
answer to a question in a meeting is one screen. `exact` marks a name that is the same as the search apart from legal suffixes,
punctuation and case; when no hit is exact, the page offers to add the company."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from bd.accounts import Account, can_check_free, list_accounts
from bd.config import Config
from bd.fit_gate import check_fit, load_exclusions
from bd.pipeline import LeadState, all_states
from bd.sec_ids import normalize_name

MAX_HITS = 12


@dataclass
class Hit:
    account: Account
    exact: bool
    state: LeadState
    reasons: tuple[str, ...]                 # why it is not ready to scan (empty when it is)
    score: int | None
    band: str | None
    score_date: str | None
    latest_signal: sqlite3.Row | None
    signals_90d: int
    can_check: bool = False                  # the free check could add something (see bd.accounts.can_check_free)


def find(conn: sqlite3.Connection, cfg: Config, q: str, today: str) -> list[Hit]:
    q = " ".join((q or "").split())[:120]
    if not q:
        return []
    low, key = q.lower(), normalize_name(q)
    exclusions = load_exclusions(conn)
    states = all_states(conn)
    out: list[Hit] = []
    for a in list_accounts(conn, status=None):
        exact = bool(key) and normalize_name(a.name) == key
        if not exact and low not in a.name.lower():
            continue
        sc = conn.execute("SELECT score, band, run_date FROM scores WHERE account_id = ? ORDER BY run_date DESC LIMIT 1", (a.id,)).fetchone()
        last = conn.execute("SELECT * FROM signals WHERE account_id = ? ORDER BY event_date DESC, id DESC LIMIT 1", (a.id,)).fetchone()
        n = conn.execute("SELECT COUNT(*) FROM signals WHERE account_id = ? AND event_date >= date(?, '-90 days')", (a.id, today)).fetchone()[0]
        reasons = check_fit(a, exclusions, cfg).reasons
        out.append(Hit(a, exact, states.get(a.id, LeadState()), reasons, sc["score"] if sc else None, sc["band"] if sc else None,
                       sc["run_date"] if sc else None, last, n, can_check_free(a)))
    out.sort(key=lambda h: (not h.exact, h.account.name.lower()))
    return out[:MAX_HITS]
