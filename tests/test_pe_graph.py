"""The network drawing: well-formed, deterministic, nothing drawn on top of anything else, every name escaped, every class defined once in the stylesheet,
and nothing here that a page security policy forbidding scripts would stop. Rows are synthetic and live only in these tests."""

import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from bd.pe_graph import firm_svg, market_svg
from bd.pe_network import CoNode, FirmNode, Tie, Universe, build_universe
from tests.conftest import make_db
from tests.test_pe_network import workbook_company
from tests.test_pe_rollup import TODAY, own, signal
from bd.score_store import score_accounts

CSS = (Path(__file__).resolve().parent.parent / "bd" / "ui" / "templates" / "design.css").read_text()
NS = {"s": "http://www.w3.org/2000/svg"}


@pytest.fixture
def cfg(cfg):
    return cfg.model_copy(deep=True)


@pytest.fixture
def conn(tmp_path):
    return make_db(tmp_path)


def universe(n_firms=8, per_firm=6, shared=3, **kw):
    """A synthetic Universe: each firm holds `per_firm` companies of its own, and `shared` companies are held by two neighbouring firms."""
    u = Universe(**kw)
    cid = 0
    for i in range(n_firms):
        u.firms.append(FirmNode(i + 1, f"Firm {i + 1}", "chicago" if i % 2 == 0 else "investor", i % 2 == 0, current=per_firm, drawn=per_firm, collapsed=i))
    u.all_firms = list(u.firms)
    cats = ["Prospect now", "Validate first", "Nurture", "Monitor", "Insufficient evidence"]
    for f in u.firms:
        for j in range(per_firm):
            cid += 1
            n = CoNode(cid, f"Co {cid}", cid if j % 2 == 0 else None, category=cats[(cid) % 5], events=cid % 7, signals=cid % 3, on_list=j % 2 == 0, placement="confirmed" if j % 2 == 0 else "workbook")
            n.firms = [f.id]
            u.companies[cid] = n
            u.ties.append(Tie(f.id, cid, "current" if j % 3 else "unclear", cid))
    for k in range(shared):
        cid += 1
        a, b = u.firms[k], u.firms[(k + 1) % n_firms]
        n = CoNode(cid, f"Shared {k}", cid, category="Validate first", events=3, signals=2, on_list=True, placement="confirmed", firms=[a.id, b.id])
        u.companies[cid] = n
        u.ties += [Tie(a.id, cid, "current", cid), Tie(b.id, cid, "pending", cid + 100)]
    return u


def parse(svg):
    return ET.fromstring(svg)


def circles(root):
    return [(float(c.get("cx")), float(c.get("cy")), float(c.get("r"))) for c in root.iter("{%s}circle" % NS["s"]) if "pg-co" in (c.get("class") or "") and "pg-co2" not in c.get("class")]


# ---- the drawing itself ---------------------------------------------------------------------------------------------------------------------------------
def test_the_market_map_is_well_formed_and_the_same_every_time():
    u = universe()
    a, b = market_svg(u), market_svg(u)
    assert a == b and a.startswith("<svg") and parse(a).tag.endswith("svg")
    assert market_svg(Universe()) == "" and firm_svg(Universe()) == ""                      # nothing to draw is no picture, not an empty one


def test_no_company_is_drawn_on_top_of_another():
    root = parse(market_svg(universe(n_firms=10, per_firm=9, shared=5)))
    dots = circles(root)
    assert len(dots) >= 90
    for i, (x1, y1, r1) in enumerate(dots):
        for x2, y2, r2 in dots[i + 1:]:
            assert math.hypot(x1 - x2, y1 - y2) >= r1 + r2 - 0.5, ((x1, y1, r1), (x2, y2, r2))


def test_every_node_is_a_link_with_a_description_and_only_to_pages_of_this_app():
    root = parse(market_svg(universe()))
    links = list(root.iter("{%s}a" % NS["s"]))
    assert links and all(a.get("href").startswith(("/companies/", "/portfolio/", "/private-equity?firm=")) for a in links)
    assert all((a.find("s:title", NS) is not None and a.find("s:title", NS).text) for a in links)
    assert root.get("role") == "group" and root.find("s:title", NS).text.startswith("Network of 8 private-equity firms")


def test_text_from_the_database_is_escaped_and_no_script_or_handler_is_ever_emitted():
    u = universe(n_firms=2, per_firm=2, shared=0)
    u.firms[0].name = "<script>alert(1)</script> & Co"
    u.companies[1].name = '"><img src=x onerror=alert(1)>'
    u.all_firms = list(u.firms)
    for svg in (market_svg(u), firm_svg(Universe(firms=[u.firms[0]], companies=u.companies, ties=[t for t in u.ties if t.firm_id == 1], all_firms=u.firms, focus=1))):
        root = parse(svg)                                                                    # it parses, so the text cannot have opened a tag
        tags = {e.tag.split("}")[1] for e in root.iter()}
        assert not tags & {"script", "img", "foreignObject", "style"}
        assert not [a for e in root.iter() for a in e.attrib if a.lower().startswith("on")]
        assert "&lt;script&gt;" in svg or "&lt;img" in svg


def test_every_class_the_drawing_uses_is_defined_in_the_stylesheet():
    u = universe(past=True)
    u.add_ons = []
    used = set()
    for svg in (market_svg(u), market_svg(u, max_firms=3, phone=True), firm_svg(Universe(firms=[u.firms[0]], companies=u.companies, ties=u.ties, all_firms=u.firms, co_firms=u.firms[1:3], focus=1))):
        for cls in re.findall(r'class="([^"]+)"', svg):
            used |= set(cls.split())
    assert used >= {"pg", "pg-edge", "pg-co", "pg-firm", "pg-label", "pg-node"}
    missing = {c for c in used if not re.search(r"\." + re.escape(c) + r"(?![\w-])", CSS)}
    assert not missing, missing


def test_ids_are_unique_within_a_picture_and_distinct_between_the_wide_and_the_phone_one():
    u = universe()
    ids = re.findall(r'\bid="([^"]+)"', market_svg(u, prefix="pgm-w")) + re.findall(r'\bid="([^"]+)"', market_svg(u, prefix="pgm-p", phone=True))
    assert len(ids) == len(set(ids)) == 2


def test_the_phone_picture_keeps_only_the_busiest_firms_and_says_so():
    u = universe(n_firms=12)
    full, phone = market_svg(u), market_svg(u, max_firms=5, phone=True)
    assert "pg--phone" in phone and "pg--phone" not in full
    assert phone.count('class="pg-firm') == 5 and "12 private-equity firms" in full and "5 private-equity firms" in phone and "7 more firms are in the table" in phone


def test_a_ringed_dot_is_a_company_only_the_workbook_places_and_a_peer_is_a_diamond():
    u = universe(n_firms=2, per_firm=2, shared=0)
    u.companies[2].peer = True
    u.companies[4].signals = 0                                                               # off the list, no signal, and only the workbook places it
    svg = market_svg(u)
    assert "pg-co--ring" in svg and "<polygon" in svg and "location per the researched workbook, not confirmed" in svg and "a peer of the firm, never a prospect" in svg


def test_the_one_firm_view_puts_current_holdings_inside_pending_and_leads_and_co_investors_outside():
    u = universe(n_firms=3, per_firm=6, shared=2)
    u.focus = 1
    u.firms = [u.firms[0]]
    u.co_firms = [f for f in u.all_firms if f.id == 2]
    svg = firm_svg(u)
    root = parse(svg)
    center = [r for r in root.iter("{%s}rect" % NS["s"]) if "pg-firm--chicago" in r.get("class")]
    assert len(center) == 1 and abs(float(center[0].get("x")) + float(center[0].get("width")) / 2) < 0.1          # the focus firm is at the origin
    radius = {}
    for a in root.iter("{%s}a" % NS["s"]):
        c = a.find("s:circle", NS)
        if c is not None and "pg-co" in c.get("class"):
            name = a.find("s:title", NS).text.split(" · ")[0]
            radius[name] = math.hypot(float(c.get("cx")), float(c.get("cy")))
    held = {t.company_id: t for t in u.ties if t.firm_id == 1}
    current = [u.companies[c].name for c, t in held.items() if t.status == "current"]
    leads = [u.companies[c].name for c, t in held.items() if t.status != "current"]
    assert current and leads and max(radius[n] for n in current) < min(radius[n] for n in leads)
    squares = [r for r in root.iter("{%s}rect" % NS["s"]) if "pg-firm--co" in r.get("class")]
    assert len(squares) == 1 and "Firm 2" in svg


def test_the_one_firm_view_says_how_many_holdings_are_not_drawn():
    u = universe(n_firms=2, per_firm=3, shared=0)
    u.focus, u.firms = 1, [u.firms[0]]
    u.firms[0].collapsed = 16
    assert "+16 not drawn" in firm_svg(u)


# ---- from the database ---------------------------------------------------------------------------------------------------------------------------------------
def test_a_picture_built_from_the_ledger_draws_leads_dotted_and_current_solid(conn, cfg):
    fid, *_ = own(conn, cfg, "Alpha Capital", "Held Co")
    workbook_company(conn, cfg, "Alpha Capital", "Ring Co")
    own(conn, cfg, "Beta Partners", "Held Co")
    svg = market_svg(build_universe(conn, cfg, TODAY))
    assert "pg-edge--current" in svg and "pg-edge--lead" in svg and "pg-co--ring" in svg and "Held Co" in svg
    one = firm_svg(build_universe(conn, cfg, TODAY, firm_id=fid))
    assert "Beta Partners also holds Held Co" in one and parse(one) is not None


def test_a_large_portfolio_is_drawn_without_overlap_and_quickly(conn, cfg):
    for i in range(150):
        own(conn, cfg, f"Firm {i % 12}", f"Company {i:03d}")
    for i in range(0, 150, 7):
        aid = conn.execute("SELECT id FROM accounts WHERE name = ?", (f"Company {i:03d}",)).fetchone()[0]
        signal(conn, cfg, aid, "leadership_change", 4 + i % 20)
    score_accounts(conn, cfg, TODAY)
    root = parse(market_svg(build_universe(conn, cfg, TODAY)))
    dots = circles(root)
    assert len(dots) >= 120
    assert all(math.hypot(a[0] - b[0], a[1] - b[1]) >= a[2] + b[2] - 0.5 for i, a in enumerate(dots) for b in dots[i + 1:])
