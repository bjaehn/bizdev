"""SQLite schema and connection helpers. Dates are stored as ISO-8601 text."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    domain        TEXT,
    hq_city       TEXT,
    chicagoland   TEXT NOT NULL DEFAULT 'unknown' CHECK (chicagoland IN ('yes','no','unknown')),
    revenue       REAL,      -- USD; NULL = unknown
    employees     INTEGER,   -- NULL = unknown
    ownership     TEXT CHECK (ownership IS NULL OR ownership IN ('public','private','pe-backed')),
    sponsor       TEXT,
    industry      TEXT,
    tier          TEXT NOT NULL DEFAULT 'A' CHECK (tier IN ('A','B')),
    status        TEXT NOT NULL DEFAULT 'active',  -- e.g. active | hold_review | excluded
    last_scanned  TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_accounts_domain ON accounts(domain) WHERE domain IS NOT NULL;

CREATE TABLE IF NOT EXISTS signals (
    id             INTEGER PRIMARY KEY,
    account_id     INTEGER NOT NULL REFERENCES accounts(id),
    family         TEXT NOT NULL,
    type           TEXT NOT NULL,
    event_date     TEXT NOT NULL,   -- from the source, never the detection date
    detected_date  TEXT NOT NULL,
    summary        TEXT NOT NULL,   -- max 25 words (enforced by SignalExtract)
    source_url     TEXT NOT NULL,
    source_type    TEXT NOT NULL,
    confidence     TEXT NOT NULL CHECK (confidence IN ('High','Medium','Low')),
    service_lines  TEXT NOT NULL DEFAULT '[]',   -- JSON array
    role_or_person TEXT,
    hash           TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS ix_signals_account ON signals(account_id, event_date);

CREATE TABLE IF NOT EXISTS scores (
    account_id      INTEGER NOT NULL REFERENCES accounts(id),
    run_date        TEXT NOT NULL,
    score           INTEGER NOT NULL,
    band            TEXT NOT NULL,
    components_json TEXT NOT NULL,
    rationale       TEXT NOT NULL,
    posted          INTEGER NOT NULL DEFAULT 0,
    cooldown_until  TEXT,
    PRIMARY KEY (account_id, run_date)
);

CREATE TABLE IF NOT EXISTS exclusions (
    id      INTEGER PRIMARY KEY,
    name    TEXT NOT NULL,
    domain  TEXT,
    reason  TEXT NOT NULL CHECK (reason IN ('client','do_not_pursue','cannabis','conflict'))
);

-- account_id is nullable so cost can be logged per account per run (design rule 6)
-- as well as per step.
CREATE TABLE IF NOT EXISTS runs (
    id               INTEGER PRIMARY KEY,
    run_date         TEXT NOT NULL,
    step             TEXT NOT NULL,
    account_id       INTEGER REFERENCES accounts(id),
    accounts_scanned INTEGER NOT NULL DEFAULT 0,
    signals_found    INTEGER NOT NULL DEFAULT 0,
    tokens           INTEGER NOT NULL DEFAULT 0,
    est_cost         REAL NOT NULL DEFAULT 0,
    errors           TEXT
);
"""


def connect(path: str | Path = "bd.sqlite") -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
