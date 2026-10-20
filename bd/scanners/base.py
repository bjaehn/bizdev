"""Shared scanner flow. Claude extracts candidate signals; this code decides which survive.

A candidate is rejected (and nothing is recorded for it) unless:
  1. its type belongs to this scanner,
  2. its URL is not on the blocked list (no LinkedIn / paywalled sites),
  3. event_date is inside the scan window (and not implausibly far in the future),
  4. its URL appeared in the search tool's actual results this session (not invented),
  5. the URL resolves when fetched by code.
Survivors then get source type and confidence reconciled with the URL's real domain; the model's
own confidence can only be lowered, never raised.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from importlib import resources
from typing import ClassVar

from bd.accounts import Account
from bd.config import Config
from bd.hashing import signal_hash
from bd.llm import LLM, LLMError, Usage, generate_validated, search_failure
from pydantic import BaseModel

from bd.models import ScanResponse, SignalExtract
from bd.urls import host_in, hostname, normalize_url
from bd.verify import UrlVerifier

_RANK = {"Low": 0, "Medium": 1, "High": 2}


@dataclass
class Rejection:
    reason: str
    type: str | None = None
    url: str | None = None
    detail: str = ""


@dataclass
class AcceptedSignal:
    extract: SignalExtract
    family: str
    hash: str
    notes: list[str] = field(default_factory=list)


@dataclass
class ScanOutcome:
    account: Account
    scanner: str
    signals: list[AcceptedSignal] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    candidates: int = 0
    error: str | None = None
    postings: list = field(default_factory=list)       # job postings seen (bd.scanners.hiring.Posting), stored by the runner
    postings_complete: bool = False                    # True: `postings` is the board's complete list, so absent ones are closed
    learned_board: tuple[str, str] | None = None       # (provider, board id) learned from posting URLs
    read_filings: list[str] = field(default_factory=list)   # SEC accession numbers read (remembered so they are not re-read)

    def rejection_summary(self) -> str:
        counts: dict[str, int] = {}
        for r in self.rejections:
            key = r.reason.split(":")[0]
            counts[key] = counts.get(key, 0) + 1
        return ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))


def reconcile_source_type(source_type: str, url: str, account_domain: str | None, cfg: Config) -> str:
    """A claimed company / SEC / WARN source must really be hosted where such a source lives;
    otherwise it is just a secondary report."""
    s, host = cfg.sources, hostname(url)
    own = [account_domain] if account_domain else []
    ok = {
        "sec_filing": host_in(host, s.sec_domains),
        "warn_notice": host_in(host, s.warn_domains),
        "company_site": host_in(host, own) or host_in(host, s.ats_domains),
        "company_release": host_in(host, own) or host_in(host, s.wire_services),
    }.get(source_type, True)
    return source_type if ok else "secondary_report"


class Scanner:
    name: ClassVar[str]
    allowed_types: ClassVar[frozenset[str]] = frozenset()
    implemented: ClassVar[bool] = True
    response_model: ClassVar[type[BaseModel]] = ScanResponse   # what the prompt asks Claude to return

    def __init__(self, cfg: Config, llm: LLM | None, verifier: UrlVerifier | None) -> None:
        self.cfg, self.llm, self.verifier = cfg, llm, verifier
        self.model_name: str | None = None        # set when this scanner's calls run on a model other than the run's main one (pricing follows it)

    @classmethod
    def create(cls, cfg: Config, web: LLM | None, text: LLM | None, verifier: UrlVerifier | None, edgar: object | None,
               ats: object | None = None):
        """Build a scanner from the shared dependencies (web-search LLM, text-only LLM, URL verifier, EDGAR)."""
        return cls(cfg, web, verifier)

    reads_filings: ClassVar[bool] = False    # True for scanners that take `known_accessions` (SEC filings already read)
    free: ClassVar[bool] = False             # True when a scan makes no model call: it costs nothing, so the spending limit never holds it back

    def llm_kwargs(self, account: Account) -> dict:
        """Extra arguments for this account's model calls (e.g. a search allow-list). Default: none."""
        return {}

    def _call_kwargs(self, account: Account) -> dict:
        kw = dict(self.llm_kwargs(account))
        tier = self.cfg.scan.tiers.get(account.tier)
        if tier and tier.max_searches:
            kw["max_uses"] = tier.max_searches
        return kw

    def applies_to(self, account: Account) -> bool:
        """False when this scanner has nothing to do for the account (no calls are made or billed)."""
        return True

    # -- prompts ------------------------------------------------------------
    def system_prompt(self) -> str:
        return resources.files("bd.prompts").joinpath(f"{self.name}.md").read_text(encoding="utf-8")

    def user_prompt(self, account: Account, as_of: date) -> str:
        start = as_of - timedelta(days=self.cfg.scan.window_days)
        return (
            f"Company: {account.name}\n"
            f"Website domain: {account.domain or 'unknown'}\n"
            f"Headquarters city: {account.hq_city or 'unknown'}\n"
            f"Today's date: {as_of.isoformat()}\n"
            f"Window: events dated {start.isoformat()} through {as_of.isoformat()}\n\n"
            "Search for signals for this company per your instructions. Return the JSON object only."
        )

    # -- scan ---------------------------------------------------------------
    def scan(self, account: Account, as_of: date) -> ScanOutcome:
        out = ScanOutcome(account=account, scanner=self.name)
        if not self.implemented:
            out.error = "scanner not implemented"
            return out
        assert self.llm is not None and self.verifier is not None
        try:
            resp, result = generate_validated(
                self.llm, self.system_prompt(), self.user_prompt(account, as_of),
                self.response_model, retries=self.cfg.llm.json_retries,
                llm_kwargs=self._call_kwargs(account) or None,
            )
        except LLMError as e:  # InvalidJSON and LLMRefusal are LLMErrors
            out.error = f"{type(e).__name__}: {e}"
            out.usage = e.usage
            return out
        out.usage = result.usage
        out.candidates = len(resp.signals)
        out.signals, out.rejections = self._accept(account, as_of, resp.signals, result.retrieved_urls)
        if not out.signals and (failed := search_failure(result)):      # nothing found AND the search tool failed: not a quiet company, a failed scan
            out.error = failed
        return out

    def date_problem(self, c: SignalExtract, as_of: date) -> str | None:
        """Why event_date is unacceptable, or None. Default: inside the scan window (and not far in the future).
        Scanners with status-type signals override this."""
        lo = as_of - timedelta(days=self.cfg.scan.window_days)
        hi = as_of + timedelta(days=self.cfg.scan.max_future_days)
        return None if lo <= c.event_date <= hi else c.event_date.isoformat()

    def extra_problem(self, c: SignalExtract, account: Account, as_of: date) -> tuple[str, str] | None:
        """Scanner-specific rejection as (reason, detail), or None."""
        return None

    def _accept(self, account: Account, as_of: date, candidates: list[SignalExtract],
                retrieved: set[str]) -> tuple[list[AcceptedSignal], list[Rejection]]:
        cfg = self.cfg
        accepted: dict[str, AcceptedSignal] = {}
        rejected: list[Rejection] = []

        for c in candidates:
            rej = lambda reason, detail="": rejected.append(Rejection(reason, c.type, c.source_url, detail))  # noqa: E731
            if c.type not in self.allowed_types:
                rej("type_not_allowed", f"{self.name} scanner does not emit {c.type}")
                continue
            if host_in(hostname(c.source_url), cfg.sources.blocked_domains):
                rej("blocked_domain")
                continue
            bad_date = self.date_problem(c, as_of)
            if bad_date:
                rej("date_out_of_window", bad_date)
                continue
            extra = self.extra_problem(c, account, as_of)
            if extra:
                rej(extra[0], extra[1])
                continue
            if normalize_url(c.source_url) not in retrieved:
                rej("url_not_retrieved")
                continue
            v = self.verifier.verify(c.source_url)  # type: ignore[union-attr]
            if not v.ok:
                rej(f"unreachable:{v.reason}")
                continue

            notes: list[str] = []
            source_type = self._reconcile_source_type(c, account, notes)
            cap = cfg.sources.confidence_by_source_type[source_type]
            confidence = c.confidence if _RANK[c.confidence] <= _RANK[cap] else cap
            if confidence != c.confidence:
                notes.append(f"confidence {c.confidence}->{confidence}")
            lines = self._canonical_lines(c.service_lines)
            fixed = c.model_copy(update={"source_type": source_type, "confidence": confidence,
                                         "service_lines": lines})
            h = signal_hash(account.id, fixed.type, fixed.event_date, fixed.source_url)
            prior = accepted.get(h)
            if prior is not None:
                if _RANK[fixed.confidence] > _RANK[prior.extract.confidence]:
                    accepted[h] = AcceptedSignal(fixed, cfg.scoring.family_of(fixed.type), h, notes)
                rej("duplicate_in_batch")
                continue
            accepted[h] = AcceptedSignal(fixed, cfg.scoring.family_of(fixed.type), h, notes)
        return list(accepted.values()), rejected

    def _reconcile_source_type(self, c: SignalExtract, account: Account, notes: list[str]) -> str:
        st = reconcile_source_type(c.source_type, c.source_url, account.domain, self.cfg)
        if st != c.source_type:
            notes.append(f"source_type {c.source_type}->{st} (domain {hostname(c.source_url)} not a {c.source_type} host)")
        return st

    def _canonical_lines(self, lines: list[str]) -> list[str]:
        canon = {s.lower(): s for s in self.cfg.service_lines}
        out: list[str] = []
        for line in lines:
            m = canon.get(line.strip().lower())
            if m and m not in out:
                out.append(m)
        return out


def store_signals(conn: sqlite3.Connection, account_id: int, signals: list[AcceptedSignal],
                  detected_date: date) -> tuple[int, int]:
    """Insert accepted signals. event_date is the source's date; detected_date is the run date.
    Returns (inserted, already_known). Dedupe is by hash (account, type, event_date, source domain)."""
    inserted = known = 0
    with conn:
        for s in signals:
            e = s.extract
            cur = conn.execute(
                "INSERT OR IGNORE INTO signals(account_id,family,type,event_date,detected_date,summary,"
                "source_url,source_type,confidence,service_lines,role_or_person,hash) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (account_id, s.family, e.type, e.event_date.isoformat(), detected_date.isoformat(), e.summary,
                 e.source_url, e.source_type, e.confidence, json.dumps(e.service_lines), e.role_or_person, s.hash),
            )
            if cur.rowcount:
                inserted += 1
            else:
                known += 1
    return inserted, known
