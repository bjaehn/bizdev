"""EDGAR scanner: SEC 8-K Item 5.02 filings for accounts with a CIK.

EDGAR (not Claude) supplies the existence, URL and event date of each filing, so none of those can
be fabricated. Claude only reads the filing excerpt (no web search) to say whether it is an
executive-officer change and to write the summary. Signals are sec_filing / High by construction."""

from __future__ import annotations

import json
from datetime import date, timedelta
from importlib import resources
from typing import Literal

from pydantic import BaseModel, field_validator, model_validator

from bd.accounts import Account
from bd.config import Config
from bd.edgar import EdgarClient, EdgarError
from bd.hashing import signal_hash
from bd.llm import LLM, LLMError, generate_validated
from bd.models import MAX_SUMMARY_WORDS, SignalExtract
from bd.scanners.base import AcceptedSignal, Rejection, ScanOutcome, Scanner


class EdgarExtract(BaseModel):
    relevant: bool
    type: Literal["leadership_change", "leadership_transformation_ai_data"] | None = None
    summary: str | None = None
    role_or_person: str | None = None
    service_lines: list[str] = []

    @field_validator("summary")
    @classmethod
    def _words(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = " ".join(v.split())
        if len(v.split()) > MAX_SUMMARY_WORDS:
            raise ValueError(f"summary exceeds {MAX_SUMMARY_WORDS} words")
        return v

    @model_validator(mode="after")
    def _complete_when_relevant(self) -> "EdgarExtract":
        if self.relevant and (not self.type or not self.summary):
            raise ValueError("a relevant filing needs both type and summary")
        return self


class EdgarScanner(Scanner):
    name = "edgar"
    allowed_types = frozenset({"leadership_change", "leadership_transformation_ai_data"})

    def __init__(self, cfg: Config, llm: LLM | None, edgar: EdgarClient | None) -> None:
        super().__init__(cfg, llm, None)
        self.edgar = edgar

    @classmethod
    def create(cls, cfg, web, text, verifier, edgar, ats=None):  # type: ignore[override]
        return cls(cfg, text, edgar)

    def applies_to(self, account: Account) -> bool:
        return bool(account.cik)

    def system_prompt(self) -> str:
        return resources.files("bd.prompts").joinpath("edgar_502.md").read_text(encoding="utf-8")

    reads_filings = True

    def scan(self, account: Account, as_of: date, known_accessions: frozenset[str] = frozenset()) -> ScanOutcome:
        out = ScanOutcome(account=account, scanner=self.name)
        if not account.cik:
            return out
        assert self.edgar is not None and self.llm is not None
        cfg = self.cfg
        lo = as_of - timedelta(days=cfg.scan.window_days)
        hi = as_of + timedelta(days=cfg.scan.max_future_days)
        try:
            filings = self.edgar.recent_item_502(account.cik, lo, hi, cfg.edgar.max_filings_per_scan)
        except EdgarError as e:
            out.error = f"EdgarError: {e}"
            return out
        seen: set[str] = set()
        errors: list[str] = []
        for f in filings:
            if f.accession in known_accessions:           # read on an earlier run (found something or not)
                out.already_read += 1
                continue
            out.candidates += 1
            try:
                text = self.edgar.filing_text(f)
            except EdgarError as e:
                out.rejections.append(Rejection(f"unreachable:{e}", None, f.url))
                continue
            payload = json.dumps({"company": account.name, "form": f.form, "filing_date": f.filing_date.isoformat(),
                                  "report_date": f.report_date.isoformat() if f.report_date else None,
                                  "text": text}, ensure_ascii=False)
            try:
                resp, res = generate_validated(self.llm, self.system_prompt(), payload, EdgarExtract,
                                               retries=cfg.llm.json_retries, key="relevant")
            except LLMError as e:
                out.usage.add(e.usage)
                errors.append(f"{type(e).__name__}: {e}")
                continue
            out.usage.add(res.usage)
            out.read_filings.append(f.accession)
            if not resp.relevant:
                out.rejections.append(Rejection("not_officer_change", None, f.url))
                continue
            lines = self._canonical_lines(resp.service_lines)
            extract = SignalExtract(
                type=resp.type, event_date=f.event_date, summary=resp.summary, source_url=f.url,
                source_type="sec_filing", confidence="High", service_lines=lines,
                role_or_person=resp.role_or_person)
            h = signal_hash(account.id, extract.type, extract.event_date, extract.source_url)
            if h in seen:
                out.rejections.append(Rejection("duplicate_in_batch", extract.type, f.url))
                continue
            seen.add(h)
            out.signals.append(AcceptedSignal(extract, cfg.scoring.family_of(extract.type), h,
                                              [f"EDGAR {f.form} {f.accession}"]))
        out.error = "; ".join(errors) or None
        return out
