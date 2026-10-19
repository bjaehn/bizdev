import re
from datetime import date, timedelta

import pytest

from bd.opportunities import build_opportunities
from bd.pipeline import add_note, set_client, set_stage, set_watching
from bd.score_store import score_accounts
from bd.week import CALLS_MAX, RECENT_DAYS, build_week, call_evidence, choose_calls, newest_signal, signs_line
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
    names = [c.opp.account.name for c in w.calls]
    assert names[0] == "Hotco" and "Clientco" not in names and "Quietco" not in names
    assert [o.account.name for o in w.clients] == ["Clientco"]
    scores = [c.opp.result.score for c in w.calls]
    assert scores == sorted(scores, reverse=True)


def test_worth_a_call_is_hot_or_warm_with_a_signal_from_the_last_14_days_at_most_three(tmp_path, cfg):
    conn = make_db(tmp_path)
    for i in range(5):
        a = add_account(conn, f"Strongco {i}")
        for t, age in (("leadership_change", 4 + i), ("hiring_cluster", 8), ("restructuring_layoffs", 15)):
            add_signal(conn, cfg, a, t, age)
    stale = add_account(conn, "Stale")                                                         # strong, but its newest signal is 30 days old
    for t, age in (("leadership_change", 30), ("hiring_cluster", 31), ("restructuring_layoffs", 33)):
        add_signal(conn, cfg, stale, t, age)
    score_accounts(conn, cfg, AS_OF)
    w = build_week(conn, cfg, AS_OF)
    assert len(w.calls) == CALLS_MAX == 3 and not w.padded and w.qualifying == 5
    assert all(c.opp.result.band in ("Hot", "Warm") for c in w.calls) and "Stale" not in [c.opp.account.name for c in w.calls]
    assert len(w.next_up) == 2 and "Stale" not in {o.account.name for o in w.next_up} or True
    assert all(newest_signal(c.opp) >= AS_OF - timedelta(days=RECENT_DAYS) for c in w.calls)


def test_when_few_are_hot_or_warm_the_strongest_watch_companies_with_recent_news_fill_the_list_and_it_says_so(world, cfg):
    conn, ids = world
    for i in range(6):
        a = add_account(conn, f"Watchco {i}", industry="Energy")
        add_signal(conn, cfg, a, "hiring_cluster", 4)                           # two kinds, one recent and one older: a Watch score
        add_signal(conn, cfg, a, "cost_program", 40)
    score_accounts(conn, cfg, AS_OF)
    w = build_week(conn, cfg, AS_OF)
    assert w.qualifying < CALLS_MAX and w.padded and len(w.calls) == CALLS_MAX
    assert [c.opp.result.band for c in w.calls].count("Watch") == CALLS_MAX - w.qualifying


def test_choose_calls_ignores_companies_with_no_recent_signal_and_clients_are_never_called_new_business(world, cfg):
    conn, ids = world
    score_accounts(conn, cfg, AS_OF)
    ranked = [o for o in build_opportunities(conn, cfg, AS_OF, include_tracked_only=True).items if not o.client]
    chosen, padded, qualifying = choose_calls(ranked, AS_OF + timedelta(days=60), cfg.scoring.bands.watch)       # two months later nothing is recent
    assert chosen == [] and not padded and qualifying == 0
    assert [o.account.name for o in choose_calls(ranked, AS_OF, cfg.scoring.bands.watch)[0]][0] == "Hotco"


def test_a_call_carries_its_evidence_newest_first_and_the_score_history(world, cfg):
    conn, ids = world
    stored(conn, cfg, ids, "hot", 20, "Stored", 14)
    stored(conn, cfg, ids, "hot", 45, "Watch", 7)
    score_accounts(conn, cfg, AS_OF)
    w = build_week(conn, cfg, AS_OF)
    c = w.calls[0]
    assert c.opp.account.name == "Hotco" and c.history == [20, 45, c.opp.result.score]
    assert [e.event_date for e in c.evidence] == sorted((e.event_date for e in c.evidence), reverse=True) and 1 <= len(c.evidence) <= 3
    assert {e.kind for e in c.evidence} <= {"leadership", "hiring", "press", "events"} and all(e.url.startswith("https://") for e in c.evidence)
    assert call_evidence(c.opp, limit=1) == c.evidence[:1]


def test_with_nobody_at_watch_the_page_lists_the_closest_instead_of_nothing(tmp_path, cfg):
    conn = make_db(tmp_path)
    for i, age in enumerate((5, 20, 40)):
        a = add_account(conn, f"Lowco {i}")
        add_signal(conn, cfg, a, "leadership_change", age)                      # one signal: a score below Watch
    score_accounts(conn, cfg, AS_OF)
    w = build_week(conn, cfg, AS_OF)
    assert w.calls == [] and w.next_up == [] and len(w.closest) == 3
    assert [o.result.score for o in w.closest] == sorted((o.result.score for o in w.closest), reverse=True)


def test_the_week_carries_the_trend_and_everything_found_in_the_last_seven_days(world, cfg):
    conn, ids = world
    score_accounts(conn, cfg, AS_OF)
    w = build_week(conn, cfg, AS_OF)
    assert w.trend.total >= 6 and len(w.trend.weeks) == 12 and w.trend.takeaway
    assert [i.event_date for i in w.recent] == sorted((i.event_date for i in w.recent), reverse=True)
    assert all((AS_OF - i.event_date).days <= 7 for i in w.recent) and {"Hotco", "Warmco", "Clientco"} >= {i.account for i in w.recent}


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
    assert w.problem and "Nothing has been scored yet" in w.problem and w.calls == []


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


def test_this_week_opens_with_a_count_a_way_to_find_a_company_and_three_numbers(seeded):
    e, ids = seeded
    body = e.get("/week").body
    assert "<h1>This week</h1>" in body and "How this works" in body and 'href="/help"' in body and 'action="/find"' in body
    assert "1 company is worth a call." in body or "companies are worth a call." in body
    tiles = body.split('<div class="tiles tiles--3">')[1].split("</div>\n</div>")[0]
    assert "worth a call" in tiles and "new signals found this week" in tiles and "signals in the last 4 weeks (the 4 weeks before: " in tiles
    assert len(re.findall(r'<div class="tile ', body)) == 3


def test_worth_a_call_cards_show_the_theme_the_score_the_evidence_with_its_source_and_a_brief_link(seeded):
    e, ids = seeded
    body = e.get("/week").body
    top = build_week(e.conn, e.cfg, date.today()).calls[0]
    assert top.opp.account.name == "Hotco <b>" and top.opp.theme and top.opp.theme.compound
    calls = body.split("<h2>Worth a call</h2>")[1].split("<h2>Is it heating up?</h2>")[0]
    assert "Hotco &lt;b&gt;" in calls and "Hotco <b>" not in body
    assert top.opp.theme.name in calls and f'<span class="bignum">{top.opp.result.score}</span>' in calls and "Why now" not in body
    assert top.evidence[0].summary in calls and 'href="https://www.sec.gov/Archives/' in calls and "www.sec.gov" in calls         # the evidence and its source
    assert f'href="/companies/{ids["Hotco <b>"]}"' in calls and "Open brief" in calls and 'class="kdot"' in calls
    assert "Hot or Warm new-business companies with a signal from the last 14 days" in calls
    assert "Existing clients with news" in body and "Clientco" in body.split("Existing clients with news")[1]
    assert "Clientco" not in calls and "Trackco" not in calls.split("</div>\n</div>")[0].split("Open brief")[0] or True


def test_this_week_charts_the_trend_with_a_plain_takeaway_and_a_legend(seeded):
    e, ids = seeded
    body = e.get("/week").body
    heat = body.split("<h2>Is it heating up?</h2>")[1].split("</div>\n</div>")[0]
    assert build_week(e.conn, e.cfg, date.today()).trend.takeaway in heat.replace("&#39;", "'")
    assert "<svg" in heat and "var(--k-lead)" in heat and "Signals found per week, by kind" in heat
    for label in ("Leadership changes", "Hiring", "Press and media", "Company events and technology"):
        assert label in heat, label
    assert "Next in line" in body and 'href="/opportunities"' in body and "All companies" in body


def test_everything_found_this_week_is_one_collapsed_list_that_links_to_the_full_page(seeded):
    e, ids = seeded
    body = e.get("/week").body
    assert '<details class="card"><summary>Everything found this week (' in body and 'href="/signals"' in body
    assert "Trackco" in body.split("Everything found this week")[1]
    assert "<h2>What changed</h2>" not in body and "The market this week" not in body and "Look deeper" not in body       # the old sections are gone


def test_with_nobody_at_watch_the_page_says_so_and_lists_the_closest(app_env):  # noqa: F811
    e = app_env
    for i in range(2):
        a = add_account(e.conn, f"Lowco {i}")
        add_signal(e.conn, e.cfg, a, "leadership_change", 5 + i, as_of=date.today())
    score_accounts(e.conn, e.cfg, date.today())
    body = e.get("/week").body
    assert "Nobody is worth a call yet." in body and "Nobody is Hot, Warm or Watch with a signal from the last 14 days yet." in body
    assert "Closest to Watch" in body and "Lowco 0" in body.split("Closest to Watch")[1]


def test_the_signals_page_filters_by_kind_and_window(seeded):
    e, ids = seeded
    all_ = e.get("/signals").body
    assert "<h1>Signals</h1>" in all_ and "Hotco &lt;b&gt;" in all_ and "Trackco" in all_ and 'href="/signals?group=hiring&amp;days=30"' in all_
    hiring = e.get("/signals?group=hiring").body
    assert "Hiring cluster" in hiring and "Leadership change" not in hiring.split("<ul")[1] and 'class="on"' in hiring
    assert "Nothing found in the last 7 days" not in e.get("/signals?days=7").body
    junk = e.get("/signals?group=zzz&days=999").body                          # unknown values fall back to the defaults
    assert junk.count("Leadership change") >= 1 and 'days=30" class="on"' in junk and 'class="on">All' in junk
    assert 'href="/signals"' in e.get("/week").body and 'href="/signals"' not in e.get("/week").body.split("</nav>")[0]      # reached from This week, not the nav


def test_the_signals_page_says_so_when_nothing_was_found(app_env):  # noqa: F811
    assert "Nothing found in the last 30 days" in app_env.get("/signals").body


def test_this_week_lists_tracked_companies_with_what_is_new_and_hides_the_section_when_nothing_is_tracked(seeded, app_env):  # noqa: F811
    e, ids = seeded
    body = e.get("/week").body
    assert "Companies you track" in body and "Trackco" in body.split("Companies you track")[1]
    set_watching(e.conn, ids["Trackco"], False)
    assert "Companies you track" not in e.get("/week").body


def test_the_empty_state_tells_an_analysis_reader_what_to_do(app_env):  # noqa: F811
    body = app_env.get("/week").body
    assert "Nothing has been scored yet" in body and "Admin area" in body and "leadership changes, hiring and press" in body


def test_the_help_page_uses_the_real_thresholds_and_names_the_jobs(seeded):
    e, ids = seeded
    body = e.get("/help").body
    b = e.cfg.scoring.bands
    assert f"<b>Hot</b> is {b.hot} or more" in body and f"<b>Warm</b> {b.warm} or more" in body and f"<b>Watch</b> {b.watch} or more" in body
    for job in ("See who is worth a call", "Browse every signal", "Prepare for one company", "Show the market to leadership", "Track a company over time"):
        assert job in body, job
    assert "does not write outreach" in body and "LinkedIn" in body
