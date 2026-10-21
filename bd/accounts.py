"""Accounts: model, queries, and CSV seeding."""

from __future__ import annotations

import csv
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel

SEED_COLUMNS = ["name", "domain", "hq_city", "chicagoland", "revenue", "employees",
                "ownership", "sponsor", "industry", "tier", "cik"]
# Review-only columns written by `bd enrich`; accepted (and ignored) when seeding a proposals file.
PROPOSAL_ONLY_COLUMNS = ["evidence_urls", "notes", "sec_name"]
_OWNERSHIP = {"public", "private", "pe-backed"}


class Account(BaseModel):
    id: int
    name: str
    domain: str | None = None
    hq_city: str | None = None
    chicagoland: str = "unknown"
    revenue: float | None = None
    employees: int | None = None
    ownership: str | None = None
    sponsor: str | None = None
    industry: str | None = None
    cik: str | None = None
    tier: str = "A"
    status: str = "active"
    last_scanned: str | None = None
    origin: str = "seed"
    source_url: str | None = None
    reviewed: int = 1
    discovered_at: str | None = None
    ats_provider: str | None = None
    ats_board: str | None = None


def list_accounts(conn: sqlite3.Connection, tier: str = "all", status: str | None = "active",
                  limit: int | None = None) -> list[Account]:
    """status=None returns accounts in every status."""
    where, args = [], []
    if status is not None:
        where.append("status = ?")
        args.append(status)
    if tier != "all":
        where.append("tier = ?")
        args.append(tier)
    sql = "SELECT * FROM accounts" + (" WHERE " + " AND ".join(where) if where else "")
    sql += " ORDER BY id"
    if limit:
        sql += " LIMIT ?"
        args.append(limit)
    return [Account(**{k: r[k] for k in Account.model_fields}) for r in conn.execute(sql, args)]


_HOST = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]$")


def is_valid_domain(host: str) -> bool:
    """A plausible public host name (letters, digits, hyphens, at least one dot). Not a URL, IP or free text."""
    return bool(_HOST.match(host)) and not host.replace(".", "").isdigit()


def normalize_domain(raw: str) -> str:
    raw = raw.strip().lower()
    host = urlparse(raw if "//" in raw else "//" + raw).hostname or ""
    return host.removeprefix("www.")


FACT_COLUMNS = ["domain", "hq_city", "chicagoland", "revenue", "employees", "ownership", "sponsor", "industry",
                "tier", "cik"]


def log_audit(conn: sqlite3.Connection, actor: str, action: str, account_id: int | None, field: str | None = None,
              old: object = None, new: object = None) -> None:
    """Append to the audit log (the caller owns the transaction)."""
    from datetime import datetime, timezone

    from bd.actor import resolve
    actor = resolve(actor)                       # a signed-in person's name when the caller only said "ui"
    conn.execute("INSERT INTO audit_log(ts, actor, action, account_id, field, old, new) VALUES (?,?,?,?,?,?,?)",
                 (datetime.now(timezone.utc).isoformat(timespec="seconds"), actor, action, account_id, field,
                  None if old is None else str(old), None if new is None else str(new)))


def parse_fields(row: dict[str, str], label: str = "", clear_blank: bool = False) -> tuple[dict[str, object], list[str]]:
    """Validate and normalise fact columns present in `row`.

    clear_blank=False (CSV seeding): a blank cell means "no information" and is omitted.
    clear_blank=True  (editing a form): a blank cell means "clear this fact" (NULL, or 'unknown' for
    chicagoland). A blank tier is omitted either way (an account always has a tier)."""
    errs: list[str] = []
    pre = f"{label}: " if label else ""
    g = lambda k: (row.get(k) or "").strip()  # noqa: E731
    out: dict[str, object] = {}

    def blank(col: str, cleared: object = None) -> None:
        if clear_blank and col in row:
            out[col] = cleared

    if g("domain"):
        dom = normalize_domain(g("domain"))
        if is_valid_domain(dom):
            out["domain"] = dom
        else:
            errs.append(f"{pre}domain {g('domain')!r} is not a valid host name")
    else:
        blank("domain")
    for k in ("hq_city", "sponsor", "industry"):
        if g(k):
            out[k] = g(k)
        else:
            blank(k)
    if g("chicagoland"):
        if g("chicagoland").lower() not in ("yes", "no", "unknown"):
            errs.append(f"{pre}chicagoland must be yes/no/unknown")
        out["chicagoland"] = g("chicagoland").lower()
    else:
        blank("chicagoland", "unknown")
    if g("ownership"):
        if g("ownership").lower() not in _OWNERSHIP:
            errs.append(f"{pre}ownership must be one of {sorted(_OWNERSHIP)}")
        out["ownership"] = g("ownership").lower()
    else:
        blank("ownership")
    if g("cik"):
        if not g("cik").isdigit() or len(g("cik")) > 10 or int(g("cik")) == 0:
            errs.append(f"{pre}cik must be 1-10 digits")
        else:
            out["cik"] = g("cik").zfill(10)
    else:
        blank("cik")
    if g("tier"):
        if g("tier").upper() not in ("A", "B"):
            errs.append(f"{pre}tier must be A or B")
        out["tier"] = g("tier").upper()
    for col, cast in (("revenue", lambda s: float(s.replace(",", "").replace("$", ""))),
                      ("employees", lambda s: int(s.replace(",", "")))):
        if g(col):
            try:
                v = cast(g(col))
                if v < 0:
                    raise ValueError
                out[col] = v
            except ValueError:
                errs.append(f"{pre}{col} must be a plain non-negative number")
        else:
            blank(col)
    return out, errs


def _parse_row(n: int, row: dict[str, str]) -> tuple[dict[str, object], list[str]]:
    errs: list[str] = []
    out: dict[str, object] = {}
    name = (row.get("name") or "").strip()
    if not name:
        errs.append(f"row {n}: name is required")
    out["name"] = name
    facts, e = parse_fields(row, label=f"row {n}")
    return {**out, **facts}, errs + e


@dataclass
class SeedResult:
    inserted: int = 0
    updated: int = 0                 # existing accounts matched by name
    filled: int = 0                  # blank facts on existing accounts that the CSV filled in
    overwritten: int = 0             # only with overwrite=True
    conflicts: list[str] = field(default_factory=list)   # CSV differs from the database; database kept
    errors: list[str] = field(default_factory=list)


def _is_unset(col: str, current: object) -> bool:
    return current is None or (col == "chicagoland" and current == "unknown")


def seed_csv(conn: sqlite3.Connection, path: str | Path, overwrite: bool = False) -> SeedResult:
    """Load accounts from CSV. The DATABASE is the master copy, so for an account that already exists a CSV
    value only fills a fact that is still unknown; where the CSV and the database disagree the database wins
    and the difference is reported (pass overwrite=True to let the CSV win). Blank cells are never applied.
    All-or-nothing: any bad row aborts."""
    res = SeedResult()
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        cols = [c.strip() for c in (reader.fieldnames or [])]
        if "name" not in cols:
            res.errors.append("header must include a 'name' column")
            return res
        unknown = [c for c in cols if c not in SEED_COLUMNS and c not in PROPOSAL_ONLY_COLUMNS]
        if unknown:
            res.errors.append(f"unknown columns: {unknown}")
            return res
        rows = []
        for n, row in enumerate(reader, start=2):
            row = {(k or "").strip(): v for k, v in row.items()}
            data, errs = _parse_row(n, row)
            res.errors += errs
            rows.append(data)
    if res.errors:
        return res
    actor = "seed"
    with conn:
        for data in rows:
            existing = conn.execute("SELECT * FROM accounts WHERE lower(name) = lower(?)", (data["name"],)).fetchone()
            if existing is None:
                cols_ = list(data)
                cur = conn.execute(f"INSERT INTO accounts({', '.join(cols_)}) VALUES ({', '.join('?' * len(cols_))})",
                                   list(data.values()))
                log_audit(conn, actor, "create", cur.lastrowid, "name", None, data["name"])
                res.inserted += 1
                continue
            res.updated += 1
            for col, new in data.items():
                if col == "name":
                    continue
                cur_val = existing[col]
                if cur_val == new:
                    continue
                if _is_unset(col, cur_val) or overwrite:
                    conn.execute(f"UPDATE accounts SET {col} = ? WHERE id = ?", (new, existing["id"]))
                    log_audit(conn, actor, "update", existing["id"], col, cur_val, new)
                    if _is_unset(col, cur_val):
                        res.filled += 1
                    else:
                        res.overwritten += 1
                else:
                    res.conflicts.append(f"{existing['name']}: {col} is {cur_val!r} in the database, CSV says {new!r}")
    return res


def export_accounts_csv(conn: sqlite3.Connection, path: str | Path) -> int:
    """Write every account (all statuses) to a CSV that `bd seed` can read back. Unknown facts are blank."""
    rows = conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(SEED_COLUMNS)
        for r in rows:
            out = []
            for c in SEED_COLUMNS:
                v = r[c]
                if c == "chicagoland" and v == "unknown":
                    v = ""
                elif isinstance(v, float) and v == int(v):
                    v = int(v)
                out.append("" if v is None else v)
            w.writerow(out)
    return len(rows)


def export_exclusions_csv(conn: sqlite3.Connection, path: str | Path) -> int:
    rows = conn.execute("SELECT name, domain, reason FROM exclusions ORDER BY id").fetchall()
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["name", "domain", "reason"])
        for r in rows:
            w.writerow([r["name"], r["domain"] or "", r["reason"]])
    return len(rows)


# Which companies the SEC could tell us something about (used by bd.sec_fill, and by the UI to explain a thin list without importing it).
def wants_lookup(a: Account) -> bool:
    """No CIK on file and not recorded as private or PE-backed (a company the SEC would not list)."""
    return not a.cik and a.ownership in (None, "public")


def wants_read(a: Account) -> bool:
    return bool(a.cik) and (a.revenue is None or not a.ownership or not a.hq_city)


def facts_missing(a: Account) -> bool:
    """Location or size not on file. Such a company is still scanned for signals; this only orders the work that fills the gap in."""
    return a.chicagoland not in ("yes", "no") or (a.revenue is None and a.employees is None)


def can_check_free(a: Account) -> bool:
    """Can the free check (job boards, SEC event filings) read anything for this company? Every company that is not known out of scope."""
    return a.status != "excluded"
