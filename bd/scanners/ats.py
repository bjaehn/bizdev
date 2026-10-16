"""ATS scanner: a company's own job board, read directly (Greenhouse, Lever, Ashby). No model, no web search, no cost.

For accounts whose board is known (learned from posting URLs the `hiring` scanner verified), it fetches every open
posting from the provider's public JSON, classifies each title by the keyword lists in config.yaml, and builds the
same signals as the `hiring` scanner (cluster, leadership, interim, single) with the same builder. Because this feed is
complete for that board, the model-based `hiring` scanner skips accounts that have one. The feed is also what closes
postings that were taken down (`ScanOutcome.postings_complete`)."""

from __future__ import annotations

from datetime import date, timedelta

from bd.accounts import Account
from bd.ats import AtsClient, AtsError, classify
from bd.scanners.base import Rejection, ScanOutcome
from bd.scanners.hiring import HiringScanner, Posting, _Verified
from bd.urls import host_in, hostname, normalize_url

MAX_POSTINGS_KEPT = 200


class AtsScanner(HiringScanner):
    name = "ats"

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
        for r in sorted(raw, key=lambda r: (r.posted, r.url), reverse=True):
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
        out.signals = self._build(account, verified)
        out.postings, out.postings_complete = every, True
        return out
