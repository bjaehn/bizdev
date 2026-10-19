import re
from datetime import date, timedelta

import pytest

from bd.pipeline import set_watching
from bd.score_store import score_accounts
from tests.conftest import add_account, add_signal
from tests.test_admin import ADMIN_GROUPS, call, PW  # noqa: F401
from tests.test_update import app_env  # noqa: F401  (fixture)

ANALYSIS = ["/week", "/signals", "/help", "/opportunities", "/opportunities?view=chart", "/reports", "/market", "/hiring", "/firms", "/rfps", "/ask", "/monday"]


@pytest.fixture
def seeded(app_env):  # noqa: F811
    e = app_env
    today = date.today()
    a = add_account(e.conn, "Styleco <i>", industry="Healthcare", hq_city="Chicago")
    for t, age in (("leadership_change", 4), ("hiring_cluster", 8), ("restructuring_layoffs", 15)):
        add_signal(e.conn, e.cfg, a, t, age, as_of=today)
    set_watching(e.conn, a, True)
    score_accounts(e.conn, e.cfg, today)
    return e, a


def band(body):
    """What sits between the header and <main>: the page's header band."""
    return body.split("</header>")[1].split("<main")[0]


@pytest.mark.parametrize("path", ANALYSIS)
def test_every_analysis_page_has_its_title_in_the_header_band(seeded, path):
    e, a = seeded
    r = e.get(path)
    assert r.code == 200
    assert '<div class="hero' in band(r.body) and band(r.body).count("<h1") == 1 and r.body.count("<h1") == 1, path


def test_the_company_brief_puts_name_facts_and_badges_in_the_band(seeded):
    e, a = seeded
    b = band(e.get(f"/companies/{a}").body)
    assert "<h1>Styleco &lt;i&gt;</h1>" in b and "3 kinds of signs agree" in b and "Hot · 100" in b and "Plain-text version" in b
    assert "Healthcare · Chicago" not in b                                                      # company facts are background, not the headline
    assert "Styleco <i>" not in b


def test_admin_pages_have_no_header_band_and_keep_their_own_shell(seeded):
    e, a = seeded
    for path in ["/admin", "/accounts", "/lookup", "/health", "/settings"]:
        body = e.get(path).body
        assert 'class="hero' not in body and '<header class="admin">' in body and "<h1" in body.split("<main")[1], path


def test_the_login_page_is_a_centred_card_without_a_band(app_env):  # noqa: F811
    body = call(app_env.app, "GET", "/login").body
    assert 'class="card card--narrow"' in body and "<h1" not in band(body) and "Sign in" in body


def test_this_week_shows_three_numbers_that_match_the_page(seeded):
    e, a = seeded
    body = e.get("/week").body
    assert "<b>1</b><span>company worth a call</span>" in body                                    # the three signals made one company Hot
    assert "<b>3</b><span>new signals found this week</span>" in body
    assert "<span>signals in the last 4 weeks (the 4 weeks before: 0)</span>" in body and "&#9650;" not in body
    assert 'class="tile hot"' in body and 'class="tile good"' in body and 'class="tile accent"' in body
    assert len(re.findall(r'class="tile[ "]', body)) == 3


def test_a_call_card_shows_the_change_since_the_last_run_only_when_there_was_an_earlier_run(seeded):
    e, a = seeded
    assert "since last run" not in e.get("/week").body
    prev = (date.today() - timedelta(days=7)).isoformat()
    e.conn.execute("INSERT INTO scores(account_id, run_date, score, band, components_json, rationale) VALUES (?,?,?,?,'{}','x')", (a, prev, 10, "Stored"))
    e.conn.commit()
    assert "&#9650; +90 since last run" in e.get("/week").body or "▲ +90 since last run" in e.get("/week").body


def test_the_stylesheet_has_light_dark_phone_and_print_rules_and_no_script(seeded):
    e, a = seeded
    css = e.get("/week").body.split("<style>")[1].split("</style>")[0]
    assert "prefers-color-scheme:dark" in css and "max-width:640px" in css and "@media print" in css
    assert "<script" not in e.get("/week").body and 'class="auth"' not in e.get("/week").body
