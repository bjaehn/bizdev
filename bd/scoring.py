"""Deterministic scoring engine. Pure functions: no I/O, no clock, no LLM.

signal points = base x recency x confidence x service_match
Within a family the strongest signal counts in full and each further one at
`within_family_decay`. Score = min(max, round(sum(family totals) + stacking bonus)).
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

from bd.config import Config, Scoring
from bd.models import ScoringSignal

BAND_STORED = "Stored"


@dataclass(frozen=True)
class SignalPoints:
    type: str
    family: str
    event_date: str
    confidence: str
    base: float
    recency: float
    confidence_mult: float
    service_mult: float
    points: float
    weight_in_family: float = 1.0  # filled in by score_account
    ref: int | None = None


@dataclass(frozen=True)
class ScoreResult:
    score: int
    band: str
    families: tuple[str, ...]
    family_totals: dict[str, float]
    stacking_bonus: float
    raw_total: float
    signals: tuple[SignalPoints, ...]
    rationale: str
    components: dict = field(default_factory=dict)
    sponsor_lift: float = 0.0
    sequence_bonus: float = 0.0

    def components_json(self) -> str:
        return json.dumps(self.components, sort_keys=True)


def age_days(event_date: date, as_of: date) -> int:
    """Event age in days. A future-dated event (e.g. announced, effective later) counts as age 0."""
    return max(0, (as_of - event_date).days)


def recency_multiplier(type_: str, event_date: date, as_of: date, cfg: Scoring) -> float:
    if type_ in cfg.status_types:
        return cfg.status_multiplier
    age = age_days(event_date, as_of)
    for band in cfg.recency_bands:
        if age <= band.max_days:
            return band.multiplier
    return 0.0


def service_multiplier(service_lines: Iterable[str], valid_lines: Iterable[str], cfg: Scoring) -> float:
    valid = set(valid_lines)
    if any(s in valid for s in service_lines):
        return cfg.service_match_multiplier.match
    return cfg.service_match_multiplier.none


def signal_points(sig: ScoringSignal, as_of: date, cfg: Config) -> SignalPoints:
    s = cfg.scoring
    if sig.type not in s.base_points:
        raise KeyError(f"unknown signal type: {sig.type}")
    if sig.confidence not in s.confidence_multiplier:
        raise KeyError(f"unknown confidence: {sig.confidence}")
    base = s.base_points[sig.type]
    rec = recency_multiplier(sig.type, sig.event_date, as_of, s)
    conf = s.confidence_multiplier[sig.confidence]
    svc = service_multiplier(sig.service_lines, cfg.service_lines, s)
    return SignalPoints(
        type=sig.type,
        family=s.family_of(sig.type),
        event_date=sig.event_date.isoformat(),
        confidence=sig.confidence,
        base=base,
        recency=rec,
        confidence_mult=conf,
        service_mult=svc,
        points=base * rec * conf * svc,
        ref=sig.ref,
    )


def _half_up(x: float) -> int:
    # round(.., 6) first so float noise (e.g. 74.49999999999) cannot flip a .5 boundary
    return math.floor(round(x, 6) + 0.5)


def band_for(score: int, cfg: Scoring) -> str:
    if score >= cfg.bands.hot:
        return "Hot"
    if score >= cfg.bands.warm:
        return "Warm"
    if score >= cfg.bands.watch:
        return "Watch"
    return BAND_STORED


def sponsor_lift(firm_signals: Sequence[tuple[ScoringSignal, str]], as_of: date, cfg: Config) -> tuple[float, list[dict]]:
    """Points a company gets from its sponsor's own activity. Same recency and confidence rules as company signals
    (Low confidence and old events are worth 0); the strongest counts in full, the rest at the within-family decay;
    the total is capped at scoring.sponsor.lift_max. Returns (lift, per-signal details for the audit trail)."""
    s = cfg.scoring
    rows = []
    for sig, firm in firm_signals:
        if sig.type not in s.sponsor.base_points:
            raise KeyError(f"unknown firm signal type: {sig.type}")
        pts = (s.sponsor.base_points[sig.type] * recency_multiplier(sig.type, sig.event_date, as_of, s)
               * s.confidence_multiplier[sig.confidence])
        if pts > 0:
            rows.append({"type": sig.type, "firm": firm, "event_date": sig.event_date.isoformat(),
                         "confidence": sig.confidence, "points": pts, "ref": sig.ref})
    rows.sort(key=lambda r: (-r["points"], r["event_date"], r["type"], r["firm"]))
    total = 0.0
    for i, r in enumerate(rows):
        r["weight"] = 1.0 if i == 0 else s.within_family_decay
        total += r["points"] * r["weight"]
    return min(s.sponsor.lift_max, total), rows


def sequence_matches(scored: Sequence[SignalPoints], cfg: Scoring) -> tuple[float, list[dict]]:
    """Patterns present in the scored signals and the bonus they earn. For each configured pattern: the latest `then` signal that
    has a `first` signal on or before it within `within_days`, paired with the closest such `first`. Each pattern counts once;
    the total is capped at `sequence_bonus_max`. Deterministic: ties go to the later date, then the type name."""
    matches: list[dict] = []
    for q in cfg.sequences:
        firsts = [p for p in scored if p.type in q.first]
        thens = sorted((p for p in scored if p.type in q.then), key=lambda p: (_neg_date(p.event_date), p.type))
        found = None
        for b in thens:
            bd_ = date.fromisoformat(b.event_date)
            cands = [(( bd_ - date.fromisoformat(a.event_date)).days, a.type, a) for a in firsts
                     if 0 <= (bd_ - date.fromisoformat(a.event_date)).days <= q.within_days]
            if cands:
                gap, _, a = min(cands, key=lambda c: (c[0], c[1]))
                found = {"name": q.name, "label": q.label, "bonus": q.bonus, "gap_days": gap,
                         "first": {"type": a.type, "event_date": a.event_date}, "then": {"type": b.type, "event_date": b.event_date}}
                break
        if found:
            matches.append(found)
    matches.sort(key=lambda m: (-m["bonus"], m["name"]))
    return min(cfg.sequence_bonus_max, sum(m["bonus"] for m in matches)), matches


def score_account(signals: Sequence[ScoringSignal], as_of: date, cfg: Config,
                  firm_signals: Sequence[tuple[ScoringSignal, str]] = ()) -> ScoreResult:
    s = cfg.scoring
    # Signals worth 0 points (Low confidence, older than the last recency band) are dropped
    # before grouping so they can never count as a family for the stacking bonus.
    scored = [p for p in (signal_points(x, as_of, cfg) for x in signals) if p.points > 0]

    by_family: dict[str, list[SignalPoints]] = {}
    for p in scored:
        by_family.setdefault(p.family, []).append(p)

    family_totals: dict[str, float] = {}
    weighted: list[SignalPoints] = []
    for fam in sorted(by_family):
        # strongest first; ties broken by newest event then type name for determinism
        ranked = sorted(by_family[fam], key=lambda p: (-p.points, _neg_date(p.event_date), p.type))
        total = 0.0
        for i, p in enumerate(ranked):
            w = 1.0 if i == 0 else s.within_family_decay
            total += p.points * w
            weighted.append(SignalPoints(**{**asdict(p), "weight_in_family": w}))
        family_totals[fam] = total

    n_fam = len(family_totals)
    bonus = min(s.stacking_bonus_max, s.stacking_bonus_per_family * max(0, n_fam - 1))
    # Sponsor activity only lifts a company that already has a scoring signal of its own; it never creates a lead.
    lift, lift_rows = sponsor_lift(firm_signals, as_of, cfg) if family_totals else (0.0, [])
    seq_bonus, seq_rows = sequence_matches(scored, s) if family_totals else (0.0, [])
    raw = sum(family_totals.values()) + bonus + seq_bonus + lift
    score = min(s.score_max, _half_up(raw))
    band = band_for(score, s)
    families = tuple(sorted(family_totals))

    components = {
        "as_of": as_of.isoformat(),
        "families": {
            fam: {
                "total": round(family_totals[fam], 4),
                "signals": [asdict(p) for p in weighted if p.family == fam],
            }
            for fam in families
        },
        "stacking_bonus": bonus,
        "sequences": {"bonus": seq_bonus, "cap": s.sequence_bonus_max, "matches": seq_rows},
        "sponsor": {"lift": round(lift, 4), "cap": s.sponsor.lift_max, "signals": lift_rows},
        "raw_total": round(raw, 4),
        "score_cap": s.score_max,
    }
    return ScoreResult(
        score=score,
        band=band,
        families=families,
        family_totals=family_totals,
        stacking_bonus=bonus,
        raw_total=raw,
        signals=tuple(weighted),
        rationale=_rationale(family_totals, bonus, score, s.score_max, lift, seq_bonus),
        components=components,
        sponsor_lift=lift,
        sequence_bonus=seq_bonus,
    )


def _neg_date(iso: str) -> int:
    return -date.fromisoformat(iso).toordinal()


def _rationale(family_totals: dict[str, float], bonus: float, score: int, cap: int, lift: float = 0.0, seq: float = 0.0) -> str:
    if not family_totals:
        return "No scoreable signals."
    parts = [f"{fam} {tot:.1f}" for fam, tot in family_totals.items()]
    text = " + ".join(parts)
    if bonus:
        text += f" + stacking bonus {bonus:g}"
    if seq:
        text += f" + pattern bonus {seq:g}"
    if lift:
        text += f" + sponsor activity {lift:.1f}"
    text += f" = {score}"
    if score == cap:
        text += " (capped)"
    return text


# ---------------------------------------------------------------- post gate

def has_recent_high_confidence(signals: Iterable[ScoringSignal], as_of: date, cfg: Config) -> bool:
    window = cfg.post_gate.require_high_confidence_within_days
    return any(x.confidence == "High" and age_days(x.event_date, as_of) <= window for x in signals)


def is_postable(result: ScoreResult, signals: Sequence[ScoringSignal], as_of: date, cfg: Config) -> bool:
    """Band must be posted and at least one High-confidence signal within the gate window."""
    return result.band in cfg.scoring.posted_bands and has_recent_high_confidence(signals, as_of, cfg)


@dataclass(frozen=True)
class PriorPost:
    score: int
    families: frozenset[str]
    cooldown_until: date


def cooldown_until(posted_on: date, cfg: Config) -> date:
    return posted_on + timedelta(weeks=cfg.post_gate.cooldown_weeks)


def cooldown_allows(prior: PriorPost | None, result: ScoreResult, as_of: date, cfg: Config) -> bool:
    """True if the account may be posted again: no prior post, cooldown expired, a new
    family has appeared, or the score has risen by the configured amount."""
    if prior is None or as_of >= prior.cooldown_until:
        return True
    if set(result.families) - set(prior.families):
        return True
    return result.score - prior.score >= cfg.post_gate.cooldown_break_score_rise


def cap_leads(leads: Sequence[tuple[int, int]], cfg: Config) -> list[tuple[int, int]]:
    """leads = (account_id, score). Highest score first, account_id as stable tiebreak,
    truncated to max_leads_per_post."""
    ranked = sorted(leads, key=lambda t: (-t[1], t[0]))
    return ranked[: cfg.post_gate.max_leads_per_post]
