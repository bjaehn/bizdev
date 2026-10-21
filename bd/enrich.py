"""bd enrich: PROPOSE missing account facts (domain, Chicagoland, size, ownership, CIK) for a human.

Nothing here writes to the accounts table. Output is a CSV (with the evidence URL for each fact)
that a person reviews and edits, then loads with `bd seed`. Code, not Claude, decides what is
acceptable:
  * every sourced fact needs an evidence URL that the search tool actually returned, that resolves
    when fetched, and that is not on the blocked list;
  * revenue and employees are accepted only from a company release, SEC filing or the company's own
    site (a secondary-source figure is dropped, and mentioned in notes);
  * Chicagoland = "yes" is derived by code from the Illinois county (one of the six configured) -
    Claude's opinion is not used, and "no" is never proposed automatically;
  * a CIK must appear in a sec.gov evidence URL and be confirmed by EDGAR itself (its registered
    name is written to the CSV so the human can check it is the right company);
  * fields the account already has are never proposed, so human-entered facts are not overwritten."""

from __future__ import annotations

import csv
import re
import sqlite3
import threading
from collections import deque
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import date
from importlib import resources
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, field_validator

from bd.accounts import PROPOSAL_ONLY_COLUMNS, SEED_COLUMNS, Account, normalize_domain
from bd.budget import Reserve
from bd.config import Config
from bd.cost import estimate_cost
from bd.edgar import EdgarClient, EdgarError, facts_url, submissions_url
from bd.sec_facts import read as read_sec_facts
from bd.sec_ids import TICKERS_URL, Index, Listing, build_index
from bd.llm import LLM, LLMError, Usage, generate_validated, search_failure
from bd.models import SourceType
from bd.scanners.base import reconcile_source_type
from bd.urls import host_in, hostname, normalize_url
from bd.verify import UrlVerifier

EVIDENCE_FIELDS = ("il_county", "revenue_usd", "employees", "ownership", "sponsor", "cik")
_HIGH_SOURCES = {"company_release", "sec_filing", "company_site", "warn_notice"}
CSV_COLUMNS = SEED_COLUMNS + PROPOSAL_ONLY_COLUMNS


class Evidence(BaseModel):
    url: str
    source_type: SourceType


class Facts(BaseModel):
    domain: str | None = None
    hq_city: str | None = None
    hq_state: str | None = None
    il_county: str | None = None
    revenue_usd: float | None = None
    employees: int | None = None
    ownership: Literal["public", "private", "pe-backed"] | None = None
    sponsor: str | None = None
    industry: str | None = None
    cik: str | None = None
    notes: str | None = None
    evidence: dict[str, Evidence] = {}

    @field_validator("evidence")
    @classmethod
    def _known_keys(cls, v: dict[str, Evidence]) -> dict[str, Evidence]:
        bad = set(v) - set(EVIDENCE_FIELDS)
        if bad:
            raise ValueError(f"evidence keys must be among {EVIDENCE_FIELDS}; got {sorted(bad)}")
        return v


class EnrichResponse(BaseModel):
    facts: Facts


@dataclass
class EnrichResult:
    account: Account
    row: dict[str, str] = field(default_factory=dict)      # CSV row (proposals only)
    dropped: list[str] = field(default_factory=list)       # what was found but not accepted, and why
    missing: list[str] = field(default_factory=list)       # facts still needed that the model did not return at all
    sec_notes: list[str] = field(default_factory=list)     # what code read from the SEC's own data (or why it could not)
    usage: Usage = field(default_factory=Usage)
    error: str | None = None


def needed_facts(a: Account, from_sec: bool = False) -> list[str]:
    """The facts this account still lacks, in the model's field names (what the search should be about). `from_sec`: the account has a
    CIK and an SEC client is available, so revenue, ownership and the CIK come from the SEC's own data and the model is not asked."""
    need = []
    if not a.domain:
        need.append("domain")
    if not a.hq_city:
        need.append("hq_city and hq_state")
    if not a.industry:
        need.append("industry")
    if a.chicagoland == "unknown":
        need.append("il_county")
    if a.revenue is None and not from_sec:
        need.append("revenue_usd")
    if a.employees is None:
        need.append("employees")
    if not a.ownership and not from_sec:
        need.append("ownership")
    if not a.sponsor and a.ownership in (None, "pe-backed") and not from_sec:
        need.append("sponsor")
    if not a.cik and a.ownership in (None, "public"):
        need.append("cik")
    return need


def _parse_evidence(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (raw or "").split(";"):
        key, sep, url = part.strip().partition("=")
        if sep and key.strip():
            out[key.strip()] = url.strip()
    return out


def _county(raw: str, configured: list[str]) -> str:
    """Normalise "Cook County" / "dupage" to the configured spelling ("DuPage"); unknown names pass through."""
    name = re.sub(r"\s+county$", "", raw.strip(), flags=re.I).strip()
    return {c.lower(): c for c in configured}.get(name.lower(), name)


class Enricher:
    def __init__(self, cfg: Config, llm: LLM, verifier: UrlVerifier, edgar: EdgarClient | None) -> None:
        self.cfg, self.llm, self.verifier, self.edgar = cfg, llm, verifier, edgar
        self.no_lookup: set[int] = set()            # accounts whose CIK a person removed: never matched from the SEC list again
        self._ids: Index | None = None
        self._ids_loaded = False
        self._ids_lock = threading.Lock()          # enrichment runs on worker threads; the SEC list is fetched once

    def _listing_for(self, account: Account) -> Listing | None:
        """The SEC filer whose registered name is this company's name (a unique exact match, see bd.sec_ids), for a company with no
        CIK that may be public. Costs nothing and asks the model nothing; None when there is no client, no list or no match."""
        e = self.edgar
        if account.cik or account.id in self.no_lookup or account.ownership not in (None, "public") or e is None \
                or not hasattr(e, "company_tickers"):
            return None
        with self._ids_lock:
            if not self._ids_loaded:
                self._ids_loaded = True
                try:
                    self._ids = build_index(e.company_tickers())
                except EdgarError:
                    self._ids = None
        return self._ids.lookup(account.name).listing if self._ids else None

    def system_prompt(self) -> str:
        return resources.files("bd.prompts").joinpath("enrich.md").read_text(encoding="utf-8")

    def enrich(self, account: Account, as_of: date) -> EnrichResult:
        res = EnrichResult(account=account, row={"name": account.name})
        found = self._listing_for(account)
        if found:                                       # the SEC identified it: revenue, ownership and the CIK need no web search
            account = account.model_copy(update={"cik": found.cik})
        sec_ready = self.edgar is not None and hasattr(self.edgar, "company_facts") and bool(account.cik)
        need = needed_facts(account, from_sec=sec_ready)
        user = (f"Company: {account.name}\nKnown website domain: {account.domain or 'unknown'}\n"
                f"Known headquarters city: {account.hq_city or 'unknown'}\nToday's date: {as_of.isoformat()}\n"
                f"Facts still needed: {', '.join(need) or 'none'}. Search for these; the others are already on file, so return null for them.\n\n"
                "Find the facts per your instructions. Return the JSON object only.")
        try:
            resp, llm_res = generate_validated(self.llm, self.system_prompt(), user, EnrichResponse,
                                               retries=self.cfg.llm.json_retries, key="facts")
        except LLMError as e:
            res.usage, res.error = e.usage, f"{type(e).__name__}: {e}"
            return res
        res.usage = llm_res.usage
        self._accept(account, resp.facts, llm_res.retrieved_urls, res)
        if found:
            self._propose_listed_cik(found, res)
        self._fill_from_sec(account, as_of, res)
        got = set(res.row)
        failed = search_failure(llm_res)
        if failed and not got & set(SEED_COLUMNS[1:]):        # nothing proposed AND the search tool failed: the company was not looked up, which is not the same as "not found"
            res.error, res.missing = failed, []
            res.row["notes"] = "not looked up: " + failed.split(" (")[0]
            return res
        want = {"domain": "domain", "hq_city and hq_state": "hq_city", "industry": "industry", "il_county": "chicagoland",
                "revenue_usd": "revenue", "employees": "employees", "ownership": "ownership", "sponsor": "sponsor", "cik": "cik"}
        found_but_dropped = " ".join(res.dropped)
        own = account.ownership or res.row.get("ownership")
        res.missing = [n for n in need if want[n] not in got and want[n] not in found_but_dropped and n.split(" ")[0] not in found_but_dropped
                       and not (n == "sponsor" and own != "pe-backed") and not (n == "cik" and own not in (None, "public"))]
        if res.missing and res.row.get("notes") is None:
            res.row["notes"] = "not found: " + ", ".join(m.split(" ")[0] for m in res.missing)
        elif res.missing:
            res.row["notes"] += " | not found: " + ", ".join(m.split(" ")[0] for m in res.missing)
        return res

    @staticmethod
    def _propose_listed_cik(found: Listing, res: EnrichResult) -> None:
        evid = _parse_evidence(res.row.get("evidence_urls", ""))
        evid["cik"] = TICKERS_URL
        res.row["cik"] = found.cik
        res.row["sec_name"] = found.label
        res.row["evidence_urls"] = "; ".join(f"{k}={v}" for k, v in evid.items())
        res.sec_notes.append(f"CIK matched by exact name to the SEC filer {found.label}")

    def _fill_from_sec(self, acct: Account, as_of: date, res: EnrichResult) -> None:
        """For a company with a CIK (already on file, or just proposed and confirmed with EDGAR): revenue, public ownership and the HQ
        city come from the SEC's own data, fetched by code, so each carries the SEC URL it was read from. Fills only what the account
        lacks; where the model also proposed revenue or ownership, the SEC's value wins (it is the filing itself)."""
        e = self.edgar
        cik = acct.cik or res.row.get("cik")
        if e is None or not cik or not hasattr(e, "company_facts") or not hasattr(e, "submissions"):
            return
        row, evid = res.row, _parse_evidence(res.row.get("evidence_urls", ""))
        need_rev = acct.revenue is None
        subs = facts = None
        try:
            subs = e.submissions(cik)
        except EdgarError as err:
            res.sec_notes.append(f"SEC record not read ({err})")
        if need_rev:
            try:
                facts = e.company_facts(cik)
            except EdgarError as err:
                res.sec_notes.append(f"SEC financial data not read ({err})")
        sec = read_sec_facts(subs, facts, as_of, need_revenue=need_rev)
        res.sec_notes += sec.notes
        if sec.revenue is not None and need_rev:
            row["revenue"] = f"{sec.revenue:.0f}"
            evid["revenue_usd"] = facts_url(cik)
            res.sec_notes.append(sec.revenue_note)
        if sec.public and not acct.ownership:
            row["ownership"] = "public"
            evid["ownership"] = submissions_url(cik)
        if sec.hq and not acct.hq_city and "hq_city" not in row:
            row["hq_city"] = sec.hq
        if evid:
            row["evidence_urls"] = "; ".join(f"{k}={v}" for k, v in evid.items())
        if res.sec_notes:
            row["notes"] = " | ".join(x for x in (row.get("notes"), "SEC: " + "; ".join(res.sec_notes)) if x)

    # -- acceptance rules -----------------------------------------------------------------------
    def _evidence_ok(self, name: str, f: Facts, retrieved: set[str], res: EnrichResult) -> Evidence | None:
        ev = f.evidence.get(name)
        if ev is None:
            res.dropped.append(f"{name}: no evidence URL given")
            return None
        if host_in(hostname(ev.url), self.cfg.sources.blocked_domains):
            res.dropped.append(f"{name}: evidence on a blocked domain")
            return None
        if normalize_url(ev.url) not in retrieved:
            res.dropped.append(f"{name}: evidence URL was not in the search results")
            return None
        v = self.verifier.verify(ev.url)
        if not v.ok:
            res.dropped.append(f"{name}: evidence URL unreachable ({v.reason})")
            return None
        return ev

    def _accept(self, acct: Account, f: Facts, retrieved: set[str], res: EnrichResult) -> None:
        row, evid = res.row, []

        def put(col: str, value: object) -> None:
            row[col] = str(value)

        domain = None
        if f.domain:
            d = normalize_domain(f.domain)
            v = self.verifier.verify(f"https://{d}") if d else None
            if d and not host_in(d, self.cfg.sources.blocked_domains) and v and v.ok:
                domain = d
                if not acct.domain:
                    put("domain", d)
            else:
                res.dropped.append(f"domain: {f.domain} did not resolve or is not allowed")
        site = acct.domain or domain

        if f.hq_city and not acct.hq_city:
            put("hq_city", f.hq_city + (f", {f.hq_state}" if f.hq_state else ""))
        if f.industry and not acct.industry:
            put("industry", f.industry)

        if f.il_county and acct.chicagoland == "unknown":
            ev = self._evidence_ok("il_county", f, retrieved, res)
            if ev:
                county = _county(f.il_county, self.cfg.fit_gate.counties)
                if county in self.cfg.fit_gate.counties:
                    put("chicagoland", "yes")
                    evid.append(f"il_county={ev.url}")
                else:
                    res.dropped.append(f"il_county: {county} is not one of {self.cfg.fit_gate.counties}; "
                                       "left unknown for a human (a 'no' is never proposed automatically)")

        for name, col in (("revenue_usd", "revenue"), ("employees", "employees")):
            val = getattr(f, name)
            known = getattr(acct, col)
            if val is None or known is not None:
                continue
            ev = self._evidence_ok(name, f, retrieved, res)
            if not ev:
                continue
            st = reconcile_source_type(ev.source_type, ev.url, site, self.cfg)
            if st not in _HIGH_SOURCES:
                res.dropped.append(f"{name}: only a secondary source ({hostname(ev.url)}); not accepted as size evidence")
                continue
            put(col, int(val) if name == "employees" else f"{val:.0f}")
            evid.append(f"{name}={ev.url}")

        if f.ownership and not acct.ownership:
            ev = self._evidence_ok("ownership", f, retrieved, res)
            if ev:
                put("ownership", f.ownership)
                evid.append(f"ownership={ev.url}")
        if f.sponsor and not acct.sponsor:
            ev = self._evidence_ok("sponsor", f, retrieved, res)
            if ev:
                put("sponsor", f.sponsor)
                evid.append(f"sponsor={ev.url}")

        if f.cik and not acct.cik:
            self._accept_cik(f, retrieved, res, evid)

        if evid:
            row["evidence_urls"] = "; ".join(evid)
        notes = list(res.dropped) + ([f"model note: {f.notes.strip()[:300]}"] if f.notes else [])
        if notes:
            row["notes"] = " | ".join(notes)

    def _accept_cik(self, f: Facts, retrieved: set[str], res: EnrichResult, evid: list[str]) -> None:
        digits = (f.cik or "").strip()
        if not digits.isdigit() or len(digits) > 10 or int(digits) == 0:
            res.dropped.append("cik: not a valid CIK")
            return
        ev = self._evidence_ok("cik", f, retrieved, res)
        if not ev:
            return
        if not host_in(hostname(ev.url), self.cfg.sources.sec_domains) or int(digits) not in {int(x) for x in re.findall(r"\d+", ev.url)}:
            res.dropped.append("cik: evidence must be a sec.gov page containing that CIK")
            return
        if self.edgar is None:
            res.dropped.append("cik: not confirmed with EDGAR (set BD_USER_AGENT to enable); not proposed")
            return
        try:
            name, tickers = self.edgar.company(digits)
        except EdgarError as e:
            res.dropped.append(f"cik: EDGAR could not confirm it ({e})")
            return
        res.row["cik"] = digits.zfill(10)
        res.row["sec_name"] = name + (f" ({', '.join(tickers)})" if tickers else "")
        evid.append(f"cik={ev.url}")


def write_proposals(results: list[EnrichResult], path: str | Path) -> int:
    """Write the review CSV (loadable with `bd seed` after the human has checked it). Returns rows with proposals."""
    n = 0
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        w.writeheader()
        for r in results:
            w.writerow({c: r.row.get(c, "") for c in CSV_COLUMNS})
            n += len(r.row) > 1 and any(k in r.row for k in SEED_COLUMNS[1:])
    return n


def select_accounts(conn: sqlite3.Connection, all_accounts: bool, only: str | None, limit: int | None, exact: bool = False,
                    cfg: Config | None = None) -> list[Account]:
    """Companies whose location or size is still unverified (all of them with `all_accounts`); known out-of-scope ones are never enriched."""
    from bd.accounts import list_accounts
    from bd.config import load_config
    from bd.fit_gate import check_fit, load_exclusions

    exclusions, cfg = load_exclusions(conn), cfg or load_config()
    accts = list_accounts(conn, status=None)
    accts = [a for a in accts if a.status != "excluded" and (all_accounts or check_fit(a, exclusions, cfg).unverified)]
    if only:
        accts = [a for a in accts if (a.name.lower() == only.lower() if exact else only.lower() in a.name.lower())]
    # Never-researched companies first, then the ones tried longest ago, so a hard case that yields nothing cannot use the whole
    # batch on every run while the rest wait.
    last = {r["account_id"]: r["d"] for r in conn.execute(
        "SELECT account_id, MAX(run_date) AS d FROM runs WHERE step = 'enrich' AND account_id IS NOT NULL GROUP BY account_id")}
    accts.sort(key=lambda a: (a.id in last, last.get(a.id, ""), a.id))
    return accts[:limit] if limit else accts


def run_enrich(conn: sqlite3.Connection, cfg: Config, enricher: Enricher, accounts: list[Account], as_of: date,
               model: str | None, workers: int | None = None, max_cost: float | None = None,
               log: Callable[[str], None] = lambda s: None) -> tuple[list[EnrichResult], float, bool]:
    """Concurrent enrichment with the same cost logging and soft budget cap as scans.
    Returns (results in account order, estimated cost, budget_hit)."""
    workers = max(1, workers or cfg.llm.concurrency)
    cap = cfg.llm.max_cost_per_run_usd if max_cost is None else max_cost
    queue = deque(accounts)
    inflight: dict[Future[EnrichResult], Account] = {}
    done_results: dict[int, EnrichResult] = {}
    spent, hit = 0.0, False
    reserve = Reserve(cfg.llm.call_reserve_usd)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        while queue or inflight:
            while queue and len(inflight) < workers and not hit and reserve.allows(spent, len(inflight), cap):
                a = queue.popleft()
                inflight[ex.submit(enricher.enrich, a, as_of)] = a
            if not inflight:
                break
            done, _ = wait(inflight, return_when=FIRST_COMPLETED)
            for fut in done:
                a = inflight.pop(fut)
                try:
                    r = fut.result()
                except Exception as e:   # one bad account must not abort the batch
                    r = EnrichResult(account=a, row={"name": a.name}, error=f"{type(e).__name__}: {e}")
                cost = estimate_cost(r.usage, model, cfg)
                spent += cost or 0.0
                reserve.note(cost, r.usage.web_searches > 0)
                with conn:
                    conn.execute(
                        "INSERT INTO runs(run_date,step,account_id,accounts_scanned,signals_found,tokens,est_cost,errors) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (as_of.isoformat(), "enrich", a.id, 1, 0, r.usage.total_tokens, cost, r.error))
                done_results[a.id] = r
                got = [k for k in r.row if k in SEED_COLUMNS[1:]]
                log(f"  {a.name}: proposed {got or 'nothing'}, {len(r.dropped)} dropped, {len(r.missing)} not found, {r.usage.total_tokens:,} tokens, "
                    + ("unpriced" if cost is None else f"${cost:.4f}") + (f"  ERROR {r.error}" if r.error else ""))
            if cap is not None and queue and (spent >= cap or not reserve.allows(spent, len(inflight), cap)):
                hit = True
    return [done_results[a.id] for a in accounts if a.id in done_results], spent, hit
