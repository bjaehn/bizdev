from datetime import date, timedelta

import pytest

from bd.opportunities import build_opportunities
from bd.pipeline import add_note, set_client, set_stage, set_watching
from bd.score_store import score_accounts
from bd.week import TARGETS_MIN, build_week, signs_line
from tests.conftest import AS_OF, add_account, add_signal, make_db
from tests.test_update import app_env  # noqa: F401  (fixture)


@pytest.fixture
def world(tmp_path, cfg):
    conn = make_db(tmp_path)
    ids = {}
    ids["hot"] = a = add_account(conn, "Hotco", industry="Healthcare")
    for t, age in (("leadership_change", 4), ("hiring_cluster", 8), ("restructuring_layoffs", 15)):
        add_signal(conn, cfg, a, t, age)
    ids["warm"] = a = add_account(conn, "Warmco", industry="Insurance")
    for t, age in (("leadership_change", 6), ("restructuring_layoffs", 20)):
        add_signal(conn, cfg, a, t, age)
    ids["client"] = a = add_account(conn, "Clientco", industry="Healthcare")
    add_signal(conn, cfg, a, "leadership_change", 5)
    add_signal(conn, cfg, a, "hiring_cluster", 9)
    ids["quiet"] = add_account(conn, "Quietco")
    set_client(conn, ids["client"], True)
    return conn, ids


def stored(conn, cfg, ids, name, score, band, days_ago):
    conn.execute("INSERT INTO scores(account_id, run_date, score, band, components_json, rationale) VALUES (?,?,?,?,'{}','x')",
                 (ids[name], (AS_OF - timedelta(days=days_ago)).isoformat(), score, band))
    conn.commit()


def test_signs_line_says_in_words_how_many_kinds_of_signs_agree(world, cfg):
    conn, ids = world
    score_accounts(conn, cfg, AS_OF)
    by = {o.account.name: o for o in build_opportunities(conn, cfg, AS_OF, include_tracked_only=True).items}
    assert signs_line(by["Hotco"].result) == "3 kinds of signs agree: leadership, hiring, company events"
    assert signs_line(by["Warmco"].result).startswith("2 kinds of signs agree: leadership, company events")
    only_one = add_account(conn, "Oneco")
    add_signal(conn, cfg, only_one, "hiring_cluster", 3)
    score_accounts(conn, cfg, AS_OF)
    one = next(o for o in build_opportunities(conn, cfg, AS_OF, include_tracked_only=True).items if o.account.name == "Oneco")
    assert signs_line(one.result) == "1 kind of sign: hiring"


def test_the_list_is_new_business_only_strongest_first_with_clients_kept_apart(world, cfg):
    conn, ids = world
    score_accounts(conn, cfg, AS_OF)
    w = build_week(conn, cfg, AS_OF)
    names = [o.account.name for o in w.targets]
    assert names[0] == "Hotco" and "Clientco" not in names and "Quietco" not in names
    assert [o.account.name for o in w.clients] == ["Clientco"]
    scores = [o.result.score for o in w.targets]
    assert scores == sorted(scores, reverse=True)


def test_when_few_companies_are_hot_or_warm_the_strongest_watch_ones_fill_the_list_and_it_says_so(world, cfg):
    conn, ids = world
    for i in range(6):
        a = add_account(conn, f"Watchco {i}", industry="Energy")
        add_signal(conn, cfg, a, "hiring_cluster", 4)                           # two kinds, one recent and one older: a Watch score
        add_signal(conn, cfg, a, "cost_program", 40)
    score_accounts(conn, cfg, AS_OF)
    w = build_week(conn, cfg, AS_OF)
    assert w.hot_warm < TARGETS_MIN and w.padded and len(w.targets) == TARGETS_MIN
    assert w.more > 0
    assert w.hot_warm == sum(o.result.band in ("Hot", "Warm") for o in w.targets)


def test_when_enough_are_hot_or_warm_the_list_is_only_those_and_capped(tmp_path, cfg):
    conn = make_db(tmp_path)
    for i in range(12):
        a = add_account(conn, f"Strongco {i}")
        for t, age in (("leadership_change", 4), ("hiring_cluster", 8), ("restructuring_layoffs", 15)):
            add_signal(conn, cfg, a, t, age)
    score_accounts(conn, cfg, AS_OF)
    w = build_week(conn, cfg, AS_OF)
    assert len(w.targets) == 8 and not w.padded and w.hot_warm == 12 and w.more == 4
    assert all(o.result.band in ("Hot", "Warm") for o in w.targets)


def test_what_changed_is_a_sentence_built_from_counts_and_says_so_on_a_first_run(world, cfg):
    conn, ids = world
    score_accounts(conn, cfg, AS_OF)
    assert build_week(conn, cfg, AS_OF).changed_line == "This is the first scoring run, so there is nothing to compare it with yet."
    stored(conn, cfg, ids, "hot", 20, "Stored", 7)
    stored(conn, cfg, ids, "warm", 50, "Watch", 7)
    w = build_week(conn, cfg, AS_OF)
    assert w.changed_line.startswith(f"Since the run on {(AS_OF - timedelta(days=7)).isoformat()}: ")
    assert "became Hot or Warm" in w.changed_line and "moved up by 5 points or more" in w.changed_line and "new signal" in w.changed_line
    assert {o.account.name for o in w.changes.new_leads} == {"Hotco", "Clientco"}


def test_tracked_companies_show_what_is_new_in_the_last_14_days_and_nothing_new_is_said_plainly(world, cfg):
    conn, ids = world
    set_watching(conn, ids["warm"], True)
    set_stage(conn, ids["hot"], "pursuing", "")
    add_signal(conn, cfg, ids["hot"], "ai_commitment", 2)                    # detected in the as-of week
    old = add_account(conn, "Oldnews")
    add_signal(conn, cfg, old, "leadership_change", 60, as_of=AS_OF - timedelta(days=30))
    set_watching(conn, old, True)
    score_accounts(conn, cfg, AS_OF)
    w = build_week(conn, cfg, AS_OF)
    by = {t.opp.account.name: t for t in w.tracked}
    assert set(by) == {"Hotco", "Warmco", "Oldnews"} and "Clientco" not in by
    assert by["Hotco"].new and by["Hotco"].new[0].type == "ai_commitment"
    assert by["Oldnews"].new == []
    assert w.tracked[0].new                                                    # companies with something new come first


def test_nothing_scored_is_explained_in_plain_words(tmp_path, cfg):
    conn = make_db(tmp_path)
    w = build_week(conn, cfg, AS_OF)
    assert w.problem and "Nothing has been scored yet" in w.problem and w.targets == []


# ---- the pages ---------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def seeded(app_env):  # noqa: F811
    e = app_env
    today = date.today()
    ids = {}
    for name, sigs in (("Hotco <b>", ("leadership_change", "hiring_cluster", "restructuring_layoffs")), ("Clientco", ("leadership_change", "hiring_cluster")),
                       ("Trackco", ("leadership_change",))):
        a = add_account(e.conn, name, industry="Healthcare")
        for i, t in enumerate(sigs):
            add_signal(e.conn, e.cfg, a, t, 3 + 4 * i, as_of=today, url=f"https://www.sec.gov/Archives/{a}/{t}.htm")
        ids[name] = a
    set_client(e.conn, ids["Clientco"], True)
    set_watching(e.conn, ids["Trackco"], True)
    score_accounts(e.conn, e.cfg, today)
    return e, ids


def test_this_week_opens_with_what_the_tool_is_and_a_way_to_find_a_company(seeded):
    e, ids = seeded
    body = e.get("/week").body
    assert "<h1>This week</h1>" in body and "How this works" in body and 'href="/help"' in body
    assert "<b>different kinds</b>" in body and "Preparing for a call?" in body and 'action="/find"' in body


def test_this_week_lists_companies_in_plain_words_with_why_now_and_a_brief_link(seeded):
    e, ids = seeded
    body = e.get("/week").body
    assert "Companies to look at" in body and "Hotco &lt;b&gt;" in body and "Hotco <b>" not in body
    assert "Why now:" in body and "3 kinds of signs agree" in body and f'href="/companies/{ids["Hotco <b>"]}"' in body and "Open brief" in body
    assert "Existing clients with news" in body and "Clientco" in body.split("Existing clients with news")[1]
    assert "Clientco" not in body.split("Companies to look at")[1].split("Existing clients with news")[0]


def test_this_week_has_tracking_and_market_sections_and_links_to_the_deeper_views(seeded):
    e, ids = seeded
    body = e.get("/week").body
    assert "Companies you are tracking" in body and "Trackco" in body.split("Companies you are tracking")[1]
    assert "The market this week" in body and "Healthcare" in body.split("The market this week")[1]
    for href in ("/opportunities", "/opportunities?view=chart", "/market", "/help"):
        assert f'href="{href}"' in body, href


def test_this_week_says_how_to_start_tracking_when_nothing_is_tracked(app_env):  # noqa: F811
    e = app_env
    a = add_account(e.conn, "Solo")
    add_signal(e.conn, e.cfg, a, "leadership_change", 3, as_of=date.today())
    score_accounts(e.conn, e.cfg, date.today())
    assert "not tracking any company yet" in e.get("/week").body


def test_the_empty_state_tells_an_analysis_reader_what_to_do(app_env):  # noqa: F811
    body = app_env.get("/week").body
    assert "Nothing has been scored yet" in body and "Admin area" in body


def test_the_help_page_uses_the_real_thresholds_and_names_the_four_jobs(seeded):
    e, ids = seeded
    body = e.get("/help").body
    b = e.cfg.scoring.bands
    assert f"<b>Hot</b> is {b.hot} or more" in body and f"<b>Warm</b> {b.warm} or more" in body and f"<b>Watch</b> {b.watch} or more" in body
    for job in ("See what to look at this week", "Prepare for one company", "Show the market to leadership", "Track a company over time"):
        assert job in body, job
    assert "does not write outreach" in body and "LinkedIn" in body
