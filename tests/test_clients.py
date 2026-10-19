from datetime import date

import pytest

from bd.db import connect, init_db
from bd.opportunities import build_opportunities, filter_opportunities
from bd.pipeline import (MAX_CLIENT_NAMES, all_states, client_ids, client_rows, get_state, mark_clients, set_client, set_stage)
from bd.score_store import score_accounts
from tests.conftest import AS_OF, add_account, add_signal, make_db
from tests.test_update import app_env  # noqa: F401  (fixture)


@pytest.fixture
def conn(tmp_path):
    return make_db(tmp_path)


def audit(conn, aid):
    return [tuple(r) for r in conn.execute("SELECT actor, action, field, old, new FROM audit_log WHERE account_id = ? AND action = 'client' ORDER BY id", (aid,))]


def test_an_older_database_gets_the_client_column(tmp_path):
    c = connect(tmp_path / "old.sqlite")
    init_db(c)
    c.execute("DROP TABLE lead_state")
    c.execute("CREATE TABLE lead_state (account_id INTEGER PRIMARY KEY, stage TEXT NOT NULL DEFAULT 'new', watching INTEGER NOT NULL DEFAULT 0, updated TEXT NOT NULL)")
    c.commit()
    init_db(c)
    assert "client" in {r[1] for r in c.execute("PRAGMA table_info(lead_state)")}
    init_db(c)                                                                     # and it is idempotent


def test_marking_a_client_is_audited_and_survives_stage_changes(conn):
    a = add_account(conn, "Acme")
    assert not get_state(conn, a).client and client_ids(conn) == set()
    assert set_client(conn, a, True) and get_state(conn, a).client and client_ids(conn) == {a}
    assert set_client(conn, a, True) and audit(conn, a) == [("ui", "client", "client", "0", "1")]       # no second row for no change
    set_stage(conn, a, "pursuing")
    assert all_states(conn)[a].client and all_states(conn)[a].stage == "pursuing"
    assert set_client(conn, a, False) and not get_state(conn, a).client and audit(conn, a)[-1][-2:] == ("1", "0")
    assert not set_client(conn, 999, True)


def test_a_pasted_list_matches_only_unique_names_and_never_creates_accounts(conn):
    cme = add_account(conn, "CME Group")
    wtfc = add_account(conn, "Wintrust Financial")
    add_account(conn, "Twin Industries Inc")
    add_account(conn, "Twin Industries Corp")
    before = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    r = mark_clients(conn, "CME Group Inc.\n\n  wintrust   financial corporation \nTwin Industries\nNobody Co\nCME GROUP\n")
    assert sorted(r.marked) == ["CME Group", "Wintrust Financial"] and r.unmatched == ["Nobody Co"] and r.ambiguous == ["Twin Industries"]
    assert client_ids(conn) == {cme, wtfc} and conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == before
    r = mark_clients(conn, "CME Group")
    assert r.already == ["CME Group"] and r.marked == []
    r = mark_clients(conn, "\n".join(f"Company {i}" for i in range(MAX_CLIENT_NAMES + 5)))
    assert r.too_many and len(r.unmatched) == MAX_CLIENT_NAMES
    assert mark_clients(conn, "Inc.\n,").unmatched == ["Inc.", ","]                  # a name that is only legal words matches nothing


def test_opportunities_carry_a_client_flag_and_scores_do_not_change(conn, cfg):
    a, b = add_account(conn, "Aco"), add_account(conn, "Bco")
    for x in (a, b):
        add_signal(conn, cfg, x, "leadership_change", 5)
        add_signal(conn, cfg, x, "hiring_cluster", 5)
    score_accounts(conn, cfg, AS_OF)
    before = {o.account.name: o.result.score for o in build_opportunities(conn, cfg, AS_OF).items}
    set_client(conn, b, True)
    items = build_opportunities(conn, cfg, AS_OF).items
    assert {o.account.name: o.result.score for o in items} == before                    # a client is scored like any other company
    assert [o.account.name for o in filter_opportunities(items, client="yes")] == ["Bco"]
    assert [o.account.name for o in filter_opportunities(items, client="no")] == ["Aco"]
    assert len(filter_opportunities(items)) == 2 and len(filter_opportunities(items, client="whatever")) == 2


def test_client_rows_lead_with_the_ones_that_have_news(conn, cfg):
    quiet, busy = add_account(conn, "Quietco"), add_account(conn, "Busyco")
    add_signal(conn, cfg, busy, "restructuring_layoffs", 10)
    for x in (quiet, busy):
        set_client(conn, x, True)
    rows = client_rows(conn, AS_OF)
    assert [r.name for r in rows] == ["Busyco", "Quietco"] and rows[0].signals_90d == 1 and rows[0].latest_signal["type"] == "restructuring_layoffs"
    assert rows[1].latest_signal is None and rows[1].score is None


def test_pages_and_forms(app_env):  # noqa: F811
    e = app_env
    a = add_account(e.conn, "Acme")
    add_signal(e.conn, e.cfg, a, "leadership_change", 5, as_of=date.today())
    add_signal(e.conn, e.cfg, a, "hiring_cluster", 5, as_of=date.today())
    score_accounts(e.conn, e.cfg, date.today())
    assert "No clients marked yet" in e.get("/clients").body and "Mark as an existing client" in e.get(f"/accounts/{a}").body
    assert e.post(f"/accounts/{a}/client", {"on": "1"}).code == 303 and get_state(e.conn, a).client
    assert "Not a client" in e.get(f"/accounts/{a}").body
    body = e.get("/clients").body
    assert "Acme" in body and "2 signals in 90 days" in body and "Hiring cluster" in body
    opp = e.get("/opportunities?view=matrix").body
    assert '<span class="badge good">client</span>' in opp and "client" in e.get("/opportunities").body
    assert "Acme" in e.get("/opportunities?client=yes").body and "No companies match" in e.get("/opportunities?client=no").body
    r = e.post("/clients/mark", {"names": "Acme Inc\nNew Target <b>\n"})
    assert r.code == 200 and "Already marked: Acme" in r.body and "/lookup?q=New%20Target%20%3Cb%3E" in r.body and "&lt;b&gt;" in r.body
    assert e.post("/clients/mark", {"names": "  "}).code == 303
    assert e.post("/clients/mark", {"names": "Acme"}, csrf=False).code == 403
    assert e.post(f"/accounts/{a}/client", {"on": "0"}).code == 303 and not get_state(e.conn, a).client
    assert e.post("/accounts/999/client", {"on": "1"}).code == 303
