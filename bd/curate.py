"""Human curation of the account list: edit facts, add accounts, manage exclusions, apply enrich proposals.

The database is the master copy, so every change here goes through one writer that validates the input,
records an audit-log row per changed field, and re-runs the fit gate so the account's status always
matches its facts. Nothing in this module calls an LLM, scans, scores or posts."""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from bd.accounts import (FACT_COLUMNS, PROPOSAL_ONLY_COLUMNS, SEED_COLUMNS, Account, _is_unset, is_valid_domain,
                         list_accounts, log_audit, normalize_domain, parse_fields)
from bd.config import Config
from bd.fit_gate import FitResult, apply_fit_gate, check_fit, load_exclusions

EXCLUSION_REASONS = ("client", "do_not_pursue", "cannabis", "conflict")
# enrich writes the evidence for these columns under a different key
_EVIDENCE_KEY = {"chicagoland": "il_county", "revenue": "revenue_usd"}


@dataclass
class WriteResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    changed: list[tuple[str, object, object]] = field(default_factory=list)   # (column, old, new)
    fit: FitResult | None = None
    account_id: int | None = None


def _account(conn: sqlite3.Connection, account_id: int) -> Account | None:
    return next(iter([a for a in list_accounts(conn, status=None) if a.id == account_id]), None)


def gate_one(conn: sqlite3.Connection, cfg: Config, account_id: int, actor: str = "ui") -> FitResult:
    """Re-run the fit gate for one account and store the resulting status."""
    acc = _account(conn, account_id)
    if acc is None:
        raise KeyError(account_id)
    res = check_fit(acc, load_exclusions(conn), cfg)
    if res.status != acc.status:
        with conn:
            conn.execute("UPDATE accounts SET status = ? WHERE id = ?", (res.status, account_id))
            log_audit(conn, actor, "status", account_id, "status", acc.status, res.status)
    return res


def _friendly(e: sqlite3.IntegrityError) -> str:
    m = str(e)
    if "accounts.domain" in m or "ux_accounts_domain" in m:
        return "another account already uses that domain"
    if "ux_accounts_name" in m or "accounts.name" in m:
        return "an account with that name already exists"
    return f"could not save ({m})"


def write_facts(conn: sqlite3.Connection, cfg: Config, account_id: int, data: dict[str, object], actor: str) -> WriteResult:
    """Apply already-validated fact values; audit what changed; re-run the gate."""
    cur = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if cur is None:
        return WriteResult(False, ["no such account"])
    changed = [(c, cur[c], v) for c, v in data.items() if c in FACT_COLUMNS and cur[c] != v]
    try:
        with conn:
            for c, old, new in changed:
                conn.execute(f"UPDATE accounts SET {c} = ? WHERE id = ?", (new, account_id))
                log_audit(conn, actor, "update", account_id, c, old, new)
    except sqlite3.IntegrityError as e:
        return WriteResult(False, [_friendly(e)], account_id=account_id)
    return WriteResult(True, [], changed, gate_one(conn, cfg, account_id, actor), account_id)


def update_account(conn: sqlite3.Connection, cfg: Config, account_id: int, form: dict[str, str], actor: str = "ui") -> WriteResult:
    """Edit form semantics: a blank field clears that fact (it becomes unknown)."""
    data, errs = parse_fields(form, clear_blank=True)
    if errs:
        return WriteResult(False, errs, account_id=account_id)
    return write_facts(conn, cfg, account_id, data, actor)


_EXTRA_COLUMNS = {"origin", "source_url", "reviewed", "discovered_at"}


def create_account(conn: sqlite3.Connection, cfg: Config, name: str, form: dict[str, str], actor: str = "ui",
                   extra: dict[str, object] | None = None) -> WriteResult:
    """`extra` carries provenance columns (origin, source_url, reviewed, discovered_at); facts come from `form`."""
    name = " ".join((name or "").split())
    if not name:
        return WriteResult(False, ["name is required"])
    if conn.execute("SELECT 1 FROM accounts WHERE lower(name) = lower(?)", (name,)).fetchone():
        return WriteResult(False, ["an account with that name already exists"])
    data, errs = parse_fields(form, clear_blank=False)
    if errs:
        return WriteResult(False, errs)
    try:
        with conn:
            row = {**data, **{k: v for k, v in (extra or {}).items() if k in _EXTRA_COLUMNS}}
            cols = ["name", *row]
            cur = conn.execute(f"INSERT INTO accounts({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", [name, *row.values()])
            log_audit(conn, actor, "create", cur.lastrowid, "name", None, name)
    except sqlite3.IntegrityError as e:
        return WriteResult(False, [_friendly(e)])
    return WriteResult(True, [], [(c, None, v) for c, v in data.items()], gate_one(conn, cfg, cur.lastrowid, actor), cur.lastrowid)


# ---- exclusions ---------------------------------------------------------------------------------------------

def add_exclusion(conn: sqlite3.Connection, cfg: Config, name: str, domain: str, reason: str, actor: str = "ui") -> list[str]:
    name, errs = " ".join((name or "").split()), []
    if not name:
        errs.append("name is required")
    if reason not in EXCLUSION_REASONS:
        errs.append(f"reason must be one of {list(EXCLUSION_REASONS)}")
    dom = normalize_domain(domain) if (domain or "").strip() else None
    if dom is not None and not is_valid_domain(dom):
        dom = None
        errs.append("domain is not a valid host name")
    if errs:
        return errs
    if conn.execute("SELECT 1 FROM exclusions WHERE lower(name) = lower(?) AND reason = ?", (name, reason)).fetchone():
        return ["that exclusion already exists"]
    with conn:
        cur = conn.execute("INSERT INTO exclusions(name, domain, reason) VALUES (?,?,?)", (name, dom, reason))
        log_audit(conn, actor, "exclusion_add", None, reason, None, f"{name} ({dom or 'no domain'}) #{cur.lastrowid}")
    apply_fit_gate(conn, cfg)          # an excluded account must stop being scanned immediately
    return []


def remove_exclusion(conn: sqlite3.Connection, cfg: Config, exclusion_id: int, actor: str = "ui") -> bool:
    row = conn.execute("SELECT * FROM exclusions WHERE id = ?", (exclusion_id,)).fetchone()
    if row is None:
        return False
    with conn:
        conn.execute("DELETE FROM exclusions WHERE id = ?", (exclusion_id,))
        log_audit(conn, actor, "exclusion_remove", None, row["reason"], f"{row['name']} ({row['domain'] or 'no domain'})", None)
    apply_fit_gate(conn, cfg)
    return True


# ---- bd enrich proposals ----------------------------------------------------------------------------------------

@dataclass
class FieldDiff:
    column: str
    current: object
    proposed: object
    status: str                    # new (fills an unknown) | same | conflict (differs from a known value)
    evidence_url: str | None = None


@dataclass
class Proposal:
    name: str
    account_id: int | None         # None = not in the database
    diffs: list[FieldDiff] = field(default_factory=list)
    notes: str = ""
    sec_name: str = ""


def _evidence(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (raw or "").split(";"):
        key, sep, url = part.strip().partition("=")
        if sep and url.startswith(("http://", "https://")):      # never render other schemes (e.g. javascript:) as links
            out[key.strip()] = url.strip()
    return out


def load_proposals(conn: sqlite3.Connection, path: str | Path) -> tuple[list[Proposal], list[str]]:
    """Parse a `bd enrich` CSV and compare each proposed fact with the database."""
    errors: list[str] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        cols = [c.strip() for c in (reader.fieldnames or [])]
        if "name" not in cols:
            return [], ["header must include a 'name' column"]
        bad = [c for c in cols if c not in SEED_COLUMNS and c not in PROPOSAL_ONLY_COLUMNS]
        if bad:
            return [], [f"unknown columns: {bad}"]
        rows = [{(k or "").strip(): v for k, v in r.items()} for r in reader]
    out: list[Proposal] = []
    for n, row in enumerate(rows, start=2):
        name = (row.get("name") or "").strip()
        facts, errs = parse_fields(row, label=f"row {n} ({name})")
        errors += errs
        acc = conn.execute("SELECT * FROM accounts WHERE lower(name) = lower(?)", (name,)).fetchone()
        ev = _evidence(row.get("evidence_urls") or "")
        prop = Proposal(name=name, account_id=acc["id"] if acc else None, notes=(row.get("notes") or "").strip(),
                        sec_name=(row.get("sec_name") or "").strip())
        if acc:
            for col, new in facts.items():
                cur = acc[col]
                status = "same" if cur == new else "new" if _is_unset(col, cur) else "conflict"
                prop.diffs.append(FieldDiff(col, cur, new, status, ev.get(_EVIDENCE_KEY.get(col, col))))
        out.append(prop)
    return out, errors


def apply_selected(conn: sqlite3.Connection, cfg: Config, account_id: int, selections: dict[str, str], actor: str = "enrich") -> WriteResult:
    """Apply only the facts the reviewer ticked. Values are re-validated here, never trusted from the page."""
    allowed = {c: v for c, v in selections.items() if c in FACT_COLUMNS}
    data, errs = parse_fields(allowed, clear_blank=False)
    if errs:
        return WriteResult(False, errs, account_id=account_id)
    return write_facts(conn, cfg, account_id, data, actor)


# ---- discovery review queue ----------------------------------------------------------------------------------------

def mark_reviewed(conn: sqlite3.Connection, account_id: int | None = None, actor: str = "ui") -> int:
    """A person has looked at auto-added account(s): they now scan like any other. None = all unreviewed."""
    where, args = ("AND id = ?", [account_id]) if account_id is not None else ("", [])
    rows = conn.execute(f"SELECT id FROM accounts WHERE reviewed = 0 {where}", args).fetchall()
    with conn:
        for r in rows:
            conn.execute("UPDATE accounts SET reviewed = 1 WHERE id = ?", (r["id"],))
            log_audit(conn, actor, "update", r["id"], "reviewed", 0, 1)
    return len(rows)


def accept_candidate(conn: sqlite3.Connection, cfg: Config, candidate_id: int, actor: str = "ui") -> WriteResult:
    """Create an account from a queued candidate's researched facts. The person accepting is the review, so it is
    marked reviewed; the fit gate still decides whether it is scanned."""
    import json
    from datetime import datetime, timezone

    c = conn.execute("SELECT * FROM candidates WHERE id = ? AND status = 'queued'", (candidate_id,)).fetchone()
    if c is None:
        return WriteResult(False, ["no such queued candidate"])
    facts = {k: v for k, v in json.loads(c["facts_json"]).items() if k in SEED_COLUMNS and k != "name"}
    facts.setdefault("domain", c["domain"] or "")
    facts["tier"] = "B"
    res = create_account(conn, cfg, c["name"], facts, actor,
                         extra={"origin": "discovered", "source_url": c["source_url"], "reviewed": 1,
                                "discovered_at": datetime.now(timezone.utc).date().isoformat()})
    if res.ok:
        with conn:
            conn.execute("UPDATE candidates SET status = 'added', account_id = ? WHERE id = ?", (res.account_id, candidate_id))
            conn.execute("UPDATE portfolio_links SET account_id = ? WHERE account_id IS NULL AND lower(company_name) = lower(?)",
                         (res.account_id, c["name"]))
    return res


def reject_candidate(conn: sqlite3.Connection, candidate_id: int, actor: str = "ui") -> bool:
    with conn:
        cur = conn.execute("UPDATE candidates SET status = 'rejected', reason = COALESCE(reason, '') || ' | rejected by a person' "
                           "WHERE id = ? AND status = 'queued'", (candidate_id,))
        if cur.rowcount:
            log_audit(conn, actor, "update", None, "candidate_rejected", None, candidate_id)
    return bool(cur.rowcount)
