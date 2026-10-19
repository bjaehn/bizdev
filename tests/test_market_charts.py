import re
from datetime import date, timedelta

import pytest

from bd.charts import trend_svg
from bd.market import UNKNOWN_INDUSTRY, build_market
from bd.score_store import score_accounts
from tests.conftest import AS_OF, add_account, add_signal, make_db
from tests.test_update import app_env  # noqa: F401  (fixture)


def store_score(conn, account_id, days_ago, score, band, as_of=AS_OF):
    conn.execute("INSERT OR REPLACE INTO scores(account_id, run_date, score, band, components_json, rationale) VALUES (?,?,?,?,'{}','x')",
                 (account_id, (as_of - timedelta(days=days_ago)).isoformat(), score, band))
    conn.commit()


@pytest.fixture
def conn(tmp_path):
    return make_db(tmp_path)


# ---- the industry heatmap's data --------------------------------------------------------------------------------------------------
def test_heat_counts_signals_of_each_type_in_the_last_90_days_per_industry(conn, cfg):
    a = add_account(conn, "Medco", industry="Health care")
    b = add_account(conn, "Medtwo", industry="Health care")
    c = add_account(conn, "Steelco", industry="Manufacturing")
    add_account(conn, "Noindustry")
    for age in (3, 10):
        add_signal(conn, cfg, a, "leadership_change", age)                  # two leadership signals at one company
    add_signal(conn, cfg, b, "leadership_change", 20)                       # one more at another
    add_signal(conn, cfg, a, "hiring_cluster", 95)                          # outside 90 days: not counted
    add_signal(conn, cfg, c, "restructuring_layoffs", 45)
    m = build_market(conn, AS_OF, families=cfg.scoring.families)
    lead = m.heat["Health care"]["leadership"]
    assert (lead.signals, lead.companies) == (3, 2)
    assert "hiring" not in m.heat["Health care"]
    assert m.heat["Manufacturing"]["events"].signals == 1 and m.heat[UNKNOWN_INDUSTRY] == {}
    assert m.heat_max == 3 and m.families == list(cfg.scoring.families)
    assert list(m.heat) == [s.industry for s in m.segments]                 # same row order as the industry table


def test_heat_ignores_held_companies_and_future_dated_signals(conn, cfg):
    held = add_account(conn, "Held", status="hold_review", industry="Energy")
    add_signal(conn, cfg, held, "leadership_change", 5)
    ok = add_account(conn, "Okco", industry="Energy")
    add_signal(conn, cfg, ok, "leadership_change", -5)                      # dated after the as-of day
    m = build_market(conn, AS_OF, families=cfg.scoring.families)
    assert m.heat["Energy"] == {} and m.heat_max == 0


def test_without_a_family_list_the_columns_are_the_ones_seen(conn, cfg):
    a = add_account(conn, "Acme")
    add_signal(conn, cfg, a, "leadership_change", 3)
    add_signal(conn, cfg, a, "hiring_cluster", 3)
    assert build_market(conn, AS_OF).families == ["hiring", "leadership"]


# ---- the weekly trend's data --------------------------------------------------------------------------------------------------------
def test_trend_has_one_point_per_scoring_run_counting_bands_as_stored(conn, cfg):
    a, b, c = (add_account(conn, n) for n in ("Acme", "Beta", "Gamma"))
    for acc, band, score in ((a, "Hot", 90), (b, "Warm", 65), (c, "Watch", 45)):
        store_score(conn, acc, 0, score, band)
    store_score(conn, a, 7, 62, "Warm")
    store_score(conn, b, 7, 20, "Stored")                                   # below Watch: kept apart
    store_score(conn, c, 14, 41, "Watch")
    m = build_market(conn, AS_OF)
    assert [p.run_date for p in m.trend] == sorted(p.run_date for p in m.trend) and len(m.trend) == 3
    now, week, two = m.trend[-1], m.trend[-2], m.trend[0]
    assert (now.hot, now.warm, now.watch, now.hot_warm) == (1, 1, 1, 2)
    assert (week.hot, week.warm, week.watch, week.other) == (0, 1, 0, 1)
    assert (two.hot, two.warm, two.watch) == (0, 0, 1)


def test_trend_keeps_only_the_latest_runs_and_nothing_after_the_as_of_day(conn, cfg):
    a = add_account(conn, "Acme")
    for w in range(20):
        store_score(conn, a, 7 * w, 50, "Watch")
    store_score(conn, a, -7, 90, "Hot")                                     # a run dated after the as-of day
    m = build_market(conn, AS_OF, runs=12)
    assert len(m.trend) == 12 and m.trend[-1].run_date == AS_OF.isoformat() and all(p.hot == 0 for p in m.trend)


def test_no_scores_means_no_trend(conn, cfg):
    assert build_market(conn, AS_OF).trend == []


# ---- the trend chart --------------------------------------------------------------------------------------------------------------
def test_trend_chart_stacks_hot_then_warm_then_watch_per_run_sized_by_count():
    svg = trend_svg([("Oct 5", 1, 2, 3), ("Oct 12", 3, 0, 1)])
    assert svg.count("<rect") == 6 - 1 and "Oct 5: 1 Hot" in svg and "Oct 5: 2 Warm" in svg and "Oct 12: 1 Watch" in svg
    assert "Oct 12: 0 Warm" not in svg                                      # an empty band draws nothing
    rects = re.findall(r'<rect x="[\d.]+" y="([\d.]+)" width="[\d.]+" height="([\d.]+)"[^>]*><title>([^<]*)</title>', svg)
    h = {t: float(height) for _, height, t in rects}
    assert h["Oct 12: 3 Hot"] > h["Oct 5: 2 Warm"] > h["Oct 5: 1 Hot"] and h["Oct 5: 3 Watch"] > h["Oct 12: 1 Watch"]
    y = {t: float(y0) for y0, _, t in rects}
    assert y["Oct 5: 1 Hot"] > y["Oct 5: 2 Warm"] > y["Oct 5: 3 Watch"]    # Hot at the bottom, Watch on top (smaller y is higher)


def test_trend_chart_keeps_the_latest_bars_and_escapes_labels_and_is_empty_without_data():
    many = [(f"R{i}", 1, 0, 0) for i in range(20)]
    assert trend_svg(many).count("<rect") == 12 and "R19" in trend_svg(many) and "R7<" not in trend_svg(many)
    assert "<b>" not in trend_svg([("<b>x</b>", 1, 1, 1)]) and "&lt;b&gt;" in trend_svg([("<b>x</b>", 1, 1, 1)])
    assert trend_svg([]) == "" and trend_svg([("a", 0, 0, 0)]) == ""


def test_one_run_alone_still_draws_a_bar():
    svg = trend_svg([("Oct 19", 2, 1, 0)])
    assert svg.count("<rect") == 2 and "NaN" not in svg


# ---- the page ---------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def seeded(app_env):  # noqa: F811
    e = app_env
    today = date.today()
    a = add_account(e.conn, "Medco", industry="Health <care>")
    add_signal(e.conn, e.cfg, a, "leadership_change", 3, as_of=today)
    add_signal(e.conn, e.cfg, a, "hiring_cluster", 6, as_of=today)
    b = add_account(e.conn, "Steelco", industry="Manufacturing")
    add_signal(e.conn, e.cfg, b, "leadership_change", 9, as_of=today)
    score_accounts(e.conn, e.cfg, today)
    return e, today


def test_the_market_page_has_a_heatmap_whose_cells_open_the_matching_companies(seeded):
    e, today = seeded
    body = e.get("/market").body
    assert 'class="matrix"' in body and "Health &lt;care&gt;" in body and "Health <care>" not in body
    assert "Leadership" in body and "Company events" in body and "Financials" in body
    assert 'href="/opportunities?industry=Manufacturing&amp;family=leadership&amp;tracked=1"' in body
    assert 'class="cell none"' in body and 'title="1 signal at 1 company"' in body
    assert "industry=Health+%3Ccare%3E&amp;family=hiring&amp;tracked=1" in body


def test_the_heatmap_cell_link_lands_on_an_opportunities_page_that_filters_to_those_companies(seeded):
    e, today = seeded
    body = e.get("/opportunities?industry=Manufacturing&family=leadership&tracked=1").body
    assert "Steelco" in body and "Medco" not in body


def test_the_trend_says_when_there_is_only_one_run_and_compares_two(seeded):
    e, today = seeded
    one = e.get("/market").body
    assert "Companies by band, run by run" in one and "<svg" in one and "no trend yet" in one
    for acc in (1, 2):
        store_score(e.conn, acc, 7, 20, "Stored", as_of=today)
    store_score(e.conn, 1, 14, 45, "Watch", as_of=today)
    two = e.get("/market").body
    assert "no trend yet" not in two and "Hot or Warm:" in two and "from" in two.split("Hot or Warm:")[1][:80]


def test_an_empty_market_page_renders_with_plain_messages(app_env):
    body = app_env.get("/market").body
    assert "Nothing has been scored yet" in body and "No signals in the last 90 days" in body
