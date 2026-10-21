"""Collecting PE portfolios from the firms' own pages into the ownership ledger. Free (no model); the only cost is polite page reads.

Per source, in isolation (one failing site never stops the others):
  read the listing (bd.pe_listing) -> keep every entry as a relationship with page-verified evidence -> derive status by code ->
  note what changed since the previous reading (only when there was one).

What a run will not do: treat a failed or partial read as an empty portfolio, call a company that vanished from a page an exit, drop evidence
it already holds, or claim completeness it cannot support. A failed source keeps its last good evidence and is shown as stale by age."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone

from bd.config import Config
from bd.htmltext import links, ld_objects, node_text, parse_html
from bd.pe_listing import HQ_WORDS, PLACE, Collected, Entry, collect_listing
from bd.pe_ownership import (AmbiguousCompany, add_evidence, company_geography, ensure_ownership, recompute, record_location, resolve_company,
                             source_is_stale)  # noqa: F401  (re-exported: the stale check lives where the UI can import it without the page reader)
from bd.pe_sites import find_portfolio_by_path
from bd.pe_store import norm_name
from bd.sources import FetchError, Fetcher, now_iso, recently_read, store_snapshot
from bd.urls import host_in, hostname

BLOCKED = {"blocked_domain", "robots_disallowed", "robots_unavailable", "challenge_page", "http_401", "http_403", "private_address"}
PORTFOLIO_LINK = re.compile(r"^\s*(our\s+)?(portfolio( companies)?|companies|investments|our investments|our companies)\s*$", re.I)


@dataclass
class SourceOutcome:
    source_id: int
    firm: str
    url: str
    status: str                       # ok | cached | failed | blocked
    error: str | None = None
    coverage: str = "unknown"
    coverage_note: str = ""
    pages: int = 0
    entries: int = 0
    new_relationships: int = 0
    status_changes: int = 0
    marked_absent: int = 0
    unresolved: list[str] = field(default_factory=list)
    added: list[str] | None = None            # names newly listed since the previous reading (None = there was no previous reading)
    removed: list[str] | None = None
    via: str = "html"


@dataclass
class CollectSummary:
    run_id: int = 0
    firms: int = 0
    sources: int = 0
    ok: int = 0
    cached: int = 0
    failed: int = 0
    entries: int = 0
    new_relationships: int = 0
    status_changes: int = 0
    marked_absent: int = 0
    outcomes: list[SourceOutcome] = field(default_factory=list)

    def line(self) -> str:
        cov = Counter(o.coverage for o in self.outcomes if o.status == "ok")
        bits = [f"{self.sources} source(s) for {self.firms} firm(s): {self.ok} read" + (f" ({cov['complete']} complete, {cov['partial']} partial, {cov['unknown']} unknown)" if self.ok else ""),
                f"{self.cached} not re-read (recent)" if self.cached else "", f"{self.failed} FAILED" if self.failed else "",
                f"{self.entries} entries", f"{self.new_relationships} new relationship(s)", f"{self.status_changes} status change(s)"]
        return "; ".join(b for b in bits if b)


# ---- the source registry -----------------------------------------------------------------------------------------------------------------------
def register_sources(conn: sqlite3.Connection) -> int:
    """Every active firm with a portfolio URL has it as a source. Idempotent. Returns sources added."""
    n = 0
    with conn:
        for f in conn.execute("SELECT id, portfolio_url FROM pe_firms WHERE status = 'active' AND portfolio_url IS NOT NULL AND trim(portfolio_url) != ''"):
            n += conn.execute("INSERT OR IGNORE INTO pe_sources(firm_id, kind, url, origin) VALUES (?,?,?, 'firm_config')",
                              (f["id"], "portfolio_page", f["portfolio_url"].strip())).rowcount
    return n


def discover_portfolio_pages(conn: sqlite3.Connection, fetcher: Fetcher, cfg: Config, log: Callable[[str], None] = lambda s: None) -> int:
    """For active firms that have a domain but no portfolio URL: read the home page and register the link that says Portfolio / Companies /
    Investments on the firm's own host. The registered page is then read like any other. Returns pages registered."""
    n = 0
    for f in conn.execute("SELECT * FROM pe_firms WHERE status = 'active' AND (portfolio_url IS NULL OR trim(portfolio_url) = '') AND domain IS NOT NULL").fetchall():
        try:
            got = fetcher.get(f"https://{f['domain']}/")
        except FetchError as e:
            log(f"  {f['name']}: home page not read ({e.reason}); no portfolio page registered")
            continue
        page = parse_html(got.body)
        for href, text, _a in links(page.root, got.final_url):
            if PORTFOLIO_LINK.match(text) and hostname(href) == hostname(got.final_url):
                with conn:
                    n += conn.execute("INSERT OR IGNORE INTO pe_sources(firm_id, kind, url, origin) VALUES (?,?,?, 'discovered')",
                                      (f["id"], "portfolio_page", href)).rowcount
                log(f"  {f['name']}: portfolio page found at {href}")
                break
        else:
            found = find_portfolio_by_path(fetcher, f["domain"])
            if found:
                with conn:
                    n += conn.execute("INSERT OR IGNORE INTO pe_sources(firm_id, kind, url, origin) VALUES (?,?,?, 'discovered')", (f["id"], "portfolio_page", found)).rowcount
                log(f"  {f['name']}: no portfolio link on the home page; a listing of companies was found at {found}")
            else:
                log(f"  {f['name']}: no portfolio link on the home page and no listing at the usual paths; add the page under Firm details")
    return n


# ---- one source --------------------------------------------------------------------------------------------------------------------------------
def _source_type(firm: sqlite3.Row, url: str) -> str:
    """The page is the firm's own when it is on the firm's domain (or the firm has none recorded: the URL was entered as its page)."""
    return "firm_site" if not firm["domain"] or host_in(hostname(url), [firm["domain"]]) else "secondary_report"


def _entries_by_run(conn: sqlite3.Connection, source_id: int, run_id: int) -> dict[str, str]:
    """name key -> the name as the page wrote it, for one reading of a source."""
    return {norm_name(r["name"]): r["name"] for r in conn.execute("SELECT name FROM pe_listing_entries WHERE source_id = ? AND run_id = ?", (source_id, run_id))}


def _apply(conn: sqlite3.Connection, cfg: Config, firm: sqlite3.Row, src: sqlite3.Row, res: Collected, run_id: int, today: date, out: SourceOutcome) -> None:
    ids = sorted({e.snapshot_id for e in res.entries if e.snapshot_id})
    snaps = {r["id"]: r for r in conn.execute(f"SELECT * FROM pe_snapshots WHERE id IN ({','.join('?' * len(ids))})", ids)} if ids else {}
    domain_use = Counter(e.domain for e in res.entries if e.domain)      # a link shared by several entries is not any one company's site
    present: set[int] = set()
    for e in res.entries:
        conn.execute("INSERT INTO pe_listing_entries(source_id, run_id, name, href, domain, section, state, location, excerpt) VALUES (?,?,?,?,?,?,?,?,?)",
                     (src["id"], run_id, e.name, e.href, e.domain, e.section, e.state, e.location, e.excerpt))
        if not norm_name(e.name):                        # nothing to match on (empty, or only a legal suffix): kept as read, never a company
            out.unresolved.append(f"{e.name.strip() or '(no name)'} (a name with nothing to match on)")
            continue
        try:
            m = resolve_company(conn, e.name, e.domain if e.domain and domain_use[e.domain] == 1 else None, source_url=e.page_url, today=today)
        except AmbiguousCompany:
            out.unresolved.append(e.name)
            continue
        oid, created = ensure_ownership(conn, firm["id"], m.id, today, "unspecified", "portfolio_page")
        out.new_relationships += created
        snap = snaps.get(e.snapshot_id) if e.snapshot_id else None
        verified = bool(snap and e.excerpt and e.excerpt in snap["text"])
        if verified:
            claim = {"current": "listed_current", "historical": "listed_historical"}.get(e.state, "listed")
            kw = dict(retrieved_at=snap["fetched_at"], source_type=_source_type(firm, e.page_url), verification="page_verified",
                      content_hash=snap["content_hash"], excerpt=e.excerpt, title=snap["title"], publisher=firm["name"])
            add_evidence(conn, "ownership", oid, claim, e.page_url, value="by_contrast" if e.by_contrast else None, **kw)
            if e.location:
                m_loc = PLACE.search(e.location)
                eid, _ = add_evidence(conn, "company", m.id, "location", e.page_url, value=e.location, **kw)
                if m_loc:
                    record_location(conn, "company", m.id, e.location_kind or "office", m_loc.group(1), m_loc.group(2), eid, today)
        else:
            out.unresolved.append(f"{e.name} (excerpt not found in the stored page)")
        present.add(oid)
        conn.execute("UPDATE pe_ownership SET listing_absent_since = NULL WHERE id = ?", (oid,))
        before = conn.execute("SELECT status FROM pe_ownership WHERE id = ?", (oid,)).fetchone()["status"]
        after = recompute(conn, oid, today, cfg, verified=verified).status
        out.status_changes += int(before != after and not created)
    if res.coverage == "complete":
        # Only a complete reading can say a company is no longer listed; what that means is "unclear", never "exited".
        rows = conn.execute(
            "SELECT o.id, o.status FROM pe_ownership o WHERE o.firm_id = ? AND o.listing_absent_since IS NULL AND EXISTS ("
            " SELECT 1 FROM pe_evidence e WHERE e.subject_type = 'ownership' AND e.subject_id = o.id AND e.claim LIKE 'listed%' AND e.verification = 'page_verified'"
            "  AND e.url IN (SELECT url FROM pe_snapshots WHERE source_id = ?))", (firm["id"], src["id"])).fetchall()
        for r in rows:
            if r["id"] in present:
                continue
            conn.execute("UPDATE pe_ownership SET listing_absent_since = ? WHERE id = ?", (today.isoformat(), r["id"]))
            after = recompute(conn, r["id"], today, cfg).status
            out.marked_absent += 1
            out.status_changes += int(after != r["status"])


def collect_source(conn: sqlite3.Connection, cfg: Config, fetcher: Fetcher, firm: sqlite3.Row, src: sqlite3.Row, run_id: int, today: date,
                   force: bool = False) -> SourceOutcome:
    out = SourceOutcome(src["id"], firm["name"], src["url"], "ok")
    now = now_iso()
    snap = None if force or src["status"] in ("failed", "blocked") else recently_read(conn, src["url"], cfg)      # a source whose last attempt did not succeed is read again, not served from the cache
    if snap is not None:
        with conn:
            conn.execute("UPDATE pe_sources SET last_attempt = ? WHERE id = ?", (now, src["id"]))
        out.status, out.coverage, out.coverage_note, out.entries = "cached", src["coverage"], src["coverage_note"] or "", src["entries"]
        return out
    res = collect_listing(conn, fetcher, cfg, src["id"], src["url"])
    out.via, out.pages = res.via, len(res.pages)
    if res.error:
        out.status = "blocked" if res.error in BLOCKED else "failed"
        out.error = res.error                                                    # this attempt read nothing; the last good reading stands, and is described as such
        if src["last_success"]:
            out.coverage_note = f"this attempt read nothing; the last good reading ({src['last_success'][:10]}) had coverage {src['coverage']}"
        with conn:
            conn.execute("UPDATE pe_sources SET status=?, last_attempt=?, last_error=?, consecutive_failures=consecutive_failures+1 WHERE id=?",
                         (out.status, now, res.error, src["id"]))
        return out
    prev = conn.execute("SELECT MAX(run_id) AS r FROM pe_listing_entries WHERE source_id = ?", (src["id"],)).fetchone()["r"]
    with conn:
        _apply(conn, cfg, firm, src, res, run_id, today, out)
        now_names, before = _entries_by_run(conn, src["id"], run_id), _entries_by_run(conn, src["id"], prev) if prev else None
        if before is not None:
            out.added = sorted(now_names[k] for k in now_names.keys() - before.keys())
            out.removed = sorted(before[k] for k in before.keys() - now_names.keys())
        out.entries, out.coverage, out.coverage_note = len(res.entries), res.coverage, res.coverage_note
        conn.execute("UPDATE pe_sources SET status='ok', last_attempt=?, last_success=?, last_error=NULL, consecutive_failures=0, coverage=?, coverage_note=?, "
                     "entries=? WHERE id=?", (now, now, res.coverage, res.coverage_note, len(res.entries), src["id"]))
    return out


# ---- the run -----------------------------------------------------------------------------------------------------------------------------------
def run_collect(conn: sqlite3.Connection, cfg: Config, fetcher: Fetcher, today: date, firm: str | None = None, force: bool = False,
                log: Callable[[str], None] = lambda s: None) -> CollectSummary:
    """Read every registered portfolio source (optionally one firm's). Each source succeeds or fails alone; the run records what happened."""
    started = now_iso()
    run_id = conn.execute("INSERT INTO pe_runs(kind, started) VALUES ('collect', ?)", (started,)).lastrowid
    conn.commit()
    out = CollectSummary(run_id=run_id)
    q = ("SELECT s.*, f.name AS firm_name FROM pe_sources s JOIN pe_firms f ON f.id = s.firm_id WHERE f.status = 'active' AND s.kind = 'portfolio_page'"
         + (" AND lower(f.name) LIKE ?" if firm else "") + " ORDER BY f.name, s.id")
    sources = conn.execute(q, (f"%{firm.lower()}%",) if firm else ()).fetchall()
    out.firms = len({s["firm_id"] for s in sources})
    for src in sources:
        f = conn.execute("SELECT * FROM pe_firms WHERE id = ?", (src["firm_id"],)).fetchone()
        try:
            o = collect_source(conn, cfg, fetcher, f, src, run_id, today, force)
        except Exception as e:                               # failure isolation: a bug or a surprise in one source must not stop the run
            conn.rollback()
            o = SourceOutcome(src["id"], f["name"], src["url"], "failed", error=f"unexpected {type(e).__name__}: {str(e)[:160]}")
            with conn:
                conn.execute("UPDATE pe_sources SET status='failed', last_attempt=?, last_error=?, consecutive_failures=consecutive_failures+1 WHERE id=?",
                             (now_iso(), o.error, src["id"]))
        out.outcomes.append(o)
        out.sources += 1
        out.ok += o.status == "ok"
        out.cached += o.status == "cached"
        out.failed += o.status in ("failed", "blocked")
        out.entries += o.entries if o.status == "ok" else 0
        out.new_relationships += o.new_relationships
        out.status_changes += o.status_changes
        out.marked_absent += o.marked_absent
        log(f"  {o.firm}: {o.status}" + (f" ({o.error})" if o.error else f", {o.entries} entries, coverage {o.coverage}")
            + (f"; {o.coverage_note}" if o.coverage_note and o.status == "ok" else ""))
    with conn:
        conn.execute("UPDATE pe_runs SET finished = ?, summary = ?, detail = ? WHERE id = ?",
                     (now_iso(), out.line(), json.dumps([asdict(o) for o in out.outcomes]), run_id))
    return out


def run_history(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM pe_runs WHERE kind = 'collect' ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


# ---- where a company is, from its own site (free) -------------------------------------------------------------------------------------------------
LOCATE_PATHS = ("/", "/contact", "/contact-us", "/about", "/about-us", "/locations")


def _addresses(html: str) -> list[tuple[str, str, str, str]]:
    """(city, state, kind, excerpt) for places a company's page shows: structured postal addresses, <address> elements, and text that says
    headquarters. A place is a headquarters only when the page says so."""
    page = parse_html(html)
    found: list[tuple[str, str, str, str]] = []
    for o in ld_objects(page):
        a = o.get("address")
        if isinstance(a, dict) and a.get("addressLocality") and a.get("addressRegion"):
            region = str(a["addressRegion"]).strip().upper()
            if len(region) == 2:
                txt = f"{a['addressLocality']}, {region}"
                found.append((str(a["addressLocality"]), region, "office", txt))
    for n in page.root.walk():
        if n.tag == "address" or HQ_WORDS.search(" ".join(n.classes())):
            t = node_text(n)
            m = PLACE.search(t)
            if m and len(t) < 300:
                found.append((m.group(1), m.group(2), "headquarters" if HQ_WORDS.search(t) else "office", t[:300]))
    for n in page.root.walk():
        if n.tag in ("p", "li", "div", "span") and len(n.elements()) == 0:
            t = node_text(n)
            if HQ_WORDS.search(t) and len(t) < 200 and (m := PLACE.search(t)):
                found.append((m.group(1), m.group(2), "headquarters", t))
    return found


def locate_companies(conn: sqlite3.Connection, cfg: Config, fetcher: Fetcher, today: date, limit: int | None = None,
                     log: Callable[[str], None] = lambda s: None) -> int:
    """For portfolio companies with an official domain and no headquarters on file, read the company's own pages (bounded) for an address and
    record it with the page as evidence. Free. Returns companies for which a place was recorded."""
    cap = cfg.pe_portfolio.locate_per_run if limit is None else limit
    rows = conn.execute(
        "SELECT c.* FROM pe_companies c WHERE c.domain IS NOT NULL AND (c.account_id IS NULL OR NOT EXISTS (SELECT 1 FROM accounts a WHERE a.id = c.account_id AND a.chicagoland IN ('yes','no'))) "
        "AND NOT EXISTS (SELECT 1 FROM pe_locations l WHERE l.entity_type='company' AND l.entity_id = c.id AND l.kind='headquarters') "
        "AND EXISTS (SELECT 1 FROM pe_ownership o WHERE o.company_id = c.id AND o.status != 'historical') ORDER BY c.last_seen DESC, c.id LIMIT ?", (cap,)).fetchall()
    done = 0
    for c in rows:
        placed = False
        for path in LOCATE_PATHS[: cfg.pe_portfolio.fetch.max_pages_per_source]:
            url = f"https://{c['domain']}{path}"
            try:
                got = fetcher.get(url)
            except FetchError as e:
                if path == "/":
                    log(f"  {c['name']}: site not read ({e.reason})")
                    break
                continue
            with conn:
                sid = store_snapshot(conn, cfg, None, got)
                text = conn.execute("SELECT text FROM pe_snapshots WHERE id = ?", (sid,)).fetchone()["text"]
                for city, state, kind, excerpt in _addresses(got.body):
                    if " ".join(excerpt.split()) not in text and f"{city}, {state}" not in text:
                        continue                                                    # structured data that is not on the visible page is not shown as evidence
                    ex = " ".join(excerpt.split())
                    ex = ex if ex in text else f"{city}, {state}"
                    eid, _ = add_evidence(conn, "company", c["id"], "location", got.final_url, retrieved_at=got.fetched_at, source_type="company_site",
                                          verification="page_verified", content_hash=got.content_hash, excerpt=ex, value=f"{city}, {state}", publisher=c["name"])
                    record_location(conn, "company", c["id"], kind, city, state, eid, today)
                    placed = True
            if placed and conn.execute("SELECT 1 FROM pe_locations WHERE entity_type='company' AND entity_id=? AND kind='headquarters'", (c["id"],)).fetchone():
                break
        done += placed
        if placed:
            g = company_geography(conn, c["id"], cfg)
            log(f"  {c['name']}: place recorded; geography {g.status} ({g.reason})")
    return done
