from datetime import timedelta

import pytest

from bd.feed import GROUP_LABELS, GROUPS, build_feed, group_of, load_items
from bd.models import SIGNAL_TYPES, SOURCE_TYPES
from bd.pipeline import set_client
from bd.score_store import score_accounts
from tests.conftest import AS_OF, add_account, add_signal, make_db


@pytest.fixture
def cfg(cfg):
    return cfg.model_copy(deep=True)


def test_every_signal_type_and_source_lands_in_exactly_one_group():
    assert {group_of(t, s) for t in SIGNAL_TYPES for s in SOURCE_TYPES} <= set(GROUP_LABELS)


def test_groups_follow_what_happened_then_where_it_was_reported():
    assert group_of("leadership_change", "secondary_report") == "leadership" and group_of("pe_platform_or_sponsor_exec", "company_release") == "leadership"
    assert group_of("hiring_cluster", "company_site") == "hiring"
    assert group_of("restructuring_layoffs", "secondary_report") == "press" and group_of("ma_divestiture", "company_release") == "press"
    assert group_of("restructuring_layoffs", "warn_notice") == "events" and group_of("platform_migration", "sec_filing") == "events"
    assert group_of("risk_factor", "sec_filing") == "background" and group_of("financial_pressure", "company_release") == "background"


@pytest.fixture
def world(cfg, tmp_path):
    conn = make_db(tmp_path)
    acme, beta, gone = add_account(conn, "Acme"), add_account(conn, "Beta"), add_account(conn, "Goneco", status="excluded")
    add_signal(conn, cfg, acme, "leadership_change", 3, summary="Acme names a COO.")
    add_signal(conn, cfg, acme, "hiring_cluster", 6, summary="Acme posts data roles.", source_type="company_site")
    add_signal(conn, cfg, acme, "restructuring_layoffs", 9, summary="Acme cuts a plant.", conf="Medium", source_type="secondary_report")
    add_signal(conn, cfg, acme, "restructuring_layoffs", 9, summary="Acme cuts a plant (release).", url="https://acme.com/news/cut", source_type="company_release")
    add_signal(conn, cfg, beta, "restructuring_layoffs", 12, summary="Beta WARN notice.", source_type="warn_notice")
    add_signal(conn, cfg, beta, "risk_factor", 20, summary="Beta lists a risk.", source_type="sec_filing")
    add_signal(conn, cfg, beta, "ma_divestiture", 5, conf="Low", summary="Beta rumour.", url="https://gossip.example/beta", source_type="rumor")
    add_signal(conn, cfg, beta, "vendor_deal", 90, summary="Beta signs a vendor.", source_type="company_release")      # outside the 30-day window
    add_signal(conn, cfg, gone, "leadership_change", 2, summary="Goneco names a CEO.")
    set_client(conn, beta, True)
    score_accounts(conn, cfg, AS_OF)
    return conn, dict(acme=acme, beta=beta, gone=gone)


def test_the_feed_groups_the_newest_signals_and_leaves_out_rumours_old_news_and_out_of_scope_companies(world):
    conn, ids = world
    f = build_feed(conn, AS_OF)
    assert [g.key for g in f.groups] == [k for k, _ in GROUPS]
    by = {g.key: [i.summary for i in g.items] for g in f.groups}
    assert by["leadership"] == ["Acme names a COO."] and by["hiring"] == ["Acme posts data roles."]
    assert by["events"] == ["Beta WARN notice."] and by["background"] == ["Beta lists a risk."]
    all_text = " ".join(s for v in by.values() for s in v)
    assert "rumour" not in all_text and "vendor" not in all_text and "Goneco" not in all_text
    assert f.companies == 2 and f.total == 5 and f.newest == AS_OF - timedelta(days=3)


def test_one_event_found_at_two_sources_shows_once_at_the_more_trustworthy_one(world):
    conn, ids = world
    press = build_feed(conn, AS_OF).group("press")
    assert press.total == 1 and press.items[0].summary == "Acme cuts a plant (release)." and press.items[0].confidence == "High"
    assert press.items[0].url == "https://acme.com/news/cut"


def test_items_carry_the_company_its_band_and_client_flag_and_are_newest_first(world):
    conn, ids = world
    items = load_items(conn, AS_OF)
    assert [i.event_date for i in items] == sorted((i.event_date for i in items), reverse=True)
    beta = [i for i in items if i.account == "Beta"]
    assert beta and all(i.client and i.account_id == ids["beta"] for i in beta)
    assert all(i.band is not None for i in items) and {i.account for i in items} == {"Acme", "Beta"}
    assert load_items(conn, AS_OF, account_id=ids["acme"]) and {i.account for i in load_items(conn, AS_OF, account_id=ids["acme"])} == {"Acme"}


def test_each_group_is_capped_but_still_counts_everything_in_the_window(cfg, tmp_path):
    conn = make_db(tmp_path)
    a = add_account(conn, "Acme")
    for n in range(9):
        add_signal(conn, cfg, a, "hiring_single", n + 1, url=f"https://acme.com/jobs/{n}", summary=f"Role {n}")
    g = build_feed(conn, AS_OF, per_group=4).group("hiring")
    assert g.total == 9 and len(g.items) == 4 and g.items[0].summary == "Role 0"
    assert build_feed(conn, AS_OF, days=3).group("hiring").total == 3


def test_an_empty_database_gives_an_empty_feed(cfg, tmp_path):
    conn = make_db(tmp_path)
    f = build_feed(conn, AS_OF)
    assert f.total == 0 and f.companies == 0 and f.newest is None and all(g.items == [] and g.total == 0 for g in f.groups)
