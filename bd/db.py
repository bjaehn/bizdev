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
    cik           TEXT,      -- SEC Central Index Key, 10 digits; human-supplied or confirmed via bd enrich
    tier          TEXT NOT NULL DEFAULT 'A' CHECK (tier IN ('A','B')),
    status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','hold_review','excluded')),
    last_scanned  TEXT,
    origin        TEXT NOT NULL DEFAULT 'seed' CHECK (origin IN ('seed','manual','discovered')),
    source_url    TEXT,      -- where discovery found it (provenance, not a fact source)
    reviewed      INTEGER NOT NULL DEFAULT 1,   -- 0 = auto-added by discovery, not yet looked at by a person
    discovered_at TEXT,
    ats_provider  TEXT CHECK (ats_provider IS NULL OR ats_provider IN ('greenhouse','lever','ashby')),
    ats_board     TEXT       -- the company's job-board id at that provider (learned from posting URLs)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_accounts_name ON accounts(lower(name));
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

-- Every change made to accounts or exclusions outside the weekly scan (UI edits, seeding, applied
-- enrich proposals). The database is the master copy, so changes must be traceable.
CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY,
    ts         TEXT NOT NULL,
    actor      TEXT NOT NULL,      -- ui | seed | enrich
    action     TEXT NOT NULL,      -- create | update | status | exclusion_add | exclusion_remove
    account_id INTEGER,
    field      TEXT,
    old        TEXT,
    new        TEXT
);
CREATE INDEX IF NOT EXISTS ix_audit_account ON audit_log(account_id, id);

-- Cached briefer output (LLM-written only) so regenerating a card is free and identical.
CREATE TABLE IF NOT EXISTS briefs (
    account_id        INTEGER NOT NULL REFERENCES accounts(id),
    run_date          TEXT NOT NULL,
    why_now           TEXT NOT NULL,
    best_service_line TEXT NOT NULL,
    model             TEXT,
    PRIMARY KEY (account_id, run_date)
);

-- Job postings seen on a company's job board (one row per posting URL). `open` is 1 while the board still lists it.
-- Only an authoritative feed (the `ats` scanner) closes postings; signals are built from this by code.
CREATE TABLE IF NOT EXISTS postings (
    id          INTEGER PRIMARY KEY,
    account_id  INTEGER NOT NULL REFERENCES accounts(id),
    url         TEXT NOT NULL,
    title       TEXT NOT NULL,
    posted_date TEXT NOT NULL,
    theme       TEXT NOT NULL,
    seniority   TEXT NOT NULL,
    interim     INTEGER NOT NULL DEFAULT 0,
    source      TEXT NOT NULL,       -- ats provider name, or 'search' when found by the model scanner
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    open        INTEGER NOT NULL DEFAULT 1,
    UNIQUE (account_id, url)
);
CREATE INDEX IF NOT EXISTS ix_postings_account ON postings(account_id, open, posted_date);

-- SEC filings already read by the edgar / earnings scanners, so a daily refresh only pays for new ones.
CREATE TABLE IF NOT EXISTS filings_read (
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    scanner    TEXT NOT NULL,
    accession  TEXT NOT NULL,
    read_date  TEXT NOT NULL,
    PRIMARY KEY (account_id, scanner, accession)
);

-- Companies found by `bd discover` that did not clear the auto-add bar (or were rejected by the fit gate).
-- status: queued (waiting for a person) | added | rejected. Rows are kept so a company is never researched twice.
CREATE TABLE IF NOT EXISTS candidates (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    domain     TEXT,
    segment    TEXT,
    source_url TEXT NOT NULL,
    found_date TEXT NOT NULL,
    status     TEXT NOT NULL CHECK (status IN ('queued','added','rejected')),
    reason     TEXT,
    facts_json TEXT NOT NULL DEFAULT '{}',
    account_id INTEGER REFERENCES accounts(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_candidates_name ON candidates(lower(name));

-- Settings edited in the app (overrides of config.yaml defaults). Only whitelisted keys; see bd/settings_store.py.
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated    TEXT NOT NULL
);

-- Private equity firms and the companies they own. Firms are derived from accounts with a sponsor, or found by
-- portfolio research; a link with account_id NULL is a portfolio company that is not (yet) an account.
CREATE TABLE IF NOT EXISTS pe_firms (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL,
    domain         TEXT,
    hq_city        TEXT,
    portfolio_url  TEXT,
    origin         TEXT NOT NULL DEFAULT 'derived' CHECK (origin IN ('derived','manual')),
    status         TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','excluded')),
    created        TEXT NOT NULL,
    last_scanned   TEXT,
    last_portfolio TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_pe_firms_name ON pe_firms(lower(name));

CREATE TABLE IF NOT EXISTS portfolio_links (
    id             INTEGER PRIMARY KEY,
    firm_id        INTEGER NOT NULL REFERENCES pe_firms(id),
    account_id     INTEGER REFERENCES accounts(id),
    company_name   TEXT NOT NULL,
    company_domain TEXT,
    status         TEXT NOT NULL DEFAULT 'current' CHECK (status IN ('current','exited')),
    origin         TEXT NOT NULL DEFAULT 'account' CHECK (origin IN ('account','portfolio','manual')),
    source_url     TEXT,
    found_date     TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_portfolio_firm_company ON portfolio_links(firm_id, lower(company_name));

-- Firm-level signals (new fund, operating-partner hire, exit). They never score a company by themselves; see
-- bd.scoring.sponsor_lift.
CREATE TABLE IF NOT EXISTS firm_signals (
    id            INTEGER PRIMARY KEY,
    firm_id       INTEGER NOT NULL REFERENCES pe_firms(id),
    type          TEXT NOT NULL,
    event_date    TEXT NOT NULL,
    detected_date TEXT NOT NULL,
    summary       TEXT NOT NULL,
    source_url    TEXT NOT NULL,
    source_type   TEXT NOT NULL,
    confidence    TEXT NOT NULL CHECK (confidence IN ('High','Medium','Low')),
    hash          TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS ix_firm_signals_firm ON firm_signals(firm_id, event_date);

-- Where each lead stands with you (bd/pipeline.py). One row per account that has ever been touched; no row = 'new'.
CREATE TABLE IF NOT EXISTS lead_state (
    account_id INTEGER PRIMARY KEY REFERENCES accounts(id),
    stage      TEXT NOT NULL DEFAULT 'new' CHECK (stage IN ('new','reviewing','pursuing','passed','won','lost')),
    watching   INTEGER NOT NULL DEFAULT 0,
    updated    TEXT NOT NULL
);

-- Notes and stage changes, newest last. A stage row keeps the score at the time, so outcomes can later be compared with
-- what the score predicted.
CREATE TABLE IF NOT EXISTS lead_notes (
    id         INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    ts         TEXT NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('note','stage')),
    stage      TEXT,
    score_at   INTEGER,
    body       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_lead_notes_account ON lead_notes(account_id, id);

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
    est_cost         REAL,   -- NULL = not priced (pricing lands in Stage 3), never a fake 0
    errors           TEXT
);
"""


def connect(path: str | Path = "bd.sqlite") -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=15)   # the hosted UI and job subprocesses write concurrently
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # Lightweight migration for databases created before a column existed.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(accounts)")}
    if "cik" not in cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN cik TEXT")
    for col, ddl in (("origin", "TEXT NOT NULL DEFAULT 'seed'"), ("source_url", "TEXT"), ("ats_provider", "TEXT"), ("ats_board", "TEXT"),
                     ("reviewed", "INTEGER NOT NULL DEFAULT 1"), ("discovered_at", "TEXT")):
        if col not in cols:
            conn.execute(f"ALTER TABLE accounts ADD COLUMN {col} {ddl}")
    conn.commit()
