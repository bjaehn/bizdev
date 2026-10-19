"""A company's brief: what its signals add up to (a named theme with the evidence), what happened (with sources), and how the score is built,
from what the scoring engine stored.

Read-only and deterministic (no model, no network). Reuses the dossier (signals, timeline, postings, owners) and adds the explanation of
the score: points per signal type, the stacking bonus for different signal types agreeing, any pattern bonus and sponsor lift."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date

from bd.accounts import Account, can_check_free
from bd.charts import breakdown_svg
from bd.config import Config
from bd.dossier import build_dossier
from bd.labels import FAMILY_LABELS, TYPE_LABELS, fmt_date, money
from bd.timeline import FAMILY_COLORS
from bd.trend import score_history
from bd.week import signs_line

EVIDENCE_MAX = 12

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


def facts_line(a: Account) -> str:
    bits = [a.industry, a.hq_city, a.ownership, f"{money(a.revenue)} revenue" if a.revenue else None,
            f"{a.employees:,} employees" if a.employees else None]
    return " · ".join(b for b in bits if b)


def build_profile(conn: sqlite3.Connection, cfg: Config, a: Account, today: date) -> dict:
    d = build_dossier(conn, cfg, a, today)
    d["explain"] = explain(d["opp"], cfg) if d["opp"] else None
    d["signs"] = signs_line(d["opp"].result) if d["opp"] else ""
    d["history"] = score_history(conn, [a.id]).get(a.id, [])
    d["unverified"] = d["fit"].unverified and a.status != "excluded"
    d["facts"] = facts_line(a)
    d["can_check"] = can_check_free(a)
    return d


def brief_text(d: dict, hiring_days: int, safe_url=lambda u: u) -> str:
    """The same brief as plain text, to paste into an email or a chat. Only what is stored: no wording of our own about the company,
    and a link only where it is a real http(s) page."""
    a, opp = d["a"], d["opp"]
    out = [a.name, d["facts"] or "No company details on file"]
    if opp:
        out.append(f"{opp.result.band} {opp.result.score}" + (f" · {d['signs']}" if d["signs"] else "") + (" · existing SEI client" if d["state"].client else ""))
        if opp.theme:
            t = opp.theme
            out += ["", "WHAT IT ADDS UP TO", t.name + (f" (points to {t.service_line})" if t.service_line else "") + ". " + t.rule + ".",
                    *(f"- {fmt_date(e.event_date)} · {e.label}: {e.summary}" + (f" Source: {safe_url(e.url)}" if safe_url(e.url) else "") for e in t.evidence)]
            if len(opp.themes) > 1:
                out.append("Also: " + ", ".join(x.name for x in opp.themes[1:]) + ".")
    else:
        out.append("Not scored yet." if d["signals"] else ("No signals found." if a.last_scanned else "Not scanned yet."))
    if d["signals"]:
        out += ["", "WHAT HAPPENED"]
        for s in d["signals"][:EVIDENCE_MAX]:
            who = f" ({s['role_or_person']})" if s["role_or_person"] else ""
            link = safe_url(s["source_url"])
            out.append(f"- {fmt_date(date.fromisoformat(s['event_date']))} · {TYPE_LABELS.get(s['type'], s['type'])}: {s['summary']}{who}"
                       + (f" Source: {link}" if link else ""))
    else:
        out += ["", "WHAT HAPPENED", "No signals found."]
    ctx = []
    if d["unverified"]:
        ctx.append("Not confirmed: " + "; ".join(d["fit"].reasons) + ". That does not stop it being scanned for signals.")
    if d["firms"]:
        ctx.append("Owned by " + ", ".join(d["firms"]) + ".")
    if d["postings"]:
        ctx.append(f"{d['postings']} open job posting{'' if d['postings'] == 1 else 's'} in the last {hiring_days} days"
                   + (f", {d['leadership_postings']} at leadership level." if d["leadership_postings"] else "."))
    if ctx:
        out += ["", "CONTEXT", *ctx]
    out += ["", f"Built {fmt_date(d['today'])} from public sources by BD Signals. Research only; every link is a page that was retrieved when found."]
    return "\n".join(out) + "\n"
