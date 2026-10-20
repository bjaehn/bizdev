"""Transaction research for PE firms: what a firm bought, what its companies bought, what it sold. Claude searches; code decides what is kept.

A model reports each deal with a verbatim excerpt. Code then retrieves the source page itself and requires that:
  * the page is retrievable and not blocked, and was among the pages the search returned;
  * the excerpt is on the page, names the target, and the firm is named on the page;
  * "completed" rests on the page's own words (completed, closed, consummated...) and never on a forward-looking sentence ("expected to close");
    anything else is "announced", and an announcement is never a closing;
  * a date is kept only if it appears on the page; otherwise it stays unknown;
  * an add-on needs an acquirer that is already one of the firm's holdings, or a page that says it is one;
  * a stake is kept only when the page says majority or minority.
What survives goes into the ownership ledger (bd.pe_ownership) with the page's hash and excerpt as evidence."""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, timedelta
from importlib import resources
from typing import Literal

from pydantic import BaseModel, field_validator

from bd.config import Config
from bd.cost import estimate_cost
from bd.htmltext import page_text, parse_html
from bd.llm import LLM, LLMError, Usage, generate_validated
from bd.pe_ownership import (AmbiguousCompany, add_evidence, ensure_ownership, recompute, record_add_on, resolve_company)
from bd.pe_store import find_firm, norm_name
from bd.sources import FetchError, Fetched, Fetcher, now_iso, store_snapshot, visible_text
from bd.urls import host_in, hostname, normalize_url

MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]
FORWARD = re.compile(r"\b(expected to|is expected|are expected|anticipated|scheduled to|subject to|pending|will close|to close|upon (?:the )?(?:closing|completion)|"
                     r"will be completed|once the (?:transaction|deal) closes|assuming)\b", re.I)
COMPLETE = re.compile(r"\b(completed|completes|completion of|has closed|have closed|closed (?:its|the|on|an|a)\b|closes (?:its|the|an|a)\b|closing of the|"
                      r"consummated|finalized|finalizes)\b", re.I)
ANNOUNCE = re.compile(r"\b(to acquire|agreed to|agreement to|definitive agreement|announce[sd]?|will acquire|plans to acquire|signed|entered into|to be acquired|"
                      r"intends to|acquires|acquired|invests?|invested|investment in|partner(?:s|ed)? with|recapitaliz\w+|sells?|sold|sale of|divest\w*)\b", re.I)
SALE = re.compile(r"\b(sale|sold|sells|divest\w*|exit\w*|realiz\w+|acquired by|to be acquired)\b", re.I)
STAKE = re.compile(r"\b(majority|minority)\b", re.I)
WIRES = ("prnewswire.com", "businesswire.com", "globenewswire.com", "accesswire.com", "einpresswire.com")


class DealItem(BaseModel):
    deal_type: Literal["acquisition", "add_on", "exit", "investment", "merger"]
    target: str
    target_domain: str | None = None
    acquirer: str | None = None
    seller: str | None = None
    co_investors: list[str] = []
    stake: Literal["majority", "minority"] | None = None
    announcement_date: date | None = None
    completion_date: date | None = None
    expected_close: date | None = None
    published_date: date | None = None
    source_url: str
    source_type: str = "secondary_report"
    excerpt: str

    @field_validator("target")
    @classmethod
    def _t(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v or len(v) > 120:
            raise ValueError("target must be 1-120 characters")
        return v

    @field_validator("excerpt")
    @classmethod
    def _x(cls, v: str) -> str:
        v = " ".join(v.split())
        if len(v) < 25 or len(v) > 600:
            raise ValueError("excerpt must be 25-600 characters")
        return v


class DealsResponse(BaseModel):
    deals: list[DealItem] = []


# ---- reading the page's own words --------------------------------------------------------------------------------------------------------------
def norm_text(s: str) -> str:
    s = (s or "").lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"').replace("–", "-").replace("—", "-")
    return " ".join(s.split())


def classify_event(excerpt: str) -> tuple[str | None, bool]:
    """('completed' | 'announced' | None, forward-looking). A sentence is completed only if it uses completion language and no forward-looking
    language; one that does both is an announcement with an expected closing."""
    sentences = re.split(r"(?<=[.!?])\s+", excerpt)
    done = any(COMPLETE.search(s) and not FORWARD.search(s) for s in sentences)
    forward = bool(FORWARD.search(excerpt))
    if done:
        return "completed", False
    if ANNOUNCE.search(excerpt) or COMPLETE.search(excerpt):
        return "announced", forward
    return None, forward


def date_in_text(d: date | None, text: str) -> bool:
    """True if the date is written on the page in a common form. A date the page does not show is not kept."""
    if d is None:
        return False
    t = norm_text(text)
    mon, short = MONTHS[d.month - 1], MONTHS[d.month - 1][:3]
    forms = [d.isoformat(), f"{d.month}/{d.day}/{d.year}", f"{d.month:02d}/{d.day:02d}/{d.year}"]
    for m in {mon, short}:
        forms += [f"{m} {d.day}, {d.year}", f"{m} {d.day} {d.year}", f"{m}. {d.day}, {d.year}", f"{d.day} {m} {d.year}", f"{d.day} {m}. {d.year}",
                  f"{m} {d.day:02d}, {d.year}"]
    return any(f in t for f in forms)


def classify_source(url: str, firm_domain: str | None, target_domain: str | None, cfg: Config) -> str:
    h = hostname(url)
    if host_in(h, ["sec.gov"]):
        return "sec_filing"
    if firm_domain and host_in(h, [firm_domain]):
        return "firm_site"
    if target_domain and host_in(h, [target_domain]):
        return "company_site"
    if host_in(h, WIRES):
        return "company_release"
    return "secondary_report"


# ---- verification (code) -------------------------------------------------------------------------------------------------------------------------
@dataclass
class Verified:
    item: DealItem
    event: str                          # completed | announced
    event_date: date | None
    announced_date: date | None
    published: date | None
    expected_close: date | None
    source_type: str
    fetched: Fetched
    text: str
    forward: bool
    target_domain: str | None


def verify_deal(item: DealItem, firm: sqlite3.Row, retrieved: set[str], fetcher: Fetcher, cfg: Config, as_of: date,
                known_holdings: set[str]) -> tuple[Verified | None, str | None]:
    """Returns (verified deal, None) or (None, the reason it was dropped). Never raises."""
    if host_in(hostname(item.source_url), cfg.sources.blocked_domains):
        return None, "blocked_domain"
    if normalize_url(item.source_url) not in retrieved:
        return None, "url_not_retrieved"
    try:
        got = fetcher.get(item.source_url)
    except FetchError as e:
        return None, f"unreachable:{e.reason}"                          # a failed fetch drops this item; it says nothing about the deal
    text, _t = visible_text(got, cfg.pe_portfolio.text_cap_bytes)
    nt, nx = norm_text(text), norm_text(item.excerpt)
    if nx not in nt:
        return None, "excerpt_not_on_page"
    if norm_name(item.target) not in norm_name(item.excerpt):
        return None, "target_not_in_excerpt"
    firm_named = norm_name(firm["name"]) in norm_name(text)
    parent_named = any(h and h in norm_name(item.excerpt) for h in known_holdings)
    if not (firm_named or (item.deal_type in ("add_on", "merger") and parent_named)):
        return None, "firm_not_named_on_page"
    event, forward = classify_event(item.excerpt)
    if event is None:
        return None, "no_transaction_language"
    if item.deal_type == "exit" and not SALE.search(item.excerpt):
        return None, "no_sale_language"
    if event == "announced" and item.deal_type == "exit" and not FORWARD.search(item.excerpt) and not ANNOUNCE.search(item.excerpt):
        return None, "no_transaction_language"
    ev_date = item.completion_date if event == "completed" else item.announcement_date
    ev_date = ev_date if date_in_text(ev_date, text) else None
    ann = item.announcement_date if date_in_text(item.announcement_date, text) else None
    pub = item.published_date if date_in_text(item.published_date, text) else None
    exp = item.expected_close if forward and date_in_text(item.expected_close, text) else None
    if ev_date and ev_date > as_of + timedelta(days=cfg.scan.max_future_days):
        return None, f"implausible_date:{ev_date.isoformat()}"
    if event == "completed" and ev_date and ev_date > as_of:
        return None, f"completion_in_the_future:{ev_date.isoformat()}"
    dom = item.target_domain.lower().removeprefix("www.") if item.target_domain else None
    if dom and dom not in got.body.lower():
        dom = None                                                    # a domain the page does not show is not recorded
    return Verified(item, event, ev_date, ann, pub, exp, classify_source(got.final_url, firm["domain"], dom, cfg), got, text, forward, dom), None


# ---- writing the ledger --------------------------------------------------------------------------------------------------------------------------
def _holding_keys(conn: sqlite3.Connection, firm_id: int) -> set[str]:
    return {r["name_key"] for r in conn.execute("SELECT c.name_key FROM pe_ownership o JOIN pe_companies c ON c.id = o.company_id "
                                                "WHERE o.firm_id = ? AND o.status != 'historical'", (firm_id,))}


def apply_deal(conn: sqlite3.Connection, cfg: Config, firm: sqlite3.Row, v: Verified, today: date) -> str | None:
    """Record one verified deal. Returns a note when part of it could not be recorded (and why), else None. Caller owns the transaction."""
    it, f = v.item, v.fetched
    sid = store_snapshot(conn, cfg, None, f)
    snap = conn.execute("SELECT title, content_hash, fetched_at FROM pe_snapshots WHERE id = ?", (sid,)).fetchone()
    kw = dict(retrieved_at=snap["fetched_at"], source_type=v.source_type, verification="page_verified", content_hash=snap["content_hash"],
              excerpt=it.excerpt, title=snap["title"], publisher=hostname(f.final_url), published=v.published)
    url = f.final_url
    try:
        target = resolve_company(conn, it.target, v.target_domain, source_url=url, today=today)
    except AmbiguousCompany:
        return f"{it.target}: the name fits several companies and the page gives no domain"
    stake = (m.group(1).lower() if (m := STAKE.search(it.excerpt)) else None)
    claim_event = "announced" if v.event == "announced" else "completed"
    if it.deal_type in ("acquisition", "investment"):
        rel_kind = "platform" if it.deal_type == "acquisition" else "unspecified"
        oid, _ = ensure_ownership(conn, firm["id"], target.id, today, rel_kind, "announcement")
        add_evidence(conn, "ownership", oid, claim_event, url, event=v.event_date, **kw)
        if v.announced_date and v.event == "completed":
            add_evidence(conn, "ownership", oid, "announced", url, event=v.announced_date, **kw)
        if v.expected_close:
            add_evidence(conn, "ownership", oid, "expected_close", url, event=v.expected_close, **kw)
        if stake:
            add_evidence(conn, "ownership", oid, "stake", url, value=stake, **kw)
        recompute(conn, oid, today, cfg, verified=True)
        nt = norm_name(v.text)
        for name in it.co_investors:
            co = find_firm(conn, name)
            if co is not None and co["id"] != firm["id"] and norm_name(co["name"]) in nt:
                cid, _ = ensure_ownership(conn, co["id"], target.id, today, "co_invest", "announcement")
                add_evidence(conn, "ownership", cid, claim_event, url, event=v.event_date, **kw)
                recompute(conn, cid, today, cfg, verified=True)
        return None
    if it.deal_type == "exit":
        oid, _ = ensure_ownership(conn, firm["id"], target.id, today, "unspecified", "announcement")
        claim = "exited" if v.event == "completed" else "exit_announced"
        add_evidence(conn, "ownership", oid, claim, url, event=v.event_date, **kw)
        recompute(conn, oid, today, cfg, verified=True)
        return None
    # add_on / merger: the acquirer must already be one of the firm's holdings (or the page must say so)
    acquirer = it.acquirer or ""
    parent = None
    if acquirer:
        try:
            m = resolve_company(conn, acquirer, None, source_url=url, today=today) if norm_name(acquirer) in _holding_keys(conn, firm["id"]) else None
        except AmbiguousCompany:
            m = None
        parent = m
    says_portfolio = bool(re.search(r"portfolio company of\s+" + re.escape(norm_text(firm["name"]).split(",")[0]), norm_text(it.excerpt) + " " + norm_text(v.text)[:20000]))
    if parent is None and acquirer and says_portfolio:
        try:
            parent = resolve_company(conn, acquirer, None, source_url=url, today=today)
        except AmbiguousCompany:
            parent = None
    if parent is None:
        return f"{it.target}: the acquirer {acquirer or '(not named)'} is not known to be one of {firm['name']}'s holdings"
    lid = record_add_on(conn, parent.id, target.id, today, announced=v.announced_date or (v.event_date if v.event == "announced" else None),
                        closed=v.event_date if v.event == "completed" else None, completed=v.event == "completed",
                        expected_close=v.expected_close, kind="merger" if it.deal_type == "merger" else "add_on")
    add_evidence(conn, "link", lid, "add_on", url, event=v.event_date, **kw)
    return None


# ---- the run -------------------------------------------------------------------------------------------------------------------------------------
@dataclass
class DealOutcome:
    firm: sqlite3.Row
    items: list[DealItem] = field(default_factory=list)
    retrieved: set[str] = field(default_factory=set)
    usage: Usage = field(default_factory=Usage)
    error: str | None = None


@dataclass
class DealsSummary:
    firms: int = 0
    found: int = 0
    verified: int = 0
    rejected: Counter = field(default_factory=Counter)
    notes: list[str] = field(default_factory=list)
    errors: int = 0
    tokens: int = 0
    est_cost: float = 0.0
    unpriced_calls: int = 0
    budget_hit: bool = False


class DealResearcher:
    def __init__(self, cfg: Config, llm: LLM, fetcher: Fetcher) -> None:
        self.cfg, self.llm, self.fetcher = cfg, llm, fetcher

    def system_prompt(self) -> str:
        return resources.files("bd.prompts").joinpath("pe_deals.md").read_text(encoding="utf-8")

    def user_prompt(self, firm: sqlite3.Row, as_of: date) -> str:
        start = as_of - timedelta(days=self.cfg.pe_portfolio.deal_window_days)
        return (f"Firm: {firm['name']}\nWebsite domain: {firm['domain'] or 'unknown'}\nTransactions page or portfolio page: {firm['portfolio_url'] or 'unknown'}\n"
                f"Today's date: {as_of.isoformat()}\nWindow: events dated {start.isoformat()} through {as_of.isoformat()}\n"
                f"Return at most {self.cfg.pe_portfolio.deals_max_per_firm} deals.\n\nSearch per your instructions. Return the JSON object only.")

    def research(self, firm: sqlite3.Row, as_of: date) -> DealOutcome:
        out = DealOutcome(firm)
        try:
            resp, res = generate_validated(self.llm, self.system_prompt(), self.user_prompt(firm, as_of), DealsResponse,
                                           retries=self.cfg.llm.json_retries, key="deals")
        except LLMError as e:
            out.error, out.usage = f"{type(e).__name__}: {e}", e.usage
            return out
        out.items, out.retrieved, out.usage = resp.deals[: self.cfg.pe_portfolio.deals_max_per_firm], res.retrieved_urls, res.usage
        return out


def run_deals(conn: sqlite3.Connection, cfg: Config, researcher: DealResearcher, firms: list[sqlite3.Row], as_of: date, model: str | None,
              workers: int | None = None, max_cost: float | None = None, log: Callable[[str], None] = lambda s: None) -> DealsSummary:
    workers = max(1, workers or cfg.llm.concurrency)
    cap = cfg.llm.max_cost_per_run_usd if max_cost is None else max_cost
    out = DealsSummary()
    firms = firms[: cfg.pe_firms.max_firms_per_run]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(lambda f: researcher.research(f, as_of), firms))
    for r in results:
        out.firms += 1
        cost = estimate_cost(r.usage, model, cfg) if r.usage.total_tokens else None
        out.tokens += r.usage.total_tokens
        out.est_cost += cost or 0.0
        out.unpriced_calls += int(cost is None and r.usage.total_tokens > 0)
        out.errors += bool(r.error)
        out.found += len(r.items)
        known = _holding_keys(conn, r.firm["id"])
        kept = 0
        for item in r.items:
            v, why = verify_deal(item, r.firm, r.retrieved, researcher.fetcher, cfg, as_of, known)
            if v is None:
                out.rejected[why.split(":")[0]] += 1
                continue
            with conn:
                note = apply_deal(conn, cfg, r.firm, v, as_of)
            if note:
                out.notes.append(note)
                out.rejected["not_recorded"] += 1
            else:
                kept += 1
                out.verified += 1
        with conn:
            conn.execute("INSERT INTO runs(run_date,step,account_id,accounts_scanned,signals_found,tokens,est_cost,errors) VALUES (?,?,?,?,?,?,?,?)",
                         (as_of.isoformat(), "pe_deals", None, 1, kept, r.usage.total_tokens, cost, r.error))
            if not r.error:
                conn.execute("UPDATE pe_firms SET last_deals = ? WHERE id = ?", (as_of.isoformat(), r.firm["id"]))
        log(f"  {r.firm['name']}: {len(r.items)} reported, {kept} verified and recorded" + (f"  ERROR {r.error}" if r.error else ""))
    out.budget_hit = cap is not None and out.est_cost >= cap
    return out
