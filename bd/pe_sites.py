"""Find a PE firm's own website and its portfolio page without anyone typing them in. Free (no model); a few polite page reads per firm.

A candidate domain is built from the firm's name and is accepted only when the page code retrieved from it names the firm (in its title, or at
least twice in its visible text) and the request stayed on that host. A candidate that cannot be read, or does not name the firm, is dropped:
the firm's site stays unknown and the Firm details form is the way to give it. Nothing is accepted on a guess.

The portfolio page is found the same way: a Portfolio / Companies / Investments link on the home page (bd.pe_collect), else a short list of
conventional paths, accepted only when the page they return parses as a listing of companies (or exposes the site's public listing API)."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable

from bd.accounts import log_audit
from bd.htmltext import page_text, parse_html
from bd.pe_listing import parse_listing
from bd.sources import FetchError, Fetcher
from bd.urls import host_in, hostname

GENERIC = frozenset({"capital", "partners", "partner", "group", "holdings", "equity", "management", "advisors", "advisers", "investments", "investment",
                     "asset", "assets", "fund", "funds", "private", "ventures", "associates", "and", "the", "company", "co"})
LEGAL = frozenset({"llc", "lp", "llp", "inc", "ltd", "corp", "plc", "sa", "ag"})
SUFFIXES = ("capital", "partners", "group", "equity")
PORTFOLIO_PATHS = ("/portfolio", "/our-portfolio", "/portfolio-companies", "/companies", "/our-companies", "/investments")
MIN_ENTRIES = 3                      # a page that parses to fewer repeated company cards is not taken for a portfolio listing
MAX_CANDIDATES = 6


def name_tokens(name: str) -> list[str]:
    toks = re.findall(r"[a-z0-9]+", (name or "").lower())
    while toks and toks[-1] in LEGAL:
        toks.pop()
    return toks


def candidate_domains(name: str) -> list[str]:
    """Hosts to try, most likely first, built only from the firm's own name."""
    toks = name_tokens(name)
    if not toks:
        return []
    core = [t for t in toks if t not in GENERIC]
    out: list[str] = []
    for s in ["".join(toks), *(["".join(core)] if core else []), *(["".join(core) + x for x in SUFFIXES] if core else [])]:
        d = f"{s}.com"
        if len(s) >= 3 and d not in out:
            out.append(d)
    return out[:MAX_CANDIDATES]


def _norm(text: str) -> str:
    return " " + " ".join(re.findall(r"[a-z0-9]+", (text or "").lower())) + " "


def names_firm(title: str, text: str, name: str) -> bool:
    """The firm's name is in the page's title, or appears at least twice in its visible text."""
    phrase = " ".join(name_tokens(name))
    if not phrase:
        return False
    needle = f" {phrase} "
    return needle in _norm(title) or _norm(text).count(needle) >= 2


def find_firm_domain(fetcher: Fetcher, name: str, log: Callable[[str], None] = lambda s: None) -> str | None:
    for cand in candidate_domains(name):
        try:
            got = fetcher.get(f"https://{cand}/", retries=0)           # an address that does not exist is an answer, not a reason to wait and retry
        except FetchError:
            continue
        if "html" not in got.content_type or not host_in(hostname(got.final_url), [cand]):
            continue
        page = parse_html(got.body)
        if names_firm(page.title, page_text(page), name):
            log(f"  {name}: website found at {cand} (the page it returns names the firm)")
            return cand
    log(f"  {name}: no website could be confirmed from its name; add it under Firm details")
    return None


def locate_firm_sites(conn: sqlite3.Connection, fetcher: Fetcher, log: Callable[[str], None] = lambda s: None, only: str | None = None) -> int:
    """Set the domain of active firms that have none, when a candidate is confirmed by its own page. Returns firms updated."""
    n = 0
    for f in conn.execute("SELECT id, name FROM pe_firms WHERE status = 'active' AND (domain IS NULL OR trim(domain) = '') "
                          "AND COALESCE(scope, '') != 'investor' ORDER BY id").fetchall():     # an investor-only firm's site is not guessed from its name; add it under Firm details
        if only and only.lower() not in f["name"].lower():
            continue
        dom = find_firm_domain(fetcher, f["name"], log)
        if dom:
            with conn:
                conn.execute("UPDATE pe_firms SET domain = ? WHERE id = ? AND (domain IS NULL OR trim(domain) = '')", (dom, f["id"]))
                log_audit(conn, "pe-collect", "update", None, "pe_firm", f"#{f['id']} {f['name']}", f"domain: none -> {dom} (found by code; the page names the firm)")
            n += 1
    return n


def find_portfolio_by_path(fetcher: Fetcher, domain: str) -> str | None:
    """A conventional path on the firm's own host that returns a page of company cards (or the site's public listing API)."""
    for path in PORTFOLIO_PATHS:
        try:
            got = fetcher.get(f"https://{domain}{path}", retries=0)
        except FetchError:
            continue
        if "html" not in got.content_type or not host_in(hostname(got.final_url), [domain]):
            continue
        listing = parse_listing(got.body, got.final_url)
        if len(listing.entries) >= MIN_ENTRIES or listing.wp_api:
            return got.final_url
    return None
