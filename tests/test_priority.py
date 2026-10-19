import re
from datetime import date

import pytest

from bd.charts import priority_svg
from bd.opportunities import build_opportunities
from bd.pipeline import set_client
from bd.priority import RECENT_DAYS, Dot, build_priority
from bd.score_store import score_accounts
from tests.conftest import AS_OF, add_account, add_signal, make_db
from tests.test_update import app_env  # noqa: F401  (fixture)

BANDS = [("Hot", 75), ("Warm", 60), ("Watch", 40)]


def dot(i, name, band="Hot", score=90, revenue=2e9, types=3, recent=True):
    return Dot(i, name, "Healthcare", band, score, revenue, types, recent, False, f"{name}: {band} {score}")


# ---- the data ---------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def world(tmp_path, cfg):
    conn = make_db(tmp_path)
    big = add_account(conn, "Bigco", industry="Healthcare", revenue=4e9)                 # four signal types, recent
    for t, age in (("leadership_change", 4), ("hiring_cluster", 10), ("restructuring_layoffs", 20), ("platform_migration", 25)):
        add_signal(conn, cfg, big, t, age)
    small = add_account(conn, "Smallco", industry="Insurance", revenue=2.5e8)            # one type, old
    add_signal(conn, cfg, small, "hiring_cluster", 85)
    add_signal(conn, cfg, small, "hiring_cluster", 88)
    nosize = add_account(conn, "Nosizeco", industry="Insurance", revenue=None)           # strong signals but no revenue on file
    add_signal(conn, cfg, nosize, "leadership_change", 5)
    add_signal(conn, cfg, nosize, "hiring_cluster", 8)
    score_accounts(conn, cfg, AS_OF)
    ol = build_opportunities(conn, cfg, AS_OF, include_tracked_only=True)
    return conn, ol


def test_a_company_with_revenue_on_file_becomes_a_dot_and_one_without_is_listed_apart(world):
    conn, ol = world
    p = build_priority(ol.items, AS_OF)
    assert {d.name for d in p.dots} == {"Bigco", "Smallco"}
    assert [(n, s) for _, n, s in p.unplaced] == [("Nosizeco", next(o.result.score for o in ol.items if o.account.name == "Nosizeco"))]
    big = next(d for d in p.dots if d.name == "Bigco")
    o = next(o for o in ol.items if o.account.name == "Bigco")
    assert big.score == o.result.score and big.band == o.result.band and big.revenue == 4e9 and big.types == len(o.result.families) == 4
    assert big.recent and not next(d for d in p.dots if d.name == "Smallco").recent
    assert RECENT_DAYS == 30 and "4 signal types" in big.title and "$4.0B" in big.title and "Leadership" in big.title


def test_dots_are_ordered_strongest_first_and_a_zero_revenue_is_not_placed(world, cfg):
    conn, ol = world
    zero = add_account(conn, "Zeroco", revenue=0)
    add_signal(conn, cfg, zero, "leadership_change", 3)
    score_accounts(conn, cfg, AS_OF)
    p = build_priority(build_opportunities(conn, cfg, AS_OF, include_tracked_only=True).items, AS_OF)
    assert [d.score for d in p.dots] == sorted((d.score for d in p.dots), reverse=True)
    assert "Zeroco" in [n for _, n, _ in p.unplaced] and "Zeroco" not in [d.name for d in p.dots]


def test_clients_are_flagged_and_an_empty_list_gives_nothing(world, cfg):
    conn, ol = world
    set_client(conn, next(o.account.id for o in ol.items if o.account.name == "Bigco"), True)
    p = build_priority(build_opportunities(conn, cfg, AS_OF, include_tracked_only=True).items, AS_OF)
    assert {d.name: d.client for d in p.dots} == {"Bigco": True, "Smallco": False}
    empty = build_priority([], AS_OF)
    assert empty.dots == [] and empty.unplaced == []


# ---- the chart ---------------------------------------------------------------------------------------------------------------------
def test_the_chart_draws_one_linked_dot_per_company_and_names_the_strongest():
    svg = priority_svg([dot(1, "Alpha", "Hot", 95, 3e9), dot(2, "Beta", "Warm", 65, 5e8, 2, False), dot(3, "Gamma", "Watch", 45, 8e8, 1, False)], BANDS, 100)
    assert svg.count("<circle") == 3 and 'href="/companies/1"' in svg and 'href="/companies/3"' in svg
    assert svg.count(">Alpha<") == 1 and ">Beta<" in svg and ">Gamma<" in svg
    assert "Hot 75+" in svg and "Warm 60+" in svg and "Watch 40+" in svg and "Company revenue (log scale)" in svg
    assert "$1B" in svg and "<title>Alpha: Hot 95</title>" in svg and svg.startswith("<svg") and svg.endswith("</svg>")


def test_size_follows_signal_types_and_position_follows_score_and_revenue():
    svg = priority_svg([dot(1, "Wide", "Hot", 95, 3e9, 5), dot(2, "Narrow", "Hot", 80, 3e8, 1)], BANDS, 100)
    circles = {int(m.group(1)): (float(m.group(2)), float(m.group(3)), float(m.group(4)))
               for m in re.finditer(r'href="/companies/(\d+)"><title>[^<]*</title><circle cx="([\d.]+)" cy="([\d.]+)" r="([\d.]+)"', svg)}
    assert circles[1][2] > circles[2][2]                       # more signal types, bigger dot
    assert circles[1][0] > circles[2][0]                       # larger revenue, further right
    assert circles[1][1] < circles[2][1]                       # higher score, higher up (smaller y)


def test_a_dot_at_the_score_cap_is_not_cut_off_by_the_top_edge():
    svg = priority_svg([dot(1, "Top", "Hot", 100, 1e9, 6)], BANDS, 100)
    cy = float(re.search(r'<circle cx="[\d.]+" cy="([\d.]+)" r="([\d.]+)"', svg).group(1))
    r = float(re.search(r'<circle cx="[\d.]+" cy="[\d.]+" r="([\d.]+)"', svg).group(1))
    assert cy - r > 0


def test_a_label_never_sits_on_another_label_or_on_another_dot_and_every_dot_keeps_its_title():
    close = [dot(i, f"Company number {i}", "Hot", 90, 2e9 * (1 + i / 50), 2) for i in range(1, 8)]      # seven dots nearly on top of each other
    svg = priority_svg(close, BANDS, 100)
    assert svg.count("<circle") == 7 and svg.count("<title>") == 7
    circles = {int(i): (float(x), float(y), float(r)) for i, x, y, r in re.findall(
        r'href="/companies/(\d+)"><title>[^<]*</title><circle cx="([\d.]+)" cy="([\d.]+)" r="([\d.]+)"', svg)}
    boxes = []
    for x, y, anchor, text in re.findall(r'<text x="([\d.]+)" y="([\d.]+)" font-size="12" text-anchor="(\w+)" style="fill:var\(--ink\)[^>]*>([^<]+)</text>', svg):
        x, y, w = float(x), float(y), 6.1 * len(text)
        x0 = x - (w if anchor == "end" else w / 2 if anchor == "middle" else 0)
        boxes.append((x0, y - 11, x0 + w, y + 2, text))
    assert boxes and len({b[4] for b in boxes}) == len(boxes)
    hit = lambda a, b: not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])      # noqa: E731
    for i, a in enumerate(boxes):
        assert not any(hit(a, b) for b in boxes[i + 1:])
        own = int(a[4][-1])
        for j, (cx, cy, r) in circles.items():
            if j != own:
                assert not hit(a, (cx - r, cy - r, cx + r, cy + r)), (a[4], j)


def test_text_from_the_database_is_escaped_and_an_empty_list_draws_nothing():
    svg = priority_svg([dot(1, "<script>alert(1)</script>", "Hot", 90, 1e9)], BANDS, 100)
    assert "<script>" not in svg and "&lt;script&gt;" in svg
    assert priority_svg([], BANDS, 100) == ""


def test_one_company_alone_still_draws(tmp_path):
    svg = priority_svg([dot(1, "Solo", "Warm", 62, 1e9, 1)], BANDS, 100)
    assert svg.count("<circle") == 1 and "NaN" not in svg and "inf" not in svg.lower().replace("info", "")


# ---- the page ----------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def seeded(app_env):  # noqa: F811
    e = app_env
    today = date.today()
    ids = {}
    for name, rev, sigs in (("Bigco", 4e9, ("leadership_change", "hiring_cluster", "restructuring_layoffs")),
                            ("Midco", 8e8, ("leadership_change", "hiring_cluster")), ("Nosizeco <i>", None, ("leadership_change", "hiring_cluster"))):
        a = add_account(e.conn, name, industry="Healthcare", revenue=rev)
        for i, t in enumerate(sigs):
            add_signal(e.conn, e.cfg, a, t, 3 + 4 * i, as_of=today)
        ids[name] = a
    score_accounts(e.conn, e.cfg, today)
    return e, ids


def test_the_chart_is_a_third_view_of_the_same_companies(seeded):
    e, ids = seeded
    body = e.get("/opportunities?view=chart").body
    assert "<svg" in body and f'href="/companies/{ids["Bigco"]}"' in body and "Company revenue (log scale)" in body
    assert 'class="on">Priority chart<' in body and "Signal matrix</a>" in body and "List</a>" in body
    assert 'class="matrix"' not in body
    assert "view=chart" in body                                                       # the filter form and tabs keep the chart view
    assert '<input type="hidden" name="view" value="chart">' in body


def test_a_company_without_revenue_is_named_below_the_chart_with_a_plain_reason(seeded):
    e, ids = seeded
    body = e.get("/opportunities?view=chart").body
    assert "no revenue on file" in body and "Nosizeco &lt;i&gt;" in body and "Nosizeco <i>" not in body
    assert f'href="/companies/{ids["Nosizeco <i>"]}"' in body


def test_filters_apply_to_the_chart_and_switching_views_keeps_them(seeded):
    e, ids = seeded
    body = e.get("/opportunities?view=chart&q=Midco").body
    assert body.count("<circle") == 1 and f'href="/companies/{ids["Midco"]}"' in body
    assert "q=Midco" in body and "view=list" in body                                  # the other tabs carry the search along
    none = e.get("/opportunities?view=chart&q=zzzz").body
    assert "<svg viewBox=\"0 0 960" not in none and "No companies match" in none


def test_when_no_company_has_a_revenue_the_page_says_so_instead_of_drawing_an_empty_chart(seeded):
    e, ids = seeded
    e.conn.execute("UPDATE accounts SET revenue = NULL")
    e.conn.commit()
    body = e.get("/opportunities?view=chart").body
    assert "nothing to place on a size axis" in body and "<circle" not in body


def test_the_matrix_and_the_list_still_work_and_unknown_views_fall_back_to_the_matrix(seeded):
    e, ids = seeded
    assert 'class="matrix"' in e.get("/opportunities").body and 'class="matrix"' in e.get("/opportunities?view=bogus").body
    assert 'class="matrix"' not in e.get("/opportunities?view=list").body
