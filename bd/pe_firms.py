"""Private equity firm research: firm-level signals and portfolio discovery.

Claude reads public pages and reports what it found; code decides what is kept:
  * firm signals (new fund, exit, operating-partner hire) use the same acceptance rules as company signals: allowed
    type, not a blocked domain, event date inside the scan window and from the source, URL actually returned by the
    search and resolving, source type reconciled with the real domain, confidence only ever lowered;
  * a portfolio company is recorded as owned by the firm ("link") only when the listing page is hosted on the firm's own
    site (or an SEC page); a company listed only by a third party is still researched, but no link is made until its
    verified facts show the same sponsor;
  * unknown portfolio companies go through the same research / fit gate / auto-add path as market discovery."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, timedelta
from importlib import resources

from pydantic import BaseModel

from bd.accounts import is_valid_domain, normalize_domain
from bd.config import Config
from bd.cost import estimate_cost
from bd.discover import Company, DiscoverSummary, Found, _known, _norm_name, research_and_add
from bd.enrich import Enricher
from bd.hashing import signal_hash
from bd.llm import LLM, LLMError, Usage, generate_validated
from bd.models import FirmScanResponse, FirmSignalExtract, SourceType
from bd.pe_store import link_company, store_firm_signals
from bd.scanners.base import reconcile_source_type
from bd.urls import host_in, hostname, normalize_url
from bd.verify import UrlVerifier

_RANK = {"Low": 0, "Medium": 1, "High": 2}
_VERIFIED_LISTING = {"company_site", "company_release", "sec_filing"}


@dataclass
class AcceptedFirmSignal:
    extract: FirmSignalExtract
    hash: str


@dataclass
class FirmOutcome:
    firm: sqlite3.Row
    signals: list[AcceptedFirmSignal] = field(default_factory=list)
    rejections: list[str] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    error: str | None = None


class FirmScanner:
    def __init__(self, cfg: Config, llm: LLM, verifier: UrlVerifier) -> None:
        self.cfg, self.llm, self.verifier = cfg, llm, verifier

    def system_prompt(self) -> str:
        return resources.files("bd.prompts").joinpath("pe_firm.md").read_text(encoding="utf-8")

    def user_prompt(self, firm: sqlite3.Row, as_of: date) -> str:
        start = as_of - timedelta(days=self.cfg.scan.window_days)
        return (f"Firm: {firm['name']}\nWebsite domain: {firm['domain'] or 'unknown'}\nHeadquarters city: {firm['hq_city'] or 'unknown'}\n"
                f"Today's date: {as_of.isoformat()}\nWindow: events dated {start.isoformat()} through {as_of.isoformat()}\n\n"
                "Search for firm-level signals per your instructions. Return the JSON object only.")

    def scan(self, firm: sqlite3.Row, as_of: date) -> FirmOutcome:
        out = FirmOutcome(firm)
        try:
            resp, res = generate_validated(self.llm, self.system_prompt(), self.user_prompt(firm, as_of), FirmScanResponse,
                                           retries=self.cfg.llm.json_retries)
        except LLMError as e:
            out.error, out.usage = f"{type(e).__name__}: {e}", e.usage
            return out
        out.usage = res.usage
        cfg = self.cfg
        lo, hi = as_of - timedelta(days=cfg.scan.window_days), as_of + timedelta(days=cfg.scan.max_future_days)
        accepted: dict[str, AcceptedFirmSignal] = {}
        for c in resp.signals:
            if host_in(hostname(c.source_url), cfg.sources.blocked_domains):
                out.rejections.append("blocked_domain")
            elif not lo <= c.event_date <= hi:
                out.rejections.append(f"date_out_of_window:{c.event_date.isoformat()}")
            elif normalize_url(c.source_url) not in res.retrieved_urls:
                out.rejections.append("url_not_retrieved")
            else:
                v = self.verifier.verify(c.source_url)
                if not v.ok:
                    out.rejections.append(f"unreachable:{v.reason}")
                    continue
                st = reconcile_source_type(c.source_type, c.source_url, firm["domain"], cfg)
                cap = cfg.sources.confidence_by_source_type[st]
                conf = c.confidence if _RANK[c.confidence] <= _RANK[cap] else cap
                fixed = c.model_copy(update={"source_type": st, "confidence": conf})
                h = signal_hash(f"firm{firm['id']}", fixed.type, fixed.event_date, fixed.source_url)
                if h not in accepted or _RANK[conf] > _RANK[accepted[h].extract.confidence]:
                    accepted[h] = AcceptedFirmSignal(fixed, h)
        out.signals = list(accepted.values())
        return out


@dataclass
class FirmRunSummary:
    firms: int = 0
    new_signals: int = 0
    tokens: int = 0
    est_cost: float = 0.0
    unpriced_calls: int = 0
    errors: int = 0
    budget_hit: bool = False


def select_firms(conn: sqlite3.Connection, name: str | None, limit: int | None, oldest_first: str = "last_scanned") -> list[sqlite3.Row]:
    rows = conn.execute(f"SELECT * FROM pe_firms WHERE status = 'active' ORDER BY {oldest_first} IS NOT NULL, {oldest_first}, id").fetchall()
    if name:
        rows = [r for r in rows if name.lower() in r["name"].lower()]
    return rows[:limit] if limit else rows


def run_firm_scans(conn: sqlite3.Connection, cfg: Config, scanner: FirmScanner, firms: list[sqlite3.Row], as_of: date,
                   model: str | None, workers: int | None = None, max_cost: float | None = None,
                   log: Callable[[str], None] = lambda s: None) -> FirmRunSummary:
    workers = max(1, workers or cfg.llm.concurrency)
    cap = cfg.llm.max_cost_per_run_usd if max_cost is None else max_cost
    out = FirmRunSummary()
    firms = firms[: cfg.pe_firms.max_firms_per_run]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(lambda f: scanner.scan(f, as_of), firms))
    for r in results:
        out.firms += 1
        cost = estimate_cost(r.usage, model, cfg) if r.usage.total_tokens else None
        out.tokens += r.usage.total_tokens
        out.est_cost += cost or 0.0
        out.unpriced_calls += int(cost is None and r.usage.total_tokens > 0)
        out.errors += bool(r.error)
        new, known = store_firm_signals(conn, r.firm["id"], r.signals, as_of)
        out.new_signals += new
        with conn:
            conn.execute("INSERT INTO runs(run_date,step,account_id,accounts_scanned,signals_found,tokens,est_cost,errors) "
                         "VALUES (?,?,?,?,?,?,?,?)", (as_of.isoformat(), "pe_firm", None, 1, new, r.usage.total_tokens, cost,
                                                      r.error or (f"{len(r.rejections)} rejected" if r.rejections else None)))
            if not r.error:
                conn.execute("UPDATE pe_firms SET last_scanned = ? WHERE id = ?", (as_of.isoformat(), r.firm["id"]))
        log(f"  {r.firm['name']}: {new} new signal(s), {known} known, {len(r.rejections)} rejected"
            + (f"  ERROR {r.error}" if r.error else ""))
    out.budget_hit = cap is not None and out.est_cost >= cap
    return out


# ---- portfolio research ---------------------------------------------------------------------------------------------
class PortfolioCompany(Company):
    exited: bool = False


class PortfolioResponse(BaseModel):
    firm_domain: str | None = None
    portfolio: list[PortfolioCompany] = []


class PortfolioFinder:
    def __init__(self, cfg: Config, llm: LLM, verifier: UrlVerifier) -> None:
        self.cfg, self.llm, self.verifier = cfg, llm, verifier

    def system_prompt(self) -> str:
        return resources.files("bd.prompts").joinpath("pe_portfolio.md").read_text(encoding="utf-8")

    def user_prompt(self, firm: sqlite3.Row, known: list[str]) -> str:
        return (f"Firm: {firm['name']}\nKnown website domain: {firm['domain'] or 'unknown'}\n"
                f"Portfolio page: {firm['portfolio_url'] or 'unknown'}\n"
                f"Illinois counties: {', '.join(self.cfg.fit_gate.counties)}\n"
                f"Return at most {self.cfg.pe_firms.portfolio_max_per_firm} companies.\n"
                f"Already known (do not return): {'; '.join(known[:200]) or 'none'}\n\nReturn the JSON object only.")

    def find(self, firm: sqlite3.Row, known: list[str]):
        try:
            resp, res = generate_validated(self.llm, self.system_prompt(), self.user_prompt(firm, known), PortfolioResponse,
                                           retries=self.cfg.llm.json_retries, key="portfolio")
        except LLMError as e:
            return None, set(), e.usage, f"{type(e).__name__}: {e}"
        return resp, res.retrieved_urls, res.usage, None


def run_portfolio(conn: sqlite3.Connection, cfg: Config, finder: PortfolioFinder, enricher: Enricher, firms: list[sqlite3.Row],
                  as_of: date, model: str | None, workers: int | None = None, max_cost: float | None = None,
                  log: Callable[[str], None] = lambda s: None) -> DiscoverSummary:
    """Find each firm's Chicagoland portfolio companies; link the ones the firm's own site lists; research the unknown
    ones with the discovery rules (auto-add / queue / reject). Cost is logged as pe_portfolio and discover_enrich."""
    workers = max(1, workers or cfg.llm.concurrency)
    cap = cfg.llm.max_cost_per_run_usd if max_cost is None else max_cost
    out = DiscoverSummary()
    out.segments = len(firms)

    def spend(u: Usage) -> float | None:
        cost = estimate_cost(u, model, cfg) if u.total_tokens else None
        out.tokens += u.total_tokens
        out.est_cost += cost or 0.0
        out.unpriced_calls += int(cost is None and u.total_tokens > 0)
        return cost

    names, domains = _known(conn)
    by_name = {_norm_name(r["name"]): r["id"] for r in conn.execute("SELECT id, name FROM accounts")}
    by_domain = {r["domain"]: r["id"] for r in conn.execute("SELECT id, domain FROM accounts WHERE domain IS NOT NULL")}
    known_list = sorted(r["name"] for r in conn.execute("SELECT name FROM accounts"))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(lambda f: (f, finder.find(f, known_list)), firms))
    new: list[Found] = []
    for firm, (resp, retrieved, usage, err) in results:
        cost = spend(usage)
        out.errors += bool(err)
        found = resp.portfolio[: cfg.pe_firms.portfolio_max_per_firm] if resp else []
        with conn:
            conn.execute("INSERT INTO runs(run_date,step,account_id,accounts_scanned,signals_found,tokens,est_cost,errors) "
                         "VALUES (?,?,?,?,?,?,?,?)", (as_of.isoformat(), "pe_portfolio", None, 1, len(found), usage.total_tokens, cost, err))
            if not err:
                conn.execute("UPDATE pe_firms SET last_portfolio = ? WHERE id = ?", (as_of.isoformat(), firm["id"]))
            fd = normalize_domain(resp.firm_domain) if resp and resp.firm_domain else ""
            if fd and not firm["domain"] and is_valid_domain(fd) and not host_in(fd, cfg.sources.blocked_domains) \
                    and finder.verifier.verify(f"https://{fd}").ok:
                conn.execute("UPDATE pe_firms SET domain = ? WHERE id = ?", (fd, firm["id"]))
                firm = conn.execute("SELECT * FROM pe_firms WHERE id = ?", (firm["id"],)).fetchone()
        log(f"  {firm['name']}: {len(found)} portfolio companies" + (f"  ERROR {err}" if err else ""))
        for c in found:
            out.found += 1
            if host_in(hostname(c.source_url), cfg.sources.blocked_domains) or normalize_url(c.source_url) not in retrieved:
                out.skipped_source += 1
                continue
            if not finder.verifier.verify(c.source_url).ok:
                out.skipped_source += 1
                continue
            st = reconcile_source_type(c.source_type, c.source_url, firm["domain"], cfg)
            verified_listing = st in _VERIFIED_LISTING
            dom = normalize_domain(c.domain) if c.domain else ""
            account_id = by_name.get(_norm_name(c.name)) or (by_domain.get(dom) if dom else None)
            if account_id is not None:                         # already an account: record the link, no research cost
                out.skipped_known += 1
                if verified_listing:
                    with conn:
                        link_company(conn, firm["id"], c.name, dom or None, c.source_url, as_of, account_id,
                                     "exited" if c.exited else "current")
                continue
            if _norm_name(c.name) in names or (dom and dom in domains):
                out.skipped_known += 1                          # a queued/rejected candidate or an exclusion
                continue
            names.add(_norm_name(c.name))
            if dom:
                domains.add(dom)
            new.append(Found(c, f"PE: {firm['name']}", firm["id"], c.source_url if verified_listing else None, c.exited))
    if cap is not None and out.est_cost >= cap:
        out.budget_hit = True
        return out
    new = new[: cfg.discovery.max_candidates_per_run]
    research_and_add(conn, cfg, finder.verifier, enricher, new, as_of, workers, out, spend, log)
    out.budget_hit = cap is not None and out.est_cost >= cap
    return out
