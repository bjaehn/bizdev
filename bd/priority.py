"""The priority chart's data: one dot per company, score against size (revenue), so the biggest prizes with the strongest signals stand out.

Read-only and deterministic: it only re-arranges what `bd.opportunities` already worked out (the score and band come from the scoring
engine; revenue is the figure on file for the company). A company with no revenue on file cannot be placed on a size axis, so it is
listed apart rather than drawn at a guessed size."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from bd.labels import FAMILY_LABELS
from bd.opportunities import Opportunity

RECENT_DAYS = 30


@dataclass(frozen=True)
class Dot:
    account_id: int
    name: str
    industry: str
    band: str
    score: int
    revenue: float
    types: int                  # signal types that fired
    recent: bool                # a signal in the last RECENT_DAYS
    client: bool
    title: str                  # hover text


@dataclass
class Priority:
    dots: list[Dot] = field(default_factory=list)
    unplaced: list[tuple[int, str, int]] = field(default_factory=list)     # (account id, name, score): no revenue on file


def build_priority(items: list[Opportunity], as_of: date) -> Priority:
    out = Priority()
    for o in items:
        a, r = o.account, o.result
        if not a.revenue or a.revenue <= 0:
            out.unplaced.append((a.id, a.name, r.score))
            continue
        newest = max((date.fromisoformat(p.event_date) for p in r.signals), default=None)
        fams = ", ".join(FAMILY_LABELS.get(f, f) for f in r.families)
        title = f"{a.name}: {r.band} {r.score} · revenue ${a.revenue / 1e9:.1f}B" if a.revenue >= 1e9 else f"{a.name}: {r.band} {r.score} · revenue ${a.revenue / 1e6:.0f}M"
        if fams:
            title += f" · {len(r.families)} signal type{'s' if len(r.families) != 1 else ''}: {fams}"
        out.dots.append(Dot(a.id, a.name, (a.industry or "").strip(), r.band, r.score, float(a.revenue), len(r.families),
                            bool(newest) and (as_of - newest) <= timedelta(days=RECENT_DAYS), o.client, title))
    out.dots.sort(key=lambda d: (-d.score, -d.revenue, d.name))
    out.unplaced.sort(key=lambda t: (-t[2], t[1]))
    return out
