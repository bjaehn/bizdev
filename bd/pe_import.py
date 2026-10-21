"""Load the researched Chicago PE seed (firms, companies, relationships) into the ownership ledger as LEADS. Free: no model, no network.

The seed files are produced once, by a person, from the researched workbook (scripts/xlsx_to_pe_seed.py) and committed beside accounts.csv:
  pe_firms.csv           the Chicago-based firms (scope 'chicago') and the investors in companies on the Chicago list (scope 'investor')
  pe_companies.csv       the Chicago-area companies, with the workbook's location, industry and ownership wording
  pe_relationships.csv   one row per piece of workbook evidence for a (firm, company) pair

What this module refuses to do (each is a test in tests/test_pe_import.py):
  * Make anything current. A relationship the workbook names starts UNCLEAR with basis 'workbook_lead' and carries `snippet_only` evidence (source type
    'workbook'), which never supports a status. Only a page that code has fetched and read can change that (pe-collect on the firm's portfolio page, the
    free lead check, a verified deal). An announcement stays an announcement; the loader never writes 'completed' or 'exited'.
  * Write a place. The workbook's location is kept as unverified evidence only; a company's place in `pe_locations` comes from a retrieved page.
  * Overwrite what the app already knows: it fills blanks, adds evidence and adds rows. The database is the master copy (the seed_csv rule).
  * Half-load. Every file is validated first; one error and nothing is written.
  * Split a sponsor name. The seed has one row per firm; the reviewed split is done when the CSVs are made."""

from __future__ import annotations

import csv
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from bd.accounts import is_valid_domain, log_audit, normalize_domain
from bd.config import Config
from bd.fit_gate import is_competitor
from bd.pe_ownership import AmbiguousCompany, _account_taken, add_evidence, ensure_ownership, resolve_company
from bd.pe_store import ensure_firm, norm_name

SEED_FILES = ("pe_firms.csv", "pe_companies.csv", "pe_relationships.csv")
SEED_DIR = Path(__file__).resolve().parent.parent          # the seed files sit at the repository root, beside accounts.csv
ALLOWED_CLAIMS = ("listed", "listed_current", "listed_historical", "announced")      # never completed or exited: that needs a page code has read
REQUIRED = {
    "pe_firms.csv": ("seed_ref", "name", "scope", "domain", "local_city", "presence", "focus", "portfolio_url", "presence_source_url", "note"),
    "pe_companies.csv": ("seed_ref", "name", "aliases", "kind", "location", "industry", "ownership_role", "evidence_status", "confidence", "evidence_date",
                         "research_date", "note"),
    "pe_relationships.csv": ("seed_ref", "firm", "firm_note", "company", "company_seed_ref", "aliases", "claim", "workbook_status", "role", "source_url",
                             "evidence_date", "research_date", "text", "note"),
}
URL = re.compile(r"^https?://[^\s]+$")


class SeedError(Exception):
    """The seed files are missing, unreadable or inconsistent. Nothing was loaded."""


@dataclass
class ImportResult:
    firms: int = 0
    firms_added: int = 0
    companies: int = 0
    companies_added: int = 0
    accounts_linked: int = 0
    relationships: int = 0
    relationships_added: int = 0
    evidence_added: int = 0
    peers: list[str] = field(default_factory=list)
    peers_unlisted: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)

    def line(self) -> str:
        parts = [f"{self.firms} firm(s) ({self.firms_added} new)", f"{self.companies} compan{'y' if self.companies == 1 else 'ies'} ({self.companies_added} new, "
                 f"{self.accounts_linked} linked to an account)", f"{self.relationships} relationship(s) ({self.relationships_added} new, each a lead until a page "
                 f"confirms it)", f"{self.evidence_added} evidence row(s) added"]
        if self.peers:
            parts.append(f"{len(self.peers)} peer(s) of the firm, never prospects: {', '.join(self.peers)}")
        return "PE seed: " + "; ".join(parts) + "."


def _read(path: Path, required: tuple[str, ...], problems: list[str]) -> list[dict[str, str]]:
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            missing = [c for c in required if c not in (reader.fieldnames or [])]
            if missing:
                problems.append(f"{path.name}: missing column(s) {', '.join(missing)}")
                return []
            return [{k: (v or "").strip() for k, v in row.items()} for row in reader]
    except (OSError, UnicodeDecodeError, csv.Error) as e:
        problems.append(f"{path.name}: {e}")
        return []


def _iso(value: str, where: str, problems: list[str]) -> None:
    if value:
        try:
            date.fromisoformat(value)
        except ValueError:
            problems.append(f"{where}: {value!r} is not a date (YYYY-MM-DD)")


def load_seed(directory: Path) -> tuple[list[dict], list[dict], list[dict]]:
    """Read and validate all three files. Raises SeedError listing every problem."""
    problems: list[str] = []
    firms = _read(directory / "pe_firms.csv", REQUIRED["pe_firms.csv"], problems)
    companies = _read(directory / "pe_companies.csv", REQUIRED["pe_companies.csv"], problems)
    rels = _read(directory / "pe_relationships.csv", REQUIRED["pe_relationships.csv"], problems)
    if problems:
        raise SeedError("; ".join(problems))
    firm_keys: set[str] = set()
    for i, f in enumerate(firms, 2):
        where = f"pe_firms.csv line {i}"
        k = norm_name(f["name"])
        if not k:
            problems.append(f"{where}: a firm needs a name")
        elif k in firm_keys:
            problems.append(f"{where}: {f['name']!r} is listed twice")
        firm_keys.add(k)
        if f["scope"] not in ("chicago", "investor"):
            problems.append(f"{where}: scope must be chicago or investor, not {f['scope']!r}")
        if f["domain"] and not is_valid_domain(normalize_domain(f["domain"])):
            problems.append(f"{where}: {f['domain']!r} is not a valid domain")
        if f["portfolio_url"] and not URL.match(f["portfolio_url"]):
            problems.append(f"{where}: portfolio_url must start with http:// or https://")
    refs: set[str] = set()
    for i, c in enumerate(companies, 2):
        where = f"pe_companies.csv line {i}"
        if not norm_name(c["name"]) or not c["seed_ref"]:
            problems.append(f"{where}: a company needs a name and a seed_ref")
        if c["seed_ref"] in refs:
            problems.append(f"{where}: seed_ref {c['seed_ref']!r} is used twice")
        refs.add(c["seed_ref"])
        if c["kind"] not in ("company", "peer"):
            problems.append(f"{where}: kind must be company or peer")
        _iso(c["evidence_date"], where, problems)
        _iso(c["research_date"], where, problems)
    for i, r in enumerate(rels, 2):
        where = f"pe_relationships.csv line {i}"
        if norm_name(r["firm"]) not in firm_keys:
            problems.append(f"{where}: firm {r['firm']!r} is not in pe_firms.csv")
        if r["company_seed_ref"] and r["company_seed_ref"] not in refs:
            problems.append(f"{where}: company_seed_ref {r['company_seed_ref']!r} is not in pe_companies.csv")
        if not norm_name(r["company"]):
            problems.append(f"{where}: a relationship needs a company")
        if r["claim"] not in ALLOWED_CLAIMS:
            problems.append(f"{where}: claim must be one of {', '.join(ALLOWED_CLAIMS)} (a completion or an exit needs a page code has read), not {r['claim']!r}")
        if not URL.match(r["source_url"]):
            problems.append(f"{where}: source_url must start with http:// or https://")
        _iso(r["evidence_date"], where, problems)
        _iso(r["research_date"], where, problems)
    if problems:
        shown = problems[:12]
        raise SeedError("; ".join(shown) + (f"; and {len(problems) - len(shown)} more" if len(problems) > len(shown) else ""))
    return firms, companies, rels


def _stamp(research_date: str) -> str:
    return f"{research_date or date.today().isoformat()}T12:00:00+00:00"        # when the researcher read the source (noon: the sheet records only the day)


def _excerpt(r: dict[str, str]) -> str:
    bits = [f"Workbook ({r['research_date'] or 'undated'}): {r['workbook_status']}" if r["workbook_status"] else "Workbook"]
    for label, value in (("role", r["role"]), ("note", r["firm_note"])):
        if value and value.lower() != "stake not specified":
            bits.append(f"{label}: {value}")
    if r["text"]:
        bits.append(r["text"])
    return ". ".join(b.rstrip(".") for b in bits)


def _link_account(conn: sqlite3.Connection, company_id: int, names: list[str]) -> bool:
    """Point the company at an existing account when one of its names is the account's name (resolve_company only tries the primary name)."""
    if conn.execute("SELECT account_id FROM pe_companies WHERE id = ?", (company_id,)).fetchone()["account_id"] is not None:
        return False
    keys = {norm_name(n) for n in names if norm_name(n)}
    for a in conn.execute("SELECT id, name FROM accounts").fetchall():
        if norm_name(a["name"]) in keys and not _account_taken(conn, a["id"]):
            conn.execute("UPDATE pe_companies SET account_id = ? WHERE id = ?", (a["id"], company_id))
            return True
    return False


def import_seed(conn: sqlite3.Connection, cfg: Config, directory: Path | None = None, today: date | None = None) -> ImportResult:
    """Validate the seed files, then load them in one transaction. Idempotent: a second run adds nothing."""
    day = today or date.today()
    firms, companies, rels = load_seed(directory or SEED_DIR)
    out = ImportResult(firms=len(firms), companies=len(companies))
    with conn:
        firm_ids: dict[str, int] = {}
        for f in firms:
            dom = normalize_domain(f["domain"]) if f["domain"] else None
            fid, created = ensure_firm(conn, f["name"], "manual", "pe-import", dom, None, f["portfolio_url"] or None)
            out.firms_added += created
            firm_ids[norm_name(f["name"])] = fid
            if not created:                                     # the database wins: only blanks are filled
                if dom:
                    conn.execute("UPDATE pe_firms SET domain = ? WHERE id = ? AND (domain IS NULL OR trim(domain) = '')", (dom, fid))
                if f["portfolio_url"]:
                    conn.execute("UPDATE pe_firms SET portfolio_url = ? WHERE id = ? AND (portfolio_url IS NULL OR trim(portfolio_url) = '')", (f["portfolio_url"], fid))
            conn.execute("UPDATE pe_firms SET scope = COALESCE(scope, ?), presence = COALESCE(presence, ?), presence_note = COALESCE(presence_note, ?), "
                         "local_city = COALESCE(local_city, ?), focus = COALESCE(focus, ?), seed_ref = COALESCE(seed_ref, ?) WHERE id = ?",
                         (f["scope"], f["presence"] or None, f["note"] or None, f["local_city"] or None, f["focus"] or None, f["seed_ref"] or None, fid))

        first_source: dict[str, str] = {}
        for r in rels:
            if r["company_seed_ref"]:
                first_source.setdefault(r["company_seed_ref"], r["source_url"])
        company_ids: dict[str, int] = {}
        for c in companies:
            aliases = tuple(a.strip() for a in c["aliases"].split(";") if a.strip())
            try:
                m = resolve_company(conn, c["name"], None, aliases, today=day)
            except AmbiguousCompany:
                out.unresolved.append(c["name"])
                continue
            company_ids[c["seed_ref"]] = m.id
            out.companies_added += m.created
            out.accounts_linked += _link_account(conn, m.id, [c["name"], *aliases])
            conn.execute("UPDATE pe_companies SET industry = COALESCE(industry, ?), seed_ref = COALESCE(seed_ref, ?) WHERE id = ?",
                         (c["industry"] or None, c["seed_ref"], m.id))
            url = first_source.get(c["seed_ref"])
            if c["location"] and url:                           # kept as unverified evidence; pe_locations is written only from a retrieved page
                _, new = add_evidence(conn, "company", m.id, "location", url, retrieved_at=_stamp(c["research_date"]), source_type="workbook",
                                      verification="snippet_only", excerpt=f"Workbook: Chicago-area location {c['location']}", value=c["location"],
                                      published=c["evidence_date"] or None)
                out.evidence_added += new
            if c["kind"] == "peer":
                (out.peers if is_competitor(c["name"], None, cfg) or any(is_competitor(a, None, cfg) for a in aliases) else out.peers_unlisted).append(c["name"])

        groups: dict[tuple[str, str], list[dict]] = {}
        for r in rels:
            groups.setdefault((norm_name(r["firm"]), r["company_seed_ref"] or norm_name(r["company"])), []).append(r)
        for (firm_key, _co), rows in groups.items():
            first = rows[0]
            if first["company_seed_ref"]:
                cid = company_ids.get(first["company_seed_ref"])
            else:
                aliases = tuple(a.strip() for a in first["aliases"].split(";") if a.strip())
                try:
                    m = resolve_company(conn, first["company"], None, aliases, today=day)
                    cid = m.id
                    out.companies_added += m.created
                    out.accounts_linked += _link_account(conn, cid, [first["company"], *aliases])
                except AmbiguousCompany:
                    cid = None
            if cid is None:
                out.unresolved.append(first["company"])
                continue
            fid = firm_ids[firm_key]
            oid, created = ensure_ownership(conn, fid, cid, day, "unspecified", "manual")
            out.relationships += 1
            if created:
                out.relationships_added += 1
                conn.execute("UPDATE pe_ownership SET status = 'unclear', basis = 'workbook_lead', confidence = 'Low', seed_ref = ? WHERE id = ?", (first["seed_ref"], oid))
                conn.execute("INSERT INTO pe_ownership_log(ownership_id, at, from_status, to_status, reason) VALUES (?,?,?,?,?)",
                             (oid, day.isoformat(), None, "unclear", "workbook_lead"))
            for r in rows:
                _, new = add_evidence(conn, "ownership", oid, r["claim"], r["source_url"], retrieved_at=_stamp(r["research_date"]), source_type="workbook",
                                      verification="snippet_only", excerpt=_excerpt(r), title=r["workbook_status"] or None,
                                      published=r["evidence_date"] or None, event=(r["evidence_date"] or None) if r["claim"] == "announced" else None)
                out.evidence_added += new
        if out.firms_added or out.companies_added or out.relationships_added or out.evidence_added:
            log_audit(conn, "pe-import", "create", None, "pe_seed", None,
                      f"{out.firms_added} firm(s), {out.companies_added} company(ies), {out.relationships_added} relationship(s), {out.evidence_added} evidence row(s)")
    return out
