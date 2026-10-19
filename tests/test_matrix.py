from datetime import date, timedelta

import pytest

from bd.matrix import RECENT_DAYS, SHADE_FULL, build_matrix, columns, sort_rows
from bd.opportunities import build_opportunities
from bd.pipeline import set_client
from bd.score_store import score_accounts
from tests.conftest import AS_OF, add_account, add_signal, make_db


@pytest.fixture
def world(tmp_path, cfg):
    conn = make_db(tmp_path)
    wide = add_account(conn, "Wideco", industry="Healthcare")           # four signal types
    for t, age in (("leadership_change", 4), ("hiring_cluster", 10), ("restructuring_layoffs", 40), ("platform_migration", 70)):
        add_signal(conn, cfg, wide, t, age)
    deep = add_account(conn, "Deepco", industry="Insurance")            # one signal type, three signals
    for age in (3, 20, 50):
        add_signal(conn, cfg, deep, "hiring_cluster", age)
    old = add_account(conn, "Oldco", industry="Insurance")              # two types, but both older
    add_signal(conn, cfg, old, "leadership_change", 100)
    add_signal(conn, cfg, old, "cost_program", 120)
    add_account(conn, "Quietco")                                        # no signals at all
    score_accounts(conn, cfg, AS_OF)
    ol = build_opportunities(conn, cfg, AS_OF, include_tracked_only=True)
    return conn, ol, {"wide": wide, "deep": deep, "old": old}


def row(m, name):
    return next(r for r in m.rows if r.opp.account.name == name)


def test_columns_are_the_signal_types_in_config_order(cfg):
    cols = columns(cfg)
    assert [k for k, _ in cols] == list(cfg.scoring.families) and cols[0] == ("leadership", "Leadership") and len(cols) == 8


def test_a_cell_carries_the_points_the_scoring_engine_gave_that_signal_type(world, cfg):
    conn, ol, ids = world
    m = build_matrix(ol.items, cfg, AS_OF)
    w = row(m, "Wideco")
    by = {c.family: c for c in w.cells}
    assert w.opp.result.family_totals["leadership"] == pytest.approx(by["leadership"].points) and by["leadership"].count == 1
    assert by["leadership"].recent and by["hiring"].recent and not by["events"].recent and not by["technology"].recent   # 4, 10 days vs 40, 70 days
    assert by["procurement"].points == 0 and by["procurement"].title == "" and by["procurement"].shade == 0
    assert 0 < by["leadership"].shade <= 1 and by["leadership"].shade == min(1.0, by["leadership"].points / SHADE_FULL)
    assert "Leadership change" in by["leadership"].title and "points" in by["leadership"].title
    assert RECENT_DAYS == 30


def test_types_firing_counts_signal_types_not_signals(world, cfg):
    conn, ol, ids = world
    m = build_matrix(ol.items, cfg, AS_OF)
    assert row(m, "Wideco").types == 4 and row(m, "Deepco").types == 1
    deep = {c.family: c for c in row(m, "Deepco").cells}["hiring"]
    assert deep.count == 3 and "(+2 more)" in deep.title                      # three hiring signals share one cell
    assert row(m, "Wideco").opp.result.stacking_bonus > row(m, "Deepco").opp.result.stacking_bonus == 0


def test_a_company_with_no_signals_is_all_empty_cells(world, cfg):
    conn, ol, ids = world
    m = build_matrix(ol.items, cfg, AS_OF)
    assert not [r for r in m.rows if r.opp.account.name == "Quietco"]            # a company with no signals is not an opportunity at all
    empty = build_matrix(ol.items[:0], cfg, AS_OF)
    assert empty.rows == [] and len(empty.columns) == 8                         # nothing to rank still gives the header


def test_the_private_equity_column_includes_the_owners_news(tmp_path, cfg):
    from bd.pe_store import sync_from_accounts
    conn = make_db(tmp_path)
    a = add_account(conn, "Portco", ownership="pe-backed", sponsor="Acme Capital")
    add_signal(conn, cfg, a, "pe_addon", 5)
    add_signal(conn, cfg, a, "leadership_change", 5)
    sync_from_accounts(conn)
    fid = conn.execute("SELECT id FROM pe_firms WHERE name = 'Acme Capital'").fetchone()[0]
    conn.execute("INSERT INTO firm_signals(firm_id, type, event_date, detected_date, summary, source_url, source_type, confidence, hash) "
                 "VALUES (?,?,?,?,?,?,?,?,?)", (fid, "firm_new_fund", (AS_OF - timedelta(days=5)).isoformat(), AS_OF.isoformat(), "New fund.",
                                               "https://acme.example/fund", "company_release", "High", "h1"))
    conn.commit()
    score_accounts(conn, cfg, AS_OF)
    m = build_matrix(build_opportunities(conn, cfg, AS_OF, include_tracked_only=True).items, cfg, AS_OF)
    c = {x.family: x for x in m.rows[0].cells}["pe"]
    assert m.rows[0].opp.result.sponsor_lift > 0 and c.points == pytest.approx(m.rows[0].opp.result.family_totals["pe"] + m.rows[0].opp.result.sponsor_lift)
    assert "owner's own news" in c.title


def test_sorting_by_score_types_newest_and_change(world, cfg):
    conn, ol, ids = world
    m = build_matrix(ol.items, cfg, AS_OF)
    assert [r.opp.account.name for r in sort_rows(m.rows, "score")] == [r.opp.account.name for r in sorted(m.rows, key=lambda r: r.opp.rank)]
    types = sort_rows(m.rows, "types")
    assert types[0].opp.account.name == "Wideco" and [r.types for r in types] == sorted((r.types for r in types), reverse=True)
    newest = sort_rows(m.rows, "newest")
    assert [r.opp.account.name for r in newest] == ["Deepco", "Wideco", "Oldco"]            # newest signal: 3, 4 and 100 days ago
    assert [r.opp.account.name for r in sort_rows(m.rows, "bogus")] == [r.opp.account.name for r in sort_rows(m.rows, "score")]


def test_the_score_bar_is_a_share_of_the_maximum(world, cfg):
    conn, ol, ids = world
    m = build_matrix(ol.items, cfg, AS_OF)
    for r in m.rows:
        assert 0 <= r.score_pct <= 100 and r.score_pct == round(100 * r.opp.result.score / cfg.scoring.score_max)
