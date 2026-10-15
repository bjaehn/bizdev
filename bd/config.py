"""config.yaml loading and validation. Secrets and the model name come from env vars."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

from bd.models import SIGNAL_TYPES, SOURCE_TYPES


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RecencyBand(_Strict):
    max_days: int
    multiplier: float


class ServiceMatch(_Strict):
    match: float
    none: float


class Bands(_Strict):
    hot: int
    warm: int
    watch: int


class Scoring(_Strict):
    base_points: dict[str, float]
    families: dict[str, list[str]]
    recency_bands: list[RecencyBand]
    status_types: list[str]
    status_multiplier: float
    confidence_multiplier: dict[str, float]
    service_match_multiplier: ServiceMatch
    within_family_decay: float
    stacking_bonus_per_family: float
    stacking_bonus_max: float
    score_max: int
    bands: Bands
    posted_bands: list[str]

    @model_validator(mode="after")
    def _consistent(self) -> "Scoring":
        known = set(SIGNAL_TYPES)
        if set(self.base_points) != known:
            diff = set(self.base_points) ^ known
            raise ValueError(f"base_points keys must equal the signal types; mismatch: {sorted(diff)}")
        flat = [t for ts in self.families.values() for t in ts]
        if len(flat) != len(set(flat)):
            raise ValueError("a signal type appears in more than one family")
        if set(flat) != known:
            raise ValueError(f"families must cover every type exactly once; mismatch: {sorted(set(flat) ^ known)}")
        missing = set(self.status_types) - known
        if missing:
            raise ValueError(f"unknown status_types: {sorted(missing)}")
        days = [b.max_days for b in self.recency_bands]
        if days != sorted(days) or len(set(days)) != len(days):
            raise ValueError("recency_bands must be strictly ascending by max_days")
        if set(self.confidence_multiplier) != {"High", "Medium", "Low"}:
            raise ValueError("confidence_multiplier must define High, Medium, Low")
        if not (self.bands.hot > self.bands.warm > self.bands.watch > 0):
            raise ValueError("bands must satisfy hot > warm > watch > 0")
        return self

    def family_of(self, type_: str) -> str:
        for fam, types in self.families.items():
            if type_ in types:
                return fam
        raise KeyError(f"unknown signal type: {type_}")


class PostGate(_Strict):
    require_high_confidence_within_days: int
    max_leads_per_post: int
    cooldown_weeks: int
    cooldown_break_score_rise: float


class SizeFloor(_Strict):
    revenue_usd: int
    employees: int | None = None


class FitGate(_Strict):
    counties: list[str]
    state: str
    size_floor: dict[str, SizeFloor]
    excluded_industries: list[str]
    exclusion_reasons: list[str]


class Tier(_Strict):
    every_weeks: int


class Scan(_Strict):
    window_days: int
    max_future_days: int
    cadence_grace_days: int
    status_type_age_days: dict[str, tuple[int, int]] = {}
    tiers: dict[str, Tier]


class Llm(_Strict):
    max_tokens: int
    web_search_max_uses: int
    web_search_tool_type: str
    max_continuations: int
    json_retries: int
    concurrency: int
    max_cost_per_run_usd: float | None = None


class ModelPrice(_Strict):
    input: float
    output: float
    cache_write_5m: float
    cache_read: float


class Pricing(_Strict):
    retrieved: date
    web_search_per_1000: float
    models: dict[str, ModelPrice]


class Verify(_Strict):
    timeout_seconds: float
    max_redirects: int
    block_private_addresses: bool


class Sources(_Strict):
    blocked_domains: list[str]
    wire_services: list[str]
    ats_domains: list[str]
    sec_domains: list[str]
    warn_domains: list[str]
    confidence_by_source_type: dict[str, str]

    @model_validator(mode="after")
    def _consistent(self) -> "Sources":
        if set(self.confidence_by_source_type) != set(SOURCE_TYPES):
            raise ValueError("confidence_by_source_type must cover every source type")
        if not set(self.confidence_by_source_type.values()) <= {"High", "Medium", "Low"}:
            raise ValueError("confidence_by_source_type values must be High, Medium or Low")
        return self


class Hiring(_Strict):
    window_days: int
    cluster_min: int
    senior_levels: list[str]
    theme_service_lines: dict[str, list[str]]


class Edgar(_Strict):
    min_interval_seconds: float
    max_filings_per_scan: int
    max_chars: int
    timeout_seconds: float


class Briefer(_Strict):
    max_words: int
    max_signals_in_prompt: int
    banned_phrases: list[str]


class Card(_Strict):
    max_bytes: int
    max_buttons_per_lead: int
    title: str


class Config(_Strict):
    scoring: Scoring
    post_gate: PostGate
    service_lines: list[str]
    fit_gate: FitGate
    scan: Scan
    llm: Llm
    pricing: Pricing
    verify: Verify
    sources: Sources
    hiring: Hiring
    edgar: Edgar
    briefer: Briefer
    card: Card

    @model_validator(mode="after")
    def _status_bounds(self) -> "Config":
        for t, (lo, hi) in self.scan.status_type_age_days.items():
            if t not in self.scoring.status_types:
                raise ValueError(f"scan.status_type_age_days names {t!r}, which is not a status type")
            if not 0 <= lo <= hi:
                raise ValueError(f"scan.status_type_age_days[{t}] must satisfy 0 <= min <= max")
        return self


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    p = Path(path or os.environ.get("BD_CONFIG", "config.yaml"))
    with p.open() as f:
        return Config.model_validate(yaml.safe_load(f))


@dataclass(frozen=True)
class Settings:
    """Environment-only settings. Nothing here is ever read from config.yaml."""

    anthropic_api_key: str | None
    anthropic_model: str | None
    teams_webhook_url: str | None
    db_path: str
    user_agent: str
    user_agent_configured: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ
        return cls(
            anthropic_api_key=e.get("ANTHROPIC_API_KEY") or None,
            anthropic_model=e.get("ANTHROPIC_MODEL") or None,
            teams_webhook_url=e.get("TEAMS_WEBHOOK_URL") or None,
            db_path=e.get("BD_DB_PATH", "bd.sqlite"),
            user_agent=e.get(
                "BD_USER_AGENT",
                "SEI-BD-Signal-Agent/0.1 (public-source research; set BD_USER_AGENT with a contact address)",
            ),
            user_agent_configured=bool(e.get("BD_USER_AGENT")),
        )

    def require_llm(self) -> tuple[str, str]:
        if not self.anthropic_api_key or not self.anthropic_model:
            raise RuntimeError("ANTHROPIC_API_KEY and ANTHROPIC_MODEL must be set in the environment")
        return self.anthropic_api_key, self.anthropic_model
