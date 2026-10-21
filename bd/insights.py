"""Opportunity themes: what the signals at one company add up to, named by plain rules over the stored signal types.

Nothing here is written by a model and nothing is invented. A theme is a fixed rule ("a leadership signal AND a hiring signal were both
found"); when it fires it carries the stored signals that satisfied it, with their dates and source links, and the service line those
signals were tagged with. The rule is shown beside the theme so a reader can see exactly why it fired. It is read-only and
deterministic, and it never sets or changes a score.

Themes, most specific first (a company can match several; the strongest is its main theme):

  leadership_hiring        new leadership AND a hiring signal
  turnaround               operating or cost pressure AND a leadership change (or interim hiring)
  deal_integration         a deal (M&A, PE add-on, PE hold) AND leadership, hiring or pressure
  tech_build               a technology programme AND leadership or hiring
  leadership_transition    a leadership signal
  team_buildout            a hiring signal
  operating_pressure       restructuring, cost, financial or operating pressure
  deal_activity            a deal signal
  technology_investment    a technology, AI or RFP signal
  strategy_shift           a strategy or rebrand signal

Every signal type belongs to at least one single-signal theme (tests/test_insights.py checks it), so any scored company has a theme."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Sequence

from bd.labels import FAMILY_LABELS, TYPE_LABELS
from bd.models import SIGNAL_TYPES

LEADERSHIP = frozenset({"leadership_change", "leadership_transformation_ai_data", "open_exec_search", "pe_platform_or_sponsor_exec"})
HIRING = frozenset({"hiring_cluster", "hiring_leadership", "hiring_interim", "hiring_single"})
PRESSURE = frozenset({"restructuring_layoffs", "cost_program", "financial_pressure", "financial_distress", "ops_failure", "activist"})
DEALS = frozenset({"ma_divestiture", "pe_addon", "pe_hold_2_4"})
TECHNOLOGY = frozenset({"platform_migration", "vendor_deal", "rfp_match", "ai_commitment"})
STRATEGY = frozenset({"strategy_rebrand"})


@dataclass(frozen=True)
class Need:
    """One requirement of a rule: a signal of any of these types."""
    label: str
    types: frozenset[str]


@dataclass(frozen=True)
class ThemeRule:
    key: str
    name: str
    needs: tuple[Need, ...]                # each requirement must be met by a different signal

    @property
    def rule(self) -> str:
        return "Shown when " + " and ".join(f"a {n.label} signal" for n in self.needs) + (" are both found" if len(self.needs) > 1 else " is found")


N_LEADERSHIP = Need("leadership", LEADERSHIP)
N_HIRING = Need("hiring", HIRING)
N_PRESSURE = Need("restructuring, cost or financial pressure", PRESSURE | {"risk_factor"})
N_DEAL = Need("deal (M&A, PE add-on or PE hold)", DEALS)
N_TECH = Need("technology, AI or RFP", TECHNOLOGY)

# Order is priority for ties; compound themes (more than one requirement) always outrank single ones.
RULES: tuple[ThemeRule, ...] = (
    ThemeRule("leadership_hiring", "New leadership and a hiring push", (N_LEADERSHIP, N_HIRING)),
    ThemeRule("turnaround", "Turnaround: pressure and a leadership change",
              (N_PRESSURE, Need("leadership or interim-hiring", LEADERSHIP | {"hiring_interim"}))),
    ThemeRule("deal_integration", "Integrating a deal",
              (N_DEAL, Need("leadership, hiring or pressure", LEADERSHIP | HIRING | PRESSURE))),
    ThemeRule("tech_build", "Technology programme with people behind it",
              (N_TECH, Need("leadership or hiring", LEADERSHIP | HIRING))),
    ThemeRule("leadership_transition", "Leadership transition", (N_LEADERSHIP,)),
    ThemeRule("team_buildout", "Building a team", (N_HIRING,)),
    ThemeRule("operating_pressure", "Operating or cost pressure", (N_PRESSURE,)),
    ThemeRule("deal_activity", "Deal activity", (N_DEAL,)),
    ThemeRule("technology_investment", "Technology investment", (N_TECH,)),
    ThemeRule("strategy_shift", "Strategy shift", (Need("strategy or rebrand", STRATEGY),)),
)
THEME_NAMES = {r.key: r.name for r in RULES}
assert all(t in SIGNAL_TYPES for r in RULES for n in r.needs for t in n.types)


@dataclass(frozen=True)
class Evidence:
    """One stored, sourced signal that a theme rests on."""
    id: int
    type: str
    family: str
    event_date: date
    summary: str
    url: str
    source_type: str
    confidence: str
    role_or_person: str | None
    service_lines: tuple[str, ...]
    points: float

    @property
    def label(self) -> str:
        return TYPE_LABELS.get(self.type, self.type)


@dataclass
class Insight:
    key: str
    name: str
    rule: str
    evidence: list[Evidence] = field(default_factory=list)     # the signals that satisfied the rule, one per requirement, strongest first
    more: int = 0                                              # other scored signals of the same kinds that are not listed
    service_line: str | None = None

    @property
    def kinds(self) -> list[str]:
        """Signal families behind the theme, in words ('Leadership', 'Hiring')."""
        seen: list[str] = []
        for e in self.evidence:
            k = FAMILY_LABELS.get(e.family, e.family)
            if k not in seen:
                seen.append(k)
        return seen

    @property
    def points(self) -> float:
        return sum(e.points for e in self.evidence)

    @property
    def compound(self) -> bool:
        return len(self.evidence) > 1


def evidence_from_scored(result, rows) -> list[Evidence]:
    """Evidence for every signal that actually scored (Low-confidence and expired signals are not in `result.signals`).

    `result` is a bd.scoring.ScoreResult and `rows` maps signals.id to the stored row (bd.score_store.ScoredAccount.rows)."""
    out: list[Evidence] = []
    for p in result.signals:
        row = rows.get(p.ref) if p.ref is not None else None
        if row is None:
            continue
        out.append(Evidence(
            id=row["id"], type=row["type"], family=p.family, event_date=date.fromisoformat(row["event_date"]), summary=row["summary"],
            url=row["source_url"], source_type=row["source_type"], confidence=row["confidence"], role_or_person=row["role_or_person"],
            service_lines=tuple(json.loads(row["service_lines"] or "[]")), points=p.points))
    return out


def _match(rule: ThemeRule, ranked: Sequence[Evidence]) -> list[Evidence] | None:
    """One distinct signal per requirement (the strongest that fits), or None if some requirement has no signal.
    The scarcest requirement is filled first so a signal that fits two sets is not wasted on the roomier one."""
    used: set[int] = set()
    chosen: dict[int, Evidence] = {}
    order = sorted(range(len(rule.needs)), key=lambda i: sum(e.type in rule.needs[i].types for e in ranked))
    for i in order:
        pick = next((e for e in ranked if e.type in rule.needs[i].types and e.id not in used), None)
        if pick is None:
            return None
        used.add(pick.id)
        chosen[i] = pick
    return sorted(chosen.values(), key=lambda e: (-e.points, e.event_date, e.id))


def best_service_line(evidence: Iterable[Evidence], order: Sequence[str] = ()) -> str | None:
    """The service line the signals were tagged with most strongly (points-weighted); config order breaks ties."""
    weights: dict[str, float] = {}
    for e in evidence:
        for line in e.service_lines:
            weights[line] = weights.get(line, 0.0) + e.points
    rank = {line: i for i, line in enumerate(order)}
    return min(weights, key=lambda line: (-weights[line], rank.get(line, 99), line)) if weights else None


def insights_for(evidence: Sequence[Evidence], service_lines: Sequence[str] = ()) -> list[Insight]:
    """Every theme the signals satisfy, strongest first: compound themes before single ones, then by points."""
    ranked = sorted(evidence, key=lambda e: (-e.points, e.event_date, e.id))
    found: list[tuple[int, Insight]] = []
    for idx, rule in enumerate(RULES):
        chosen = _match(rule, ranked)
        if chosen is None:
            continue
        union = frozenset().union(*(n.types for n in rule.needs))
        more = sum(1 for e in ranked if e.type in union and e.id not in {c.id for c in chosen})
        found.append((idx, Insight(rule.key, rule.name, rule.rule, chosen, more, best_service_line(chosen, service_lines))))
    found.sort(key=lambda t: (-len(RULES[t[0]].needs), -t[1].points, t[0]))
    return [i for _, i in found]


def main_theme(evidence: Sequence[Evidence], service_lines: Sequence[str] = ()) -> Insight | None:
    found = insights_for(evidence, service_lines)
    return found[0] if found else None
