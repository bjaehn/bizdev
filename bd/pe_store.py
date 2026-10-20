"""Private equity firms, their portfolio links and firm-level signals: storage and rules. No LLM, no network.

A portfolio link says "this firm owns this company". Its origin is how we know:
  account    - the company is an account whose ownership is pe-backed and whose sponsor names the firm (facts that
               already needed verified evidence); `sync_from_accounts` keeps these in step with the accounts;
  portfolio  - the firm's own portfolio page lists the company (verified by `bd pe-portfolio`);
  manual     - a person added it.
Only `current` links let a firm's signals lift a company's score."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone

from bd.accounts import is_valid_domain, log_audit, normalize_domain
from bd.models import ScoringSignal

_SUFFIX = re.compile(r"\b(llc|l l c|lp|l p|llp|inc|ltd|limited|co|corp|corporation|the)\b")
_RANK = {"Low": 0, "Medium": 1, "High": 2}


def norm_name(name: str) -> str:
    """Comparison key for firm and company names: case, punctuation and legal suffixes ignored."""
    n = re.sub(r"[^a-z0-9& ]+", " ", (name or "").lower().replace("&", " & "))
    return " ".join(_SUFFIX.sub(" ", n).split())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def find_firm(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    key = norm_name(name)
    if not key:
        return None
    for r in conn.execute("SELECT * FROM pe_firms"):
        if norm_name(r["name"]) == key:
            return r
    return None


def ensure_firm(conn: sqlite3.Connection, name: str, origin: str = "derived", actor: str = "sync",
                domain: str | None = None, hq_city: str | None = None, portfolio_url: str | None = None) -> tuple[int, bool]:
    """Return (firm id, created). Idempotent on the normalised name. The caller owns the transaction."""
    name = " ".join((name or "").split())
    row = find_firm(conn, name)
    if row:
        return row["id"], False
    dom = normalize_domain(domain) if domain else None
    dom = dom if dom and is_valid_domain(dom) else None
    cur = conn.execute("INSERT INTO pe_firms(name, domain, hq_city, portfolio_url, origin, created) VALUES (?,?,?,?,?,?)",
                       (name, dom, hq_city, portfolio_url, origin, _now()))
    log_audit(conn, actor, "create", None, "pe_firm", None, f"{name} #{cur.lastrowid}")
    return cur.lastrowid, True


def add_firm(conn: sqlite3.Connection, name: str, domain: str = "", hq_city: str = "", portfolio_url: str = "",
             actor: str = "ui") -> list[str]:
    name = " ".join((name or "").split())
    errs: list[str] = []
    if not name or len(name) > 120:
        errs.append("name is required (120 characters at most)")
    dom = normalize_domain(domain) if (domain or "").strip() else None
    if dom is not None and not is_valid_domain(dom):
        errs.append("domain is not a valid host name")
    url = (portfolio_url or "").strip()
    if url and not re.match(r"^https?://[^\s]+$", url):
        errs.append("portfolio URL must start with http:// or https://")
    if errs:
        return errs
    if find_firm(conn, name):
        return ["that firm already exists"]
    with conn:
        ensure_firm(conn, name, "manual", actor, dom, (hq_city or "").strip() or None, url or None)
    return []


def update_firm(conn: sqlite3.Connection, firm_id: int, domain: str = "", hq_city: str = "", hq_state: str = "", portfolio_url: str = "",
                actor: str = "ui") -> tuple[list[str], list[str]]:
    """Set the details of a firm that already exists (one derived from an account's sponsor has no domain or portfolio page). A blank value means
    "not known": it clears the field, it is never guessed. Returns (errors, notes). The firm's name is not editable here: a name is what the
    relationships and their evidence were recorded under.

    Changing the portfolio page registers the new page; the old one is dropped only when it was never read, because a page that has been read has
    evidence and history behind it, so it stays registered and the notes say so."""
    row = conn.execute("SELECT * FROM pe_firms WHERE id = ?", (firm_id,)).fetchone()
    if row is None:
        return ["no such firm"], []
    errs: list[str] = []
    dom = normalize_domain(domain) if (domain or "").strip() else None
    if dom is not None and not is_valid_domain(dom):
        errs.append("domain is not a valid host name")
    url = (portfolio_url or "").strip()
    if url and not re.match(r"^https?://[^\s]+$", url):
        errs.append("portfolio URL must start with http:// or https://")
    city, state = " ".join((hq_city or "").split()), " ".join((hq_state or "").split())
    if len(city) > 80 or len(state) > 40:
        errs.append("city or state is too long")
    if errs:
        return errs, []
    notes: list[str] = []
    old_url = (row["portfolio_url"] or "").strip()
    changed = [f"{k}: {a or 'none'} -> {b or 'none'}" for k, a, b in
               (("domain", row["domain"], dom), ("hq_city", row["hq_city"], city or None), ("hq_state", row["hq_state"], state or None),
                ("portfolio_url", old_url or None, url or None)) if (a or None) != (b or None)]
    if not changed:
        return [], ["Nothing changed."]
    with conn:
        conn.execute("UPDATE pe_firms SET domain = ?, hq_city = ?, hq_state = ?, portfolio_url = ? WHERE id = ?", (dom, city or None, state or None, url or None, firm_id))
        if url != old_url:
            if url:
                conn.execute("INSERT OR IGNORE INTO pe_sources(firm_id, kind, url, origin) VALUES (?,?,?, 'firm_config')", (firm_id, "portfolio_page", url))
            if old_url:
                src = conn.execute("SELECT id, status FROM pe_sources WHERE firm_id = ? AND kind = 'portfolio_page' AND url = ?", (firm_id, old_url)).fetchone()
                if src is not None:
                    read = conn.execute("SELECT (SELECT COUNT(*) FROM pe_snapshots WHERE source_id = ?) + (SELECT COUNT(*) FROM pe_listing_entries WHERE source_id = ?)",
                                        (src["id"], src["id"])).fetchone()[0]
                    if src["status"] == "never" and not read:
                        conn.execute("DELETE FROM pe_sources WHERE id = ?", (src["id"],))
                    else:
                        notes.append(f"The earlier page {old_url} was read before, so it stays registered with its history and will still be read.")
        log_audit(conn, actor, "update", None, "pe_firm", f"#{firm_id} {row['name']}", "; ".join(changed))
    notes.insert(0, "Saved." + (" Run \"Read PE portfolio pages\" from Operate to read it." if url or dom else ""))
    return [], notes


def set_firm_status(conn: sqlite3.Connection, firm_id: int, status: str, actor: str = "ui") -> bool:
    if status not in ("active", "excluded"):
        return False
    row = conn.execute("SELECT status FROM pe_firms WHERE id = ?", (firm_id,)).fetchone()
    if row is None:
        return False
    with conn:
        conn.execute("UPDATE pe_firms SET status = ? WHERE id = ?", (status, firm_id))
        log_audit(conn, actor, "update", None, "pe_firm_status", f"#{firm_id} {row['status']}", status)
    return True


@dataclass
class SyncResult:
    firms_added: int = 0
    links_added: int = 0
    links_exited: int = 0


def split_sponsor(sponsor: str, splits: dict[str, list[str]]) -> list[str]:
    """The firms a sponsor string stands for: the listed firms when the string is in `pe_portfolio.sponsor_splits`, else the string itself."""
    key = norm_name(sponsor)
    for k, names in splits.items():
        if norm_name(k) == key and any(n.strip() for n in names):
            return [" ".join(n.split()) for n in names if n.strip()]
    return [" ".join((sponsor or "").split())]


def retire_split_firm(conn: sqlite3.Connection, combined: str, actor: str = "sync") -> str | None:
    """After a combined sponsor name has been split, take the combined firm out of the way. Its relationships are removed only when every one of them was
    only ever mirrored from an account's sponsor field (nothing was read from a page); the firm is then deleted if nothing else refers to it, else
    excluded. A firm with anything read or researched behind it is only excluded. Returns what was done, or None when there is no such firm."""
    f = find_firm(conn, combined)
    if f is None:
        return None
    fid = f["id"]
    owns = conn.execute("SELECT id, basis FROM pe_ownership WHERE firm_id = ?", (fid,)).fetchall()
    verified = conn.execute("SELECT COUNT(*) FROM pe_evidence e JOIN pe_ownership o ON o.id = e.subject_id WHERE e.subject_type = 'ownership' AND o.firm_id = ? "
                            "AND e.verification = 'page_verified'", (fid,)).fetchone()[0]
    legacy_only = all(o["basis"] == "legacy_unverified" for o in owns) and not verified
    with conn:
        if not legacy_only:
            conn.execute("UPDATE pe_firms SET status = 'excluded' WHERE id = ?", (fid,))
            log_audit(conn, actor, "update", None, "pe_firm", f"#{fid} {f['name']}", "excluded: split into the firms in pe_portfolio.sponsor_splits (it has evidence behind it, so it is kept)")
            return "excluded"
        ids = [o["id"] for o in owns]
        if ids:
            marks = ",".join("?" * len(ids))
            conn.execute(f"DELETE FROM pe_evidence WHERE subject_type = 'ownership' AND subject_id IN ({marks})", ids)
            conn.execute(f"DELETE FROM pe_ownership_log WHERE ownership_id IN ({marks})", ids)
        conn.execute("DELETE FROM pe_ownership WHERE firm_id = ?", (fid,))
        conn.execute("DELETE FROM portfolio_links WHERE firm_id = ? AND origin = 'account'", (fid,))
        refs = sum(conn.execute(q, (fid,)).fetchone()[0] for q in (
            "SELECT COUNT(*) FROM portfolio_links WHERE firm_id = ?", "SELECT COUNT(*) FROM firm_signals WHERE firm_id = ?",
            "SELECT COUNT(*) FROM pe_funds WHERE firm_id = ?", "SELECT COUNT(*) FROM pe_sources WHERE firm_id = ?"))
        if refs:
            conn.execute("UPDATE pe_firms SET status = 'excluded' WHERE id = ?", (fid,))
            log_audit(conn, actor, "update", None, "pe_firm", f"#{fid} {f['name']}", "excluded: split into the firms in pe_portfolio.sponsor_splits")
            return "excluded"
        conn.execute("DELETE FROM pe_firms WHERE id = ?", (fid,))
        log_audit(conn, actor, "delete", None, "pe_firm", f"#{fid} {f['name']}", "removed: split into the firms in pe_portfolio.sponsor_splits (it held only unverified links from an account's sponsor field)")
        return "removed"


def sync_from_accounts(conn: sqlite3.Connection, actor: str = "sync", today: date | None = None, splits: dict[str, list[str]] | None = None) -> SyncResult:
    """Make the firm table and the `account` links match the accounts: every pe-backed account with a sponsor gets a
    firm and a current link; a link whose account no longer names that firm as sponsor is marked exited. A sponsor string listed in
    `pe_portfolio.sponsor_splits` stands for each of the firms listed there (a company with two sponsors has two relationships)."""
    if splits is None:
        from bd.config import load_config
        splits = load_config().pe_portfolio.sponsor_splits
    out = SyncResult()
    day = (today or datetime.now(timezone.utc).date()).isoformat()
    links_before = conn.execute("SELECT COUNT(*) FROM portfolio_links").fetchone()[0]
    accts = conn.execute("SELECT id, name, domain, sponsor FROM accounts WHERE ownership = 'pe-backed' "
                         "AND sponsor IS NOT NULL AND trim(sponsor) != '' AND status != 'excluded'").fetchall()
    with conn:
        wanted: set[tuple[int, int]] = set()
        for a in accts:
            for firm_name in split_sponsor(a["sponsor"], splits):
                fid, created = ensure_firm(conn, firm_name, "derived", actor)
                out.firms_added += created
                wanted.add((fid, a["id"]))
                conn.execute(
                    "INSERT INTO portfolio_links(firm_id, account_id, company_name, company_domain, status, origin, found_date) "
                    "VALUES (?,?,?,?,'current','account',?) "
                    "ON CONFLICT(firm_id, lower(company_name)) DO UPDATE SET account_id = excluded.account_id, "
                    "status = CASE WHEN portfolio_links.status = 'exited' AND portfolio_links.origin = 'account' "
                    "THEN 'current' ELSE portfolio_links.status END",
                    (fid, a["id"], a["name"], a["domain"], day))
        for l in conn.execute("SELECT id, firm_id, account_id FROM portfolio_links WHERE origin = 'account' AND status = 'current'").fetchall():
            if (l["firm_id"], l["account_id"]) not in wanted:
                conn.execute("UPDATE portfolio_links SET status = 'exited' WHERE id = ?", (l["id"],))
                log_audit(conn, actor, "update", l["account_id"], "portfolio_link", "current", "exited")
                out.links_exited += 1
    for combined, names in splits.items():
        if len([n for n in names if n.strip()]) > 1 and norm_name(combined) not in {norm_name(n) for n in names}:
            retire_split_firm(conn, combined, actor)
    out.links_added = conn.execute("SELECT COUNT(*) FROM portfolio_links").fetchone()[0] - links_before
    from bd.pe_ownership import mirror_legacy          # the ledger lists these too, as unverified (imported here: bd.pe_ownership imports this module)
    with conn:
        mirror_legacy(conn, datetime.now(timezone.utc).date() if today is None else today)
    return out


def link_company(conn: sqlite3.Connection, firm_id: int, company_name: str, domain: str | None, source_url: str | None,
                 day: date, account_id: int | None = None, status: str = "current", origin: str = "portfolio") -> bool:
    """Record that a firm's portfolio includes a company. True if a new link was created. Caller owns the transaction."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO portfolio_links(firm_id, account_id, company_name, company_domain, status, origin, source_url, found_date) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (firm_id, account_id, " ".join(company_name.split()), domain, status, origin, source_url, day.isoformat()))
    if not cur.rowcount and account_id is not None:
        conn.execute("UPDATE portfolio_links SET account_id = ? WHERE firm_id = ? AND lower(company_name) = lower(?) "
                     "AND account_id IS NULL", (account_id, firm_id, " ".join(company_name.split())))
    return bool(cur.rowcount)


def attach_account_to_links(conn: sqlite3.Connection, account_id: int, company_name: str) -> int:
    """After a queued candidate becomes an account, point its portfolio links at it."""
    cur = conn.execute("UPDATE portfolio_links SET account_id = ? WHERE account_id IS NULL AND lower(company_name) = lower(?)",
                       (account_id, " ".join(company_name.split())))
    return cur.rowcount


def store_firm_signals(conn: sqlite3.Connection, firm_id: int, signals: list, detected: date) -> tuple[int, int]:
    """Insert accepted firm signals (objects with .extract and .hash). Returns (inserted, already_known)."""
    inserted = known = 0
    with conn:
        for s in signals:
            e = s.extract
            cur = conn.execute(
                "INSERT OR IGNORE INTO firm_signals(firm_id,type,event_date,detected_date,summary,source_url,source_type,confidence,hash) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (firm_id, e.type, e.event_date.isoformat(), detected.isoformat(), e.summary, e.source_url, e.source_type,
                 e.confidence, s.hash))
            inserted += cur.rowcount
            known += 0 if cur.rowcount else 1
    return inserted, known


def firm_signals_for_account(conn: sqlite3.Connection, account_id: int) -> list[tuple[ScoringSignal, str]]:
    """Signals of the active firms that currently own this account, one per (firm, type, event date)."""
    rows = conn.execute(
        "SELECT fs.*, f.name AS firm_name, f.id AS fid FROM firm_signals fs "
        "JOIN pe_firms f ON f.id = fs.firm_id AND f.status = 'active' "
        "JOIN portfolio_links l ON l.firm_id = f.id AND l.account_id = ? AND l.status = 'current' "
        "ORDER BY fs.id", (account_id,)).fetchall()
    best: dict[tuple[int, str, str], sqlite3.Row] = {}
    for r in rows:
        k = (r["fid"], r["type"], r["event_date"])
        if k not in best or _RANK[r["confidence"]] > _RANK[best[k]["confidence"]]:
            best[k] = r
    return [(ScoringSignal(type=r["type"], event_date=date.fromisoformat(r["event_date"]), confidence=r["confidence"], ref=r["id"]),
             r["firm_name"]) for r in best.values()]
