"""config.yaml loading and validation. Secrets and the model name come from env vars."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

from bd.models import FIRM_SIGNAL_TYPES, MAX_SUMMARY_WORDS, SIGNAL_TYPES, SOURCE_TYPES


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


class SponsorScoring(_Strict):
    base_points: dict[str, float]     # by firm signal type
    lift_max: float                   # cap on the total lift a company can get from its sponsor's activity

    @model_validator(mode="after")
    def _types(self) -> "SponsorScoring":
        if set(self.base_points) != set(FIRM_SIGNAL_TYPES):
            raise ValueError(f"sponsor.base_points keys must equal {sorted(FIRM_SIGNAL_TYPES)}")
        return self


class Sequence(_Strict):
    """A pattern worth extra points: a signal of one kind followed (same day or later, within `within_days`) by a signal of another."""
    name: str
    label: str
    first: list[str]
    then: list[str]
    within_days: int
    bonus: float


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
    sponsor: SponsorScoring
    sequences: list[Sequence] = []
    sequence_bonus_max: float = 0

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
        names = [q.name for q in self.sequences]
        if len(names) != len(set(names)):
            raise ValueError("sequence names must be unique")
        for q in self.sequences:
            if not q.first or not q.then or q.within_days < 1 or q.bonus <= 0:
                raise ValueError(f"sequence {q.name}: needs first and then types, within_days >= 1 and a positive bonus")
            bad = (set(q.first) | set(q.then)) - known
            if bad or (set(q.first) | set(q.then)) & set(self.status_types):
                raise ValueError(f"sequence {q.name}: unknown or status signal types {sorted(bad or set(q.first) | set(q.then))}")
            if set(q.first) & set(q.then):
                raise ValueError(f"sequence {q.name}: a type cannot be in both first and then")
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


class Financials(_Strict):
    revenue_decline_pct: float
    margin_drop_points: float
    flag_operating_loss: bool


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
    max_searches: int | None = None     # web searches allowed per model call for this tier (default: llm.web_search_max_uses)


class Scan(_Strict):
    window_days: int
    max_future_days: int
    cadence_grace_days: int
    status_type_age_days: dict[str, tuple[int, int]] = {}
    tiers: dict[str, Tier]
    sweep_searches: int = 6              # web searches the combined sweep may use per company (one call)
    quiet_days: int = 60                 # a scanned company with no signal dated this recently is "quiet"...
    quiet_every_weeks: int = 4           # ...and is scanned this often instead of its tier's cadence (0 turns this off)


class Llm(_Strict):
    max_tokens: int
    web_search_max_uses: int
    web_search_tool_type: str
    max_continuations: int
    json_retries: int
    concurrency: int
    call_reserve_usd: float = 0.35      # expected cost of one search call, until real calls give an average
    max_cost_per_run_usd: float | None = None


class ModelPrice(_Strict):
    input: float
    output: float
    cache_write_5m: float
    cache_read: float
    # Some models charge more for a request whose prompt is longer than long_over_tokens (each request is priced on its own).
    long_over_tokens: int | None = None
    long_input: float | None = None
    long_output: float | None = None
    long_cache_write_5m: float | None = None
    long_cache_read: float | None = None


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
    leadership_levels: list[str]
    theme_service_lines: dict[str, list[str]]
    theme_keywords: dict[str, list[str]]
    theme_title_gates: dict[str, list[str]] = {}
    interim_words: list[str]


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


class Discovery(_Strict):
    segments: list[str]
    per_segment: int                   # companies asked for per segment
    max_candidates_per_run: int        # new companies researched (enriched) per run
    max_auto_add_per_run: int          # auto-adds per run; the rest go to the review queue
    scan_unreviewed_per_run: int       # unreviewed auto-added accounts a single scan may include


class PeFirms(_Strict):
    max_firms_per_run: int            # firms checked for firm-level signals per run
    portfolio_max_per_firm: int       # portfolio companies requested per firm


class PeFetch(_Strict):
    timeout_seconds: float
    max_retries: int
    retry_backoff_seconds: float
    min_seconds_between_requests: float   # per host, so a firm's site is never hammered
    max_bytes: int                        # a response larger than this is read up to the cap and marked truncated
    max_pages_per_source: int             # pagination and API pages followed for one listing
    respect_robots: bool
    user_agent_suffix: str
    cache_hours: float                    # a page read successfully this recently is not fetched again unless forced


class PePortfolio(_Strict):
    fetch: PeFetch
    ownership_fresh_days: int             # ownership last verified longer ago than this is shown as stale (never changed by age alone)
    listing_fresh_days: int               # a firm's portfolio page read longer ago than this is stale
    pending_window_days: int              # an announced deal with no completion evidence stays "pending" this long, then becomes "unclear"
    snapshot_keep: int                    # page snapshots kept per URL (for history and for diffs)
    text_cap_bytes: int
    scope_cities: list[str]               # optional: cities treated as inside the configured geography when a company's HQ is shown to be in fit_gate.state; empty = not configured
    locate_per_run: int                   # companies per run whose own site is read (free) to find a headquarters the listing does not show
    deal_window_days: int                 # transactions dated within this many days are looked for
    deals_max_per_firm: int
    research_unresolved: int              # per run, portfolio companies whose location the listing does not show that are sent to the existing fact research (paid); 0 = none


class RfpGroup(_Strict):
    name: str
    domains: list[str]


class Rfps(_Strict):
    max_searches: int                 # web searches per agency group
    max_due_days: int                 # a due date further out than this is treated as implausible
    groups: list[RfpGroup]


class Ask(_Strict):
    max_rows: int                     # rows kept from a query (more is flagged as truncated)
    max_question_chars: int
    timeout_seconds: float            # a query running longer than this is aborted
    max_cost_usd: float               # per question (the two model calls together)


class SecFill(_Strict):
    max_accounts_per_run: int         # companies whose SEC record is read per `bd sec-fill` (about two requests each)


class SecEventItem(_Strict):
    type: str                         # the signal type this 8-K item becomes
    label: str                        # the item's title, used in the summary
    service_lines: list[str]


class SecEvents(_Strict):
    """Signals read straight from SEC 8-K item codes, with no model call (bd.scanners.sec_events)."""
    max_filings_per_scan: int
    items: dict[str, SecEventItem]    # "2.05" -> what it means

    def summary(self, item: str) -> str:
        return f"Filed an 8-K reporting {self.items[item].label} (Item {item})."


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
    financials: Financials
    sec_fill: SecFill
    sec_events: SecEvents
    rfps: Rfps
    ask: Ask
    briefer: Briefer
    card: Card
    discovery: Discovery
    pe_firms: PeFirms
    pe_portfolio: PePortfolio

    @model_validator(mode="after")
    def _sec_events(self) -> "Config":
        for item, it in self.sec_events.items.items():
            if not re.fullmatch(r"\d\.\d{2}", item):
                raise ValueError(f"sec_events item {item!r} must look like '2.05'")
            if it.type not in SIGNAL_TYPES:
                raise ValueError(f"sec_events item {item} names {it.type!r}, which is not a signal type")
            if not it.service_lines or not set(it.service_lines) <= set(self.service_lines):
                raise ValueError(f"sec_events item {item} needs service_lines from {self.service_lines}")
            if len(self.sec_events.summary(item).split()) > MAX_SUMMARY_WORDS:
                raise ValueError(f"sec_events item {item}: the summary would exceed {MAX_SUMMARY_WORDS} words; shorten the label")
        return self

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
        cfg = Config.model_validate(yaml.safe_load(f))
    # A job that runs several commands gives each the budget still left (see bd.jobs); env only, never from config.yaml.
    try:
        cap = float(os.environ.get("BD_MAX_COST_USD", ""))
    except ValueError:
        cap = None
    if cap is not None and cap >= 0:
        cfg = cfg.model_copy(update={"llm": cfg.llm.model_copy(update={"max_cost_per_run_usd": cap})})
    return cfg


DEFAULT_USER_AGENT = "SEI-BD-Signal-Agent/0.1 (public-source research; set BD_USER_AGENT with a contact address)"


def clean_user_agent(raw: str | None) -> str:
    """Make a pasted BD_USER_AGENT safe as an HTTP header: one ASCII line, no leading/trailing space or stray quotes.
    A trailing newline or space (easy to paste into a hosting dashboard) makes every request fail before it is sent."""
    text = re.sub(r"\s+", " ", (raw or "")).strip()
    text = text.encode("ascii", "replace").decode("ascii").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return text or DEFAULT_USER_AGENT


@dataclass(frozen=True)
class Settings:
    """Environment-only settings. Nothing here is ever read from config.yaml."""

    anthropic_api_key: str | None
    anthropic_model: str | None
    teams_webhook_url: str | None
    db_path: str
    user_agent: str
    user_agent_configured: bool = False
    anthropic_scan_model: str | None = None       # optional: the model the sweep scanner runs on (else ANTHROPIC_MODEL)

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ
        return cls(
            anthropic_api_key=e.get("ANTHROPIC_API_KEY") or None,
            anthropic_model=e.get("ANTHROPIC_MODEL") or None,
            teams_webhook_url=e.get("TEAMS_WEBHOOK_URL") or None,
            db_path=e.get("BD_DB_PATH", "bd.sqlite"),
            user_agent=clean_user_agent(e.get("BD_USER_AGENT")),
            user_agent_configured=bool((e.get("BD_USER_AGENT") or "").strip()),
            anthropic_scan_model=e.get("ANTHROPIC_SCAN_MODEL") or None,
        )

    def require_llm(self) -> tuple[str, str]:
        if not self.anthropic_api_key or not self.anthropic_model:
            raise RuntimeError("ANTHROPIC_API_KEY and ANTHROPIC_MODEL must be set in the environment")
        return self.anthropic_api_key, self.anthropic_model
