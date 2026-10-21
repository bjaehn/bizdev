"""Hiring scanner: public job postings that signal demand (AI, data, technology, strategy, transformation).

Claude lists postings it found, each with its own URL, posted date, theme and seniority. CODE then
verifies every posting (URL retrieved by the search tool and loading; date inside the window) and builds
the signals itself, so counts and dates cannot be invented:

  hiring_cluster   cluster_min or more related (same theme) postings inside the window. event_date is the
                   date the cluster FORMED (the cluster_min-th earliest posting), so it stays the same week to
                   week and the same surge is not counted again as new postings join. At most one per account.
  hiring_leadership a VP-or-above (hiring.leadership_levels) posting in a relevant area (newest one)
  hiring_interim   an interim / fractional senior role (newest one)
  hiring_single    one director-level posting (only when there is no cluster and no leadership posting)

Summaries are written by code from the verified titles; Claude writes none of the signal text."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, field_validator

from bd.accounts import Account
from bd.ats import board_from_url
from bd.hashing import signal_hash
from bd.labels import THEME_LABEL
from bd.llm import LLMError, generate_validated, search_failure
from bd.models import SignalExtract
from bd.scanners.base import AcceptedSignal, Rejection, ScanOutcome, Scanner, reconcile_source_type
from bd.urls import host_in, hostname, normalize_url

Theme = Literal["ai_technology", "data_analytics", "strategy_operations", "transformation_change", "product_engineering"]
Seniority = Literal["c_suite", "svp_vp", "director", "other"]
_RANK = {"Low": 0, "Medium": 1, "High": 2}


class Posting(BaseModel):
    title: str
    url: str
    posted_date: date
    theme: Theme
    seniority: Seniority
    interim: bool = False
    source_type: Literal["company_site", "secondary_report"] = "company_site"

    @field_validator("title")
    @classmethod
    def _title(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("title is empty")
        return v

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith(("http://", "https://")) or "." not in v:
            raise ValueError("url must be an absolute http(s) URL")
        return v


class HiringResponse(BaseModel):
    postings: list[Posting]


@dataclass
class _Verified:
    p: Posting
    source_type: str
    confidence: str


def _short(title: str, words: int = 8) -> str:
    w = title.split()
    return " ".join(w[:words]) + ("…" if len(w) > words else "")


class HiringScanner(Scanner):
    name = "hiring"
    allowed_types = frozenset({"hiring_cluster", "hiring_leadership", "hiring_interim", "hiring_single"})
    response_model = HiringResponse

    def applies_to(self, account: Account) -> bool:
        # An account whose job board the `ats` scanner reads directly gets the complete feed, not a model search.
        return not (account.ats_provider and account.ats_board)

    def user_prompt(self, account: Account, as_of: date) -> str:
        start = as_of - timedelta(days=self.cfg.hiring.window_days)
        return (
            f"Company: {account.name}\n"
            f"Website domain: {account.domain or 'unknown'}\n"
            f"Headquarters city: {account.hq_city or 'unknown'}\n"
            f"Today's date: {as_of.isoformat()}\n"
            f"Window: postings dated {start.isoformat()} through {as_of.isoformat()}\n\n"
            "Find the job postings per your instructions. Return the JSON object only."
        )

    def scan(self, account: Account, as_of: date) -> ScanOutcome:
        out = ScanOutcome(account=account, scanner=self.name)
        assert self.llm is not None and self.verifier is not None
        try:
            resp, result = generate_validated(self.llm, self.system_prompt(), self.user_prompt(account, as_of),
                                              HiringResponse, retries=self.cfg.llm.json_retries, key="postings")
        except LLMError as e:
            out.error = f"{type(e).__name__}: {e}"
            out.usage = e.usage
            return out
        out.usage = result.usage
        out.candidates = len(resp.postings)
        verified, out.rejections = self._verify(account, as_of, resp.postings, result.retrieved_urls)
        out.signals = self._build(account, verified)
        out.postings = [v.p for v in verified]
        out.learned_board = next((b for b in (board_from_url(v.p.url) for v in verified) if b), None)
        if not out.postings and not out.signals and (failed := search_failure(result)):      # a failed search tool is not "no postings"
            out.error = failed
        return out

    # -- verification (per posting) ---------------------------------------------------------------
    def _verify(self, account: Account, as_of: date, postings: list[Posting], retrieved: set[str]):
        cfg = self.cfg
        lo = as_of - timedelta(days=cfg.hiring.window_days)
        hi = as_of + timedelta(days=1)
        ok: list[_Verified] = []
        rejected: list[Rejection] = []
        seen: set[str] = set()
        for p in postings:
            rej = lambda reason, detail="": rejected.append(Rejection(reason, None, p.url, detail))  # noqa: E731
            if host_in(hostname(p.url), cfg.sources.blocked_domains):
                rej("blocked_domain")
            elif not (lo <= p.posted_date <= hi):
                rej("date_out_of_window", p.posted_date.isoformat())
            elif normalize_url(p.url) not in retrieved:
                rej("url_not_retrieved")
            elif normalize_url(p.url) in seen:
                rej("duplicate_in_batch")
            else:
                v = self.verifier.verify(p.url)  # type: ignore[union-attr]
                if not v.ok:
                    rej(f"unreachable:{v.reason}")
                    continue
                seen.add(normalize_url(p.url))
                st = reconcile_source_type(p.source_type, p.url, account.domain, cfg)
                conf = cfg.sources.confidence_by_source_type[st]
                ok.append(_Verified(p, st, conf))
        return ok, rejected

    # -- signals (built by code from verified postings) -------------------------------------------------
    def _signal(self, account: Account, type_: str, event_date: date, summary: str, at: _Verified, confidence: str,
                theme: str, notes: list[str]) -> AcceptedSignal:
        lines = self._canonical_lines(self.cfg.hiring.theme_service_lines.get(theme, []))
        e = SignalExtract(type=type_, event_date=event_date, summary=summary, source_url=at.p.url,
                          source_type=at.source_type, confidence=confidence, service_lines=lines,
                          role_or_person=_short(at.p.title, 12))
        return AcceptedSignal(e, self.cfg.scoring.family_of(type_),
                              signal_hash(account.id, type_, event_date, at.p.url), notes)

    def _build(self, account: Account, verified: list[_Verified]) -> list[AcceptedSignal]:
        cfg, senior = self.cfg, set(self.cfg.hiring.senior_levels)
        newest = lambda xs: max(xs, key=lambda v: (v.p.posted_date, v.p.url))  # noqa: E731
        out: list[AcceptedSignal] = []

        interim = [v for v in verified if v.p.interim and v.p.seniority in senior]
        if interim:
            v = newest(interim)
            out.append(self._signal(account, "hiring_interim", v.p.posted_date,
                                    f"Interim senior role posted: {_short(v.p.title)}.", v, v.confidence, v.p.theme,
                                    ["interim posting"]))

        regular = [v for v in verified if not v.p.interim]
        by_theme: dict[str, list[_Verified]] = {}
        for v in regular:
            by_theme.setdefault(v.p.theme, []).append(v)
        clusters = {t: vs for t, vs in by_theme.items() if len(vs) >= cfg.hiring.cluster_min}
        if clusters:
            theme = max(clusters, key=lambda t: (len(clusters[t]), max(v.p.posted_date for v in clusters[t]), t))
            vs = sorted(clusters[theme], key=lambda v: (v.p.posted_date, v.p.url))
            formed_by = vs[cfg.hiring.cluster_min - 1]                 # the posting that completed the cluster
            conf = min((v.confidence for v in vs), key=lambda c: _RANK[c])
            summary = (f"{len(vs)} related {THEME_LABEL[theme]} job postings within {cfg.hiring.window_days} days, "
                       f"including {_short(formed_by.p.title, 6)}.")
            out.append(self._signal(account, "hiring_cluster", formed_by.p.posted_date, summary, formed_by, conf, theme,
                                    [f"{len(vs)} postings, cluster formed {formed_by.p.posted_date.isoformat()}"]))
        leaders = [v for v in regular if v.p.seniority in set(cfg.hiring.leadership_levels)]
        if leaders:
            v = newest(leaders)
            out.append(self._signal(account, "hiring_leadership", v.p.posted_date,
                                    f"Senior {THEME_LABEL[v.p.theme]} leadership role posted: {_short(v.p.title)}.", v,
                                    v.confidence, v.p.theme, [f"{len(leaders)} leadership posting(s)"]))
        if not clusters and not leaders:
            seniors = [v for v in regular if v.p.seniority in senior]
            if seniors:
                v = newest(seniors)
                out.append(self._signal(account, "hiring_single", v.p.posted_date,
                                        f"Senior {THEME_LABEL[v.p.theme]} role posted: {_short(v.p.title)}.", v,
                                        v.confidence, v.p.theme, ["single senior posting"]))
        return out
