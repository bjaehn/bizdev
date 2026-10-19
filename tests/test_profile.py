from datetime import date, timedelta

import pytest

from bd.charts import breakdown_svg
from bd.pipeline import set_client
from bd.score_store import score_accounts
from tests.conftest import add_account, add_signal
from tests.test_update import app_env  # noqa: F401  (fixture)


# ---- the breakdown chart ------------------------------------------------------------------------------------------------------
def test_the_breakdown_bar_has_one_segment_per_part_sized_by_its_points():
    svg = breakdown_svg([("Leadership", 30.0, "#2563eb"), ("Hiring", 10.0, "#16a34a"), ("Stacking bonus", 10.0, "#475569")], 50.0, 50, 100)
    assert svg.count("<rect") == 3 and "Leadership: 30.0 points" in svg and "50 points in total = score 50" in svg
    widths = [float(w) for w in __import__("re").findall(r'width="([0-9.]+)" height="30"', svg)]
    assert widths[0] > widths[1] and abs(widths[1] - widths[2]) < 0.01 and abs(sum(widths) + 3 * 1.5 - 760) < 0.5


def test_the_breakdown_says_when_the_total_was_capped_and_ignores_empty_parts():
    svg = breakdown_svg([("Leadership", 70.0, "#2563eb"), ("Hiring", 60.0, "#16a34a"), ("None", 0.0, "#000")], 130.0, 100, 100)
    assert svg.count("<rect") == 2 and "(capped at 100)" in svg and "= score 100" in svg
    assert breakdown_svg([], 0, 0, 100) == "" and breakdown_svg([("x", 0.0, "#000")], 0, 0, 100) == ""


def test_labels_are_escaped_in_the_chart():
    svg = breakdown_svg([("<script>alert(1)</script>", 10.0, "#000")], 10.0, 10, 100)
    assert "<script>" not in svg and "&lt;script&gt;" in svg


# ---- the pages ---------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def seeded(app_env):  # noqa: F811
    e = app_env
    today = date.today()
    ids = {}
    for name, ind, sigs in (("Wideco <b>", "Healthcare", ("leadership_change", "hiring_cluster", "restructuring_layoffs", "platform_migration")),
                            ("Deepco", "Insurance", ("hiring_cluster",)), ("Midco", "Insurance", ("leadership_change", "hiring_cluster"))):
        a = add_account(e.conn, name, industry=ind)
        for i, t in enumerate(sigs):
            add_signal(e.conn, e.cfg, a, t, 3 + 5 * i, as_of=today, url=f"https://www.sec.gov/Archives/{a}/{t}.htm")
        ids[name] = a
    set_client(e.conn, ids["Deepco"], True)
    prev = (today - timedelta(days=7)).isoformat()
    e.conn.execute("INSERT INTO scores(account_id, run_date, score, band, components_json, rationale) VALUES (?,?,?,?,'{}','x')", (ids["Wideco <b>"], prev, 10, "Stored"))
    e.conn.commit()
    score_accounts(e.conn, e.cfg, today)
    return e, ids


def test_the_ranking_is_a_matrix_with_one_row_per_company_and_a_column_per_signal_type(seeded):
    e, ids = seeded
    body = e.get("/opportunities?view=matrix&tracked=1").body                                  # include the company that is below Watch
    head = body.split("<thead>")[1].split("</thead>")[0]
    for label in ("Company", "Score", "Leadership", "Hiring", "Company events", "Private equity", "Technology", "Procurement", "Filings", "Financials", "Types"):
        assert label in head, label
    assert body.count('<td class="co">') == 3 and "Wideco &lt;b&gt;" in body and "Wideco <b>" not in body
    assert f'href="/companies/{ids["Wideco <b>"]}"' in body                                   # every row and cell opens the company profile
    assert 'class="cell hi"' in body or 'class="cell ' in body
    assert "<b>4</b>/8" in body and "<b>1</b>/8" in body                                       # types firing, out of eight
    assert "dashed box" in body and "Signal matrix" in body
    # ranked by score: the widest company first
    assert body.index("Wideco") < body.index("Midco") < body.index("Deepco")


def test_cells_show_the_points_and_hover_text_says_what_fired(seeded):
    e, ids = seeded
    body = e.get("/opportunities?view=matrix").body
    assert 'title="Leadership: ' in body and "strongest Leadership change" in body and "points" in body
    assert "●" in body                                                                       # recent signals are marked


def test_the_default_companies_page_is_one_simple_ranked_list_with_search_and_band_tabs(seeded):
    e, ids = seeded
    body = e.get("/opportunities").body
    assert "<b>3</b> companies" in body and "Hot or Warm" in body                           # the coverage line
    assert body.count('class="bar-row"') == 2 and "Wideco &lt;b&gt;" in body and "Midco" in body and "Deepco" not in body      # below Watch is left out
    assert body.index("Wideco") < body.index("Midco")                                          # strongest first
    assert 'class="matrix"' not in body and "Signal matrix" not in body and 'name="service_line"' not in body       # no toolbar of options
    for label in ("All", "Hot", "Warm", "Watch", "Everything scanned"):
        assert f">{label}</a>" in body, label
    assert "Deepco" in e.get("/opportunities?tracked=1").body and body.count('name="q"') == 1


def test_sorting_filtering_and_the_list_view_keep_working(seeded):
    e, ids = seeded
    by_types = e.get("/opportunities?view=matrix&sort=types&tracked=1").body
    assert by_types.index("Wideco") < by_types.index("Midco") < by_types.index("Deepco")
    assert e.get("/opportunities?view=matrix&industry=Insurance&tracked=1").body.count('<td class="co">') == 2
    assert "Wideco" not in e.get("/opportunities?view=matrix&industry=Insurance&tracked=1").body.split("<tbody>")[1]
    assert e.get("/opportunities?view=matrix&client=yes&tracked=1").body.count('<td class="co">') == 1
    assert "Deepco" not in e.get("/opportunities?view=matrix").body.split("<tbody>")[1]        # below Watch: not an opportunity yet
    assert 'href="/opportunities?view=list' in e.get("/opportunities?view=matrix").body and "sort=types" in e.get("/opportunities?view=matrix").body
    lst = e.get("/opportunities?view=list&industry=Insurance").body
    assert 'class="why"' in lst and '<table class="matrix">' not in lst and "Wideco" not in lst.split('class="tabs"')[1]
    assert e.get("/opportunities?view=bogus&sort=bogus&industry=<x>").code == 200


def test_the_profile_explains_the_score_and_its_parts_add_up(seeded):
    e, ids = seeded
    from bd.profile import explain
    from bd.opportunities import build_opportunities
    opp = next(o for o in build_opportunities(e.conn, e.cfg, date.today(), include_tracked_only=True).items if o.account.id == ids["Wideco <b>"])
    ex = explain(opp, e.cfg)
    assert sum(p.points for p in ex.parts) == pytest.approx(opp.result.raw_total)
    assert [p.key for p in ex.parts][-1] == "bonus" and ex.parts[-1].points == opp.result.stacking_bonus
    assert "4 different signal types agree" in ex.sentence and ex.score == opp.result.score
    body = e.get(f"/companies/{ids['Wideco <b>']}").body
    assert "How the score is built" in body and body.count("<rect") >= 4 and "Stacking bonus" in body and "4 different signal types agree" in body
    assert "The last 180 days" in body and body.split("The last 180 days")[1].split("Background")[0].count("<circle") == 4 and "What happened" in body
    assert "Wideco &lt;b&gt;" in body and "Wideco <b>" not in body and "Manage (Admin)" in body and "noprint" in body


def test_a_company_with_one_signal_type_gets_the_no_bonus_explanation(seeded):
    e, ids = seeded
    body = e.get(f"/companies/{ids['Deepco']}").body
    assert "Only one signal type has fired" in body and "Stacking bonus" not in body and "client" in body


def test_the_profile_is_on_the_analysis_side_and_the_manage_page_on_the_admin_side(seeded):
    e, ids = seeded
    assert '<header class="admin">' not in e.get(f"/companies/{ids['Deepco']}").body
    assert '<header class="admin">' in e.get(f"/accounts/{ids['Deepco']}").body
    assert f'href="/companies/{ids["Deepco"]}"' in e.get(f"/accounts/{ids['Deepco']}").body
