"""The PE ownership ledger: portfolio companies as entities, sponsor relationships and the evidence behind every claim. No LLM, no network.

What this module guarantees (each one is a test in tests/test_pe_ownership.py):
  * A relationship's status is derived by code from evidence, never typed in: current / pending / historical / unclear.
  * An announcement never proves a closing, an old announcement never proves current ownership, and a company missing from a firm's page is
    never an exit (it becomes "unclear" until something states what happened).
  * Dates come only from evidence that states them. Retrieval time is never a publication or event date. Holding duration is computed only
    from a closing date a source states, so it is None whenever the closing is unverified.
  * Evidence that code could not check (`snippet_only`) never supports a status; it is kept and shown as unverified.
  * A company is one entity however many sponsors list it, however it is named and whether or not it is already an account.
  * A failed collection changes nothing here: evidence is only added, never removed."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from bd.accounts import is_valid_domain, log_audit, normalize_domain
from bd.config import Config
from bd.fit_gate import is_competitor
from bd.pe_store import norm_name

SOURCE_TYPES = ("firm_site", "company_site", "company_release", "sec_filing", "secondary_report", "search_snippet", "workbook")
AUTHORITATIVE = frozenset({"firm_site", "company_site", "company_release", "sec_filing"})   # the parties to the event, or a regulator
CLAIMS = ("listed", "listed_current", "listed_historical", "announced", "completed", "exited", "exit_announced", "expected_close", "location", "add_on", "fund",
          "name_change", "stake")
STATUSES = ("current", "pending", "historical", "unclear")
EXCERPT_MAX = 400


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso(d: date | str | None) -> str | None:
    if d is None or d == "":
        return None
    if isinstance(d, date):
        return d.isoformat()
    date.fromisoformat(d)          # raises ValueError for a malformed date: unknown stays None, it is never guessed
    return d


# ---- evidence ----------------------------------------------------------------------------------------------------------------------
def add_evidence(conn: sqlite3.Connection, subject_type: str, subject_id: int, claim: str, url: str, *, retrieved_at: str,
                 source_type: str, verification: str, content_hash: str = "", excerpt: str | None = None, title: str | None = None,
                 publisher: str | None = None, published: date | str | None = None, event: date | str | None = None,
                 value: str | None = None) -> tuple[int, bool]:
    """Record one piece of evidence. Idempotent: the same claim from the same page content is stored once. Returns (id, created).
    A `page_verified` row must carry the content hash of the retrieved page and an excerpt taken from it."""
    if claim not in CLAIMS:
        raise ValueError(f"unknown claim {claim!r}")
    if source_type not in SOURCE_TYPES:
        raise ValueError(f"unknown source type {source_type!r}")
    if verification not in ("page_verified", "snippet_only"):
        raise ValueError(f"unknown verification {verification!r}")
    if verification == "page_verified" and not (content_hash and excerpt):
        raise ValueError("page_verified evidence needs the page's content hash and an excerpt from it")
    cur = conn.execute(
        "INSERT OR IGNORE INTO pe_evidence(subject_type, subject_id, claim, url, publisher, title, published_date, event_date, retrieved_at, "
        "excerpt, value, source_type, content_hash, verification) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (subject_type, subject_id, claim, url.strip(), publisher, title, _iso(published), _iso(event), retrieved_at,
         " ".join(excerpt.split())[:EXCERPT_MAX] if excerpt else None, value, source_type, content_hash, verification))
    if cur.rowcount:
        return cur.lastrowid, True
    row = conn.execute("SELECT id FROM pe_evidence WHERE subject_type=? AND subject_id=? AND claim=? AND url=? AND content_hash=?",
                       (subject_type, subject_id, claim, url.strip(), content_hash)).fetchone()
    conn.execute("UPDATE pe_evidence SET retrieved_at = ? WHERE id = ? AND retrieved_at < ?", (retrieved_at, row["id"], retrieved_at))   # seen again
    return row["id"], False


def evidence_for(conn: sqlite3.Connection, subject_type: str, subject_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM pe_evidence WHERE subject_type = ? AND subject_id = ? ORDER BY retrieved_at, id",
                        (subject_type, subject_id)).fetchall()


# ---- companies: one entity however it is named --------------------------------------------------------------------------------------
class AmbiguousCompany(Exception):
    """Several known companies fit a name that carries no domain. The caller records the entry as unresolved instead of guessing."""

    def __init__(self, name: str, candidates: list[int]) -> None:
        super().__init__(f"{name!r} fits {len(candidates)} companies; a domain is needed to tell them apart")
        self.candidates = candidates


@dataclass
class CompanyMatch:
    id: int
    created: bool
    how: str                      # domain | alias | name | account | new | homonym


def _alias_keys(conn: sqlite3.Connection, company_id: int) -> set[str]:
    return {r["alias_key"] for r in conn.execute("SELECT alias_key FROM pe_aliases WHERE entity_type='company' AND entity_id=?", (company_id,))}


def add_alias(conn: sqlite3.Connection, entity_type: str, entity_id: int, alias: str, kind: str = "other_name",
              source_url: str | None = None) -> bool:
    alias = " ".join((alias or "").split())
    key = norm_name(alias)
    if not key:
        return False
    cur = conn.execute("INSERT OR IGNORE INTO pe_aliases(entity_type, entity_id, alias, alias_key, kind, source_url) VALUES (?,?,?,?,?,?)",
                       (entity_type, entity_id, alias, key, kind, source_url))
    return bool(cur.rowcount)


def _clean_domain(domain: str | None) -> str | None:
    if not domain:
        return None
    d = normalize_domain(domain)
    return d if d and is_valid_domain(d) else None


def resolve_company(conn: sqlite3.Connection, name: str, domain: str | None = None, aliases: tuple[str, ...] = (),
                    source_url: str | None = None, today: date | None = None) -> CompanyMatch:
    """Find the company this name and domain refer to, or create it. Order: the same official domain; a known name or alias whose domain does
    not contradict; an existing account of the same name or domain (so a discovered holding that is already a target is one record). A
    same-named company with a different domain is a different company. Caller owns the transaction."""
    name = " ".join((name or "").split())
    key = norm_name(name)
    if not key:
        raise ValueError("a company needs a name")
    dom = _clean_domain(domain)
    day = (today or datetime.now(timezone.utc).date()).isoformat()
    found: sqlite3.Row | None = None
    how = "new"
    if dom:
        found = conn.execute("SELECT * FROM pe_companies WHERE domain = ?", (dom,)).fetchone()
        how = "domain"
    if found is None:
        keys = {key} | {norm_name(a) for a in aliases if norm_name(a)}
        cands = []
        for r in conn.execute("SELECT * FROM pe_companies"):
            if r["name_key"] in keys or keys & _alias_keys(conn, r["id"]):
                cands.append(r)
        compatible = [r for r in cands if not (dom and r["domain"] and r["domain"] != dom)]
        if len(compatible) == 1:
            found, how = compatible[0], ("name" if compatible[0]["name_key"] == key else "alias")
        elif len(compatible) > 1:
            raise AmbiguousCompany(name, [r["id"] for r in compatible])
        elif cands and not compatible:
            how = "homonym"        # the name matches, but only companies with another official domain: do not merge them
    acct = None
    if dom:
        acct = conn.execute("SELECT id FROM accounts WHERE domain = ?", (dom,)).fetchone()
    if acct is None:
        for r in conn.execute("SELECT id, name FROM accounts"):
            if norm_name(r["name"]) == key:
                acct = r
                break
    if found is None:
        cur = conn.execute("INSERT INTO pe_companies(name, name_key, domain, account_id, created, last_seen) VALUES (?,?,?,?,?,?)",
                           (name, key, dom, acct["id"] if acct and not _account_taken(conn, acct["id"]) else None, day, day))
        cid = cur.lastrowid
        for a in aliases:
            if norm_name(a) != key:
                add_alias(conn, "company", cid, a, "other_name", source_url)
        return CompanyMatch(cid, True, "account" if acct and how == "new" else how)
    cid = found["id"]
    if norm_name(found["name"]) != key:
        add_alias(conn, "company", cid, name, "other_name", source_url)
    for a in aliases:
        if norm_name(a) != found["name_key"]:
            add_alias(conn, "company", cid, a, "other_name", source_url)
    if dom and not found["domain"]:
        conn.execute("UPDATE pe_companies SET domain = ? WHERE id = ?", (dom, cid))
    if acct and found["account_id"] is None and not _account_taken(conn, acct["id"]):
        conn.execute("UPDATE pe_companies SET account_id = ? WHERE id = ?", (acct["id"], cid))
    conn.execute("UPDATE pe_companies SET last_seen = ? WHERE id = ?", (day, cid))
    return CompanyMatch(cid, False, how)


def is_peer_company(conn: sqlite3.Connection, company_id: int, cfg: Config) -> bool:
    """The company competes with the client (fit_gate.competitors), by its name, any other name it is known by, or its domain. A peer is shown as one and
    is never researched, promoted or recommended as a prospect."""
    co = conn.execute("SELECT name, domain FROM pe_companies WHERE id = ?", (company_id,)).fetchone()
    if co is None:
        return False
    names = [co["name"]] + [r["alias"] for r in conn.execute("SELECT alias FROM pe_aliases WHERE entity_type='company' AND entity_id = ?", (company_id,))]
    return any(is_competitor(n, co["domain"], cfg) for n in names)


def _account_taken(conn: sqlite3.Connection, account_id: int) -> bool:
    return conn.execute("SELECT 1 FROM pe_companies WHERE account_id = ?", (account_id,)).fetchone() is not None


def merge_companies(conn: sqlite3.Connection, keep_id: int, drop_id: int, actor: str = "pe") -> None:
    """Fold a duplicate into the company that stays: relationships, evidence, aliases, places and links move; where both have the same sponsor
    the relationship with the stronger status stays and the other's evidence moves to it. Caller owns the transaction."""
    if keep_id == drop_id:
        return
    keep = conn.execute("SELECT * FROM pe_companies WHERE id = ?", (keep_id,)).fetchone()
    drop = conn.execute("SELECT * FROM pe_companies WHERE id = ?", (drop_id,)).fetchone()
    if keep is None or drop is None:
        raise ValueError("no such company")
    add_alias(conn, "company", keep_id, drop["name"], "other_name")
    conn.execute("UPDATE OR IGNORE pe_aliases SET entity_id = ? WHERE entity_type='company' AND entity_id = ?", (keep_id, drop_id))
    conn.execute("DELETE FROM pe_aliases WHERE entity_type='company' AND entity_id = ?", (drop_id,))
    for o in conn.execute("SELECT * FROM pe_ownership WHERE company_id = ?", (drop_id,)).fetchall():
        same = conn.execute("SELECT id FROM pe_ownership WHERE firm_id=? AND company_id=?", (o["firm_id"], keep_id)).fetchone()
        if same is None:
            conn.execute("UPDATE pe_ownership SET company_id = ? WHERE id = ?", (keep_id, o["id"]))
        else:
            if o["relationship"] != "unspecified":
                conn.execute("UPDATE pe_ownership SET relationship = ? WHERE id = ? AND relationship = 'unspecified'", (o["relationship"], same["id"]))
            conn.execute("UPDATE OR IGNORE pe_evidence SET subject_id = ? WHERE subject_type='ownership' AND subject_id = ?", (same["id"], o["id"]))
            conn.execute("DELETE FROM pe_evidence WHERE subject_type='ownership' AND subject_id = ?", (o["id"],))
            conn.execute("UPDATE pe_ownership_log SET ownership_id = ? WHERE ownership_id = ?", (same["id"], o["id"]))
            conn.execute("DELETE FROM pe_ownership WHERE id = ?", (o["id"],))
    conn.execute("UPDATE OR IGNORE pe_evidence SET subject_id = ? WHERE subject_type='company' AND subject_id = ?", (keep_id, drop_id))
    conn.execute("UPDATE OR IGNORE pe_locations SET entity_id = ? WHERE entity_type='company' AND entity_id = ?", (keep_id, drop_id))
    for col in ("parent_company_id", "child_company_id"):
        conn.execute(f"UPDATE OR IGNORE pe_company_links SET {col} = ? WHERE {col} = ?", (keep_id, drop_id))
    conn.execute("DELETE FROM pe_company_links WHERE parent_company_id = child_company_id")
    if keep["domain"] is None and drop["domain"]:
        conn.execute("UPDATE pe_companies SET domain = NULL WHERE id = ?", (drop_id,))
        conn.execute("UPDATE pe_companies SET domain = ? WHERE id = ?", (drop["domain"], keep_id))
    if keep["account_id"] is None and drop["account_id"]:
        conn.execute("UPDATE pe_companies SET account_id = NULL WHERE id = ?", (drop_id,))
        conn.execute("UPDATE pe_companies SET account_id = ? WHERE id = ?", (drop["account_id"], keep_id))
    conn.execute("DELETE FROM pe_companies WHERE id = ?", (drop_id,))
    log_audit(conn, actor, "update", None, "pe_company_merge", f"#{drop_id} {drop['name']}", f"#{keep_id} {keep['name']}")


# ---- places ---------------------------------------------------------------------------------------------------------------------------
def record_location(conn: sqlite3.Connection, entity_type: str, entity_id: int, kind: str, city: str | None, state: str | None,
                    evidence_id: int | None, today: date) -> bool:
    """Record a place a retrieved page shows. `headquarters` only when the page says so; anything else is an `office` or `facility`. A firm's
    places are never a portfolio company's."""
    city = " ".join((city or "").split()) or None
    state = (state or "").strip().upper() or None
    if not (city or state):
        return False
    cur = conn.execute("INSERT OR IGNORE INTO pe_locations(entity_type, entity_id, kind, city, state, evidence_id, created) VALUES (?,?,?,?,?,?,?)",
                       (entity_type, entity_id, kind, city, state, evidence_id, today.isoformat()))
    return bool(cur.rowcount)


def company_places(conn: sqlite3.Connection, company_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM pe_locations WHERE entity_type='company' AND entity_id = ? ORDER BY kind, city", (company_id,)).fetchall()


@dataclass
class Geography:
    status: str          # in_scope | out_of_scope | unknown
    reason: str
    city: str | None = None
    state: str | None = None


def company_geography(conn: sqlite3.Connection, company_id: int, cfg: Config) -> Geography:
    """Where the company is against the configured scope (fit_gate.state and counties). Only a headquarters that a retrieved page shows counts;
    the sponsor's location never does. A state other than the configured one is out of scope. Inside the state a city is in scope only when
    it is listed in pe_portfolio.scope_cities or the company is an account whose Chicagoland status was verified; otherwise it is unknown, and the
    reason says what is missing. An account's own verified facts win over a listing."""
    co = conn.execute("SELECT * FROM pe_companies WHERE id = ?", (company_id,)).fetchone()
    if co["account_id"]:
        a = conn.execute("SELECT hq_city, chicagoland FROM accounts WHERE id = ?", (co["account_id"],)).fetchone()
        if a and a["chicagoland"] == "yes":
            return Geography("in_scope", "its account facts say Chicagoland", a["hq_city"], cfg.fit_gate.state)
        if a and a["chicagoland"] == "no":
            return Geography("out_of_scope", "its account facts say not Chicagoland", a["hq_city"], None)
    # A place tied to evidence that code did not verify against a retrieved page (a search snippet, the researched workbook) never places a company, even if
    # one is recorded by mistake. Every real writer (bd.pe_collect) passes the page-verified evidence it read the place from.
    verified = "LEFT JOIN pe_evidence e ON e.id = l.evidence_id WHERE (l.evidence_id IS NULL OR e.verification = 'page_verified') AND "
    hq = conn.execute("SELECT l.city, l.state FROM pe_locations l " + verified + "l.entity_type='company' AND l.entity_id=? AND l.kind='headquarters' "
                      "ORDER BY l.id LIMIT 1", (company_id,)).fetchone()
    if hq is None:
        other = conn.execute("SELECT l.kind, l.city, l.state FROM pe_locations l " + verified + "l.entity_type='company' AND l.entity_id=? ORDER BY l.id LIMIT 1",
                             (company_id,)).fetchone()
        if other is not None:
            where = ", ".join(x for x in (other["city"], other["state"]) if x)
            return Geography("unknown", f"{where} is shown by a retrieved page, but no page says it is the headquarters", other["city"], other["state"])
        return Geography("unknown", "no headquarters shown by a retrieved page")
    city, state = hq["city"], hq["state"]
    if state and state != cfg.fit_gate.state.upper():
        return Geography("out_of_scope", f"headquarters in {city + ', ' if city else ''}{state}, outside {cfg.fit_gate.state}", city, state)
    if state == cfg.fit_gate.state.upper() and city and city.lower() in {c.lower() for c in cfg.pe_portfolio.scope_cities}:
        return Geography("in_scope", f"headquarters in {city}, {state}, a configured scope city", city, state)
    if not cfg.pe_portfolio.scope_cities:
        return Geography("unknown", f"headquarters in {city or 'an unnamed city'}, {state or 'an unknown state'}; counties are not mapped to cities and "
                                    "pe_portfolio.scope_cities is not configured", city, state)
    return Geography("unknown", f"headquarters in {city or 'an unnamed city'}, {state or 'an unknown state'}, not in the configured scope cities; "
                                "county not verified", city, state)


# ---- relationships ----------------------------------------------------------------------------------------------------------------------
def ensure_fund(conn: sqlite3.Connection, firm_id: int, name: str, vintage: int | None = None) -> int:
    key = norm_name(name)
    row = conn.execute("SELECT id FROM pe_funds WHERE firm_id = ? AND name_key = ?", (firm_id, key)).fetchone()
    if row:
        return row["id"]
    return conn.execute("INSERT INTO pe_funds(firm_id, name, name_key, vintage) VALUES (?,?,?,?)",
                        (firm_id, " ".join(name.split()), key, vintage)).lastrowid


def ensure_ownership(conn: sqlite3.Connection, firm_id: int, company_id: int, today: date, relationship: str = "unspecified",
                     origin: str = "portfolio_page") -> tuple[int, bool]:
    """One relationship per sponsor and company. A later source that states the role (platform, co-invest...) refines an unspecified one."""
    row = conn.execute("SELECT id, relationship FROM pe_ownership WHERE firm_id=? AND company_id=?", (firm_id, company_id)).fetchone()
    if row:
        if relationship != "unspecified" and row["relationship"] == "unspecified":
            conn.execute("UPDATE pe_ownership SET relationship = ? WHERE id = ?", (relationship, row["id"]))
        return row["id"], False
    cur = conn.execute("INSERT INTO pe_ownership(firm_id, company_id, relationship, origin, first_seen, last_checked) VALUES (?,?,?,?,?,?)",
                       (firm_id, company_id, relationship, origin, today.isoformat(), today.isoformat()))
    return cur.lastrowid, True


@dataclass
class Facts:
    """What the evidence for one relationship establishes. Built from page_verified evidence only."""
    listing_state: str | None = None              # 'current' | 'historical' | None: the latest firm-page reading
    listing_conflict: bool = False                # pages disagree about current vs historical
    announced: list[str | None] = field(default_factory=list)      # event dates ('' kept as None: stated but undated)
    completed: list[str | None] = field(default_factory=list)
    exited: list[str | None] = field(default_factory=list)
    exit_announced: list[str | None] = field(default_factory=list)
    expected_close: str | None = None
    completed_authoritative: bool = False
    exited_authoritative: bool = False
    announced_authoritative: bool = False
    listing_absent_since: str | None = None
    listing_by_contrast: bool = False             # current only because the page has a separate exited section it is not in
    workbook_lead: bool = False                   # the relationship is known only from the researched workbook, which code has not checked against a page


@dataclass
class StatusResult:
    status: str
    basis: str
    confidence: str
    closed_date: str | None = None
    exit_date: str | None = None
    announced_date: str | None = None
    conflict: str | None = None


def facts_from_evidence(rows: list[sqlite3.Row], listing_absent_since: str | None = None) -> Facts:
    f = Facts(listing_absent_since=listing_absent_since)
    latest: dict[str, tuple[str, set[str]]] = {}          # url -> (the latest reading's time, the states it shows)
    by_contrast = False
    for r in rows:
        if r["verification"] != "page_verified":
            f.workbook_lead |= r["source_type"] == "workbook"
            continue                                         # a snippet never supports a status
        auth = r["source_type"] in AUTHORITATIVE
        c = r["claim"]
        if c in ("listed", "listed_current", "listed_historical"):
            state = {"listed": "unlabeled", "listed_current": "current", "listed_historical": "historical"}[c]
            cur = latest.get(r["url"])
            if cur is None or r["retrieved_at"] > cur[0]:
                latest[r["url"]] = (r["retrieved_at"], {state})
            elif r["retrieved_at"] == cur[0]:
                cur[1].add(state)                            # one reading showing it in two sections is a conflict, not a choice
            if state == "current" and r["value"] == "by_contrast":
                by_contrast = True
        elif c == "announced":
            f.announced.append(r["event_date"])
            f.announced_authoritative |= auth
        elif c == "completed":
            f.completed.append(r["event_date"])
            f.completed_authoritative |= auth
        elif c == "exited":
            f.exited.append(r["event_date"])
            f.exited_authoritative |= auth
        elif c == "exit_announced":
            f.exit_announced.append(r["event_date"])
        elif c == "expected_close" and r["event_date"]:
            f.expected_close = max(filter(None, [f.expected_close, r["event_date"]]))
    states = set().union(*(v[1] for v in latest.values())) if latest else set()
    if len(states) == 1:
        f.listing_state = next(iter(states))
        f.listing_by_contrast = by_contrast and f.listing_state == "current"
    elif len(states) > 1:
        f.listing_conflict = True
    return f


def _earliest(dates: list[str | None]) -> str | None:
    known = [d for d in dates if d]
    return min(known) if known else None


def derive_status(f: Facts, today: date, cfg: Config) -> StatusResult:
    """The status rules, in order (see the module docstring for what they refuse to assume). A sale that was announced but not stated to have
    closed changes nothing about the status; it is noted."""
    res = _derive_core(f, today, cfg)
    if f.exit_announced and not f.exited and res.status != "historical":
        d = _earliest(f.exit_announced)
        res.conflict = "; ".join(filter(None, [res.conflict, f"a sale was announced{' on ' + d if d else ''}; no source states that it closed"]))
    return res


def _derive_core(f: Facts, today: date, cfg: Config) -> StatusResult:
    comp_day, exit_day, ann_day = _earliest(f.completed), _earliest(f.exited), _earliest(f.announced)
    base = dict(closed_date=comp_day, exit_date=exit_day, announced_date=ann_day)
    # 1. a stated exit
    if f.exited:
        latest_comp = max((d for d in f.completed if d), default=None)
        if exit_day and latest_comp and latest_comp > max(d for d in f.exited if d):
            return StatusResult("unclear", "conflict", "Low", **base,
                                conflict=f"a completion dated {latest_comp} is later than the stated exit {max(d for d in f.exited if d)}")
        return StatusResult("historical", "exit_notice", "High" if f.exited_authoritative else "Medium", **base)
    # 2. pages disagree
    if f.listing_conflict:
        return StatusResult("unclear", "conflict", "Low", **base, conflict="firm pages disagree about whether the company is a current holding")
    # 3. the firm's page puts it among past investments
    if f.listing_state == "historical":
        return StatusResult("historical", "firm_listing_historical", "Medium", **base)
    # 4. a stated completion, nothing later
    if f.completed:
        note = f"not listed on the firm's page since {f.listing_absent_since}; the completion still stands" if f.listing_absent_since else None
        return StatusResult("current", "completion_notice", "High" if f.completed_authoritative else "Medium", **base, conflict=note)
    # 5. the firm's page lists it, as current or without saying
    if f.listing_state in ("current", "unlabeled") and f.listing_absent_since:
        return StatusResult("unclear", "listing_absent", "Low", **base,
                            conflict=f"no longer listed on the firm's page since {f.listing_absent_since}; no source states an exit")
    if f.listing_state == "current":
        if f.listing_by_contrast:
            return StatusResult("current", "firm_listing_by_contrast", "Low", **base,
                                conflict="the page does not label it current; it is outside the page's separate exited section")
        note = None
        if f.announced and not f.completed:
            note = (f"an announcement{' on ' + ann_day if ann_day else ''}{' expected the deal to close ' + f.expected_close if f.expected_close else ''} is on record; "
                    "no source states the closing date, but the firm's own page lists the company")
        return StatusResult("current", "firm_listing", "Medium", **base, conflict=note)
    if f.listing_state == "unlabeled":
        return StatusResult("unclear", "listed_unlabeled", "Low", **base,
                            conflict="listed on the firm's page, which does not say whether the investment is current or exited")
    # 6. an announcement with no completion
    if f.announced:
        ref = ann_day
        in_window = bool(ref) and (today - date.fromisoformat(ref)).days <= cfg.pe_portfolio.pending_window_days
        expected_ok = bool(f.expected_close) and date.fromisoformat(f.expected_close) >= today - timedelta(days=30)
        if in_window or expected_ok:
            return StatusResult("pending", "announcement", "Medium" if f.announced_authoritative else "Low", **base)
        return StatusResult("unclear", "announcement_only", "Low", **base,
                            conflict="announced; no source states that it closed, and the announcement is older than the pending window"
                            if ref else "announced on an unstated date; no source states that it closed")
    return StatusResult("unclear", "listing_absent" if f.listing_absent_since else ("workbook_lead" if f.workbook_lead else "none"), "Low", **base)


def recompute(conn: sqlite3.Connection, ownership_id: int, today: date, cfg: Config, verified: bool = False) -> StatusResult:
    """Re-derive one relationship from its evidence and store it. Logs a status change. `verified` marks a successful fresh read of a source
    that supports the relationship (sets last_verified). Caller owns the transaction."""
    o = conn.execute("SELECT * FROM pe_ownership WHERE id = ?", (ownership_id,)).fetchone()
    rows = evidence_for(conn, "ownership", ownership_id)
    facts = facts_from_evidence(rows, o["listing_absent_since"])
    res = derive_status(facts, today, cfg)
    stakes = {r["value"] for r in rows if r["claim"] == "stake" and r["verification"] == "page_verified" and r["value"] in ("majority", "minority")}
    stake = next(iter(stakes)) if len(stakes) == 1 else "unspecified"
    note = res.conflict
    if len(stakes) > 1:
        note = "; ".join(filter(None, [note, "sources disagree about the stake"]))
    as_of = max((r["event_date"] or r["published_date"] or r["retrieved_at"][:10] for r in rows if r["verification"] == "page_verified"), default=None)
    if o["status"] != res.status:
        conn.execute("INSERT INTO pe_ownership_log(ownership_id, at, from_status, to_status, reason) VALUES (?,?,?,?,?)",
                     (ownership_id, today.isoformat(), o["status"], res.status, res.basis))
    conn.execute(
        "UPDATE pe_ownership SET status=?, basis=?, confidence=?, announced_date=?, closed_date=?, exit_date=?, expected_close=?, stake=?, "
        "conflict_note=?, as_of_date=?, last_checked=?, last_verified=CASE WHEN ? THEN ? ELSE last_verified END WHERE id=?",
        (res.status, res.basis, res.confidence, res.announced_date, res.closed_date, res.exit_date, facts.expected_close, stake, note, as_of,
         today.isoformat(), 1 if verified else 0, today.isoformat(), ownership_id))
    return res


def hold_days(o: sqlite3.Row, today: date) -> int | None:
    """Days since the closing, only when a source states the closing date and the relationship is current. An announcement date is never used,
    and nothing in scoring reads this."""
    if o["status"] != "current" or not o["closed_date"]:
        return None
    return (today - date.fromisoformat(o["closed_date"])).days


def is_stale(o: sqlite3.Row, today: date, cfg: Config) -> bool:
    """Ownership last verified longer ago than the configured window (or never verified). Staleness is shown, never acted on."""
    if not o["last_verified"]:
        return True
    return (today - date.fromisoformat(o["last_verified"])).days > cfg.pe_portfolio.ownership_fresh_days


def source_is_stale(src: sqlite3.Row, today: date, cfg: Config) -> bool:
    """Never read, or last read successfully longer ago than listing_fresh_days. Shown, never acted on."""
    if not src["last_success"]:
        return True
    return (today - datetime.fromisoformat(src["last_success"]).date()).days > cfg.pe_portfolio.listing_fresh_days


# ---- add-ons ----------------------------------------------------------------------------------------------------------------------------
def record_add_on(conn: sqlite3.Connection, parent_id: int, child_id: int, today: date, *, announced: date | str | None = None,
                  closed: date | str | None = None, expected_close: date | str | None = None, completed: bool = False,
                  kind: str = "add_on") -> int:
    """A platform company's acquisition of another business. 'completed' needs a source that says it completed (`completed`, with a closing date
    when the source states one); an announcement alone stays 'announced'. A completion is never walked back by a later announcement-only read."""
    if parent_id == child_id:
        raise ValueError("a company cannot be its own add-on")
    if kind not in ("add_on", "merger"):
        raise ValueError(f"unknown link kind {kind!r}")
    done = bool(completed or closed)
    status = "completed" if done else ("announced" if announced else "unclear")
    row = conn.execute("SELECT id, status FROM pe_company_links WHERE parent_company_id=? AND child_company_id=? AND kind=?",
                       (parent_id, child_id, kind)).fetchone()
    if row is None:
        return conn.execute(
            "INSERT INTO pe_company_links(parent_company_id, child_company_id, kind, status, announced_date, closed_date, expected_close, last_checked) "
            "VALUES (?,?,?,?,?,?,?,?)", (parent_id, child_id, kind, status, _iso(announced), _iso(closed), _iso(expected_close), today.isoformat())).lastrowid
    keep = "completed" if row["status"] == "completed" else status
    conn.execute("UPDATE pe_company_links SET status=?, announced_date=COALESCE(announced_date, ?), closed_date=COALESCE(closed_date, ?), "
                 "expected_close=COALESCE(?, expected_close), last_checked=? WHERE id=?",
                 (keep, _iso(announced), _iso(closed), _iso(expected_close), today.isoformat(), row["id"]))
    return row["id"]


# ---- the older, single-owner links ---------------------------------------------------------------------------------------------------------
def mirror_legacy(conn: sqlite3.Connection, today: date, cfg: Config | None = None) -> int:
    """Bring pre-ledger `portfolio_links` into the ledger as UNCLEAR relationships with unverified evidence, so they are listed and can be
    verified but are never shown as verified holdings. Existing ledger rows are left as they are. Returns relationships created. Needs no
    configuration: nothing here is derived from evidence that code has read. Caller owns the transaction."""
    made = 0
    for l in conn.execute("SELECT l.*, f.id AS fid FROM portfolio_links l JOIN pe_firms f ON f.id = l.firm_id").fetchall():
        m = resolve_company(conn, l["company_name"], l["company_domain"], today=today)
        if l["account_id"] and conn.execute("SELECT account_id FROM pe_companies WHERE id=?", (m.id,)).fetchone()["account_id"] is None \
                and not _account_taken(conn, l["account_id"]):
            conn.execute("UPDATE pe_companies SET account_id = ? WHERE id = ?", (l["account_id"], m.id))
        oid, created = ensure_ownership(conn, l["firm_id"], m.id, today, "unspecified", "legacy")
        if not created:
            continue
        made += 1
        if l["source_url"]:
            add_evidence(conn, "ownership", oid, "listed_historical" if l["status"] == "exited" else "listed_current", l["source_url"],
                         retrieved_at=l["found_date"] + "T00:00:00+00:00", source_type="search_snippet", verification="snippet_only")
        note = ("the account's facts name this sponsor (from researched evidence); the sponsor's own page has not been read by code" if l["origin"] == "account"
                else "recorded by an earlier version of the product from a model's report; the source has not been re-read by code")
        conn.execute("UPDATE pe_ownership SET status='unclear', basis='legacy_unverified', confidence='Low', conflict_note=?, as_of_date=NULL WHERE id=?", (note, oid))
        conn.execute("INSERT INTO pe_ownership_log(ownership_id, at, from_status, to_status, reason) VALUES (?,?,?,?,?)", (oid, today.isoformat(), None, "unclear", "legacy_unverified"))
    return made
