"""The private-equity network as inline SVG (no script, no network, no model): a market map of sponsors and the companies they hold, and a one-firm view.

Pure functions over a bd.pe_network.Universe. Layout is deterministic and needs no random numbers or physics: firms sit on a ring (connected firms next to each
other, wider where they hold more), a company only one firm holds sits near that firm, a company several firms hold sits between them (nearer the middle the
further apart they are), and a company that would land on another is moved to the nearest free spot. Every node is a link to its page (links are allowed by the
page's security policy; scripts are not) with its plain-language description in a <title>. Text from the database is escaped. All colour and line style come from
classes defined in design.css, never from this file."""

from __future__ import annotations

import math
from html import escape

from bd.opportunity_model import CATEGORY_ORDER
from bd.pe_network import CoNode, FirmNode, Tie, Universe

TAU = math.tau
LINE_CLASS = {"current": "current", "pending": "pending", "unclear": "lead", "historical": "past"}
LINE_WORD = {"current": "current", "pending": "pending", "unclear": "a lead, not confirmed", "historical": "past"}
GOLDEN = 2.399963229728653
LABELLED = ("now", "validate")           # in the market map only these companies carry a printed name; every dot has a title and the table lists them all


def _f(x: float) -> str:
    return f"{x:.1f}"


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _prio(n: CoNode) -> tuple[int, int, str]:
    return (CATEGORY_ORDER.index(n.category), -n.events, n.name.lower())


def _ring(n: CoNode) -> bool:
    """Placed in Chicago by the researched workbook alone, and nothing else speaks for it: drawn as a dashed ring."""
    return n.placement == "workbook" and not n.on_list and n.signals == 0


def _radius(n: CoNode, k: float) -> float:
    return max(6.5 + 3 * math.sqrt(min(n.events, 9)), 9.5 if _ring(n) else 0) * k


def _pt(cx: float, cy: float, r: float, a: float) -> tuple[float, float]:
    return cx + r * math.cos(a), cy + r * math.sin(a)


class _Spots:
    """Nodes already placed. A new one is moved along a golden-angle spiral from where it wanted to be until it clears all of them."""

    def __init__(self, gap: float):
        self.gap = gap
        self.placed: list[tuple[float, float, float]] = []

    def add(self, x: float, y: float, r: float) -> None:
        self.placed.append((x, y, r))

    def place(self, x: float, y: float, r: float) -> tuple[float, float]:
        step = r + self.gap
        for i in range(600):
            px, py = (x, y) if i == 0 else (x + step * math.sqrt(i) * 0.55 * math.cos(i * GOLDEN), y + step * math.sqrt(i) * 0.55 * math.sin(i * GOLDEN))
            if all(math.hypot(px - ox, py - oy) >= r + orad + self.gap for ox, oy, orad in self.placed):
                self.add(px, py, r)
                return px, py
        self.add(x, y, r)
        return x, y


def _ties_text(n: CoNode, ties: list[Tie], names: dict[int, str]) -> str:
    by: dict[str, list[str]] = {}
    for t in ties:
        if t.company_id == n.id:
            by.setdefault(LINE_WORD[t.status], []).append(names.get(t.firm_id, "a firm"))
    return "; ".join(f"{w} at {', '.join(sorted(fs))}" for w, fs in sorted(by.items(), key=lambda kv: ["current", "pending", "a lead, not confirmed", "past"].index(kv[0])))


def _co_title(n: CoNode, ties: list[Tie], names: dict[int, str], ring: bool) -> str:
    bits = [n.name, n.category]
    if n.on_list:
        bits.append(f"{n.signals} signal{'' if n.signals == 1 else 's'} in the last 90 days")
    else:
        bits.append("not on the scan list")
    bits.append(_ties_text(n, ties, names))
    if n.peer:
        bits.append("a peer of the firm, never a prospect")
    if ring:
        bits.append("location per the researched workbook, not confirmed")
    return " · ".join(b for b in bits if b)


def _co_node(n: CoNode, x: float, y: float, k: float, ties: list[Tie], names: dict[int, str], shared: bool) -> str:
    r = _radius(n, k)
    ring = _ring(n)
    cls = f"pg-co pg-co--{n.key}" + (" pg-co--ring" if ring else "")
    if n.peer:
        d = r + 2 * k
        shape = f'<polygon class="pg-peer" points="{_f(x)},{_f(y - d)} {_f(x + d)},{_f(y)} {_f(x)},{_f(y + d)} {_f(x - d)},{_f(y)}"/>'
    else:
        shape = (f'<circle class="pg-co2" cx="{_f(x)}" cy="{_f(y)}" r="{_f(r + 3.5 * k)}"/>' if shared else "") + f'<circle class="{cls}" cx="{_f(x)}" cy="{_f(y)}" r="{_f(r)}"/>'
    return f'<a href="{escape(n.href, quote=True)}" class="pg-node">{shape}<title>{escape(_co_title(n, ties, names, ring))}</title></a>'


def _firm_node(f: FirmNode, x: float, y: float, k: float, href: str, cls: str, half: float) -> str:
    return (f'<a href="{escape(href, quote=True)}" class="pg-node"><rect class="pg-firm pg-firm--{cls}" x="{_f(x - half)}" y="{_f(y - half)}" width="{_f(2 * half)}" height="{_f(2 * half)}" '
            f'rx="{_f(3 * k)}"/><title>{escape(_firm_title(f))}</title></a>')


def _firm_title(f: FirmNode) -> str:
    bits = [f.name, "Chicago firm" if f.chicago else "investor in a company on the list"]
    parts = [f"{f.current} current", f"{f.pending} pending", f"{f.leads} lead{'' if f.leads == 1 else 's'} (unconfirmed)"]
    bits.append(", ".join(parts))
    if f.collapsed:
        bits.append(f"{f.collapsed} not drawn (see the table)")
    return " · ".join(bits)


def _radial_label(x: float, y: float, a: float, text: str, cls: str, extra: str = "") -> str:
    """Text running outward along the angle `a` from (x, y), turned the right way up on the left half so neighbours never overlap."""
    deg = math.degrees(a)
    left = math.cos(a) < 0
    rot, anchor = (deg + 180, "end") if left else (deg, "start")
    return (f'<text class="{cls}" transform="translate({_f(x)} {_f(y)}) rotate({_f(rot)})" text-anchor="{anchor}" dominant-baseline="central">{escape(text)}{extra}</text>')


def _svg(prefix: str, viewbox: str, label: str, body: list[str], phone: bool = False) -> str:
    return (f'<svg class="pg{" pg--phone" if phone else ""}" viewBox="{viewbox}" role="group" aria-labelledby="{prefix}-t" xmlns="http://www.w3.org/2000/svg" focusable="false">'
            f'<title id="{prefix}-t">{escape(label)}</title>' + "".join(body) + "</svg>")


# ---- the market map ----------------------------------------------------------------------------------------------------------------------------------
def _ring_order(firms: list[FirmNode], held: dict[int, list[int]]) -> list[FirmNode]:
    """Firms that share companies sit next to each other: components by size, and inside one the firm most tied to those already placed comes next."""
    weight: dict[tuple[int, int], int] = {}
    for fs in held.values():
        u = sorted(set(fs))
        for i, a in enumerate(u):
            for b in u[i + 1:]:
                weight[(a, b)] = weight.get((a, b), 0) + 1

    def w(a: int, b: int) -> int:
        return weight.get((min(a, b), max(a, b)), 0)

    by_id = {f.id: f for f in firms}
    seen: set[int] = set()
    comps: list[list[int]] = []
    for f in sorted(firms, key=lambda f: f.name.lower()):
        if f.id in seen:
            continue
        comp, stack = [], [f.id]
        seen.add(f.id)
        while stack:
            x = stack.pop()
            comp.append(x)
            for g in firms:
                if g.id not in seen and w(x, g.id):
                    seen.add(g.id)
                    stack.append(g.id)
        comps.append(comp)
    linked = sorted((c for c in comps if len(c) > 1), key=lambda c: (-len(c), min(by_id[i].name.lower() for i in c)))
    order: list[FirmNode] = []
    for comp in linked:
        by_name = sorted(comp, key=lambda i: by_id[i].name.lower())
        placed = [max(by_name, key=lambda i: sum(w(i, j) for j in comp))]
        left = [i for i in by_name if i != placed[0]]
        while left:
            nxt = max(left, key=lambda i: sum(w(i, j) for j in placed))
            placed.append(nxt)
            left.remove(nxt)
        order.extend(by_id[i] for i in placed)
    order.extend(sorted((by_id[c[0]] for c in comps if len(c) == 1), key=lambda f: (-f.drawn, f.name.lower())))
    return order


def market_svg(u: Universe, *, prefix: str = "pgm", size: int = 1000, k: float = 1.0, font: float = 12, max_firms: int | None = None, max_label: int = 24,
               ring: float = 0.33, phone: bool = False) -> str:
    """Sponsors on a ring and the companies they hold inside it. '' when nothing is drawn."""
    firms = list(u.firms)
    omitted = 0
    if max_firms and len(firms) > max_firms:
        firms.sort(key=lambda f: (-(f.drawn + f.with_signals), f.name.lower()))
        omitted, firms = len(firms) - max_firms, firms[:max_firms]
    ids = {f.id for f in firms}
    ties = [t for t in u.ties if t.firm_id in ids]
    held: dict[int, list[int]] = {}
    for t in ties:
        held.setdefault(t.company_id, []).append(t.firm_id)
    held = {c: sorted(set(fs)) for c, fs in held.items()}
    cos = {c: u.companies[c] for c in sorted(held)}
    if not firms or not cos:
        return ""
    names = {f.id: f.name for f in u.all_firms or firms}
    cx = cy = size / 2
    r_firm = size * ring
    order = _ring_order(firms, held)
    excl: dict[int, list[CoNode]] = {f.id: [] for f in firms}
    shared: list[CoNode] = []
    for c, fs in held.items():
        (excl[fs[0]] if len(fs) == 1 else shared).append(cos[c])
    weights = {f.id: 1 + math.sqrt(len(excl[f.id])) for f in order}
    total = sum(weights.values())
    angle: dict[int, float] = {}
    span: dict[int, float] = {}
    a = -math.pi / 2
    for f in order:
        span[f.id] = TAU * weights[f.id] / total
        angle[f.id] = a + span[f.id] / 2
        a += span[f.id]
    spots = _Spots(6 * k)
    fpos = {f.id: _pt(cx, cy, r_firm, angle[f.id]) for f in order}
    for f in order:
        spots.add(*fpos[f.id], 10 * k)
    cpos: dict[int, tuple[float, float]] = {}
    for f in order:
        mine = sorted(excl[f.id], key=_prio)
        arcs = 1 + (len(mine) > 4) + (len(mine) > 10)
        rows: list[list[CoNode]] = [[] for _ in range(arcs)]
        for i, n in enumerate(mine):
            rows[i % arcs].append(n)
        for j, row in enumerate(rows):
            rk = r_firm * (0.80 - 0.15 * j)
            use = min(span[f.id] * 0.92, len(row) * 32 * k / rk)
            for i, n in enumerate(row):
                cpos[n.id] = spots.place(*_pt(cx, cy, rk, angle[f.id] + ((i + 0.5) / len(row) - 0.5) * use), _radius(n, k))
    for n in sorted(shared, key=_prio):
        angs = [angle[f] for f in held[n.id]]
        sx, sy = sum(math.cos(x) for x in angs) / len(angs), sum(math.sin(x) for x in angs) / len(angs)
        resultant = math.hypot(sx, sy)
        ang = angs[0] if resultant < 1e-6 else math.atan2(sy, sx)
        cpos[n.id] = spots.place(*_pt(cx, cy, r_firm * (0.46 - 0.34 * (1 - resultant)), ang), _radius(n, k))
    body = []
    for t in ties:
        (x1, y1), (x2, y2) = fpos[t.firm_id], cpos[t.company_id]
        body.append(f'<line class="pg-edge pg-edge--{LINE_CLASS[t.status]}" x1="{_f(x1)}" y1="{_f(y1)}" x2="{_f(x2)}" y2="{_f(y2)}"/>')
    for ao in u.add_ons:
        if ao.parent_id in cpos and ao.child_id in cpos:
            (x1, y1), (x2, y2) = cpos[ao.parent_id], cpos[ao.child_id]
            body.append(f'<line class="pg-edge pg-edge--addon{" pg-edge--announced" if ao.status != "completed" else ""}" x1="{_f(x1)}" y1="{_f(y1)}" x2="{_f(x2)}" y2="{_f(y2)}"/>')
    for f in order:
        x, y = fpos[f.id]
        body.append(_firm_node(f, x, y, k, f"/private-equity?firm={f.id}", "chicago" if f.chicago else "investor", 9 * k))
    for c, n in cos.items():
        body.append(_co_node(n, *cpos[c], k, ties, names, len(held[c]) > 1))
    for n in sorted(cos.values(), key=_prio):
        if n.key in LABELLED and n.id in cpos and len([1 for m in cos.values() if m.key in LABELLED]) <= 14:
            x, y = cpos[n.id]
            body.append(f'<text class="pg-label pg-label--co" x="{_f(x)}" y="{_f(y + _radius(n, k) + font + 1)}" text-anchor="middle">{escape(_clip(n.name, max_label))}</text>')
    for f in order:
        x, y = _pt(cx, cy, r_firm + 16 * k, angle[f.id])
        more = f'<tspan class="pg-more"> +{f.collapsed}</tspan>' if f.collapsed else ""
        body.append(_radial_label(x, y, angle[f.id], _clip(f.name, max_label), "pg-label", more))
    label = f"Network of {len(firms)} private-equity firms and {len(cos)} companies" + (f"; {omitted} more firms are in the table" if omitted else "")
    return _svg(prefix, f"0 0 {size} {size}", label, body, phone)


# ---- one firm -----------------------------------------------------------------------------------------------------------------------------------------
def firm_svg(u: Universe, *, prefix: str = "pgf", k: float = 1.0, font: float = 12, cap: int | None = None, max_label: int = 20, phone: bool = False) -> str:
    """The focus firm in the middle, its current holdings on the first ring, pending ones and leads on the second, past ones on a third, and the other firms that
    also hold one of them as squares outside. '' when no company is drawn."""
    if u.focus is None or not u.firms:
        return ""
    focus = u.firms[0]
    mine = {t.company_id: t for t in u.ties if t.firm_id == focus.id}
    cos = sorted((u.companies[c] for c in mine if c in u.companies), key=lambda n: (list(("current", "pending", "unclear", "historical")).index(mine[n.id].status),) + _prio(n))
    if cap:
        cos = cos[:cap]
    if not cos:
        return ""
    names = {f.id: f.name for f in u.all_firms or u.firms}
    rings = [[n for n in cos if mine[n.id].status == "current"], [n for n in cos if mine[n.id].status in ("pending", "unclear")], [n for n in cos if mine[n.id].status == "historical"]]
    rings = [r for r in rings if r]
    sp = 46 * k
    radii: list[float] = []
    for i, ring in enumerate(rings):
        floor = 120 * k if i == 0 else radii[-1] + 125 * k
        radii.append(max(floor, len(ring) * sp / TAU))
    pos: dict[int, tuple[float, float]] = {}
    ang: dict[int, float] = {}
    body: list[str] = []
    for i, ring in enumerate(rings):
        off = -math.pi / 2 + (i % 2) * (TAU / max(len(ring), 1) / 2)
        for j, n in enumerate(ring):
            ang[n.id] = off + TAU * j / len(ring)
            pos[n.id] = _pt(0, 0, radii[i], ang[n.id])
    r_out = radii[-1]
    spots = _Spots(6 * k)
    spots.add(0, 0, 18 * k)
    for n in cos:
        spots.add(*pos[n.id], _radius(n, k))
    co_pos: dict[int, tuple[float, float]] = {}
    shown = {n.id for n in cos}
    co_ties = [t for t in u.ties if t.firm_id != focus.id and t.company_id in shown]
    for cf in u.co_firms:
        mine_ids = [t.company_id for t in co_ties if t.firm_id == cf.id]
        if not mine_ids:
            continue
        sx, sy = sum(math.cos(ang[c]) for c in mine_ids) / len(mine_ids), sum(math.sin(ang[c]) for c in mine_ids) / len(mine_ids)
        a = ang[mine_ids[0]] if math.hypot(sx, sy) < 1e-6 else math.atan2(sy, sx)
        co_pos[cf.id] = spots.place(*_pt(0, 0, r_out + (60 + 7 * max_label) * k, a), 10 * k)
    for n in cos:
        x2, y2 = pos[n.id]
        body.append(f'<line class="pg-edge pg-edge--{LINE_CLASS[mine[n.id].status]}" x1="0" y1="0" x2="{_f(x2)}" y2="{_f(y2)}"/>')
    for t in co_ties:
        if t.firm_id in co_pos:
            (x1, y1), (x2, y2) = co_pos[t.firm_id], pos[t.company_id]
            body.append(f'<line class="pg-edge pg-edge--{LINE_CLASS[t.status]} pg-edge--co" x1="{_f(x1)}" y1="{_f(y1)}" x2="{_f(x2)}" y2="{_f(y2)}"/>')
    for ao in u.add_ons:
        if ao.parent_id in pos and ao.child_id in pos:
            (x1, y1), (x2, y2) = pos[ao.parent_id], pos[ao.child_id]
            body.append(f'<line class="pg-edge pg-edge--addon{" pg-edge--announced" if ao.status != "completed" else ""}" x1="{_f(x1)}" y1="{_f(y1)}" x2="{_f(x2)}" y2="{_f(y2)}"/>')
    body.append(_firm_node(focus, 0, 0, k, f"/firms/{focus.id}", "chicago" if focus.chicago else "investor", 17 * k))
    if focus.collapsed:
        body.append(f'<a href="/firms/{focus.id}" class="pg-node"><text class="pg-more" x="0" y="{_f(34 * k)}" text-anchor="middle">+{focus.collapsed} not drawn</text></a>')
    for cf in u.co_firms:
        if cf.id in co_pos:
            x, y = co_pos[cf.id]
            d = 8 * k
            body.append(f'<a href="/private-equity?firm={cf.id}" class="pg-node"><rect class="pg-firm pg-firm--co" x="{_f(x - d)}" y="{_f(y - d)}" width="{_f(2 * d)}" height="{_f(2 * d)}" rx="{_f(2 * k)}"/>'
                        f'<title>{escape(cf.name + " also holds " + ", ".join(sorted(u.companies[t.company_id].name for t in co_ties if t.firm_id == cf.id)))}</title></a>')
            body.append(f'<text class="pg-label" x="{_f(x)}" y="{_f(y - d - 5 * k)}" text-anchor="middle">{escape(_clip(cf.name, max_label + 4))}</text>')
    all_ties = u.ties
    for n in cos:
        body.append(_co_node(n, *pos[n.id], k, all_ties, names, len({t.firm_id for t in all_ties if t.company_id == n.id}) > 1))
    for n in cos:
        x, y = _pt(0, 0, math.hypot(*pos[n.id]) + _radius(n, k) + 7 * k, ang[n.id])
        body.append(_radial_label(x, y, ang[n.id], _clip(n.name, max_label), "pg-label pg-label--co"))
    e = r_out + ((60 + 7 * max_label) * k if co_pos else 0) + 150 * k
    return _svg(prefix, f"{_f(-e)} {_f(-e)} {_f(2 * e)} {_f(2 * e)}", f"{focus.name} and {len(cos)} of its companies", body, phone)
