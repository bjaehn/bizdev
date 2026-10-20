"""Financial-pressure scanner: reads a public company's latest 10-K figures from the SEC's structured data.

No model is involved: the SEC supplies the figures, the filing date and the URL we read, and code applies the thresholds in
config.yaml `financials`. A signal is sec_filing / High by construction, dated by the day the filing was made, and costs
nothing (one request per company, spaced under the SEC's fair-access limit)."""

from __future__ import annotations

from datetime import date, timedelta

from bd.accounts import Account
from bd.config import Config
from bd.edgar import EdgarClient, EdgarError, facts_url
from bd.financials import assess, summary
from bd.hashing import signal_hash
from bd.models import SignalExtract
from bd.scanners.base import AcceptedSignal, Rejection, ScanOutcome, Scanner


class FinancialsScanner(Scanner):
    name = "financials"
    free = True
    allowed_types = frozenset({"financial_pressure"})

    def __init__(self, cfg: Config, edgar: EdgarClient | None) -> None:
        super().__init__(cfg, None, None)
        self.edgar = edgar

    @classmethod
    def create(cls, cfg, web, text, verifier, edgar, ats=None):  # type: ignore[override]
        return cls(cfg, edgar)

    def applies_to(self, account: Account) -> bool:
        return bool(account.cik)

    def scan(self, account: Account, as_of: date) -> ScanOutcome:
        out = ScanOutcome(account=account, scanner=self.name)
        if not account.cik:
            return out
        assert self.edgar is not None
        try:
            facts = self.edgar.company_facts(account.cik)
        except EdgarError as e:
            if str(e) == "HTTP 404":      # the SEC answered: it holds no structured data under this CIK (a subsidiary that reports inside its parent's filing). Not a failure to retry every week.
                out.rejections.append(Rejection("no_company_facts", None, facts_url(account.cik), "the SEC has no structured financial data under this CIK; check the CIK"))
                return out
            out.error = f"EdgarError: {e}"
            return out
        a = assess(facts, self.cfg)
        url = facts_url(account.cik)
        if a is None:
            out.rejections.append(Rejection("no_comparable_figures", None, url, "needs revenue and operating income for two fiscal years"))
            return out
        out.candidates = 1
        lo = as_of - timedelta(days=self.cfg.scan.window_days)
        if not lo <= a.filed <= as_of + timedelta(days=self.cfg.scan.max_future_days):
            out.rejections.append(Rejection("date_out_of_window", None, url, f"latest {a.form} filed {a.filed.isoformat()}"))
            return out
        if not a.flags:
            out.rejections.append(Rejection("no_pressure_flags", None, url))
            return out
        extract = SignalExtract(type="financial_pressure", event_date=a.filed, summary=summary(a), source_url=url,
                                source_type="sec_filing", confidence="High",
                                service_lines=self._canonical_lines(["Strategy & Operations"]), role_or_person=None)
        h = signal_hash(account.id, extract.type, extract.event_date, extract.source_url)
        out.signals.append(AcceptedSignal(extract, self.cfg.scoring.family_of(extract.type), h,
                                          [f"SEC {a.form} {a.accession}; flags: {', '.join(a.flags)}"]))
        return out
