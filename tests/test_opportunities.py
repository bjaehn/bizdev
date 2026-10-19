from datetime import timedelta

import pytest

from bd.opportunities import (STATUS_CARD, STATUS_HELD, STATUS_OVER_CAP, STATUS_TRACKING, STATUS_UNKNOWN, build_opportunities,
                              filter_opportunities)
from bd.pe_store import sync_from_accounts
from bd.score_store import score_accounts
from bd.shortlist import ShortlistError
from tests.conftest import AS_OF, add_account, add_signal, make_db


@pytest.fixture
def cfg(cfg):
    return cfg.model_copy(deep=True)


@pytest.fixture
def conn(tmp_path):
    return make_db(tmp_path)


def world(conn, cfg):
    hot = add_account(conn, "Hotco", industry="Industrial", ownership="pe-backed", sponsor="Example Capital")
    for t, age in (("leadership_change", 10), ("hiring_cluster", 12), ("pe_addon", 20)):
        add_signal(conn, cfg, hot, t, age)
    warm = add_account(conn, "Warmco")                                                   # two families: 24 + 30 + 10 stacking = 64
    add_signal(conn, cfg, warm, "restructuring_layoffs", 5, lines=("Strategy & Operations",))
    add_signal(conn, cfg, warm, "hiring_leadership", 8, lines=("Strategy & Operations",))
    medium = add_account(conn, "Mediumco")                                               # Watch band, nothing High: tracked only
    add_signal(conn, cfg, medium, "rfp_match", 10, conf="Medium", lines=("Strategy & Operations",))
    add_signal(conn, cfg, medium, "restructuring_layoffs", 10, conf="Medium", lines=("Strategy & Operations",))
    quiet = add_account(conn, "Quietco")
    add_signal(conn, cfg, quiet, "risk_factor", 20)                                      # 8 pts: below Watch
    score_accounts(conn, cfg, AS_OF)
    return dict(hot=hot, warm=warm, medium=medium, quiet=quiet)


def test_ranked_by_score_with_reasons_and_only_watch_and_above_by_default(conn, cfg):
    ids = world(conn, cfg)
    ol = build_opportunities(conn, cfg, AS_OF)
    names = [o.account.name for o in ol.items]
    assert names[0] == "Hotco" and "Quietco" not in names
    assert [o.rank for o in ol.items] == list(range(1, len(names) + 1))
    assert [o.result.score for o in ol.items] == sorted((o.result.score for o in ol.items), reverse=True)
    hot = ol.items[0]
    assert {r.kind for r in hot.reasons} == {"leadership", "hiring", "pe"} and sum(r.points for r in hot.reasons) > 0
    assert hot.reasons[0].points >= hot.reasons[-1].points and "Leadership change" in next(r for r in hot.reasons if r.kind == "leadership").headline
    assert hot.why_now.endswith(".") and hot.why_source == "plain source summary" and hot.service_line
    assert len(build_opportunities(conn, cfg, AS_OF, include_tracked_only=True).items) == len(names) + 1


def test_status_says_where_each_one_stands_against_the_posting_gates(conn, cfg):
    world(conn, cfg)
    by = {o.account.name: o for o in build_opportunities(conn, cfg, AS_OF).items}
    assert by["Hotco"].status == STATUS_CARD and by["Hotco"].status_text == "on the Monday card"
    assert by["Warmco"].status in (STATUS_CARD, STATUS_HELD, STATUS_TRACKING)
    m = by["Mediumco"]
    assert m.status == STATUS_TRACKING and "tracked" in m.status_text
    cfg.post_gate.max_leads_per_post = 1
    by = {o.account.name: o for o in build_opportunities(conn, cfg, AS_OF).items}
    assert [o.status for o in by.values()].count(STATUS_CARD) == 1


def test_held_status_explains_why(conn, cfg):
    a = add_account(conn, "Heldco")                                              # Hot on points, but every signal is Medium
    for t in ("leadership_change", "restructuring_layoffs", "rfp_match"):
        add_signal(conn, cfg, a, t, 5, conf="Medium", lines=("Strategy & Operations",))
    score_accounts(conn, cfg, AS_OF)
    o = next(x for x in build_opportunities(conn, cfg, AS_OF).items if x.account.name == "Heldco")
    assert o.result.band == "Hot" and o.status == STATUS_HELD and "High-confidence" in o.status_text


def test_change_against_the_previous_run_and_new_flag(conn, cfg):
    ids = world(conn, cfg)
    prev = AS_OF - timedelta(days=7)
    with conn:
        conn.execute("INSERT INTO scores(account_id, run_date, score, band, components_json, rationale) VALUES (?,?,?,?,?,?)",
                     (ids["hot"], prev.isoformat(), 40, "Watch", "{}", "x"))
    ol = build_opportunities(conn, cfg, AS_OF)
    by = {o.account.name: o for o in ol.items}
    assert ol.prev_date == prev and by["Hotco"].prev_score == 40 and by["Hotco"].delta == by["Hotco"].result.score - 40
    assert not by["Hotco"].is_new and by["Warmco"].is_new                        # Warmco was not scored last run
    first = build_opportunities(conn, cfg, prev, include_tracked_only=True)
    assert first.prev_date is None and not any(o.is_new for o in first.items)


def test_sponsor_lift_is_a_separate_labelled_reason(conn, cfg):
    ids = world(conn, cfg)
    from bd.models import FirmSignalExtract
    from bd.pe_firms import AcceptedFirmSignal
    from bd.pe_store import find_firm, store_firm_signals
    sync_from_accounts(conn)
    fid = find_firm(conn, "Example Capital")["id"]
    ex = FirmSignalExtract(type="firm_new_fund", event_date=AS_OF - timedelta(days=5), summary="Closed Fund IV.",
                           source_url="https://examplecapital.com/n", source_type="company_release", confidence="High")
    store_firm_signals(conn, fid, [AcceptedFirmSignal(ex, "h1")], AS_OF)
    score_accounts(conn, cfg, AS_OF)
    hot = build_opportunities(conn, cfg, AS_OF).items[0]
    sp = next(r for r in hot.reasons if r.kind == "sponsor")
    assert sp.label == "Sponsor activity" and "Example Capital new fund" in sp.headline and sp.points == 8
    assert hot.firms == ["Example Capital"]


def test_open_postings_are_counted_for_the_window(conn, cfg):
    ids = world(conn, cfg)
    rows = [("https://boards.greenhouse.io/a/1", "VP Data", "svp_vp", 5, 1), ("https://boards.greenhouse.io/a/2", "Data Engineer", "other", 5, 1),
            ("https://boards.greenhouse.io/a/3", "Old Role", "svp_vp", 200, 1), ("https://boards.greenhouse.io/a/4", "Closed", "svp_vp", 3, 0)]
    for url, title, sen, age, open_ in rows:
        conn.execute("INSERT INTO postings(account_id,url,title,posted_date,theme,seniority,interim,source,first_seen,last_seen,open) "
                     "VALUES (?,?,?,?,?,?,0,'greenhouse',?,?,?)",
                     (ids["hot"], url, title, (AS_OF - timedelta(days=age)).isoformat(), "data_analytics", sen, AS_OF.isoformat(), AS_OF.isoformat(), open_))
    conn.commit()
    hot = build_opportunities(conn, cfg, AS_OF).items[0]
    assert (hot.open_postings, hot.leadership_postings) == (2, 1)


def test_stale_scores_are_flagged_and_statuses_withheld(conn, cfg):
    ids = world(conn, cfg)
    add_signal(conn, cfg, ids["hot"], "restructuring_layoffs", 2)               # signals changed after `bd score`
    ol = build_opportunities(conn, cfg, AS_OF)
    assert "Hotco" in ol.stale and all(o.status == STATUS_UNKNOWN for o in ol.items)


def test_no_scores_for_the_date_raises(conn, cfg):
    with pytest.raises(ShortlistError):
        build_opportunities(conn, cfg, AS_OF)


def test_filters(conn, cfg):
    world(conn, cfg)
    items = build_opportunities(conn, cfg, AS_OF).items
    assert [o.account.name for o in filter_opportunities(items, pe_only=True)] == ["Hotco"]
    assert [o.account.name for o in filter_opportunities(items, family="pe")] == ["Hotco"]
    assert all(o.result.band == "Hot" for o in filter_opportunities(items, band="Hot"))
    assert [o.account.name for o in filter_opportunities(items, q="WARM")] == ["Warmco"]
    assert filter_opportunities(items, status=STATUS_CARD) and all(o.status == STATUS_CARD for o in filter_opportunities(items, status=STATUS_CARD))
    assert filter_opportunities(items, family="nonexistent") == []
    by_change = filter_opportunities(items, sort="change")
    assert len(by_change) == len(items)


def test_page_renders_filters_and_escapes(tmp_path, cfg):
    from tests.test_hosted import PW, call
    from bd.jobs import JobRunner
    from bd.ui.app import App
    from bd.ui.auth import Hosted
    c = make_db(tmp_path)
    world(c, cfg)
    c.execute("UPDATE accounts SET name = '<script>alert(1)</script> Hotco' WHERE name = 'Hotco'")
    c.commit()
    db = str(tmp_path / "t.sqlite")
    app = App(cfg, db, root=tmp_path, hosted=Hosted(PW), jobs=JobRunner(db, tmp_path, ["true"]))
    app.fail_delay = 0
    ck = call(app, "POST", "/login", {"password": PW}).h["Set-Cookie"].split(";")[0].split("=", 1)[1]
    r = call(app, "GET", "/opportunities", cookie=ck)
    assert r.code == 200 and "Opportunities" in r.body and "Sponsor" not in r.body.split("<main>")[0]
    assert "<script>alert(1)" not in r.body and "&lt;script&gt;" in r.body
    assert "Leadership +" in r.body and "Why this score" in r.body and "on the Monday card" in r.body
    assert "No opportunities match" in call(app, "GET", "/opportunities?q=zzzz", cookie=ck).body
    assert "Hotco" in call(app, "GET", "/opportunities?pe=1&band=Hot", cookie=ck).body
    assert call(app, "GET", f"/opportunities?date={AS_OF.isoformat()}&sort=change&family=leadership", cookie=ck).code == 200
    assert call(app, "GET", "/opportunities?date=<script>&band=<x>", cookie=ck).code == 200
    assert "Monday card" in call(app, "GET", "/monday", cookie=ck).body
