"""The signal matrix: one row per company, one column per signal type, so a person sees at a glance which companies have SEVERAL kinds of
signals pointing the same way, and why the score is what it is.

Read-only and deterministic: it only re-arranges what `bd.opportunities` already worked out from the stored signals (the points per
signal type come from the scoring engine; nothing is recomputed or invented here). Cell shade is points relative to SHADE_FULL; the
number in the cell is the points themselves, so colour is never the only carrier of meaning."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from bd.config import Config
from bd.labels import FAMILY_LABELS, TYPE_LABELS, fmt_date
from bd.opportunities import Opportunity

SHADE_FULL = 40.0        # points in one cell that get the darkest shade (a strongest-in-family signal is worth up to ~30)
RECENT_DAYS = 30         # a dot marks a signal type that fired this recently
SORTS = ("score", "types", "newest", "change")


@dataclass(frozen=True)
class Cell:
    family: str
    points: float            # points this signal type earned (0 = none)
    shade: float             # 0..1
    recent: bool             # newest signal of this type is within RECENT_DAYS
    count: int               # scored signals of this type
    newest: date | None
    title: str               # hover text: what fired, when, how many


@dataclass
class Row:
    opp: Opportunity
    cells: list[Cell]
    types: int               # signal types that fired (the number the stacking bonus rewards)
    newest: date | None      # newest signal of any type
    score_pct: int           # score as a share of the maximum, for the bar

    @property
    def active_families(self) -> list[str]:
        return [c.family for c in self.cells if c.points]


@dataclass
class Matrix:
    columns: list[tuple[str, str]]               # (family key, label), in config order
    rows: list[Row] = field(default_factory=list)


def columns(cfg: Config) -> list[tuple[str, str]]:
    return [(f, FAMILY_LABELS.get(f, f)) for f in cfg.scoring.families]


def _cell(o: Opportunity, family: str, as_of: date) -> Cell:
    sigs = [p for p in o.result.signals if p.family == family]
    pts = float(o.result.family_totals.get(family, 0.0))
    if family == "pe":
        pts += float(o.result.sponsor_lift or 0.0)          # the owner's own news lifts the private-equity column
    if not sigs and pts <= 0:
        return Cell(family, 0.0, 0.0, False, 0, None, "")
    newest = max((date.fromisoformat(p.event_date) for p in sigs), default=None)
    top = max(sigs, key=lambda p: (p.points, p.event_date), default=None)
    bits = [FAMILY_LABELS.get(family, family) + f": {pts:.1f} points"]
    if top is not None:
        bits.append(f"strongest {TYPE_LABELS.get(top.type, top.type)}, {fmt_date(date.fromisoformat(top.event_date))}"
                    + (f" (+{len(sigs) - 1} more)" if len(sigs) > 1 else ""))
    if family == "pe" and o.result.sponsor_lift:
        bits.append(f"includes +{o.result.sponsor_lift:.1f} from the owner's own news")
    recent = bool(newest) and (as_of - newest) <= timedelta(days=RECENT_DAYS)
    return Cell(family, pts, min(1.0, pts / SHADE_FULL), recent, len(sigs), newest, " · ".join(bits))


def build_matrix(items: list[Opportunity], cfg: Config, as_of: date) -> Matrix:
    cols = columns(cfg)
    out = Matrix(cols)
    for o in items:
        cells = [_cell(o, f, as_of) for f, _ in cols]
        newest = max((c.newest for c in cells if c.newest), default=None)
        out.rows.append(Row(o, cells, len(o.result.families), newest, round(100 * o.result.score / max(1, cfg.scoring.score_max))))
    return out


def sort_rows(rows: list[Row], key: str) -> list[Row]:
    """score (the ranking), types (most signal types first), newest (most recent signal first), change (biggest move since last run)."""
    if key == "types":
        return sorted(rows, key=lambda r: (-r.types, -r.opp.result.score, r.opp.rank))
    if key == "newest":
        return sorted(rows, key=lambda r: (r.newest is None, -(r.newest.toordinal() if r.newest else 0), r.opp.rank))
    if key == "change":
        return sorted(rows, key=lambda r: (-(r.opp.delta if r.opp.delta is not None else r.opp.result.score), r.opp.rank))
    return sorted(rows, key=lambda r: r.opp.rank)
