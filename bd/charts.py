"""Small server-drawn charts as inline SVG (no script, no network, no model). Pure functions over numbers the scoring engine already
produced; text from the database is escaped."""

from __future__ import annotations

import math
from collections.abc import Sequence
from html import escape

W, BAR_Y, BAR_H = 760, 14, 30
BAND_FILL = {"Hot": "var(--hotbg)", "Warm": "var(--warmbg)", "Watch": "var(--accent)"}
BAND_TEXT = {"Hot": "var(--hotfg)", "Warm": "var(--warmfg)", "Watch": "var(--on-accent)"}


def breakdown_svg(parts: list[tuple[str, float, str]], total: float, score: int, score_cap: int) -> str:
    """One stacked bar: each segment is a signal type (or bonus), as wide as the points it added. `parts`: (label, points, color).
    The bar ends at the raw total; when the total is above the cap the caption says the score was capped. '' when there is nothing."""
    parts = [(lbl, pts, col) for lbl, pts, col in parts if pts > 0]
    total = sum(p for _, p, _ in parts)
    if not parts or total <= 0:
        return ""
    out = [f'<svg viewBox="0 0 {W} {BAR_Y + BAR_H + 22}" width="100%" role="img" aria-label="Where the score of {score} comes from" '
           f'xmlns="http://www.w3.org/2000/svg" style="max-width:{W}px">']
    x = 0.0
    for lbl, pts, col in parts:
        w = W * pts / total
        out.append(f'<rect x="{x:.1f}" y="{BAR_Y}" width="{max(w - 1.5, 1):.1f}" height="{BAR_H}" rx="2" fill="{col}">'
                   f'<title>{escape(lbl)}: {pts:.1f} points</title></rect>')
        if w >= 38:
            out.append(f'<text x="{x + w / 2:.1f}" y="{BAR_Y + BAR_H / 2 + 4.5:.1f}" font-size="12" font-weight="600" text-anchor="middle" '
                       f'fill="#fff" style="font-family:system-ui,sans-serif">{pts:.0f}</text>')
        x += w
    cap_note = f" (capped at {score_cap})" if total > score_cap else ""
    out.append(f'<text x="0" y="{BAR_Y + BAR_H + 17}" font-size="12" style="fill:var(--muted);font-family:system-ui,sans-serif">'
               f'{total:.0f} points in total{escape(cap_note)} = score {score}</text>')
    out.append("</svg>")
    return "".join(out)


PW, PH, PL, PR, PT, PB = 960, 400, 46, 84, 20, 38        # priority chart: size, then margins


TW, TH, TL, TR, TT, TB = 960, 240, 40, 20, 14, 34       # run-by-run band trend (Market view)


def trend_svg(points: Sequence[tuple[str, int, int, int]], max_bars: int = 12) -> str:
    """One stacked bar per scoring run, oldest on the left: Hot at the bottom, then Warm, then Watch. `points`: (label, hot, warm, watch).
    '' when there are no runs (or no company in any band)."""
    points = list(points)[-max_bars:]
    top = max((h + w + c for _, h, w, c in points), default=0)
    if top <= 0:
        return ""
    step = 1 if top <= 6 else 2 if top <= 14 else 5 if top <= 40 else 10
    y_hi = -(-top // step) * step
    iw, ih = TW - TL - TR, TH - TT - TB
    Y = lambda v: TT + ih * (1 - v / y_hi)                                      # noqa: E731
    slot = iw / len(points)
    bw = min(64.0, slot * 0.62)
    out = [f'<svg viewBox="0 0 {TW} {TH}" width="100%" role="img" aria-label="Hot, Warm and Watch companies at each scoring run" '
           f'xmlns="http://www.w3.org/2000/svg" style="max-width:{TW}px;font-family:system-ui,sans-serif">']
    for v in range(0, y_hi + 1, step):
        out.append(f'<line x1="{TL}" x2="{TL + iw}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" style="stroke:var(--line)"/>')
        out.append(f'<text x="{TL - 8}" y="{Y(v) + 4:.1f}" font-size="11" text-anchor="end" style="fill:var(--muted)">{v}</text>')
    for i, (label, hot, warm, watch) in enumerate(points):
        x = TL + slot * i + (slot - bw) / 2
        base = 0
        for name, n in (("Hot", hot), ("Warm", warm), ("Watch", watch)):
            if n <= 0:
                continue
            y1, y0 = Y(base + n), Y(base)
            out.append(f'<rect x="{x:.1f}" y="{y1:.1f}" width="{bw:.1f}" height="{y0 - y1 - 1:.1f}" rx="2" style="fill:{BAND_FILL[name]};'
                       f'fill-opacity:{1 if name != "Watch" else .55}"><title>{escape(label)}: {n} {name}</title></rect>')
            if y0 - y1 >= 15:
                out.append(f'<text x="{x + bw / 2:.1f}" y="{(y0 + y1) / 2 + 4:.1f}" font-size="12" font-weight="600" text-anchor="middle" '
                           f'style="fill:{BAND_TEXT[name]}">{n}</text>')
            base += n
        out.append(f'<text x="{x + bw / 2:.1f}" y="{TH - 14}" font-size="11" text-anchor="middle" style="fill:var(--muted)">{escape(label)}</text>')
    out.append(f'<line x1="{TL}" x2="{TL + iw}" y1="{Y(0):.1f}" y2="{Y(0):.1f}" style="stroke:var(--muted)"/>')
    out.append("</svg>")
    return "".join(out)


KIND_FILL = {"leadership": "var(--k-lead)", "hiring": "var(--k-hire)", "press": "var(--k-press)", "events": "var(--k-event)"}
WW, WH, WPL, WPR, WPT, WPB = 1000, 300, 34, 10, 26, 36       # weekly chart: size, then margins (drawn for the full width of the page)
WBAR, WGAP, WRADIUS = 24, 2, 4                                # column thickness cap, surface gap between stacked segments, rounded data-end


def _nice_step(top: int) -> int:
    return 1 if top <= 4 else 2 if top <= 10 else 5 if top <= 25 else 10 if top <= 60 else 20


def weekly_svg(weeks: Sequence, series: dict[str, list[int]], labels: dict[str, str], width: int = WW, height: int = WH,
               last: int | None = None, fs: int = 12) -> str:
    """Signals found per week as thin stacked columns, one colour per kind (the kind tokens in design.css), drawn for the full width of the page.
    `weeks` are the dates of each week's last day, oldest first; `series` maps kind -> counts per week. Columns grow from one baseline, are
    separated by a thin gap of surface colour, round only their top end, and the latest week carries its total; the other numbers are in the
    grid, the hover titles and the table beside the chart. `width`/`height` are the drawing's own units (it scales to the container); `last`
    keeps only the newest weeks and `fs` sets the text size, so a narrower drawing can be made for a phone. '' when nothing was found."""
    if last:
        weeks, series = weeks[-last:], {k: v[-last:] for k, v in series.items()}
    n = len(weeks)
    totals = [sum(series[k][i] for k in series) for i in range(n)]
    if not n or not any(totals):
        return ""
    top = max(totals)
    step = _nice_step(top)
    ymax = math.ceil(top / step) * step
    iw, ih = width - WPL - WPR, height - WPT - WPB
    slot = iw / n
    bw = min(WBAR, slot - 16)
    out = [f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" aria-label="Signals found per week, by kind, for the last {n} weeks" '
           f'xmlns="http://www.w3.org/2000/svg">']
    for g in range(0, ymax + 1, step):
        y = WPT + ih * (1 - g / ymax)
        out.append(f'<line x1="{WPL}" x2="{width - WPR}" y1="{y:.1f}" y2="{y:.1f}" stroke="var(--line)" stroke-width="1"/>'
                   f'<text x="{WPL - 8}" y="{y + 4:.1f}" font-size="{fs}" text-anchor="end" style="fill:var(--muted)">{g}</text>')
    for i, d in enumerate(weeks):
        x = WPL + i * slot + (slot - bw) / 2
        kinds = [k for k in series if series[k][i]]
        base = height - WPB
        for pos, kind in enumerate(kinds):
            v = series[kind][i]
            h = ih * v / ymax
            top_seg = pos == len(kinds) - 1
            y, hh = base - h + (0 if top_seg else WGAP), max(h - (0 if top_seg else WGAP), 1.0)
            fill = KIND_FILL.get(kind, "var(--muted)")
            tip = f"<title>Week ending {d:%b} {d.day}: {v} {escape(labels.get(kind, kind).lower())}</title>"
            if top_seg:                                              # square at the baseline, rounded at the data end
                r = min(WRADIUS, hh)
                out.append(f'<path d="M{x:.1f},{y + hh:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} H{x + bw - r:.1f} '
                           f'Q{x + bw:.1f},{y:.1f} {x + bw:.1f},{y + r:.1f} V{y + hh:.1f} Z" fill="{fill}">{tip}</path>')
            else:
                out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{hh:.1f}" fill="{fill}">{tip}</rect>')
            base -= h
        latest = i == n - 1
        if latest and totals[i]:
            out.append(f'<text x="{x + bw / 2:.1f}" y="{base - 6:.1f}" font-size="{fs + 1}" font-weight="700" text-anchor="middle" style="fill:var(--ink)">{totals[i]}</text>')
        out.append(f'<text x="{x + bw / 2:.1f}" y="{height - 12}" font-size="{fs}" text-anchor="middle" '
                   f'style="fill:var({"--ink" if latest else "--muted"})"{" font-weight=\"700\"" if latest else ""}>{d:%b} {d.day}</text>')
    out.append("</svg>")
    return "".join(out)


def spark_svg(values: Sequence[float], width: int = 120, height: int = 34, label: str = "Score over recent runs") -> str:
    """A small trend line through the values (oldest first) with a dot on the latest. '' for fewer than two values."""
    vals = list(values)
    if len(vals) < 2:
        return ""
    hi, lo = max(max(vals), 1), min(min(vals), 0)
    span = (hi - lo) or 1
    pts = [(3 + i * (width - 6) / (len(vals) - 1), height - 4 - (v - lo) / span * (height - 10)) for i, v in enumerate(vals)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    return (f'<svg class="spark" viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img" aria-label="{escape(label)}" '
            f'xmlns="http://www.w3.org/2000/svg"><polyline points="{line}" fill="none" stroke="var(--accent)" stroke-width="2.2" '
            f'stroke-linejoin="round" stroke-linecap="round"/><circle cx="{pts[-1][0]:.1f}" cy="{pts[-1][1]:.1f}" r="3.6" fill="var(--accent)"/></svg>')
