"""ATS scanner: a company's own job board, read directly (Greenhouse, Lever, Ashby). No model, no web search, no cost.

For accounts whose board is known (learned from posting URLs the `hiring` scanner verified), it fetches every open
posting from the provider's public JSON, classifies each title by the keyword lists in config.yaml, and builds the
same signals as the `hiring` scanner (cluster, leadership, interim, single) with the same builder. Because this feed is
complete for that board, the model-based `hiring` scanner skips accounts that have one. The feed is also what closes
postings that were taken down (`ScanOutcome.postings_complete`)."""

from __future__ import annotations

from datetime import date, timedelta

from bd.accounts import Account
from bd.ats import AtsClient, AtsError, classify, programs_in
from bd.postings import BoardReading
from bd.hashing import signal_hash
from bd.models import SignalExtract
from bd.scanners.base import AcceptedSignal, Rejection, ScanOutcome
from bd.scanners.hiring import HiringScanner, Posting, _short, _Verified
from bd.urls import host_in, hostname, normalize_url

MAX_POSTINGS_KEPT = 200


class AtsScanner(HiringScanner):
    name = "ats"
    free = True                # reads the company's public job board; no model call

    def __init__(self, cfg, client: AtsClient | None) -> None:
        super().__init__(cfg, None, None)
        self.client = client

    @classmethod
    def create(cls, cfg, web, text, verifier, edgar, ats=None):  # type: ignore[override]
        return cls(cfg, ats)

    def applies_to(self, account: Account) -> bool:
        return bool(account.ats_provider and account.ats_board)

    def scan(self, account: Account, as_of: date) -> ScanOutcome:
        out = ScanOutcome(account=account, scanner=self.name)
        assert self.client is not None
        try:
            raw = self.client.fetch(account.ats_provider or "", account.ats_board or "")
        except AtsError as e:
            out.error = f"AtsError: {e}"
            return out
        cfg = self.cfg
        lo, hi = as_of - timedelta(days=cfg.hiring.window_days), as_of + timedelta(days=1)
        out.candidates = len(raw)
        own = [d for d in (account.domain,) if d]
        verified: list[_Verified] = []
        seen: set[str] = set()
        every: list[Posting] = []
        named: dict[str, list[Posting]] = {}
        for r in sorted(raw, key=lambda r: (r.posted, r.url), reverse=True):
            if lo <= r.posted <= hi and (host_in(hostname(r.url), cfg.sources.ats_domains) or host_in(hostname(r.url), own)):
                for program in programs_in(r.title, cfg):
                    named.setdefault(program, []).append(Posting(title=r.title, url=r.url, posted_date=r.posted, theme="ai_technology", seniority="other",
                                                                 source_type="company_site"))
            c = classify(r.title, cfg)
            if c is None:
                continue
            if not (host_in(hostname(r.url), cfg.sources.ats_domains) or host_in(hostname(r.url), own)):
                out.rejections.append(Rejection("foreign_host", None, r.url, hostname(r.url)))
                continue
            if normalize_url(r.url) in seen:
                continue
            seen.add(normalize_url(r.url))
            theme, seniority, interim = c
            p = Posting(title=r.title, url=r.url, posted_date=r.posted, theme=theme, seniority=seniority, interim=interim,
                        source_type="company_site")
            if len(every) < MAX_POSTINGS_KEPT:
                every.append(p)
            if lo <= r.posted <= hi:
                verified.append(_Verified(p, "company_site", cfg.sources.confidence_by_source_type["company_site"]))
            else:
                out.rejections.append(Rejection("date_out_of_window", None, r.url, r.posted.isoformat()))
        out.signals = self._build(account, verified) + self._program_signals(account, named)
        out.postings, out.postings_complete = every, True
        out.board = BoardReading(total=len(raw), relevant=sum(1 for r in raw if classify(r.title, cfg) is not None),
                                 programs={n: [(p.title, p.url, p.posted_date.isoformat()) for p in ps] for n, ps in named.items()})
        return out

    def _program_signals(self, account: Account, named: dict[str, list[Posting]]) -> list[AcceptedSignal]:
        """One hiring_cluster per program that program_min or more postings inside the window name in their titles (the biggest first, at most
        program_signals_max). Dated by the posting that completed the cluster, so the same surge keeps its date week to week."""
        cfg = self.cfg
        out: list[AcceptedSignal] = []
        big = sorted(((n, ps) for n, ps in named.items() if len(ps) >= cfg.hiring.program_min), key=lambda x: (-len(x[1]), x[0]))
        for program, ps in big[: cfg.hiring.program_signals_max]:
            ps = sorted(ps, key=lambda p: (p.posted_date, p.url))
            formed_by = ps[cfg.hiring.program_min - 1]
            v = _Verified(formed_by, "company_site", cfg.sources.confidence_by_source_type["company_site"])
            summary = f"{len(ps)} job postings name {program} in their titles within {cfg.hiring.window_days} days, including {_short(formed_by.title, 6)}."
            e = SignalExtract(type="hiring_cluster", event_date=formed_by.posted_date, summary=summary, source_url=formed_by.url, source_type="company_site",
                              confidence=v.confidence, service_lines=self._canonical_lines(cfg.hiring.program_service_lines), role_or_person=_short(formed_by.title, 12))
            out.append(AcceptedSignal(e, cfg.scoring.family_of("hiring_cluster"), signal_hash(account.id, "hiring_cluster", formed_by.posted_date, formed_by.url),
                                      [f"{len(ps)} postings name {program}, cluster formed {formed_by.posted_date.isoformat()}"]))
        return out
