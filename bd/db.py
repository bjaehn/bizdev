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

-- One reading of a company's whole job board per day it was read (the `ats` scanner): how many roles were open, how many matched a theme, and the
-- programs named in titles (JSON: program -> {"n": postings dated inside the window, "examples": [{"title","url","posted"}]}). Counts only; history is what
-- lets a page say the board grew or shrank. Rows older than a year are removed when a newer one is written.
CREATE TABLE IF NOT EXISTS board_readings (
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    seen_date  TEXT NOT NULL,
    total      INTEGER NOT NULL,
    relevant   INTEGER NOT NULL,
    programs   TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (account_id, seen_date)
);

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
    client     INTEGER NOT NULL DEFAULT 0,   -- 1 = an existing SEI client: shown as expansion, still scanned (unlike the do-not-pursue list)
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

-- Open public solicitations (RFP / RFQ / RFI) from Chicago-area agencies (bd/rfps.py). Market-level: not tied to an account, so
-- they are shown on their own page and never scored. `url` is the page the search returned and code verified.
CREATE TABLE IF NOT EXISTS solicitations (
    id            INTEGER PRIMARY KEY,
    url           TEXT NOT NULL UNIQUE,
    title         TEXT NOT NULL,
    agency        TEXT NOT NULL,
    grp           TEXT NOT NULL,
    due_date      TEXT NOT NULL,
    summary       TEXT NOT NULL,
    service_lines TEXT NOT NULL DEFAULT '[]',
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_solicitations_due ON solicitations(due_date);

-- Plain-English questions about the data and what came back (bd/ask.py). The model's SQL and the rows it returned are kept so every
-- answer can be audited. Written by the `ask` job, read by the Ask page.
CREATE TABLE IF NOT EXISTS questions (
    id           INTEGER PRIMARY KEY,
    asked        TEXT NOT NULL,
    question     TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','done','error')),
    sql          TEXT,
    explanation  TEXT,
    columns_json TEXT,
    rows_json    TEXT,
    truncated    INTEGER NOT NULL DEFAULT 0,
    answer       TEXT,
    error        TEXT,
    tokens       INTEGER NOT NULL DEFAULT 0,
    cost         REAL,
    finished     TEXT
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
    est_cost         REAL,   -- NULL = not priced (pricing lands in Stage 3), never a fake 0
    errors           TEXT
);


-- ---- PE ownership ledger (bd/pe_ownership.py) -----------------------------------------------------------------------------------
-- A portfolio company is its own entity: it may or may not be an account (account_id), and it may have several sponsors.
CREATE TABLE IF NOT EXISTS pe_companies (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    name_key   TEXT NOT NULL,        -- bd.pe_store.norm_name(name)
    domain     TEXT,                 -- official domain, only when a retrieved page shows it
    account_id INTEGER REFERENCES accounts(id),
    created    TEXT NOT NULL,
    last_seen  TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_pe_companies_domain ON pe_companies(domain) WHERE domain IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_pe_companies_key ON pe_companies(name_key);

CREATE TABLE IF NOT EXISTS pe_aliases (
    entity_type TEXT NOT NULL CHECK (entity_type IN ('firm','company')),
    entity_id   INTEGER NOT NULL,
    alias       TEXT NOT NULL,
    alias_key   TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'other_name' CHECK (kind IN ('legal_name','brand','former_name','other_name')),
    source_url  TEXT,
    PRIMARY KEY (entity_type, entity_id, alias_key)
);

-- Where a firm or company is, by what a retrieved page says: headquarters, an office or a facility. A firm's location is never a company's.
CREATE TABLE IF NOT EXISTS pe_locations (
    id          INTEGER PRIMARY KEY,
    entity_type TEXT NOT NULL CHECK (entity_type IN ('firm','company')),
    entity_id   INTEGER NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('headquarters','office','facility')),
    city        TEXT,
    state       TEXT,
    evidence_id INTEGER,
    created     TEXT NOT NULL,
    UNIQUE (entity_type, entity_id, kind, city, state)
);

CREATE TABLE IF NOT EXISTS pe_funds (
    id         INTEGER PRIMARY KEY,
    firm_id    INTEGER NOT NULL REFERENCES pe_firms(id),
    name       TEXT NOT NULL,
    name_key   TEXT NOT NULL,
    vintage    INTEGER,
    UNIQUE (firm_id, name_key)
);

-- One row per sponsor and company (the role is refined as evidence states it, never duplicated). Status is derived by code from the evidence (bd.pe_ownership.derive_status):
-- current / pending / historical / unclear. Dates are only ever set from evidence that states them.
CREATE TABLE IF NOT EXISTS pe_ownership (
    id                  INTEGER PRIMARY KEY,
    firm_id             INTEGER NOT NULL REFERENCES pe_firms(id),
    fund_id             INTEGER REFERENCES pe_funds(id),
    company_id          INTEGER NOT NULL REFERENCES pe_companies(id),
    relationship        TEXT NOT NULL DEFAULT 'unspecified'
                        CHECK (relationship IN ('platform','co_invest','continuation','unspecified')),
    stake               TEXT NOT NULL DEFAULT 'unspecified' CHECK (stake IN ('majority','minority','unspecified')),
    status              TEXT NOT NULL DEFAULT 'unclear' CHECK (status IN ('current','pending','historical','unclear')),
    basis               TEXT NOT NULL DEFAULT 'none',
    announced_date      TEXT,
    closed_date         TEXT,
    exit_date           TEXT,
    expected_close      TEXT,        -- a stated expectation; never treated as a closing
    as_of_date          TEXT,        -- the date the status statement is as of (source date, else the day the page was read)
    first_seen          TEXT NOT NULL,
    last_checked        TEXT NOT NULL,
    last_verified       TEXT,
    confidence          TEXT NOT NULL DEFAULT 'Low' CHECK (confidence IN ('High','Medium','Low')),
    conflict_note       TEXT,
    listing_absent_since TEXT,       -- the first successful full read of the firm's page that did not list it (an exit is NOT inferred)
    origin              TEXT NOT NULL DEFAULT 'portfolio_page' CHECK (origin IN ('portfolio_page','announcement','legacy','manual')),
    UNIQUE (firm_id, company_id)
);
CREATE INDEX IF NOT EXISTS ix_pe_ownership_company ON pe_ownership(company_id);

CREATE TABLE IF NOT EXISTS pe_ownership_log (
    id           INTEGER PRIMARY KEY,
    ownership_id INTEGER NOT NULL REFERENCES pe_ownership(id),
    at           TEXT NOT NULL,
    from_status  TEXT,
    to_status    TEXT NOT NULL,
    reason       TEXT NOT NULL
);

-- A platform company acquiring another business (an add-on). The target stays one entity, whatever its history.
CREATE TABLE IF NOT EXISTS pe_company_links (
    id                 INTEGER PRIMARY KEY,
    parent_company_id  INTEGER NOT NULL REFERENCES pe_companies(id),
    child_company_id   INTEGER NOT NULL REFERENCES pe_companies(id),
    kind               TEXT NOT NULL DEFAULT 'add_on' CHECK (kind IN ('add_on','merger')),
    status             TEXT NOT NULL DEFAULT 'unclear' CHECK (status IN ('announced','completed','unclear')),
    announced_date     TEXT,
    closed_date        TEXT,
    expected_close     TEXT,
    last_checked       TEXT NOT NULL,
    UNIQUE (parent_company_id, child_company_id, kind)
);

-- Every material claim about ownership, a deal or a place points at rows here. `verification`: page_verified = code retrieved the page and the
-- excerpt is in it; snippet_only = a search result or a model's report that code could not check (never shown as a fact).
CREATE TABLE IF NOT EXISTS pe_evidence (
    id             INTEGER PRIMARY KEY,
    subject_type   TEXT NOT NULL CHECK (subject_type IN ('ownership','link','company','firm','fund')),
    subject_id     INTEGER NOT NULL,
    claim          TEXT NOT NULL CHECK (claim IN ('listed','listed_current','listed_historical','announced','completed','exited','exit_announced','expected_close',
                                                  'location','add_on','fund','name_change','stake')),
    url            TEXT NOT NULL,
    publisher      TEXT,
    title          TEXT,
    published_date TEXT,             -- NULL = unknown; retrieval time is never a publication date
    event_date     TEXT,             -- the date of the event the source states, NULL = unknown
    retrieved_at   TEXT NOT NULL,
    excerpt        TEXT,
    value          TEXT,             -- claim-specific: 'majority' / 'minority' for stake, the place for location
    source_type    TEXT NOT NULL,
    content_hash   TEXT NOT NULL DEFAULT '',
    verification   TEXT NOT NULL CHECK (verification IN ('page_verified','snippet_only')),
    UNIQUE (subject_type, subject_id, claim, url, content_hash)
);
CREATE INDEX IF NOT EXISTS ix_pe_evidence_subject ON pe_evidence(subject_type, subject_id);

-- ---- sources, snapshots and runs (bd/sources.py, bd/pe_collect.py) ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pe_sources (
    id                INTEGER PRIMARY KEY,
    firm_id           INTEGER NOT NULL REFERENCES pe_firms(id),
    kind              TEXT NOT NULL CHECK (kind IN ('portfolio_page','newsroom')),
    url               TEXT NOT NULL,
    origin            TEXT NOT NULL DEFAULT 'firm_config' CHECK (origin IN ('firm_config','discovered','manual')),
    status            TEXT NOT NULL DEFAULT 'never' CHECK (status IN ('never','ok','failed','blocked')),
    last_attempt      TEXT,
    last_success      TEXT,
    last_error        TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    coverage          TEXT NOT NULL DEFAULT 'unknown' CHECK (coverage IN ('complete','partial','unknown')),
    coverage_note     TEXT,
    entries           INTEGER NOT NULL DEFAULT 0,
    UNIQUE (firm_id, kind, url)
);

CREATE TABLE IF NOT EXISTS pe_snapshots (
    id           INTEGER PRIMARY KEY,
    source_id    INTEGER REFERENCES pe_sources(id),
    url          TEXT NOT NULL,
    fetched_at   TEXT NOT NULL,
    http_status  INTEGER,
    content_type TEXT,
    bytes        INTEGER,
    content_hash TEXT NOT NULL,
    title        TEXT,
    text         TEXT NOT NULL,      -- visible text, capped; the excerpts of evidence are checked against it
    truncated    INTEGER NOT NULL DEFAULT 0,
    UNIQUE (url, content_hash)
);

CREATE TABLE IF NOT EXISTS pe_listing_entries (
    id          INTEGER PRIMARY KEY,
    source_id   INTEGER NOT NULL REFERENCES pe_sources(id),
    run_id      INTEGER NOT NULL,
    name        TEXT NOT NULL,
    href        TEXT,
    domain      TEXT,
    section     TEXT,                -- the heading the entry sat under, as written
    state       TEXT NOT NULL CHECK (state IN ('current','historical','unlabeled')),
    location    TEXT,                -- text such as "Chicago, IL" only when the listing shows it
    excerpt     TEXT
);
CREATE INDEX IF NOT EXISTS ix_pe_entries_source ON pe_listing_entries(source_id, run_id);

CREATE TABLE IF NOT EXISTS pe_runs (
    id        INTEGER PRIMARY KEY,
    kind      TEXT NOT NULL,
    started   TEXT NOT NULL,
    finished  TEXT,
    summary   TEXT,                  -- one line
    detail    TEXT                   -- JSON: per-source results
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
    firm_cols = {r[1] for r in conn.execute("PRAGMA table_info(pe_firms)")}
    for col in ("hq_state", "hq_source_url", "last_deals"):
        if col not in firm_cols:
            conn.execute(f"ALTER TABLE pe_firms ADD COLUMN {col} TEXT")
    if "client" not in {r[1] for r in conn.execute("PRAGMA table_info(lead_state)")}:
        conn.execute("ALTER TABLE lead_state ADD COLUMN client INTEGER NOT NULL DEFAULT 0")
    conn.commit()
