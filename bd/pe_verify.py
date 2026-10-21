"""Checking the researched workbook's transaction leads against the pages they cite. Free: no model, no search; a polite fetch of a page the workbook already names.

A workbook relationship that says a deal was announced is stored as a lead (bd.pe_import): snippet-only evidence, status *unclear*. This reads the cited page
itself and, only if one sentence on it says the firm acquired or invested in the company (not merely names both), hands that sentence to the same rules a model-reported deal goes through
(bd.pe_deals.verify_deal: the sentence is on the page, the firm is named, completion needs the page's own completion words and never forward-looking ones,
a date is kept only if the page writes it). What survives becomes page-verified evidence, so the ledger derives the status: an announcement is *pending*, a
page that says the deal closed is *current*, and nothing here ever calls an announcement a closing. A page that does not say it plainly stays a lead: the lead is
never promoted on the workbook's word. Plain portfolio listings are not handled here: bd.pe_collect reads those pages and their current and past sections."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta

from bd.config import Config
from bd.pe_deals import DealItem, apply_deal, verify_deal
from bd.pe_store import norm_name
from bd.sources import FetchError, Fetched, Fetcher, visible_text
from bd.urls import normalize_url

LEAD_RETRY_DAYS = 7                 # a page that was read (or could not be) is read again after this long
MIN_KEY = 3                         # a name shorter than this is too easy to find inside other words to match on
SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
# What a sentence must say for it to confirm "the firm did a deal to the company" (run on bd.pe_store.norm_name output: lower case, no punctuation). The firm must act on
# the company (it acquires or invests in it) with nothing but the verb between them, or the company must be "acquired by" or receive an investment "from" the firm.
# Sentences where a portfolio company buys someone else ("AHEAD, backed by the firm, acquires CDI"), where the firm only sells, or that only say the firm backs or owns
# a company, say something else and never confirm a deal. A page that does not say it plainly leaves the lead a lead.
ACTIVE = re.compile(r"\b(?:to acquire|acquires|acquired|acquire|agreed to buy|to buy|buys|bought|invests? in|invested in|investment in|growth investment in|"
                    r"(?:majority|minority) (?:investment|stake) in|(?:completes?|completed|announces?|announced|signs?|signed|closes?|closed) (?:its |an |a )?"
                    r"(?:definitive agreement for )?(?:the )?(?:acquisition|investment|recapitalization|take private) of|recapitalization of|recapitalizes|"
                    r"(?:to )?takes? private)\b")
PASSIVE = re.compile(r"\b(?:to be acquired by|acquired by|acquisition by|investment (?:from|by)|growth investment from|recapitalization (?:led )?by|recapitalized by|"
                     r"(?:to be )?taken private by)\b")
MAX_BETWEEN = 90                    # characters between the two names: a longer stretch is another clause


@dataclass
class VerifySummary:
    leads: int = 0                                   # transaction leads looked at this run
    pages: int = 0                                   # distinct pages read
    confirmed: list[str] = field(default_factory=list)       # "Firm / Company: pending" ...
    no_sentence: int = 0                             # the page was read but no sentence says the firm acquired or invested in the company
    rejected: int = 0                                # a sentence was found but the deal rules refused it
    unreachable: int = 0
    recent: int = 0                                  # left alone: read within LEAD_RETRY_DAYS
    notes: list[str] = field(default_factory=list)

    def line(self) -> str:
        if not self.leads and not self.recent:
            return "No transaction lead is waiting for a page check."
        bits = [f"{self.leads} transaction lead(s) checked against {self.pages} page(s): {len(self.confirmed)} confirmed by a page"]
        bits += [f"{n} not stated on the page" for n in (self.no_sentence,) if n]
        bits += [f"{n} refused by the deal rules" for n in (self.rejected,) if n]
        bits += [f"{n} page(s) unreachable" for n in (self.unreachable,) if n]
        bits += [f"{n} read within {LEAD_RETRY_DAYS} days, left alone" for n in (self.recent,) if n]
        return "; ".join(bits) + ". A lead a page does not confirm stays a lead."


class _Once:
    """Hands verify_deal the page that was already read, so one lead costs one fetch."""

    def __init__(self, got: Fetched) -> None:
        self.got = got

    def get(self, url: str, **kw) -> Fetched:
        return self.got


def _keys(conn: sqlite3.Connection, company_id: int, name: str) -> list[tuple[str, str]]:
    """(normalised key, the name as written) for the company and every other name it is known by, longest first."""
    names = [name] + [r["alias"] for r in conn.execute("SELECT alias FROM pe_aliases WHERE entity_type = 'company' AND entity_id = ?", (company_id,))]
    out = {}
    for n in names:
        k = norm_name(n)
        if len(k) >= MIN_KEY:
            out.setdefault(k, n)
    return sorted(out.items(), key=lambda kv: -len(kv[0]))


def _named(key: str, norm_sentence: str) -> bool:
    return bool(re.search(r"(?<![a-z0-9])" + re.escape(key) + r"(?![a-z0-9])", norm_sentence))


def _bound(key: str) -> re.Pattern:
    return re.compile(r"(?<![a-z0-9])" + re.escape(key) + r"(?![a-z0-9])")


def says_deal(ns: str, firm_key: str, company_key: str) -> bool:
    """Does the normalised sentence say the firm acquired or invested in the company: the firm first with an acquiring or investing verb between the two names, or the
    company first with 'acquired by' or an investment 'from' the firm between them, and the names close together."""
    for f in _bound(firm_key).finditer(ns):
        for c in _bound(company_key).finditer(ns):
            if f.end() <= c.start():
                between, rx = ns[f.end():c.start()], ACTIVE
            elif c.end() <= f.start():
                between, rx = ns[c.end():f.start()], PASSIVE
            else:
                continue
            if len(between) <= MAX_BETWEEN and rx.search(between):
                return True
    return False


def find_sentence(text: str, company_keys: list[tuple[str, str]], firm_name: str) -> tuple[str, str] | None:
    """(the sentence, the company name as written in it) for the first sentence that says the firm acquired or invested in the company, else None."""
    fk = norm_name(firm_name)
    if len(fk) < MIN_KEY:
        return None
    for raw in SENTENCE.split(text):
        s = " ".join(raw.split())
        if not 25 <= len(s) <= 600:
            continue
        ns = norm_name(s)
        if not _named(fk, ns):
            continue
        for key, written in company_keys:
            if _named(key, ns) and says_deal(ns, fk, key) and not re.search(re.escape(written) + r"(?:'s|’s)", s, re.I):      # "Acme's rival" is not Acme
                return s, written
    return None


def _date(s: str | None) -> date | None:
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None


def waiting(conn: sqlite3.Connection, as_of: date) -> tuple[list[sqlite3.Row], int]:
    """(leads to check, leads left alone because they were read recently): an unclear relationship the workbook says was announced, with no page-verified evidence
    yet, at an active firm; Chicago firms first."""
    cutoff = (as_of - timedelta(days=LEAD_RETRY_DAYS)).isoformat()
    rows = conn.execute(
        "SELECT o.id AS oid, o.firm_id, o.company_id, o.lead_checked, f.name AS firm, c.name AS company, e.url, e.event_date FROM pe_ownership o "
        "JOIN pe_firms f ON f.id = o.firm_id JOIN pe_companies c ON c.id = o.company_id "
        "JOIN pe_evidence e ON e.subject_type = 'ownership' AND e.subject_id = o.id AND e.source_type = 'workbook' AND e.claim = 'announced' "
        "WHERE o.status = 'unclear' AND f.status = 'active' AND NOT EXISTS (SELECT 1 FROM pe_evidence v WHERE v.subject_type = 'ownership' AND v.subject_id = o.id "
        "AND v.verification = 'page_verified') GROUP BY o.id ORDER BY (f.scope = 'chicago') DESC, o.id").fetchall()
    due = [r for r in rows if not r["lead_checked"] or r["lead_checked"] < cutoff]
    return due, len(rows) - len(due)


def run_verify_leads(conn: sqlite3.Connection, cfg: Config, fetcher: Fetcher, as_of: date, limit: int | None = None,
                     log: Callable[[str], None] = lambda s: None) -> VerifySummary:
    out = VerifySummary()
    due, out.recent = waiting(conn, as_of)
    due = due[:limit] if limit else due
    pages: dict[str, Fetched | str] = {}
    for r in due:
        out.leads += 1
        url = r["url"]
        if url not in pages:
            try:
                pages[url] = fetcher.get(url)
                out.pages += 1
            except FetchError as e:
                pages[url] = e.reason
        got = pages[url]
        with conn:
            conn.execute("UPDATE pe_ownership SET lead_checked = ? WHERE id = ?", (as_of.isoformat(), r["oid"]))
        if isinstance(got, str):
            out.unreachable += 1
            log(f"  {r['firm']} / {r['company']}: page not read ({got})")
            continue
        firm = conn.execute("SELECT * FROM pe_firms WHERE id = ?", (r["firm_id"],)).fetchone()
        text, _title = visible_text(got, cfg.pe_portfolio.text_cap_bytes)
        found = find_sentence(text, _keys(conn, r["company_id"], r["company"]), r["firm"])
        if found is None:
            out.no_sentence += 1
            log(f"  {r['firm']} / {r['company']}: no sentence on the page says the firm acquired or invested in it; still a lead")
            continue
        sentence, written = found
        try:
            item = DealItem(deal_type="acquisition", target=written, announcement_date=_date(r["event_date"]), source_url=url, excerpt=sentence)
        except ValueError:
            out.no_sentence += 1
            continue
        v, why = verify_deal(item, firm, {normalize_url(url)}, _Once(got), cfg, as_of, set())
        if v is None:
            out.rejected += 1
            log(f"  {r['firm']} / {r['company']}: refused by the deal rules ({why})")
            continue
        with conn:
            note = apply_deal(conn, cfg, firm, v, as_of)
        if note:
            out.notes.append(note)
            out.rejected += 1
            continue
        status = conn.execute("SELECT status FROM pe_ownership WHERE id = ?", (r["oid"],)).fetchone()["status"]
        out.confirmed.append(f"{r['firm']} / {r['company']}: {status}")
        log(f"  {r['firm']} / {r['company']}: {v.event} on the page, now {status}")
    return out
