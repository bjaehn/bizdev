from datetime import date

import pytest

from bd.score_store import score_accounts
from tests.conftest import add_account, add_signal
from tests.test_update import app_env  # noqa: F401  (fixture)


@pytest.fixture
def world(app_env):  # noqa: F811
    e = app_env
    today = date.today()
    ids = {
        "held": add_account(e.conn, "Heldco", status="hold_review", chicagoland="unknown", revenue=None, ownership=None),
        "excluded": add_account(e.conn, "Excludedco", status="excluded"),
        "quiet": add_account(e.conn, "Quietco", industry=None, revenue=None, ownership=None),
        "unscored": add_account(e.conn, "Unscoredco", industry="Energy"),
    }
    add_signal(e.conn, e.cfg, ids["unscored"], "leadership_change", 5, as_of=today)         # signals exist but no scoring run has seen them
    scored = add_account(e.conn, "Scoredco", industry="Energy")
    add_signal(e.conn, e.cfg, scored, "leadership_change", 5, as_of=today)
    score_accounts(e.conn, e.cfg, today)                                                     # scores only the active accounts that exist now
    ids["scored"] = scored
    return e, ids


@pytest.mark.parametrize("who", ["held", "excluded", "quiet", "unscored", "scored"])
def test_a_profile_opens_for_a_company_in_any_state_without_an_error(world, who):
    e, ids = world
    r = e.get(f"/companies/{ids[who]}")
    assert r.code == 200 and "<h1" in r.body and "Traceback" not in r.body
    assert "Manage (Admin)" in r.body


def test_a_company_with_nothing_to_explain_says_so_rather_than_drawing_an_empty_chart(world):
    e, ids = world
    for who in ("quiet", "held"):
        body = e.get(f"/companies/{ids[who]}").body
        assert "How the score is built" not in body and "<svg" not in body, who
        assert "not scored" in body and "No signals in the last 180 days" in body, who
    assert "needs facts" in e.get(f"/companies/{ids['held']}").body


def test_a_company_whose_signals_have_not_been_scored_yet_is_explained_from_its_signals(world):
    e, ids = world
    body = e.get(f"/companies/{ids['unscored']}").body
    assert "How the score is built" in body and "<svg" in body and "30 points in total = score 30" in body


def test_the_ranking_and_reports_open_when_the_only_companies_are_held_or_quiet(app_env):  # noqa: F811
    e = app_env
    add_account(e.conn, "Heldco", status="hold_review", chicagoland="unknown", revenue=None, ownership=None)
    add_account(e.conn, "Quietco", revenue=None)
    for path in ("/opportunities", "/opportunities?view=chart", "/opportunities?view=list", "/market", "/reports", "/companies/abc", "/companies/0"):
        assert e.get(path).code in (200, 404), path
