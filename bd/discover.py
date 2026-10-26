"""bd discover: market research that finds Chicagoland companies and adds the ones that clear a strict bar.

Claude only names candidates and says where it saw them. Code decides everything else:
  * a candidate is researched only if its name and domain are new (not an account, an exclusion or an earlier candidate),
    its source URL was actually returned by the search tool, is not on the blocked list, and resolves;
  * its facts come from `bd.enrich.Enricher`, so every fact (Illinois county, size, ownership) needs verified evidence;
  * auto-add needs: the fit gate says `pass` on those facts, a verified domain, and room under the per-run auto-add cap.
    Anything else is queued for a person (`unknown`) or recorded as rejected (`fail`) so it is never researched twice;
  * auto-added accounts are tier B, marked `reviewed = 0`, and scanned only up to `discovery.scan_unreviewed_per_run`
    per run until a person marks them reviewed;
  * cost is logged per call and the per-run cap stops the run."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from importlib import resources

from pydantic import BaseModel, field_validator

from bd.accounts import Account, is_valid_domain, list_accounts, normalize_domain, parse_fields
from bd.budget import Reserve
from bd.config import Config
from bd.cost import estimate_cost
from bd.curate import create_account
from bd.enrich import Enricher
from bd.fit_gate import check_fit, is_competitor, load_exclusions
from bd.llm import LLM, LLMError, Usage, generate_validated, search_failure
from bd.models import SourceType
from bd.urls import host_in, hostname, normalize_url
from bd.verify import UrlVerifier, homepage_ok

KNOWN_NAMES_IN_PROMPT = 200


class Company(BaseModel):
    name: str
    domain: str | None = None
    hq_city: str | None = None
    hq_state: str | None = None
    source_url: str
    source_type: SourceType

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v or len(v) > 120:
            raise ValueError("name must be 1-120 characters")
        return v


class DiscoverResponse(BaseModel):
    companies: list[Company] = []
    note: str = ""                       # why the list is empty, when it is (shown in the run log; never stored or scored)


BLOCKED_NOTE = "source blocks our reader ({reason}): the search returned the page, code could not read it"


@dataclass
class Found:
    company: Company
    segment: str
    firm_id: int | None = None       # set when the company came from a PE firm's portfolio page
    link_source: str | None = None   # verified portfolio listing (on the firm's own site) to record as a link
    exited: bool = False
    source_note: str | None = None   # set when the source answered 401/403/429 to our reader: kept, but never shown as page-checked


@dataclass
class DiscoverSummary:
    segments: int = 0
    found: int = 0
    skipped_known: int = 0
    skipped_competitor: int = 0       # on the competitor list (fit_gate.competitors): not researched, not added, not queued
    rejected_queued: list[str] = field(default_factory=list)   # queued candidates the competitor list rejected at the start of the run
    skipped_source: int = 0
    kept_blocked: list[str] = field(default_factory=list)      # kept although their source blocks our reader (the search returned it)
    kept_blocked_domain: list[str] = field(default_factory=list)    # their own homepage answered 401/403/429, so the domain was kept unread
    researched: int = 0
    added: list[str] = field(default_factory=list)
    queued: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    errors: int = 0
    tokens: int = 0
    est_cost: float = 0.0
    unpriced_calls: int = 0
    budget_hit: bool = False
    segments_skipped: int = 0         # segment searches never started because the spending limit would have been passed


def _norm_name(n: str) -> str:
    return " ".join(n.lower().replace(",", " ").replace(".", " ").split())


def pick_segments(conn: sqlite3.Connection, segments: list[str], limit: int | None) -> list[str]:
    """The segments to search this run: never searched first, then the least recently searched, the configured order breaking ties. With a limit below the
    number of segments this rotates through all of them (it used to take the first N every time, so the last ones were never searched)."""
    last = {r["segment"]: r["last_searched"] for r in conn.execute("SELECT segment, last_searched FROM discovery_segments")}
    order = sorted(range(len(segments)), key=lambda i: (segments[i] in last, last.get(segments[i], ""), i))
    picked = [segments[i] for i in order]
    return picked[:limit] if limit else picked


def note_segment(conn: sqlite3.Connection, segment: str, as_of: date, found: int) -> None:
    """Remember a segment was searched. Only a search that worked counts: one that errored is tried again next run."""
    with conn:
        conn.execute("INSERT INTO discovery_segments(segment, last_searched, searches, last_found) VALUES (?,?,1,?) "
                     "ON CONFLICT(segment) DO UPDATE SET last_searched = excluded.last_searched, searches = searches + 1, last_found = excluded.last_found",
                     (segment, as_of.isoformat(), found))


def reject_queued_competitors(conn: sqlite3.Connection, cfg: Config) -> list[str]:
    """Queued candidates that are on the competitor list are rejected (a person would only have to do it), and the audit log says so."""
    from bd.accounts import log_audit

    done = []
    for r in conn.execute("SELECT id, name, domain FROM candidates WHERE status = 'queued'").fetchall():
        if is_competitor(r["name"], r["domain"], cfg):
            with conn:
                conn.execute("UPDATE candidates SET status = 'rejected', reason = COALESCE(reason, '') || ' | competitor of the firm (fit_gate.competitors)' WHERE id = ?", (r["id"],))
                log_audit(conn, "discover", "update", None, "candidate_rejected", None, r["id"])
            done.append(r["name"])
    return done


class Discoverer:
    def __init__(self, cfg: Config, llm: LLM, verifier: UrlVerifier) -> None:
        self.cfg, self.llm, self.verifier = cfg, llm, verifier
        self.notes: dict[str, str] = {}              # segment -> the model's own reason for an empty list

    def system_prompt(self) -> str:
        return resources.files("bd.prompts").joinpath("discover.md").read_text(encoding="utf-8")

    def user_prompt(self, segment: str, known: list[str]) -> str:
        g = self.cfg.fit_gate
        d, pe = g.size_floor["default"], g.size_floor["pe-backed"]
        return (f"Segment: {segment}\nCounties (Illinois): {', '.join(g.counties)}\n"
                f"Size guide: annual revenue of about ${d.revenue_usd / 1e6:,.0f}M or more, or about {d.employees:,} employees or more "
                f"(private-equity-owned companies: about ${pe.revenue_usd / 1e6:,.0f}M revenue or more).\n"
                f"Return at most {self.cfg.discovery.per_segment} companies.\n"
                f"Already known (do not return): {'; '.join(known[:KNOWN_NAMES_IN_PROMPT]) or 'none'}\n\n"
                "Return the JSON object only.")

    def find(self, segment: str, known: list[str]) -> tuple[list[Company], set[str], Usage, str | None]:
        """One search call. Returns (companies whose source verified, retrieved urls, usage, error)."""
        try:
            resp, res = generate_validated(self.llm, self.system_prompt(), self.user_prompt(segment, known),
                                           DiscoverResponse, retries=self.cfg.llm.json_retries, key="companies")
        except LLMError as e:
            return [], set(), e.usage, f"{type(e).__name__}: {e}"
        if not resp.companies and (failed := search_failure(res)):       # a segment the search tool could not search is not an empty segment
            return [], res.retrieved_urls, res.usage, failed
        self.notes[segment] = " ".join((resp.note or "").split())[:300]
        return resp.companies[: self.cfg.discovery.per_segment], res.retrieved_urls, res.usage, None

    def source_ok(self, c: Company, retrieved: set[str]) -> tuple[str | None, str | None]:
        """(why not, note): why not is None when the discovery source is acceptable. A source the search tool itself returned that answers 401, 403 or 429 to our
        reader is accepted with a note saying so (the status line is all the verifier ever reads, so the search result is what proves the page exists); a source
        that was not in the search results, is on a blocked domain, or is missing, down or unresolvable is not."""
        if host_in(hostname(c.source_url), self.cfg.sources.blocked_domains):
            return "source on a blocked domain", None
        if normalize_url(c.source_url) not in retrieved:
            return "source URL was not in the search results", None
        v = self.verifier.verify(c.source_url)
        if v.ok:
            return None, None
        if v.blocks_reader:
            return None, BLOCKED_NOTE.format(reason=v.reason)
        return f"source unreachable ({v.reason})", None


def _known(conn: sqlite3.Connection) -> tuple[set[str], set[str]]:
    names, domains = set(), set()
    for r in conn.execute("SELECT name, domain FROM accounts UNION ALL SELECT name, domain FROM candidates "
                          "UNION ALL SELECT name, domain FROM exclusions"):
        names.add(_norm_name(r["name"]))
        if r["domain"]:
            domains.add(r["domain"])
    return names, domains


def _log(conn: sqlite3.Connection, as_of: date, step: str, account_id: int | None, tokens: int, cost: float | None,
         err: str | None, found: int = 0) -> None:
    with conn:
        conn.execute("INSERT INTO runs(run_date,step,account_id,accounts_scanned,signals_found,tokens,est_cost,errors) "
                     "VALUES (?,?,?,?,?,?,?,?)", (as_of.isoformat(), step, account_id, 0, found, tokens, cost, err))


def run_discover(conn: sqlite3.Connection, cfg: Config, discoverer: Discoverer, enricher: Enricher, as_of: date,
                 model: str | None, segments: list[str] | None = None, workers: int | None = None,
                 max_cost: float | None = None, log: Callable[[str], None] = lambda s: None) -> DiscoverSummary:
    workers = max(1, workers or cfg.llm.concurrency)
    cap = cfg.llm.max_cost_per_run_usd if max_cost is None else max_cost
    dc = cfg.discovery
    out = DiscoverSummary()

    def spend(u: Usage) -> float | None:
        cost = estimate_cost(u, model, cfg) if u.total_tokens else None
        out.tokens += u.total_tokens
        out.est_cost += cost or 0.0
        out.unpriced_calls += int(cost is None and u.total_tokens > 0)
        return cost

    # ---- phase 1: find candidates, one search call per segment ------------------------------------------------
    segs = segments if segments is not None else dc.segments
    out.segments = len(segs)
    out.rejected_queued = reject_queued_competitors(conn, cfg)
    if out.rejected_queued:
        log(f"  {len(out.rejected_queued)} queued candidate(s) are on the competitor list and were rejected: {', '.join(out.rejected_queued)}")
    searched = {r["segment"]: r["last_searched"] for r in conn.execute("SELECT segment, last_searched FROM discovery_segments")}
    names, domains = _known(conn)
    known_list = sorted({r["name"] for r in conn.execute("SELECT name FROM accounts")})
    new: list[Found] = []
    # Each segment search is one paid call (about $0.35), so they start in small batches and only while the spending limit would hold with
    # every running search finished; an update's last few dollars are no longer spent on searches that were never allowed.
    reserve, pending = Reserve(), list(segs)
    results: list = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        while pending:
            batch: list[str] = []
            while pending and len(batch) < workers and reserve.allows(out.est_cost, len(batch), cap):
                batch.append(pending.pop(0))
            if not batch:
                break
            for seg, found in zip(batch, ex.map(lambda s: discoverer.find(s, known_list), batch)):
                cost = spend(found[2])
                reserve.note(cost)
                results.append((seg, found, cost))
    out.segments_skipped = len(pending)
    if pending:
        out.budget_hit = True
    for seg, (companies, retrieved, usage, err), cost in results:
        _log(conn, as_of, "discover", None, usage.total_tokens, cost, err, len(companies))
        out.errors += bool(err)
        if not err:
            note_segment(conn, seg, as_of, len(companies))
        note = getattr(discoverer, "notes", {}).get(seg, "")
        log(f"  segment '{seg}': {len(companies)} companies, {usage.total_tokens:,} tokens (" + (f"last searched {searched[seg]}" if seg in searched else "not searched before") + ")"
            + (f"  ERROR {err}" if err else "") + (f"\n    model note: {note}" if note and not companies else ""))
        for c in companies:
            out.found += 1
            dom = normalize_domain(c.domain) if c.domain else ""
            if _norm_name(c.name) in names or (dom and dom in domains):
                out.skipped_known += 1
                continue
            if is_competitor(c.name, dom or c.domain, cfg):
                out.skipped_competitor += 1
                log(f"    skipped {c.name}: on the competitor list")
                continue
            why, source_note = discoverer.source_ok(c, retrieved)
            if why:
                out.skipped_source += 1
                log(f"    dropped {c.name}: {why}")
                continue
            if source_note:
                out.kept_blocked.append(c.name)
                log(f"    kept {c.name}: {source_note}")
            names.add(_norm_name(c.name))
            if dom:
                domains.add(dom)
            new.append(Found(c, seg, source_note=source_note))
    if out.budget_hit or (cap is not None and out.est_cost >= cap):
        out.budget_hit = True
        return out

    # ---- phase 2: research each new candidate's facts with the enrich rules ------------------------------------
    new = new[: dc.max_candidates_per_run]
    if not out.budget_hit:
        research_and_add(conn, cfg, discoverer.verifier, enricher, new, as_of, workers, out, spend, log)
    if cap is not None and out.est_cost >= cap:
        out.budget_hit = True
    return out


def research_and_add(conn: sqlite3.Connection, cfg: Config, verifier: UrlVerifier, enricher: Enricher, new: list[Found],
                     as_of: date, workers: int, out: DiscoverSummary,
                     spend: Callable[[Usage], float | None],
                     log: Callable[[str], None] = lambda s: None, accept_unverified_size: bool = False) -> dict[str, int | None]:
    """Research each new company's facts (verified evidence only), then auto-add / queue / reject it. Shared by market
    discovery and PE portfolio research. Returns {company name: account id or None}.

    `accept_unverified_size` (private-equity promotion only): a company the research shows to be in Chicagoland whose only open question is its size is added
    all the same, as an unverified account that is scanned like any other; an unconfirmed location is never accepted, and nothing else about the gate changes."""
    from bd.pe_store import link_company

    dc = cfg.discovery
    exclusions = load_exclusions(conn)

    def research(f: Found):
        dom = normalize_domain(f.company.domain) if f.company.domain else ""
        hint = note = None
        if dom and is_valid_domain(dom) and not host_in(dom, cfg.sources.blocked_domains):
            why, note = homepage_ok(verifier, dom)
            hint = None if why else dom
            note = note if hint else None
        city = ", ".join(x for x in (f.company.hq_city, f.company.hq_state) if x) or None
        res = enricher.enrich(Account(id=0, name=f.company.name, domain=hint, hq_city=city), as_of)
        return hint, note or (res.domain_note if not hint and res.row.get("domain") else None), res

    with ThreadPoolExecutor(max_workers=workers) as ex:
        researched = list(ex.map(research, new))
    auto_added = 0
    ids: dict[str, int | None] = {}
    for f, (hint, home_note, r) in zip(new, researched):
        out.researched += 1
        if home_note:
            out.kept_blocked_domain.append(f.company.name)
            log(f"    kept the domain of {f.company.name}: {home_note}")
        cost = spend(r.usage)
        facts = {**({"domain": hint} if hint else {}), **{k: v for k, v in r.row.items() if k != "name"}}
        form = {k: v for k, v in facts.items() if k in ("domain", "hq_city", "chicagoland", "revenue", "employees",
                                                        "ownership", "sponsor", "industry", "cik")}
        form["tier"] = "B"
        data, errs = parse_fields(form)
        verdict = None
        if not r.error and not errs:
            verdict = check_fit(Account(id=0, name=f.company.name, **{k: v for k, v in data.items()}), exclusions, cfg)
        reason = r.error or "; ".join(errs) or "; ".join(verdict.reasons if verdict else [])
        status = "queued"
        account_id = None
        size_only = bool(verdict and verdict.verdict == "unknown" and data.get("chicagoland") == "yes" and verdict.reasons
                         and all(x.startswith("size unverified") for x in verdict.reasons))
        accepted = bool(verdict and (verdict.verdict == "pass" or (accept_unverified_size and size_only)))
        if verdict and verdict.verdict == "fail":
            status = "rejected"
        elif accepted and data.get("domain") and auto_added < dc.max_auto_add_per_run:
            res = create_account(conn, cfg, f.company.name, form, actor="discover",
                                 extra={"origin": "discovered", "source_url": f.company.source_url, "reviewed": 0,
                                        "discovered_at": as_of.isoformat()})
            if res.ok:
                status, account_id, auto_added = "added", res.account_id, auto_added + 1
            else:
                reason = "; ".join(res.errors)
        elif accepted:
            reason = "passed the fit gate but " + ("no verified domain" if not data.get("domain")
                                                    else "the per-run auto-add cap was reached")
        if r.error:
            out.errors += 1
        stored_reason = "; ".join(x for x in (f.source_note, home_note, reason) if x)           # the notes travel with the company, whatever happens to it
        (out.added if status == "added" else out.rejected if status == "rejected" else out.queued).append(f.company.name)
        ids[f.company.name] = account_id
        _log(conn, as_of, "discover_enrich", account_id, r.usage.total_tokens, cost, r.error)
        with conn:
            conn.execute("INSERT OR IGNORE INTO candidates(name,domain,segment,source_url,found_date,status,reason,facts_json,account_id) "
                         "VALUES (?,?,?,?,?,?,?,?,?)",
                         (f.company.name, data.get("domain"), f.segment, f.company.source_url, as_of.isoformat(), status,
                          stored_reason or None, json.dumps({**form, **{k: v for k, v in r.row.items() if k in ("evidence_urls", "notes")}}),
                          account_id))
            if f.firm_id is not None and f.link_source:
                link_company(conn, f.firm_id, f.company.name, data.get("domain"), f.link_source, as_of, account_id,
                             "exited" if f.exited else "current")
        log(f"  {f.company.name}: {status}" + (f" ({reason})" if reason and status != 'added' else ""))
    return ids


def select_scan_accounts(accounts: list[Account], cfg: Config) -> tuple[list[Account], int]:
    """Reviewed accounts always; unreviewed (auto-added) ones only up to the per-run limit, oldest first.
    Returns (accounts to scan, number of unreviewed accounts held back)."""
    unreviewed = [a for a in accounts if not a.reviewed]
    allowed = {a.id for a in unreviewed[: cfg.discovery.scan_unreviewed_per_run]}
    keep = [a for a in accounts if a.reviewed or a.id in allowed]
    return keep, len(unreviewed) - len(allowed)
