#!/usr/bin/env python3
"""One-off developer tool: turn the Chicago PE workbook (.xlsx) into the three seed CSVs that `bd pe-import` loads.

    python3 scripts/xlsx_to_pe_seed.py Chicago_PE_Firms_and_Portfolio_Companies.xlsx [--out .]

It reads the workbook with the standard library only (an .xlsx is a zip of XML; no openpyxl, which the app does not depend on), takes every URL from the
HYPERLINK formulas, and writes `pe_firms.csv`, `pe_companies.csv` and `pe_relationships.csv`. A person reviews the CSVs before they are committed; the app
never reads the workbook and never splits a sponsor name itself.

Nothing here decides that an ownership is current. Each relationship row carries one of four claims, chosen conservatively from the workbook's own wording:
  listed_current     the workbook says a sponsor page or biography calls the company current or active
  listed             a plain listing, or a listing the workbook itself caveats (filtered, mixed-status, no exit marker)
  listed_historical  the workbook calls the sponsor historical
  announced          ANY transaction wording ("completed", "acquired", "take-private", "recapitalization", "announced", "proposed"). Never "completed":
                     several rows say "completed" on the strength of an announcement alone, and a completion needs a page that code has read.
The workbook's own words are kept in the evidence text, so nothing it said is lost. The loader stores every row as unverified evidence (a lead)."""

from __future__ import annotations

import argparse
import csv
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlparse

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main", "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
RESEARCH_DATE = "2026-10-19"           # printed on every row of the workbook

# ---- a minimal .xlsx reader ----------------------------------------------------------------------------------------------------------------


def _col(ref: str) -> int:
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):
        n = n * 26 + ord(ch) - 64
    return n


def read_workbook(path: str) -> dict[str, dict[tuple[int, int], tuple[str | None, str | None]]]:
    """sheet name -> {(row, col): (value, formula)}"""
    z = zipfile.ZipFile(path)
    shared: list[str] = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", NS):
            shared.append("".join(t.text or "" for t in si.iter("{%s}t" % NS["m"])))
    rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
    out = {}
    for s in ET.fromstring(z.read("xl/workbook.xml")).find("m:sheets", NS):
        target = rels[s.get("{%s}id" % NS["r"])].lstrip("/")
        target = target if target.startswith("xl/") else "xl/" + target
        cells = {}
        for c in ET.fromstring(z.read(target)).iter("{%s}c" % NS["m"]):
            ref, t = c.get("r"), c.get("t")
            v, f = c.find("m:v", NS), c.find("m:f", NS)
            val = None
            if t == "s" and v is not None:
                val = shared[int(v.text)]
            elif t == "inlineStr":
                val = "".join(x.text or "" for x in c.iter("{%s}t" % NS["m"]))
            elif v is not None:
                val = v.text
            cells[(int(re.search(r"\d+", ref).group(0)), _col(ref))] = (val, f.text if f is not None else None)
        out[s.get("name")] = cells
    return out


def text(cells, r: int, c: int) -> str:
    v = cells.get((r, c), (None, None))[0]
    return " ".join((v or "").split())


def link(cells, r: int, c: int) -> str:
    """The URL inside a HYPERLINK("url","name") formula; empty when the cell has none."""
    f = cells.get((r, c), (None, None))[1] or ""
    m = re.match(r'\s*HYPERLINK\(\s*"([^"]+)"', f, re.I)
    return m.group(1).strip() if m else ""


def day(cells, r: int, c: int) -> str:
    """A date cell as YYYY-MM-DD ('' when blank). Excel stores dates as day counts from 1899-12-30."""
    v = (cells.get((r, c), (None, None))[0] or "").strip()
    if not v:
        return ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", v):
        return v[:10]
    return (date(1899, 12, 30) + timedelta(days=int(float(v)))).isoformat()


# ---- how the workbook's words become claims and firm names -----------------------------------------------------------------------------------
TRANSACTION = re.compile(r"announc|proposed|complet|acqui|take-private|recapital|investment confirmed|majority sponsor confirmed|co-sponsor investment|"
                         r"growth investment", re.I)
CURRENT = re.compile(r"\bcurrent\b|\bactive\b|recent sponsor portfolio confirmation", re.I)


def claim_for(status: str) -> str:
    if TRANSACTION.search(status):
        return "announced"
    if "dated portfolio confirmation" in status.lower():
        return "listed"                               # an old confirmation the workbook itself says to refresh
    if CURRENT.search(status):
        return "listed_current"
    return "listed"


# A sponsor string in the 'Chicago Companies' sheet holds several firms separated by ';'. This table is the reviewed split: part as written -> (canonical firm
# name, note, claim override). Anything not in the table or in the 27 firms is a plain firm name and becomes an investor-only firm.
SPONSOR_PARTS: dict[str, tuple[str, str, str | None]] = {
    "Bain Capital Tech Opportunities": ("Bain Capital", "Tech Opportunities fund", None),
    "CVC Growth": ("CVC Growth Partners", "", None),
    "BDT & MSD Partners (BDT funds)": ("BDT & MSD Partners", "BDT funds", None),
    "BDT & MSD Partners (legacy MSD Partners)": ("BDT & MSD Partners", "legacy MSD Partners", None),
    "Advent International (historical sponsor)": ("Advent International", "historical sponsor, per the workbook", "listed_historical"),
    "Blackstone, indirectly through Copeland": ("Blackstone", "indirect, through Copeland", None),
    "Elliott Investment Management (proposed buyers)": ("Elliott Investment Management", "proposed buyer with GTCR; not completed", "announced"),
}
# The workbook's own classification of two companies as peers of the client (consultancies), not prospects. `bd pe-import` checks that fit_gate.competitors
# names them so they are never scanned or researched.
PEERS = {"C020", "C028"}
# Name fixes for companies that appear under more than one name. (name as written) -> (canonical name, extra aliases)
COMPANY_NAMES: dict[str, tuple[str, tuple[str, ...]]] = {
    "Weber / Weber Blackstone": ("Weber", ("Weber Blackstone",)),
    "Jet Support Services (JSSI)": ("Jet Support Services", ("JSSI",)),
    "NIQ (NielsenIQ)": ("NielsenIQ", ("NIQ",)),
    "Walgreens": ("Walgreens", ("Walgreens Boots Alliance",)),
    "Medline": ("Medline", ("Medline Industries",)),
    "Culligan International": ("Culligan International", ("Culligan",)),
    "HUB International": ("HUB International", ("HUB",)),
    "West Monroe": ("West Monroe", ("West Monroe Partners",)),
}


def split_name(name: str) -> tuple[str, tuple[str, ...]]:
    """'Advanced Personnel Management (APM)' -> ('Advanced Personnel Management', ('APM',)); a name in COMPANY_NAMES uses that entry."""
    if name in COMPANY_NAMES:
        return COMPANY_NAMES[name]
    m = re.fullmatch(r"(.+?)\s*\(([^)]+)\)", name)
    return (m.group(1).strip(), (m.group(2).strip(),)) if m else (name, ())


def host(url: str) -> str:
    h = (urlparse(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def key(name: str) -> str:
    return re.sub(r"[^a-z0-9&]+", " ", re.sub(r"^the\s+", "", name.lower())).strip()


def build(path: str) -> tuple[list[dict], list[dict], list[dict]]:
    wb = read_workbook(path)
    cc, pf, pc = wb["Chicago Companies"], wb["PE Firms"], wb["Portfolio Companies"]
    problems: list[str] = []

    # ---- firms ----
    firms: list[dict] = []
    by_key: dict[str, str] = {}
    for r in range(9, 36):
        ref = text(pf, r, 1)
        if not ref:
            continue
        name = text(pf, r, 2)
        portfolio_url, firm_url = link(pf, r, 8), link(pf, r, 9)
        domain = host(firm_url) or host(portfolio_url)
        firms.append({"seed_ref": ref, "name": name, "scope": "chicago", "domain": domain, "local_city": text(pf, r, 3), "presence": text(pf, r, 4),
                      "focus": text(pf, r, 5), "portfolio_url": portfolio_url, "presence_source_url": firm_url, "note": text(pf, r, 10)})
        by_key[key(name)] = name
    firm_ids = {f["seed_ref"]: f["name"] for f in firms}

    def canonical(part: str) -> tuple[str, str, str | None]:
        part = part.strip()
        if part in SPONSOR_PARTS:
            return SPONSOR_PARTS[part]
        return by_key.get(key(part), part), "", None

    # ---- the 36 Chicago companies, and one relationship row per (company, sponsor, source) ----
    companies: list[dict] = []
    rels: list[dict] = []
    investor_names: dict[str, str] = {}
    for r in range(9, 45):
        ref = text(cc, r, 1)
        if not ref:
            continue
        raw = text(cc, r, 2)
        name, aliases = split_name(raw)
        status, role = text(cc, r, 7), text(cc, r, 6)
        companies.append({"seed_ref": ref, "name": name, "aliases": ";".join(aliases), "kind": "peer" if ref in PEERS else "company",
                          "location": text(cc, r, 3), "industry": text(cc, r, 4), "ownership_role": role, "evidence_status": status,
                          "confidence": text(cc, r, 8), "evidence_date": day(cc, r, 9), "research_date": day(cc, r, 10) or RESEARCH_DATE,
                          "note": text(cc, r, 14)})
        sources = [u for u in (link(cc, r, 12), link(cc, r, 13)) if u]
        if not sources:
            problems.append(f"{ref} {raw}: no source link")
        for part in text(cc, r, 5).split(";"):
            firm, note, override = canonical(part)
            if firm not in firm_ids.values():
                investor_names.setdefault(key(firm), firm)
            for i, url in enumerate(sources, 1):
                rels.append({"seed_ref": f"{ref}-S{i}", "firm": firm, "firm_note": note, "company": name, "company_seed_ref": ref,
                             "claim": override or claim_for(status), "workbook_status": status, "role": role, "source_url": url,
                             "evidence_date": day(cc, r, 9), "research_date": day(cc, r, 10) or RESEARCH_DATE,
                             "text": text(cc, r, 11), "note": text(cc, r, 14)})
    company_refs = {c["seed_ref"] for c in companies}
    name_of = {c["seed_ref"]: c["name"] for c in companies}
    for key_, nm in sorted(investor_names.items(), key=lambda kv: kv[1].lower()):
        firms.append({"seed_ref": "", "name": nm, "scope": "investor", "domain": "", "local_city": "", "presence": "", "focus": "", "portfolio_url": "",
                      "presence_source_url": "", "note": "an investor in a company on the Chicago list; no portfolio page is read unless an address is added"})
    for i, f in enumerate([f for f in firms if not f["seed_ref"]], 1):
        f["seed_ref"] = f"I{i:02d}"

    # ---- the 208 researched relationships ----
    for r in range(9, 217):
        ref = text(pc, r, 1)
        if not ref:
            continue
        fid, cid = text(pc, r, 2), text(pc, r, 7)
        if fid not in firm_ids:
            problems.append(f"{ref}: firm {fid!r} is not in the PE Firms sheet")
            continue
        if cid and cid not in company_refs:
            problems.append(f"{ref}: Chicago Company ID {cid!r} is not in the Chicago Companies sheet")
            continue
        raw = text(pc, r, 4)
        name, aliases = (name_of[cid], ()) if cid else split_name(raw)
        status = text(pc, r, 5)
        url = link(pc, r, 9)
        if not url:
            problems.append(f"{ref} {raw}: no source link")
        rels.append({"seed_ref": ref, "firm": firm_ids[fid], "firm_note": "", "company": name, "company_seed_ref": cid, "claim": claim_for(status),
                     "workbook_status": status, "role": text(pc, r, 6), "source_url": url, "evidence_date": day(pc, r, 10),
                     "research_date": day(pc, r, 11) or RESEARCH_DATE, "text": text(pc, r, 8), "note": "",
                     "aliases": ";".join(aliases)})
    for row in rels:
        row.setdefault("aliases", "")
    for p in PEERS:
        if p not in company_refs:
            problems.append(f"peer {p} is not in the Chicago Companies sheet")
    if problems:
        raise SystemExit("The workbook did not convert cleanly:\n  " + "\n  ".join(problems))
    return firms, companies, rels


def write(path: Path, rows: list[dict], columns: list[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="raise", lineterminator="\n")
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in columns})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("workbook")
    ap.add_argument("--out", default=".", help="directory for pe_firms.csv, pe_companies.csv, pe_relationships.csv (default: the current directory)")
    a = ap.parse_args()
    firms, companies, rels = build(a.workbook)
    out = Path(a.out)
    write(out / "pe_firms.csv", firms, ["seed_ref", "name", "scope", "domain", "local_city", "presence", "focus", "portfolio_url", "presence_source_url", "note"])
    write(out / "pe_companies.csv", companies, ["seed_ref", "name", "aliases", "kind", "location", "industry", "ownership_role", "evidence_status", "confidence",
                                                "evidence_date", "research_date", "note"])
    write(out / "pe_relationships.csv", rels, ["seed_ref", "firm", "firm_note", "company", "company_seed_ref", "aliases", "claim", "workbook_status", "role",
                                               "source_url", "evidence_date", "research_date", "text", "note"])
    claims: dict[str, int] = {}
    for r in rels:
        claims[r["claim"]] = claims.get(r["claim"], 0) + 1
    print(f"{len([f for f in firms if f['scope'] == 'chicago'])} Chicago firms, {len([f for f in firms if f['scope'] == 'investor'])} investor-only firms, "
          f"{len(companies)} companies ({len(PEERS)} peers), {len(rels)} evidence rows; claims: {claims}", file=sys.stderr)


if __name__ == "__main__":
    main()
