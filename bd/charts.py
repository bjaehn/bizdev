"""Small server-drawn charts as inline SVG (no script, no network, no model). Pure functions over numbers the scoring engine already
produced; text from the database is escaped."""

from __future__ import annotations

import math
from collections.abc import Sequence
from html import escape

from bd.priority import Dot

W, BAR_Y, BAR_H = 760, 14, 30
BAND_FILL = {"Hot": "var(--hot)", "Warm": "var(--warm)", "Watch": "var(--accent)"}


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


def _money_tick(v: float) -> str:
    return f"${v / 1e9:g}B" if v >= 1e9 else f"${v / 1e6:g}M"


def priority_svg(dots: Sequence[Dot], bands: Sequence[tuple[str, int]], score_max: int, max_labels: int = 14) -> str:
    """Score (up) against revenue (right, log scale: each gridline is ten times the last). One dot per company: colour = band, size =
    how many different signal types fired, outline = news in the last 30 days. Each dot links to the company's profile. The strongest
    companies are named (as many as fit without overlapping); every dot has a hover title. `bands` is (label, threshold) from the config. '' when there is nothing to draw."""
    if not dots:
        return ""
    lo = math.log10(min(d.revenue for d in dots)) - 0.3
    hi = math.log10(max(d.revenue for d in dots)) + 0.3
    y_lo = min(30, int(min(d.score for d in dots) // 10 * 10))
    y_hi = max(score_max, max(d.score for d in dots)) + 6                  # headroom so a dot at the cap is not cut off
    ix, iy = PW - PL - PR, PH - PT - PB
    X = lambda rev: PL + ix * (math.log10(rev) - lo) / (hi - lo)           # noqa: E731
    Y = lambda sc: PT + iy * (1 - (sc - y_lo) / (y_hi - y_lo))             # noqa: E731
    out = [f'<svg viewBox="0 0 {PW} {PH}" width="100%" role="img" aria-label="Score against company revenue, one dot per company" '
           f'xmlns="http://www.w3.org/2000/svg" style="max-width:{PW}px;font-family:system-ui,sans-serif">']
    # band zones: the score thresholds, drawn as faint strips with the band name at the right
    edges = sorted(((t, name) for name, t in bands), reverse=True)
    for i, (t, name) in enumerate(edges):
        top = Y(min(y_hi, edges[i - 1][0] if i else y_hi))
        bottom = Y(max(y_lo, t))
        out.append(f'<rect x="{PL}" y="{top:.1f}" width="{ix}" height="{max(bottom - top, 0):.1f}" style="fill:{BAND_FILL.get(name, "var(--muted)")};opacity:.07"/>')
        out.append(f'<line x1="{PL}" x2="{PL + ix}" y1="{bottom:.1f}" y2="{bottom:.1f}" style="stroke:{BAND_FILL.get(name, "var(--muted)")};opacity:.45" '
                   f'stroke-dasharray="4 4"/>')
        out.append(f'<text x="{PL + ix + 6}" y="{(top + bottom) / 2 + 4:.1f}" font-size="12" style="fill:{BAND_FILL.get(name, "var(--muted)")}">{escape(name)} {t}+</text>')
    for v in range(int(y_lo // 10 * 10), int(y_hi - 6) + 1, 10):
        if v < y_lo:
            continue
        out.append(f'<text x="{PL - 8}" y="{Y(v) + 4:.1f}" font-size="11" text-anchor="end" style="fill:var(--muted)">{v}</text>')
    d = math.ceil(lo)
    while d <= hi:
        gx = X(10 ** d)
        out.append(f'<line x1="{gx:.1f}" x2="{gx:.1f}" y1="{PT}" y2="{PT + iy}" style="stroke:var(--line)"/>')
        out.append(f'<text x="{gx:.1f}" y="{PT + iy + 16}" font-size="11" text-anchor="middle" style="fill:var(--muted)">{_money_tick(10 ** d)}</text>')
        d += 1
    out.append(f'<line x1="{PL}" x2="{PL + ix}" y1="{PT + iy}" y2="{PT + iy}" style="stroke:var(--muted)"/>')
    out.append(f'<line x1="{PL}" x2="{PL}" y1="{PT}" y2="{PT + iy}" style="stroke:var(--muted)"/>')
    out.append(f'<text x="{PL + ix / 2:.1f}" y="{PH - 6}" font-size="12" text-anchor="middle" style="fill:var(--muted)">Company revenue (log scale)</text>')
    out.append(f'<text transform="translate(12 {PT + iy / 2:.1f}) rotate(-90)" font-size="12" text-anchor="middle" style="fill:var(--muted)">Score</text>')

    radius = lambda dot: 4.5 + 1.6 * min(dot.types, 6)                      # noqa: E731
    boxes = {dot.account_id: (X(dot.revenue) - radius(dot) - 1, Y(dot.score) - radius(dot) - 1, X(dot.revenue) + radius(dot) + 1,
                              Y(dot.score) + radius(dot) + 1) for dot in dots}
    placed: list[tuple[float, float, float, float]] = []      # label boxes already used
    labelled = 0
    # the strongest dots are drawn last, so they sit on top
    for dot in sorted(dots, key=lambda x: (x.score, -x.types)):
        r = radius(dot)
        cx, cy = X(dot.revenue), Y(dot.score)
        fill = BAND_FILL.get(dot.band, "var(--muted)")
        ring = "var(--ink)" if dot.recent else "var(--bg)"
        out.append(f'<a href="/companies/{int(dot.account_id)}"><title>{escape(dot.title)}</title>'
                   f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" style="fill:{fill};fill-opacity:.82;stroke:{ring};stroke-width:{2 if dot.recent else 1.2}"/></a>')
    for dot in sorted(dots, key=lambda x: (-x.score, -x.revenue, x.name)):          # the strongest companies get a name first
        if labelled >= max_labels:
            break
        name = dot.name if len(dot.name) <= 34 else dot.name[:33] + "…"
        w, h = 6.1 * len(name), 13.0
        r = radius(dot)
        cx, cy = X(dot.revenue), Y(dot.score)
        for dx, dy, anchor in ((r + 4, 4, "start"), (-r - 4, 4, "end"), (0, -r - 5, "middle"), (0, r + 14, "middle")):
            x0 = cx + dx - (w if anchor == "end" else w / 2 if anchor == "middle" else 0)
            box = (x0, cy + dy - h + 2, x0 + w, cy + dy + 2)
            if box[0] < PL or box[2] > PW - 4 or box[1] < 2 or box[3] > PT + iy:
                continue
            others = placed + [b for k, b in boxes.items() if k != dot.account_id]        # other labels and other dots
            if any(not (box[2] < b[0] or box[0] > b[2] or box[3] < b[1] or box[1] > b[3]) for b in others):
                continue
            placed.append(box)
            labelled += 1
            out.append(f'<a href="/companies/{int(dot.account_id)}"><text x="{cx + dx:.1f}" y="{cy + dy:.1f}" font-size="12" text-anchor="{anchor}" '
                       f'style="fill:var(--ink);paint-order:stroke;stroke:var(--bg);stroke-width:3px">{escape(name)}</text></a>')
            break
    out.append("</svg>")
    return "".join(out)


TW, TH, TL, TR, TT, TB = 960, 240, 40, 20, 14, 34       # weekly trend chart


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
                           f'style="fill:var(--on-accent)">{n}</text>')
            base += n
        out.append(f'<text x="{x + bw / 2:.1f}" y="{TH - 14}" font-size="11" text-anchor="middle" style="fill:var(--muted)">{escape(label)}</text>')
    out.append(f'<line x1="{TL}" x2="{TL + iw}" y1="{Y(0):.1f}" y2="{Y(0):.1f}" style="stroke:var(--muted)"/>')
    out.append("</svg>")
    return "".join(out)
