import pytest

from bd.readiness import waiting_for_facts
from tests.conftest import add_account, make_db
from tests.test_update import app_env  # noqa: F401  (fixture)


@pytest.fixture
def conn(tmp_path):
    return make_db(tmp_path)


def test_nothing_waiting_means_no_diagnosis(conn, cfg):
    add_account(conn, "Readyco")
    assert waiting_for_facts(conn, cfg, {"research": 25, "budget": 8}) is None


def test_the_fit_gate_reasons_are_counted_and_split_into_free_and_research(conn, cfg):
    for i in range(3):                                         # public-looking, no CIK: the free SEC step will try them
        add_account(conn, f"Publico {i}", status="hold_review", chicagoland="unknown", revenue=None, ownership=None)
    for i in range(30):                                        # private: the SEC cannot answer, so they need research
        add_account(conn, f"Privateco {i}", status="hold_review", chicagoland="yes", revenue=None, ownership="private")
    w = waiting_for_facts(conn, cfg, {"research": 25, "budget": 8})
    assert w.count == 33 and w.sec_can_try == 3 and w.needs_research == 30
    assert dict(w.reasons)["Chicagoland status unverified"] == 3 and dict(w.reasons)["size unverified (revenue/employees/ownership)"] == 33
    assert w.reasons[0][1] >= w.reasons[-1][1]                  # most common first
    assert w.presses == 2 and w.per_press == 25 and w.spend_cap == 8


def test_with_research_switched_off_there_is_no_press_estimate(conn, cfg):
    add_account(conn, "Privateco", status="hold_review", chicagoland="yes", revenue=None, ownership="private")
    w = waiting_for_facts(conn, cfg, {"research": 0, "budget": 8})
    assert w.needs_research == 1 and w.presses is None


def test_a_company_the_sec_can_fill_needs_no_press(conn, cfg):
    add_account(conn, "Publico", status="hold_review", chicagoland="unknown", revenue=None, ownership=None)
    w = waiting_for_facts(conn, cfg, {"research": 25, "budget": 8})
    assert w.needs_research == 0 and w.presses == 0 and w.sec_can_try == 1


def test_the_admin_home_explains_why_the_list_is_thin_in_plain_words(app_env):  # noqa: F811
    e = app_env
    assert "Why the list is thin" not in e.get("/").body
    for i in range(30):
        add_account(e.conn, f"Privateco {i}", status="hold_review", chicagoland="yes", revenue=None, ownership="private")
    add_account(e.conn, "Publico", status="hold_review", chicagoland="unknown", revenue=None, ownership=None)
    body = e.get("/").body
    assert "Why the list is thin" in body and "31</b> companies are waiting for facts" in body
    assert "31 &times; size unverified" in body and "1 &times; Chicagoland status unverified" in body
    assert "may be public companies" in body and "30</b> need research" in body and "about <b>2</b> presses, each spending at most $8" in body
