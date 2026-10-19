from datetime import date, timedelta

import pytest

from bd.charts import spark_svg, weekly_svg
from bd.trend import KIND_LABELS, build_trend, score_history, takeaway, week_index
from tests.conftest import AS_OF, add_account, add_signal, make_db


@pytest.fixture
def cfg(cfg):
    return cfg.model_copy(deep=True)


def test_a_signal_belongs_to_the_week_its_source_dates_it_in():
    assert week_index(AS_OF, AS_OF) == 11 and week_index(AS_OF - timedelta(days=6), AS_OF) == 11
    assert week_index(AS_OF - timedelta(days=7), AS_OF) == 10 and week_index(AS_OF - timedelta(days=83), AS_OF) == 0
    assert week_index(AS_OF - timedelta(days=84), AS_OF) is None and week_index(AS_OF + timedelta(days=1), AS_OF) is None


def S(**kw):
    base = {"leadership": [0] * 12, "hiring": [0] * 12, "press": [0] * 12, "events": [0] * 12}
    for k, v in kw.items():
        base[k] = v
    return base


def test_the_takeaway_is_built_only_from_the_counts():
    none, *_ = takeaway(S())
    assert none == "No signals were found in the last eight weeks."
    s, recent, earlier, pct = takeaway(S(press=[0] * 8 + [1, 1, 1, 0]))
    assert s == "3 signals in the last four weeks, none in the four weeks before." and (recent, earlier, pct) == (3, 0, None)
    s, recent, earlier, pct = takeaway(S(press=[0] * 4 + [1, 0, 0, 0] + [3, 3, 2, 1], hiring=[0] * 4 + [1, 1, 0, 0] + [1, 1, 0, 0]))
    assert s == "More press and media: 9 in the last four weeks against 1 before (11 signals in all, against 3)." and (recent, earlier, pct) == (11, 3, 267)
    s, *_ = takeaway(S(leadership=[0] * 4 + [1, 1, 1, 1] + [1, 1, 1, 1]))
    assert s == "About the same as before: 4 signals in the last four weeks against 4."
    s, recent, earlier, pct = takeaway(S(leadership=[0] * 4 + [3, 3, 3, 3] + [1, 0, 0, 0]))
    assert s.startswith("Fewer signals than before: 1 in the last four weeks against 12") and pct == -92
    s, *_ = takeaway(S(leadership=[0] * 4 + [2, 2, 0, 0] + [0] * 4))
    assert s == "No signals in the last four weeks, against 4 in the four weeks before."
    one, *_ = takeaway(S(hiring=[0] * 11 + [1]))
    assert one.startswith("1 signal in the last four weeks, none")                  # singular


def test_a_kind_is_named_only_when_it_rose_by_enough_otherwise_the_total_speaks():
    s, *_ = takeaway(S(hiring=[0] * 4 + [2, 0, 0, 0] + [3, 0, 0, 0]))                 # +1 only: no single kind is named
    assert s == "More signals than before: 3 in the last four weeks against 2 in the four weeks before."
    s, *_ = takeaway(S(hiring=[0] * 4 + [5, 0, 0, 0] + [5, 0, 0, 1]))                 # +1 on 5: within a fifth, about the same
    assert s == "About the same as before: 6 signals in the last four weeks against 5."


def test_build_trend_counts_scanned_companies_dedupes_and_leaves_out_rumours_and_background(cfg, tmp_path):
    conn = make_db(tmp_path)
    a, gone = add_account(conn, "Acme"), add_account(conn, "Goneco", status="excluded")
    add_signal(conn, cfg, a, "leadership_change", 3)
    add_signal(conn, cfg, a, "hiring_cluster", 10)
    add_signal(conn, cfg, a, "restructuring_layoffs", 4, source_type="secondary_report", conf="Medium")
    add_signal(conn, cfg, a, "restructuring_layoffs", 4, source_type="company_release", url="https://acme.com/news/x")      # same event, one source better
    add_signal(conn, cfg, a, "risk_factor", 5, source_type="sec_filing", url="https://sec.gov/x")                          # background: not charted
    add_signal(conn, cfg, a, "vendor_deal", 6, conf="Low", url="https://gossip.example/v")                                  # rumour
    add_signal(conn, cfg, a, "platform_migration", 200)                                                                   # outside the window
    add_signal(conn, cfg, gone, "leadership_change", 2)
    t = build_trend(conn, AS_OF)
    assert t.series["leadership"][11] == 1 and t.series["hiring"][10] == 1 and t.series["press"][11] == 1 and t.series["events"] == [0] * 12
    assert t.total == 3 and len(t.weeks) == 12 and t.weeks[-1] == AS_OF and t.max_week == 2
    assert t.found_this_week == 5                                                    # stored on AS_OF; one event at two sources counts once
    empty = build_trend(conn, AS_OF + timedelta(days=400))
    assert empty.total == 0 and empty.found_this_week == 0 and empty.takeaway == "No signals were found in the last eight weeks." and empty.pct is None


def test_score_history_is_the_last_stored_scores_oldest_first(cfg, tmp_path):
    conn = make_db(tmp_path)
    a = add_account(conn, "Acme")
    for i, sc in enumerate((10, 20, 30, 40, 50, 60, 70, 80, 90, 100)):
        conn.execute("INSERT INTO scores(account_id, run_date, score, band, components_json, rationale) VALUES (?,?,?,?,'{}','x')",
                     (a, (date(2026, 8, 1) + timedelta(days=7 * i)).isoformat(), sc, "Hot"))
    conn.commit()
    assert score_history(conn, [a]) == {a: [30, 40, 50, 60, 70, 80, 90, 100]} and score_history(conn, []) == {}
    assert score_history(conn, [a], points=2) == {a: [90, 100]}


def test_the_weekly_chart_stacks_kinds_with_hover_titles_and_is_empty_when_nothing_was_found():
    weeks = [AS_OF - timedelta(days=7 * (11 - i)) for i in range(12)]
    svg = weekly_svg(weeks, S(leadership=[0] * 11 + [3], press=[1] * 12), KIND_LABELS)
    assert svg.startswith("<svg") and "var(--k-lead)" in svg and "var(--k-press)" in svg and "<title>Week ending Oct 15: 3 leadership changes</title>" in svg
    assert "var(--k-hire)" not in svg and "#" not in svg.replace("&#", "")             # kinds come from tokens, never their own hex colours
    assert weekly_svg(weeks, S(), KIND_LABELS) == "" and weekly_svg([], {}, {}) == ""


def test_the_trend_line_needs_two_points_and_escapes_its_label():
    assert spark_svg([]) == "" and spark_svg([5]) == ""
    svg = spark_svg([10, 40, 100], label='Score <b>"x"</b>')
    assert "<polyline" in svg and "<circle" in svg and "<b>" not in svg and "&lt;b&gt;" in svg
    flat = spark_svg([0, 0, 0])
    assert "polyline" in flat                                                         # no division by zero on a flat line
