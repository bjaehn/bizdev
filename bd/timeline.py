"""A signal timeline as a small inline SVG (no script, no network, no model).

Pure function over already-stored rows. Every dot is one signal, placed by its event date, colored by signal group, with the date,
type and confidence in a hover tooltip. Text from the database is escaped."""

from __future__ import annotations

from datetime import date, timedelta
from html import escape

from bd.labels import FAMILY_LABELS, TYPE_LABELS

FAMILY_COLORS = {"leadership": "#2563eb", "hiring": "#16a34a", "events": "#d97706", "pe": "#9333ea", "technology": "#0891b2",
                 "procurement": "#dc2626", "filings": "#6b7280", "financials": "#be185d"}
W, H, PAD_L, PAD_R, TOP, AXIS_Y, ROW = 760, 96, 12, 12, 14, 72, 12


def timeline_svg(signals: list[dict], as_of: date, days: int = 180) -> str:
    """`signals`: dicts with event_date (ISO), family, type, confidence. Signals outside [as_of - days, as_of] are left out; the
    caller can say how many. Returns '' when there is nothing to draw."""
    start = as_of - timedelta(days=days)
    inside = []
    for s in signals:
        try:
            d = date.fromisoformat(s["event_date"])
        except (KeyError, ValueError):
            continue
        if start <= d <= as_of:
            inside.append((d, s))
    if not inside:
        return ""
    inside.sort(key=lambda t: (t[0], t[1].get("type", "")))
    span = (W - PAD_L - PAD_R)

    def x(d: date) -> float:
        return PAD_L + span * (d - start).days / days

    parts = [f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" aria-label="Signals over the last {days} days" '
             f'xmlns="http://www.w3.org/2000/svg" style="max-width:{W}px">',
             f'<line x1="{PAD_L}" y1="{AXIS_Y}" x2="{W - PAD_R}" y2="{AXIS_Y}" style="stroke:var(--line)" stroke-width="1"/>']
    # month ticks
    m = date(start.year, start.month, 1)
    while m <= as_of:
        if m >= start:
            parts.append(f'<line x1="{x(m):.1f}" y1="{AXIS_Y}" x2="{x(m):.1f}" y2="{AXIS_Y + 5}" style="stroke:var(--muted)"/>'
                         f'<text x="{x(m):.1f}" y="{AXIS_Y + 18}" font-size="10" text-anchor="middle" style="fill:var(--muted)">{m:%b}</text>')
        m = date(m.year + (m.month == 12), m.month % 12 + 1, 1)
    stack: dict[int, int] = {}
    for d, s in inside:
        col = round(x(d) / 10)                       # dots closer than ~10px stack upward instead of overlapping
        row = stack.get(col, 0)
        stack[col] = row + 1
        cy = AXIS_Y - 8 - row * ROW
        if cy < TOP:
            cy = TOP
        fam = s.get("family", "")
        tip = f"{d:%b} {d.day}, {d.year}: {TYPE_LABELS.get(s.get('type', ''), s.get('type', ''))} ({s.get('confidence', '')})"
        parts.append(f'<circle cx="{x(d):.1f}" cy="{cy}" r="5" fill="{FAMILY_COLORS.get(fam, "#6b7280")}"'
                     f'{"" if s.get("confidence") == "High" else " fill-opacity=\".55\""}><title>{escape(tip)}</title></circle>')
    parts.append("</svg>")
    return "".join(parts)


def legend(signals: list[dict]) -> list[tuple[str, str]]:
    """(label, color) for the signal groups present, in a stable order."""
    fams = sorted({s.get("family", "") for s in signals if s.get("family")})
    return [(FAMILY_LABELS.get(f, f), FAMILY_COLORS.get(f, "#6b7280")) for f in fams]
