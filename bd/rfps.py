"""Open public RFPs: a market-level feed of solicitations from Chicago-area agencies.

Unlike company signals these are not about an account, so they are stored in their own table, shown on their own page and never
scored. One web search per agency group (config.yaml `rfps.groups`), restricted to that group's own domains. Claude reports what
it found; code keeps a solicitation only when:
  * the URL is on one of the group's domains (and not a blocked domain),
  * the URL was in the search results this session, and resolves when code fetches it,
  * it states a due date that has not passed and is not implausibly far away,
  * the summary is within the word limit.
Nothing is recorded when a source cannot be retrieved."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from importlib import resources

from pydantic import BaseModel

from bd.budget import Reserve
from bd.config import Config, RfpGroup
from bd.cost import estimate_cost
from bd.llm import LLM, LLMError, Usage, generate_validated
from bd.rfp_store import RfpExtract, store_rfps
from bd.urls import host_in, hostname, normalize_url
from bd.verify import UrlVerifier


class RfpResponse(BaseModel):
    rfps: list[RfpExtract]


@dataclass
class GroupOutcome:
    group: RfpGroup
    rfps: list[RfpExtract] = field(default_factory=list)
    rejections: list[tuple[str, str]] = field(default_factory=list)      # (reason, url)
    usage: Usage = field(default_factory=Usage)
    error: str | None = None


class RfpFinder:
    def __init__(self, cfg: Config, llm: LLM, verifier: UrlVerifier) -> None:
        self.cfg, self.llm, self.verifier = cfg, llm, verifier

    def system_prompt(self) -> str:
        return resources.files("bd.prompts").joinpath("rfps.md").read_text(encoding="utf-8")

    def user_prompt(self, group: RfpGroup, as_of: date) -> str:
        return (f"Agency group: {group.name}\nWebsites searched: {', '.join(group.domains)}\nToday's date: {as_of.isoformat()}\n\n"
                "Find open solicitations per your instructions. Return the JSON object only.")

    def find(self, group: RfpGroup, as_of: date) -> GroupOutcome:
        out = GroupOutcome(group)
        try:
            resp, res = generate_validated(
                self.llm, self.system_prompt(), self.user_prompt(group, as_of), RfpResponse, retries=self.cfg.llm.json_retries,
                key="rfps", llm_kwargs={"allowed_domains": list(group.domains), "max_uses": self.cfg.rfps.max_searches})
        except LLMError as e:
            out.error = f"{type(e).__name__}: {e}"
            out.usage = e.usage
            return out
        out.usage = res.usage
        seen: set[str] = set()
        canon = {s.lower(): s for s in self.cfg.service_lines}
        for r in resp.rfps:
            url = r.source_url
            bad = self._problem(r, group, as_of, res.retrieved_urls)
            if bad:
                out.rejections.append((bad, url))
                continue
            key = normalize_url(url)
            if key in seen:
                out.rejections.append(("duplicate_in_batch", url))
                continue
            seen.add(key)
            lines = [canon[x.strip().lower()] for x in r.service_lines if x.strip().lower() in canon]
            out.rfps.append(r.model_copy(update={"service_lines": list(dict.fromkeys(lines))}))
        return out

    def _problem(self, r: RfpExtract, group: RfpGroup, as_of: date, retrieved: set[str]) -> str | None:
        host = hostname(r.source_url)
        if host_in(host, self.cfg.sources.blocked_domains):
            return "blocked_domain"
        if not host_in(host, group.domains):
            return f"not_an_agency_page:{host}"
        if r.due_date < as_of:
            return "closed"
        if r.due_date > as_of + timedelta(days=self.cfg.rfps.max_due_days):
            return "due_date_implausible"
        if normalize_url(r.source_url) not in retrieved:
            return "url_not_retrieved"
        v = self.verifier.verify(r.source_url)
        return None if v.ok else f"unreachable:{v.reason}"


@dataclass
class RfpRunSummary:
    groups: int = 0
    new: int = 0
    known: int = 0
    tokens: int = 0
    est_cost: float = 0.0
    unpriced_calls: int = 0
    errors: int = 0
    budget_hit: bool = False


def run_rfps(conn: sqlite3.Connection, cfg: Config, finder: RfpFinder, as_of: date, model: str | None,
             groups: list[RfpGroup] | None = None, max_cost: float | None = None,
             log: Callable[[str], None] = lambda s: None) -> RfpRunSummary:
    """One call per group, one at a time (so the limit cannot be overshot by calls in flight)."""
    cap = cfg.llm.max_cost_per_run_usd if max_cost is None else max_cost
    out = RfpRunSummary()
    reserve = Reserve(cfg.llm.call_reserve_usd)
    for g in (groups if groups is not None else cfg.rfps.groups):
        if not reserve.allows(out.est_cost, 0, cap) and out.groups:
            out.budget_hit = True
            break
        r = finder.find(g, as_of)
        cost = estimate_cost(r.usage, model, cfg) if r.usage.total_tokens else None
        reserve.note(cost, r.usage.web_searches > 0)
        out.groups += 1
        out.tokens += r.usage.total_tokens
        out.est_cost += cost or 0.0
        out.unpriced_calls += int(cost is None and r.usage.total_tokens > 0)
        out.errors += bool(r.error)
        new, known = store_rfps(conn, g.name, r.rfps, as_of)
        out.new += new
        out.known += known
        with conn:
            conn.execute("INSERT INTO runs(run_date,step,account_id,accounts_scanned,signals_found,tokens,est_cost,errors) "
                         "VALUES (?,?,?,?,?,?,?,?)", (as_of.isoformat(), "rfps", None, 0, new, r.usage.total_tokens, cost,
                                                      r.error or (f"{len(r.rejections)} rejected" if r.rejections else None)))
        log(f"  {g.name}: {new} new, {known} known, {len(r.rejections)} rejected, {r.usage.total_tokens:,} tokens, "
            + ("unpriced" if cost is None else f"${cost:.4f}") + (f"  ERROR {r.error}" if r.error else ""))
    return out
