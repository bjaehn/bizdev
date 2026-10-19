from datetime import date, timedelta

import pytest

from bd.leadership import ACT_COUNT, MIN_RISE, build_leadership
from bd.pipeline import set_client, set_stage
from bd.score_store import score_accounts
from tests.conftest import AS_OF, add_account, add_signal, make_db
from tests.test_update import app_env  # noqa: F401  (fixture)

PREV = (AS_OF - timedelta(days=7)).isoformat()


@pytest.fixture
def conn(tmp_path):
    return make_db(tmp_path)


def put_score(conn, account_id, score, band, run_date=PREV):
    conn.execute("INSERT INTO scores(account_id, run_date, score, band, components_json, rationale) VALUES (?,?,?,?,'{}','x')",
                 (account_id, run_date, score, band))
    conn.commit()


def seeded(conn, cfg, when=AS_OF, beta_before=(40, "Watch")):
    """Acme (new Hot, client), Beta (risen), Gamma (client with news), Deltaco (waiting for facts), Quietco (no news)."""
    acme = add_account(conn, "Acme", industry="Healthcare")
    beta = add_account(conn, "Beta", industry="Healthcare")
    gamma = add_account(conn, "Gamma", industry="Insurance")
    add_account(conn, "Deltaco", status="hold_review", revenue=None, ownership=None, chicagoland="unknown")
    add_account(conn, "Quietco", industry="Insurance")
    for x, types in ((acme, ("leadership_change", "hiring_cluster", "restructuring_layoffs")), (beta, ("leadership_change", "hiring_cluster")),
                     (gamma, ("ma_divestiture",))):
        for t in types:
            add_signal(conn, cfg, x, t, 4, as_of=when)
    set_client(conn, acme, True)
    set_client(conn, gamma, True)
    put_score(conn, acme, 20, "Stored")           # below Watch last time: now a new lead
    put_score(conn, beta, *beta_before)           # already at Watch last time: a rise, not a new lead
    score_accounts(conn, cfg, when)
    return acme, beta, gamma


def test_an_empty_database_says_what_to_do_and_still_reports_coverage(conn, cfg):
    v = build_leadership(conn, cfg, AS_OF)
    assert v.problem and "Update my list" in v.problem and v.act == [] and v.coverage.total == 0 and v.coverage.scored == 0
    assert v.pulse.lines == [] and v.clients == []


def test_coverage_counts_what_is_actually_there(conn, cfg):
    seeded(conn, cfg)
    c = build_leadership(conn, cfg, AS_OF).coverage
    assert (c.total, c.ready, c.waiting) == (5, 4, 1) and c.with_signal_90d == 3 and c.clients == 2
    assert c.scored >= 2 and c.hot + c.warm >= 1 and c.score_date == AS_OF.isoformat() and c.score_age_days == 0


def test_act_on_this_week_is_the_ranked_top_with_the_client_flag(conn, cfg):
    acme, beta, gamma = seeded(conn, cfg)
    v = build_leadership(conn, cfg, AS_OF)
    assert [o.rank for o in v.act] == list(range(1, len(v.act) + 1)) and len(v.act) <= ACT_COUNT
    by = {o.account.name: o for o in v.act}
    assert by["Acme"].client and not by["Beta"].client and by["Acme"].why_now and by["Acme"].signals
    assert v.act[0].result.score >= v.act[-1].result.score


def test_what_changed_separates_new_leads_risers_and_new_signals(conn, cfg):
    acme, beta, gamma = seeded(conn, cfg)
    v = build_leadership(conn, cfg, AS_OF)
    ch = v.changes
    names_new = {o.account.name for o in ch.new_leads}
    assert "Acme" in names_new and all(o.result.band in ("Hot", "Warm") for o in ch.new_leads)
    assert [o.account.name for o in ch.risers] == ["Beta"] and ch.risers[0].delta >= MIN_RISE and "Beta" not in names_new
    assert ch.prev_date == PREV
    assert ch.new_signal_total == 6 and len(ch.new_signals) == 6 and {s.name for s in ch.new_signals} == {"Acme", "Beta", "Gamma"}
    assert ch.new_signals[0].url.startswith("https://") and ch.new_signals == sorted(ch.new_signals, key=lambda s: s.event_date, reverse=True)


def test_without_an_earlier_ranking_nothing_is_called_new(conn, cfg):
    acme = add_account(conn, "Acme")
    for t in ("leadership_change", "hiring_cluster"):
        add_signal(conn, cfg, acme, t, 4)
    score_accounts(conn, cfg, AS_OF)
    ch = build_leadership(conn, cfg, AS_OF).changes
    assert ch.prev_date is None and ch.new_leads == [] and ch.risers == []


def test_market_pulse_sentences_come_from_counts(conn, cfg):
    seeded(conn, cfg)
    p = build_leadership(conn, cfg, AS_OF).pulse
    assert any(l.startswith("Healthcare: ") and "signals in the last 30 days" in l for l in p.lines)
    assert all(l.endswith(".") for l in p.lines) and p.families and all(n > 0 for _, n in p.families)
    assert p.quiet_total >= 1


def test_clients_with_news_are_listed_and_quiet_ones_are_not(conn, cfg):
    acme, beta, gamma = seeded(conn, cfg)
    quiet = add_account(conn, "Quietclient")
    set_client(conn, quiet, True)
    v = build_leadership(conn, cfg, AS_OF)
    assert {k.name for k in v.clients} == {"Acme", "Gamma"} and v.clients[0].signals_90d >= v.clients[-1].signals_90d
    assert v.coverage.clients == 3


def test_stages_feed_the_pursuit_count(conn, cfg):
    acme, *_ = seeded(conn, cfg)
    set_stage(conn, acme, "pursuing")
    assert build_leadership(conn, cfg, AS_OF).coverage.stages["pursuing"] == 1


# ---- the page ---------------------------------------------------------------------------------------------------------------------
def test_the_page_renders_empty_and_in_the_navigation(app_env):  # noqa: F811
    e = app_env
    r = e.get("/leadership")
    assert r.code == 200 and "Nothing has been scored yet" in r.body and 'href="/leadership">Leadership</a>' in r.body
    assert "0</b><span>companies on the list" in r.body.replace("\n", "")


def test_the_page_shows_what_leadership_needs_and_escapes_names(app_env):  # noqa: F811
    e = app_env
    today = date.today()
    acme = add_account(e.conn, "Acme <script>alert(1)</script>", industry="Healthcare")
    beta = add_account(e.conn, "Beta", industry="Healthcare")
    for x in (acme, beta):
        add_signal(e.conn, e.cfg, x, "leadership_change", 4, as_of=today, url=f"https://www.sec.gov/Archives/{x}/a.htm")
        add_signal(e.conn, e.cfg, x, "hiring_cluster", 4, as_of=today)
    set_client(e.conn, acme, True)
    add_account(e.conn, "Waitco", status="hold_review", revenue=None, ownership=None, chicagoland="unknown")
    score_accounts(e.conn, e.cfg, today)
    body = e.get("/leadership").body
    assert "Act on this week" in body and "&lt;script&gt;" in body and "<script>alert" not in body
    assert "existing client" in body and "new business" in body and "one-page summary" in body and "source: sec.gov" in body
    assert "Market pulse" in body and "Healthcare:" in body and "What changed" in body
    assert "1 company is still waiting for facts" in body and "Existing clients: where there may be more work" in body
    assert 'name="q"' in body and 'action="/lookup"' in body
    old = (today - timedelta(days=20)).isoformat()
    e.conn.execute("UPDATE scores SET run_date = ?", (old,))
    e.conn.commit()
    assert "20 days old" in e.get("/leadership").body
