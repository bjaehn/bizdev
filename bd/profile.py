"""A company's analysis profile: its score, why it is that score, and the evidence, from what the scoring engine stored.

Read-only and deterministic (no model, no network). Reuses the dossier (signals, timeline, postings, owners) and adds the explanation of
the score: points per signal type, the stacking bonus for different signal types agreeing, any pattern bonus and sponsor lift."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date

from bd.accounts import Account
from bd.charts import breakdown_svg
from bd.config import Config
from bd.dossier import build_dossier
from bd.labels import FAMILY_LABELS
from bd.timeline import FAMILY_COLORS

BONUS_COLOR, PATTERN_COLOR, SPONSOR_COLOR = "#475569", "#0d9488", "#9333ea"


@dataclass
class Part:
    key: str
    label: str
    points: float
    color: str
    note: str = ""            # the strongest signal behind it, with its date


@dataclass
class Explanation:
    parts: list[Part] = field(default_factory=list)
    sentence: str = ""
    svg: str = ""
    raw: float = 0.0
    score: int = 0
    capped: bool = False


def explain(opp, cfg: Config) -> Explanation:
    """Why this score: one part per signal type that earned points, then the bonuses, in the order they were added."""
    r = opp.result
    reasons = {x.kind: x for x in opp.reasons}
    parts = [Part(f, FAMILY_LABELS.get(f, f), float(t), FAMILY_COLORS.get(f, "#6b7280"), reasons[f].headline if f in reasons else "")
             for f, t in sorted(r.family_totals.items(), key=lambda kv: -kv[1]) if t > 0]
    n = len(r.families)
    if r.stacking_bonus:
        parts.append(Part("bonus", "Stacking bonus", float(r.stacking_bonus), BONUS_COLOR, f"{n} different signal types agree"))
    if r.sequence_bonus:
        parts.append(Part("pattern", "Pattern bonus", float(r.sequence_bonus), PATTERN_COLOR,
                          "; ".join(x.headline for x in opp.reasons if x.kind == "sequence")))
    if r.sponsor_lift:
        parts.append(Part("sponsor", "Sponsor activity", float(r.sponsor_lift), SPONSOR_COLOR,
                          reasons["sponsor"].headline if "sponsor" in reasons else "the owner's own news"))
    labels = [FAMILY_LABELS.get(f, f) for f in r.families]
    if n >= 2:
        sentence = (f"{n} different signal types agree ({', '.join(labels)}), which adds a stacking bonus of +{r.stacking_bonus:g}. "
                    "Agreement across independent signals is the strongest sign of a real need.")
    elif n == 1:
        sentence = (f"Only one signal type has fired ({labels[0]}), so there is no stacking bonus yet. "
                    "The score rises when other kinds of signal appear for this company.")
    else:
        sentence = "No scoring signals yet."
    raw = float(r.raw_total)
    return Explanation(parts, sentence, breakdown_svg([(p.label, p.points, p.color) for p in parts], raw, r.score, cfg.scoring.score_max),
                       raw, r.score, raw > cfg.scoring.score_max)


def build_profile(conn: sqlite3.Connection, cfg: Config, a: Account, today: date) -> dict:
    d = build_dossier(conn, cfg, a, today)
    d["explain"] = explain(d["opp"], cfg) if d["opp"] else None
    return d
