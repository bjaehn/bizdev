"""Pydantic models shared across the agent."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal, get_args
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

SignalType = Literal[
    "leadership_change",
    "leadership_transformation_ai_data",
    "open_exec_search",
    "pe_platform_or_sponsor_exec",
    "pe_addon",
    "pe_hold_2_4",
    "restructuring_layoffs",
    "ma_divestiture",
    "activist",
    "ops_failure",
    "cost_program",
    "ai_commitment",
    "strategy_rebrand",
    "hiring_cluster",
    "hiring_interim",
    "hiring_single",
    "platform_migration",
    "vendor_deal",
    "rfp_match",
    "risk_factor",
]
SIGNAL_TYPES: tuple[str, ...] = get_args(SignalType)

Confidence = Literal["High", "Medium", "Low"]

# High = company_release | sec_filing | warn_notice | company_site
# Medium = secondary_report (one) ; Low = rumor
SourceType = Literal[
    "company_release",
    "sec_filing",
    "warn_notice",
    "company_site",
    "secondary_report",
    "rumor",
]

SOURCE_TYPES: tuple[str, ...] = get_args(SourceType)

MAX_SUMMARY_WORDS = 25


class SignalExtract(BaseModel):
    """What a scanner returns for one signal. Family is derived by code from `type`
    (config.yaml), never supplied by the model."""

    type: SignalType
    event_date: date  # from the source, never the detection date
    summary: str
    source_url: str
    source_type: SourceType
    confidence: Confidence
    service_lines: list[str] = Field(default_factory=list)
    role_or_person: str | None = None

    @field_validator("summary")
    @classmethod
    def _summary_len(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("summary is empty")
        if len(v.split()) > MAX_SUMMARY_WORDS:
            raise ValueError(f"summary exceeds {MAX_SUMMARY_WORDS} words")
        return v

    @field_validator("source_url")
    @classmethod
    def _url_shape(cls, v: str) -> str:
        p = urlparse(v.strip())
        if p.scheme not in ("http", "https") or not p.netloc:
            raise ValueError("source_url must be an absolute http(s) URL")
        return v.strip()


class ScanResponse(BaseModel):
    """The JSON object a scanner prompt asks Claude to return."""

    signals: list[SignalExtract]


@dataclass(frozen=True)
class ScoringSignal:
    """The minimal view of a signal the scoring engine needs."""

    type: str
    event_date: date
    confidence: str
    service_lines: tuple[str, ...] = ()
    ref: int | None = None  # signals.id; carried into the score components for audit
