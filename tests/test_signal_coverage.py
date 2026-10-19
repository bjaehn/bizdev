from datetime import date, timedelta

from bd.signal_coverage import signal_coverage
from tests.conftest import AS_OF, add_account, add_signal, make_db
from tests.test_update import app_env  # noqa: F401  (fixture)


def world(conn, cfg):
    scanned = add_account(conn, "Scanned", last_scanned=(AS_OF - timedelta(days=3)).isoformat())
    add_signal(conn, cfg, scanned, "leadership_change", 5)
    add_signal(conn, cfg, scanned, "hiring_cluster", 8)
    add_account(conn, "Waiting", chicagoland="unknown", revenue=None, ownership=None)              # unverified and never scanned
    add_account(conn, "Waiting too")
    add_account(conn, "Gone", status="excluded")
    return scanned


def test_it_counts_what_was_scanned_what_is_waiting_and_how_many_updates_that_takes(cfg, tmp_path):
    conn = make_db(tmp_path)
    world(conn, cfg)
    c = signal_coverage(conn, cfg, AS_OF, {"scan": 1})
    assert (c.on_list, c.out_of_scope, c.scanned, c.unscanned, c.unverified, c.with_signals) == (3, 1, 1, 2, 1, 1)
    assert c.updates_needed == 2 and c.last_scanned == (AS_OF - timedelta(days=3)).isoformat()
    assert dict(c.by_group)["Leadership changes"] == 1 and dict(c.by_group)["Hiring"] == 1 and dict(c.by_group)["Press and media"] == 0
    assert signal_coverage(conn, cfg, AS_OF, {"scan": 25}).updates_needed == 1
    assert signal_coverage(conn, cfg, AS_OF, {}).updates_needed == 0                                    # no per-update number: no estimate, no division


def test_an_empty_list_has_no_coverage_and_nothing_to_divide(cfg, tmp_path):
    c = signal_coverage(make_db(tmp_path), cfg, AS_OF, {"scan": 25})
    assert (c.on_list, c.scanned, c.unscanned, c.updates_needed, c.last_scanned) == (0, 0, 0, 0, None)


def test_signals_older_than_the_window_do_not_count(cfg, tmp_path):
    conn = make_db(tmp_path)
    a = add_account(conn, "Oldnews")
    add_signal(conn, cfg, a, "leadership_change", 200)
    assert signal_coverage(conn, cfg, AS_OF, {"scan": 25}).with_signals == 0


def test_the_admin_home_explains_a_thin_list_by_what_was_scanned_not_by_missing_facts(app_env):  # noqa: F811
    e = app_env
    world(e.conn, e.cfg)
    e.conn.execute("UPDATE accounts SET last_scanned = ? WHERE name = 'Scanned'", (date.today().isoformat(),))
    e.conn.commit()
    body = e.get("/").body
    assert "Signal coverage" in body and "<b>1</b> of <b>3</b> companies on the scan list has been scanned" in body
    assert "<b>2</b> have not been scanned yet" in body and "never-scanned companies go first" in body
    assert "1 company has an unconfirmed location or size; it is scanned anyway." in body and "1 known out of scope" in body
    assert "Why the list is thin" not in body and "waiting for facts" not in body and "Leadership changes 1" in body and "Press and media 0" in body
