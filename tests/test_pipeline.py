from datetime import date, timedelta

import pytest

from bd.pipeline import (OPEN_STAGES, add_note, all_states, get_state, notes_for, set_stage, set_watching, stage_counts,
                         watchlist_changes)
from bd.opportunities import build_opportunities, filter_opportunities
from bd.score_store import score_accounts
from tests.conftest import AS_OF, add_account, add_signal, make_db
from tests.test_update import app_env  # noqa: F401  (fixture)


@pytest.fixture
def conn(tmp_path):
    return make_db(tmp_path)


def test_stage_changes_are_recorded_with_the_score_and_audited(conn, cfg):
    a = add_account(conn, "Acme")
    add_signal(conn, cfg, a, "leadership_change", 5)
    add_signal(conn, cfg, a, "hiring_cluster", 5)
    score_accounts(conn, cfg, AS_OF)
    assert get_state(conn, a).stage == "new" and not get_state(conn, a).watching
    assert set_stage(conn, a, "pursuing", "CIO is new") is True
    assert get_state(conn, a).stage == "pursuing"
    assert set_stage(conn, a, "pursuing") is False                      # no change, no note
    n = notes_for(conn, a)[0]
    assert (n["kind"], n["stage"], n["body"]) == ("stage", "pursuing", "CIO is new") and n["score_at"] is not None
    a_row = conn.execute("SELECT action, field, old, new FROM audit_log WHERE account_id = ? AND action = 'stage'", (a,)).fetchone()
    assert tuple(a_row) == ("stage", "stage", "new", "pursuing")
    assert stage_counts(conn)["pursuing"] == 1


def test_bad_input_is_refused(conn):
    a = add_account(conn, "Acme")
    assert not set_stage(conn, a, "bogus") and not set_stage(conn, 999, "won")
    assert not add_note(conn, a, "   ") and not add_note(conn, 999, "hi") and not set_watching(conn, 999, True)
    assert add_note(conn, a, "x" * 5000) and len(notes_for(conn, a)[0]["body"]) == 2000


def test_watching_is_kept_when_the_stage_changes(conn):
    a = add_account(conn, "Acme")
    set_watching(conn, a, True)
    set_stage(conn, a, "reviewing")
    assert all_states(conn)[a].watching and all_states(conn)[a].stage == "reviewing"
    set_watching(conn, a, False)
    assert not get_state(conn, a).watching


def test_watchlist_lists_new_signals_and_quiet_accounts(conn, cfg):
    busy, quiet = add_account(conn, "Busyco"), add_account(conn, "Quietco")
    add_signal(conn, cfg, busy, "leadership_change", 2)
    for x in (busy, quiet):
        set_watching(conn, x, True)
    ch = {w.name: w for w in watchlist_changes(conn, date.today() - timedelta(days=30))}
    assert len(ch["Busyco"].new_signals) == 1 and ch["Quietco"].new_signals == []
    assert [w.name for w in watchlist_changes(conn, date.today() - timedelta(days=30))][0] == "Busyco"   # busiest first
    assert all(w.new_signals == [] for w in watchlist_changes(conn, date.today() + timedelta(days=1)))
    not_watched = add_account(conn, "Other")
    assert "Other" not in {w.name for w in watchlist_changes(conn, date.today() - timedelta(days=30))}
    assert not_watched


def test_opportunities_carry_and_filter_by_stage(conn, cfg):
    a, b = add_account(conn, "Aco"), add_account(conn, "Bco")
    for x in (a, b):
        add_signal(conn, cfg, x, "leadership_change", 5)
        add_signal(conn, cfg, x, "hiring_cluster", 5)
    score_accounts(conn, cfg, AS_OF)
    set_stage(conn, b, "passed")
    set_watching(conn, a, True)
    items = build_opportunities(conn, cfg, AS_OF).items
    by = {o.account.name: o for o in items}
    assert by["Bco"].stage == "passed" and by["Aco"].stage == "new" and by["Aco"].watching
    assert [o.account.name for o in filter_opportunities(items, stage="open")] == ["Aco"]
    assert [o.account.name for o in filter_opportunities(items, stage="passed")] == ["Bco"]
    assert len(filter_opportunities(items)) == 2 and "passed" not in OPEN_STAGES


def test_pages_and_forms(app_env):  # noqa: F811
    e = app_env
    a = add_account(e.conn, "Acme")
    page = e.get(f"/accounts/{a}").body
    assert "Your pipeline" in page and "Watch this company" in page
    r = e.post(f"/accounts/{a}/stage", {"stage": "pursuing", "note": "Met their CFO"})
    assert r.code == 303 and r.h["Location"] == f"/accounts/{a}"
    assert "Met their CFO" in e.get(f"/accounts/{a}").body and "moved to" in e.get(f"/accounts/{a}").body
    e.post(f"/accounts/{a}/note", {"body": "Follow up Friday <b>"})
    assert "Follow up Friday &lt;b&gt;" in e.get(f"/accounts/{a}").body                   # escaped
    assert "not watching any company" in e.get("/watchlist").body
    e.post(f"/accounts/{a}/watch", {"on": "1"})
    assert "Acme" in e.get("/watchlist").body and "Stop watching" in e.get(f"/accounts/{a}").body
    assert e.post(f"/accounts/{a}/stage", {"stage": "bogus"}).code == 303      # refused politely
    assert get_state(e.conn, a).stage == "pursuing"
    assert e.post(f"/accounts/{a}/stage", {"stage": "won"}, csrf=False).code == 403
    r = e.post(f"/accounts/{a}/note", {"body": "x", "back": "//evil.example"})
    assert r.h["Location"] == f"/accounts/{a}"                                           # no open redirect
