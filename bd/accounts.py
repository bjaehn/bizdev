"""Accounts: model, queries, and CSV seeding."""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel

SEED_COLUMNS = ["name", "domain", "hq_city", "chicagoland", "revenue", "employees",
                "ownership", "sponsor", "industry", "tier"]
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
    tier: str = "A"
    status: str = "active"
    last_scanned: str | None = None


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


def normalize_domain(raw: str) -> str:
    raw = raw.strip().lower()
    host = urlparse(raw if "//" in raw else "//" + raw).hostname or ""
    return host.removeprefix("www.")


@dataclass
class SeedResult:
    inserted: int = 0
    updated: int = 0
    errors: list[str] = field(default_factory=list)


def _parse_row(n: int, row: dict[str, str]) -> tuple[dict[str, object], list[str]]:
    errs: list[str] = []
    g = lambda k: (row.get(k) or "").strip()  # noqa: E731
    out: dict[str, object] = {}
    if not g("name"):
        errs.append(f"row {n}: name is required")
    out["name"] = g("name")
    if g("domain"):
        out["domain"] = normalize_domain(g("domain"))
    for k in ("hq_city", "sponsor", "industry"):
        if g(k):
            out[k] = g(k)
    if g("chicagoland"):
        if g("chicagoland").lower() not in ("yes", "no", "unknown"):
            errs.append(f"row {n}: chicagoland must be yes/no/unknown")
        out["chicagoland"] = g("chicagoland").lower()
    if g("ownership"):
        if g("ownership").lower() not in _OWNERSHIP:
            errs.append(f"row {n}: ownership must be one of {sorted(_OWNERSHIP)}")
        out["ownership"] = g("ownership").lower()
    if g("tier"):
        if g("tier").upper() not in ("A", "B"):
            errs.append(f"row {n}: tier must be A or B")
        out["tier"] = g("tier").upper()
    try:
        if g("revenue"):
            out["revenue"] = float(g("revenue").replace(",", ""))
        if g("employees"):
            out["employees"] = int(g("employees").replace(",", ""))
    except ValueError:
        errs.append(f"row {n}: revenue/employees must be plain numbers")
    return out, errs


def seed_csv(conn: sqlite3.Connection, path: str | Path) -> SeedResult:
    """Upsert accounts by name (case-insensitive). Blank cells never overwrite existing values and
    are never guessed: unknown stays NULL / 'unknown'. All-or-nothing: any bad row aborts."""
    res = SeedResult()
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        cols = [c.strip() for c in (reader.fieldnames or [])]
        if "name" not in cols:
            res.errors.append("header must include a 'name' column")
            return res
        unknown = [c for c in cols if c not in SEED_COLUMNS]
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
    with conn:
        for data in rows:
            existing = conn.execute("SELECT id FROM accounts WHERE lower(name) = lower(?)", (data["name"],)).fetchone()
            if existing:
                sets = {k: v for k, v in data.items() if k != "name"}
                if sets:
                    conn.execute(f"UPDATE accounts SET {', '.join(k + ' = ?' for k in sets)} WHERE id = ?",
                                 [*sets.values(), existing["id"]])
                res.updated += 1
            else:
                cols_ = list(data)
                conn.execute(f"INSERT INTO accounts({', '.join(cols_)}) VALUES ({', '.join('?' * len(cols_))})",
                             list(data.values()))
                res.inserted += 1
    return res
