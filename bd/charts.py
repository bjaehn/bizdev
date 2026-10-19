"""Small server-drawn charts as inline SVG (no script, no network, no model). Pure functions over numbers the scoring engine already
produced; text from the database is escaped."""

from __future__ import annotations

from html import escape

W, BAR_Y, BAR_H = 760, 14, 30


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
