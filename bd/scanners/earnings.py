"""Earnings scanner: SEC 8-K Item 2.02 earnings releases for accounts with a CIK.

EDGAR (not Claude) supplies the existence, URL and date of each release, so none of those can be fabricated. Claude only
reads the release text (no web search) and says which of a fixed set of events it states: a guidance cut or cost
program, a restructuring, a deal, a specific AI commitment, an operational failure, a new strategy. Routine results are
not signals. Signals are sec_filing / High by construction. Earnings-call transcripts are not used (they are paywalled)."""

from __future__ import annotations

import json
from datetime import date, timedelta
from importlib import resources
from typing import Literal

from pydantic import BaseModel, field_validator

from bd.accounts import Account
from bd.config import Config
from bd.edgar import EdgarClient, EdgarError
from bd.hashing import signal_hash
from bd.llm import LLM, LLMError, generate_validated
from bd.models import MAX_SUMMARY_WORDS, SignalExtract
from bd.scanners.base import AcceptedSignal, Rejection, ScanOutcome, Scanner

EarningsType = Literal["cost_program", "restructuring_layoffs", "ma_divestiture", "ai_commitment", "ops_failure", "strategy_rebrand"]
MAX_PER_RELEASE = 3


class EarningsItem(BaseModel):
    type: EarningsType
    summary: str
    role_or_person: str | None = None
    service_lines: list[str] = []

    @field_validator("summary")
    @classmethod
    def _words(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v or len(v.split()) > MAX_SUMMARY_WORDS:
            raise ValueError(f"summary must be 1-{MAX_SUMMARY_WORDS} words")
        return v


class EarningsResponse(BaseModel):
    signals: list[EarningsItem]


class EarningsScanner(Scanner):
    name = "earnings"
    allowed_types = frozenset(EarningsType.__args__)  # type: ignore[attr-defined]

    def __init__(self, cfg: Config, llm: LLM | None, edgar: EdgarClient | None) -> None:
        super().__init__(cfg, llm, None)
        self.edgar = edgar

    @classmethod
    def create(cls, cfg, web, text, verifier, edgar):  # type: ignore[override]
        return cls(cfg, text, edgar)

    def applies_to(self, account: Account) -> bool:
        return bool(account.cik)

    def system_prompt(self) -> str:
        return resources.files("bd.prompts").joinpath("earnings_202.md").read_text(encoding="utf-8")

    def scan(self, account: Account, as_of: date) -> ScanOutcome:
        out = ScanOutcome(account=account, scanner=self.name)
        if not account.cik:
            return out
        assert self.edgar is not None and self.llm is not None
        cfg = self.cfg
        lo, hi = as_of - timedelta(days=cfg.scan.window_days), as_of + timedelta(days=cfg.scan.max_future_days)
        try:
            filings = self.edgar.recent_items(account.cik, lo, hi, cfg.edgar.max_filings_per_scan, "2.02")
        except EdgarError as e:
            out.error = f"EdgarError: {e}"
            return out
        seen: set[str] = set()
        errors: list[str] = []
        for f in filings:
            out.candidates += 1
            try:
                url, text = self.edgar.earnings_release(f)
            except EdgarError as e:
                out.rejections.append(Rejection(f"unreachable:{e}", None, f.url))
                continue
            payload = json.dumps({"company": account.name, "form": f.form, "filing_date": f.filing_date.isoformat(),
                                  "report_date": f.report_date.isoformat() if f.report_date else None, "text": text},
                                 ensure_ascii=False)
            try:
                resp, res = generate_validated(self.llm, self.system_prompt(), payload, EarningsResponse,
                                               retries=cfg.llm.json_retries)
            except LLMError as e:
                out.usage.add(e.usage)
                errors.append(f"{type(e).__name__}: {e}")
                continue
            out.usage.add(res.usage)
            if not resp.signals:
                out.rejections.append(Rejection("no_event_in_release", None, url))
            for item in resp.signals[:MAX_PER_RELEASE]:
                extract = SignalExtract(type=item.type, event_date=f.event_date, summary=item.summary, source_url=url,
                                        source_type="sec_filing", confidence="High",
                                        service_lines=self._canonical_lines(item.service_lines), role_or_person=item.role_or_person)
                h = signal_hash(account.id, extract.type, extract.event_date, extract.source_url)
                if h in seen:
                    out.rejections.append(Rejection("duplicate_in_batch", extract.type, url))
                    continue
                seen.add(h)
                out.signals.append(AcceptedSignal(extract, cfg.scoring.family_of(extract.type), h,
                                                  [f"EDGAR {f.form} {f.accession} item 2.02"]))
        out.error = "; ".join(errors) or None
        return out
