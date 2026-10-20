import re
from datetime import date, timedelta

import pytest

from bd.opportunities import build_opportunities
from bd.pipeline import set_client, set_stage, set_watching
from bd.score_store import score_accounts
from bd.week import CALLS_MAX, build_week, call_evidence, choose_calls, newest_signal, signs_line
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


# ---- who is shown, and with which score --------------------------------------------------------------------------------------------
def test_the_list_is_everyone_at_watch_or_above_numbered_by_rank_with_clients_kept_in_it(world, cfg):
    conn, ids = world
    score_accounts(conn, cfg, AS_OF)
    w = build_week(conn, cfg, AS_OF)
    watch = cfg.scoring.bands.watch
    names = [o.account.name for o in w.ranked]
    assert "Clientco" in names and "Quietco" not in names                                        # a client is listed (and tagged); a company with no signal is not
    assert all(o.result.score >= watch for o in w.ranked) and {o.account.name for o in w.items if o.result.score >= watch} == set(names)
    assert [o.result.score for o in w.ranked] == sorted((o.result.score for o in w.ranked), reverse=True)
    assert [o.rank for o in w.ranked] == list(range(1, len(w.ranked) + 1))                       # numbered 1 to n, no gaps
    assert w.coverage.scored == len(w.ranked) and w.coverage.hot + w.coverage.warm == sum(o.result.band in ("Hot", "Warm") for o in w.ranked)


def test_the_cards_are_the_top_three_new_business_companies_by_score_and_carry_the_rank_they_hold(tmp_path, cfg):
    conn = make_db(tmp_path)
    for i in range(5):
        a = add_account(conn, f"Strongco {i}")
        for t, age in (("leadership_change", 4 + i), ("hiring_cluster", 8), ("restructuring_layoffs", 15)):
            add_signal(conn, cfg, a, t, age)
    client = add_account(conn, "Bigclient")
    for t, age in (("leadership_change", 2), ("hiring_cluster", 3), ("restructuring_layoffs", 4), ("ai_commitment", 5)):
        add_signal(conn, cfg, client, t, age)
    set_client(conn, client, True)
    score_accounts(conn, cfg, AS_OF)
    w = build_week(conn, cfg, AS_OF)
    new_business = [o for o in w.ranked if not o.client]
    assert len(w.calls) == CALLS_MAX == 3 and [c.opp for c in w.calls] == new_business[:3]
    assert "Bigclient" not in [c.opp.account.name for c in w.calls] and w.ranked[0].account.name == "Bigclient"      # the client leads the list, but is not a new logo
    assert [c.opp.rank for c in w.calls] == [2, 3, 4]                                            # the card shows the rank it holds in the numbered list
    assert [c.opp.result.score for c in w.calls] == sorted((c.opp.result.score for c in w.calls), reverse=True)


def test_the_dates_of_the_signals_do_not_decide_who_gets_a_card(tmp_path, cfg):
    conn = make_db(tmp_path)
    older = add_account(conn, "Olderco")                                                         # strong, but its newest signal is a month old
    for t, age in (("leadership_change", 30), ("hiring_cluster", 31), ("restructuring_layoffs", 33)):
        add_signal(conn, cfg, older, t, age)
    score_accounts(conn, cfg, AS_OF)
    w = build_week(conn, cfg, AS_OF)
    assert w.ranked and [c.opp.account.name for c in w.calls] == ["Olderco"]
    assert w.calls[0].newest == AS_OF - timedelta(days=30) == newest_signal(w.calls[0].opp)      # and the card says how old the newest one is


def test_choose_calls_takes_the_first_three_new_business_companies_of_the_list(world, cfg):
    conn, ids = world
    score_accounts(conn, cfg, AS_OF)
    ranked = build_week(conn, cfg, AS_OF).ranked
    assert [o.account.name for o in choose_calls(ranked)][0] == "Hotco" and all(not o.client for o in choose_calls(ranked))
    assert choose_calls([]) == [] and len(choose_calls(ranked * 3)) == CALLS_MAX


def test_every_score_shown_is_the_stored_score_for_that_run(world, cfg):
    conn, ids = world
    score_accounts(conn, cfg, AS_OF)
    w = build_week(conn, cfg, AS_OF)
    assert w.stale == [] and w.run_date == AS_OF.isoformat()
    for o in w.items:
        row = conn.execute("SELECT score, band FROM scores WHERE account_id = ? AND run_date = ?", (o.account.id, w.run_date)).fetchone()
        assert (o.result.score, o.result.band) == (row["score"], row["band"]), o.account.name


def test_a_score_that_no_longer_matches_the_signals_is_flagged_not_hidden(world, cfg):
    conn, ids = world
    score_accounts(conn, cfg, AS_OF)
    add_signal(conn, cfg, ids["warm"], "hiring_cluster", 2)                                      # a signal stored after the last Score run
    w = build_week(conn, cfg, AS_OF)
    assert w.stale == ["Warmco"]


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


def test_with_nobody_at_watch_the_closest_are_listed_instead_of_nothing(tmp_path, cfg):
    conn = make_db(tmp_path)
    for i, age in enumerate((5, 20, 40)):
        a = add_account(conn, f"Lowco {i}")
        add_signal(conn, cfg, a, "leadership_change", age)                      # one signal: a score below Watch
    score_accounts(conn, cfg, AS_OF)
    w = build_week(conn, cfg, AS_OF)
    assert w.calls == [] and w.ranked == [] and len(w.closest) == 3
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
    assert w.problem and "Nothing has been scored yet" in w.problem and w.calls == [] and w.ranked == []


# ---- the page ----------------------------------------------------------------------------------------------------------------------
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


def section(body, start, end):
    return body.split(start)[1].split(end)[0]


def test_analysis_is_one_page_that_opens_with_a_count_a_way_to_find_a_company_and_three_numbers(seeded):
    e, ids = seeded
    body = e.get("/opportunities").body
    assert "<h1>Analysis</h1>" in body and "How this works" in body and 'href="/help"' in body and 'action="/find"' in body
    assert re.search(r"\d+ compan(y is|ies are) ranked, scored ", body)
    tiles = body.split('<div class="tiles tiles--3">')[1].split("</div>\n</div>")[0]
    assert "Hot or Warm" in tiles and "new signals found this week" in tiles and "signals in the last 4 weeks (the 4 weeks before: " in tiles
    assert len(re.findall(r'<div class="tile ', body)) == 3


def test_the_sections_come_in_one_order_and_the_old_pages_lead_here(seeded):
    e, ids = seeded
    body = e.get("/opportunities").body
    order = [body.index(h) for h in ("<h2>The top three</h2>", "<h2>Is it heating up?</h2>", '<h2 id="ranked">Ranked</h2>', "<h2>Companies you track</h2>",
                                     "Signals dated in the last 7 days", '<h2 id="hiring">Hiring</h2>')]
    assert order == sorted(order) and body.index('<h2 id="hiring">Hiring</h2>') > body.index("Companies you track")      # hiring is at the bottom
    for old, new in (("/week", "/opportunities"), ("/leadership", "/opportunities"), ("/hiring", "/opportunities#hiring"),
                     ("/hiring?theme=data_analytics", "/opportunities?theme=data_analytics#hiring")):
        r = e.get(old)
        assert r.code == 303 and r.h["Location"] == new, old
    assert "Next in line" not in body and "Worth a call" not in body and "All companies" not in body


def test_the_top_three_cards_show_the_theme_the_score_the_rank_the_evidence_with_its_source_and_a_brief_link(seeded):
    e, ids = seeded
    body = e.get("/opportunities").body
    top = build_week(e.conn, e.cfg, date.today()).calls[0]
    assert top.opp.account.name == "Hotco <b>" and top.opp.theme and top.opp.theme.compound
    cards = section(body, "<h2>The top three</h2>", "<h2>Is it heating up?</h2>")
    assert "Hotco &lt;b&gt;" in cards and "Hotco <b>" not in body
    assert top.opp.theme.name in cards and f'<span class="bignum">{top.opp.result.score}</span>' in cards
    assert f"Rank {top.opp.rank} · newest signal " in cards
    assert top.evidence[0].summary in cards and 'href="https://www.sec.gov/Archives/' in cards and "www.sec.gov" in cards          # the evidence and its source
    assert f'href="/companies/{ids["Hotco <b>"]}"' in cards and "Open brief" in cards and 'class="kdot"' in cards
    assert "Clientco" not in cards and "Trackco" not in cards.split("Open brief")[0]            # a client is not a new logo


def test_the_numbered_list_agrees_with_the_cards_and_tags_clients(seeded):
    e, ids = seeded
    body = e.get("/opportunities").body
    w = build_week(e.conn, e.cfg, date.today())
    rows = re.findall(r'<li class="rank-row"><span class="rank-no">(\d+)</span>.*?href="/companies/(\d+)">(.*?)</a>(.*?)<div class="rank-score"><b>(\d+)</b>',
                      section(body, 'id="ranked"', "Existing clients are tagged"), re.S)
    assert [(int(n), int(i), s) for n, i, _, _, s in rows] == [(o.rank, o.account.id, str(o.result.score)) for o in w.ranked]    # same companies, ranks and scores as the data
    assert [int(r[0]) for r in rows] == list(range(1, len(rows) + 1))
    by = {name: tail for _, _, name, tail, _ in rows}
    assert '<span class="badge good">client</span>' in by["Clientco"] and 'class="badge good"' not in by["Hotco &lt;b&gt;"]
    cards = section(body, "<h2>The top three</h2>", "<h2>Is it heating up?</h2>")
    for c in w.calls:                                                                            # every card sits in the list at the rank it shows
        assert f"Rank {c.opp.rank} " in cards and c.opp.account.id in [int(r[1]) for r in rows]
    assert "Trackco" not in section(body, 'id="ranked"', "Existing clients are tagged")          # one signal: below Watch, so not listed
    assert "Trackco" in section(e.get("/opportunities?tracked=1").body, 'id="ranked"', "Existing clients are tagged")


def test_the_list_tabs_filter_by_band(seeded):
    e, ids = seeded
    w = build_week(e.conn, e.cfg, date.today())
    for label in ("All", "Hot", "Warm", "Watch", "Everything scanned"):
        assert f">{label}</a>" in e.get("/opportunities").body, label
    for band in ("Hot", "Warm", "Watch"):
        body = section(e.get(f"/opportunities?band={band}").body, 'id="ranked"', "Existing clients are tagged")
        expect = [o for o in w.ranked if o.result.band == band]
        assert body.count('class="rank-row"') == len(expect), band
        assert all(o.account.name.replace("<", "&lt;").replace(">", "&gt;") in body for o in expect)
    assert e.get("/opportunities?band=<x>").code == 200                                          # an unknown band is ignored


def test_analysis_charts_the_trend_full_width_with_a_plain_takeaway_a_legend_and_the_numbers(seeded):
    e, ids = seeded
    body = e.get("/opportunities").body
    heat = section(body, "<h2>Is it heating up?</h2>", "<h2>Ranked")
    assert build_week(e.conn, e.cfg, date.today()).trend.takeaway in heat.replace("&#39;", "'")
    assert '<div class="chart chart--wide">' in heat and '<div class="chart chart--phone">' in heat and heat.count("<svg") == 2 and "<svg" in heat and "var(--k-lead)" in heat and "Signals found per week, by kind" in heat
    for label in ("Leadership changes", "Hiring", "Press and media", "Company events and technology"):
        assert label in heat, label
    assert "<details><summary>Show the numbers</summary>" in heat and "<th>Week ending</th>" in heat
    assert heat.count("<tr><td>") == 12 and "The last 6 weeks; all 12 are in the numbers below." in heat      # the phone drawing says what it leaves out


def test_the_hiring_section_is_at_the_bottom_of_the_same_page(seeded):
    e, ids = seeded
    body = e.get("/opportunities").body
    hiring = body.split('<h2 id="hiring">Hiring</h2>')[1]
    assert "Newest postings" in hiring and "No postings recorded yet" in hiring and 'href="/opportunities#hiring"' in hiring
    assert "Open job postings" in hiring and 'id="hiring"' not in body.split('<h2 id="hiring">')[0]


def test_everything_found_this_week_is_one_collapsed_list_that_links_to_the_full_page(seeded):
    e, ids = seeded
    body = e.get("/opportunities").body
    assert '<details class="card"><summary>Signals dated in the last 7 days (' in body and 'href="/signals"' in body
    assert "Trackco" in body.split("Signals dated in the last 7 days")[1]
    assert "<h2>What changed</h2>" not in body and "The market this week" not in body and "Look deeper" not in body       # the old sections are gone


def test_with_nobody_at_watch_the_page_says_so_and_lists_the_closest(app_env):  # noqa: F811
    e = app_env
    for i in range(2):
        a = add_account(e.conn, f"Lowco {i}")
        add_signal(e.conn, e.cfg, a, "leadership_change", 5 + i, as_of=date.today())
    score_accounts(e.conn, e.cfg, date.today())
    body = e.get("/opportunities").body
    assert "Nobody has reached Watch yet." in body and "The companies closest are in the list below." in body
    assert "Nobody has enough signals to be ranked yet; these have the most." in body and "Lowco 0" in body.split('id="ranked"')[1]


def test_the_signals_page_filters_by_kind_and_window(seeded):
    e, ids = seeded
    all_ = e.get("/signals").body
    assert "<h1>Signals</h1>" in all_ and "Hotco &lt;b&gt;" in all_ and "Trackco" in all_ and 'href="/signals?group=hiring&amp;days=30"' in all_
    hiring = e.get("/signals?group=hiring").body
    assert "Hiring cluster" in hiring and "Leadership change" not in hiring.split("<ul")[1] and 'class="on"' in hiring
    assert "Nothing found in the last 7 days" not in e.get("/signals?days=7").body
    junk = e.get("/signals?group=zzz&days=999").body                          # unknown values fall back to the defaults
    assert junk.count("Leadership change") >= 1 and 'days=30" class="on"' in junk and 'class="on">All' in junk
    page = e.get("/opportunities").body
    assert 'href="/signals"' in page and 'href="/signals"' not in page.split("</nav>")[0]      # reached from Analysis, not the nav


def test_the_signals_page_says_so_when_nothing_was_found(app_env):  # noqa: F811
    assert "Nothing found in the last 30 days" in app_env.get("/signals").body


def test_analysis_lists_tracked_companies_with_what_is_new_and_hides_the_section_when_nothing_is_tracked(seeded, app_env):  # noqa: F811
    e, ids = seeded
    body = e.get("/opportunities").body
    assert "Companies you track" in body and "Trackco" in body.split("Companies you track")[1]
    set_watching(e.conn, ids["Trackco"], False)
    assert "Companies you track" not in e.get("/opportunities").body


def test_the_empty_state_tells_an_analysis_reader_what_to_do(app_env):  # noqa: F811
    body = app_env.get("/opportunities").body
    assert "Nothing has been scored yet" in body and "Admin area" in body and "Nothing is ranked yet." in body
    assert "Is it heating up?" in body and 'id="hiring"' in body                                  # the rest of the page still renders


def test_the_help_page_uses_the_real_thresholds_and_names_the_jobs(seeded):
    e, ids = seeded
    body = e.get("/help").body
    b = e.cfg.scoring.bands
    assert f"<b>Hot</b> is {b.hot} or more" in body and f"<b>Warm</b> {b.warm} or more" in body and f"<b>Watch</b> {b.watch} or more" in body
    assert "See who ranks first" in body and 'href="/opportunities"' in body and "This week" not in body
    assert "does not write outreach" in body and "LinkedIn" in body
