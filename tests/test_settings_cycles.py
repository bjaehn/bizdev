"""Admin values: the scan cycles are editable, a release raises a saved number once and says so, and the notices are shown."""
import json
from datetime import date

import pytest

from bd.settings_store import (CYCLE_FIELDS, RELEASE_CHANGES, apply_overrides, apply_release_changes, cycle_values, load_cycle_overrides, load_update,
                               recent_release_changes, reset_cycles, save_cycles, save_update)
from tests.conftest import add_account, make_db
from tests.test_update import app_env  # noqa: F401  (fixture)


@pytest.fixture
def conn(tmp_path):
    return make_db(tmp_path)


def put_update(conn, **vals):
    conn.execute("INSERT INTO settings(key, value_json, updated) VALUES ('update', ?, 'x') ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json", (json.dumps(vals),))
    conn.commit()


# ---- scan cycles ------------------------------------------------------------------------------------------------------------------------------
def test_cycles_show_the_default_until_one_is_saved_and_a_saved_value_reaches_every_run(conn, cfg):
    v = cycle_values(cfg, conn)
    assert v["deep_every_weeks"] == (4, 4, False) and v["ats_find_every_weeks"] == (8, 8, False)
    assert save_cycles(conn, {"deep_every_weeks": "2", "ats_find_every_weeks": "0", "ats_find_per_run": "40", "enrich_retry_every_weeks": "4"}) == []
    assert load_cycle_overrides(conn) == {"deep_every_weeks": 2, "ats_find_every_weeks": 0, "ats_find_per_run": 40, "enrich_retry_every_weeks": 4}
    eff = apply_overrides(cfg, conn)
    assert (eff.scan.deep_every_weeks, eff.hiring.ats_find_every_weeks, eff.hiring.ats_find_per_run) == (2, 0, 40)
    assert cfg.scan.deep_every_weeks == 4 and cfg.hiring.ats_find_per_run == 25                          # the base config is not mutated
    assert cycle_values(cfg, conn)["deep_every_weeks"] == (2, 4, True)
    reset_cycles(conn)
    assert apply_overrides(cfg, conn).scan.deep_every_weeks == 4 and cycle_values(cfg, conn)["deep_every_weeks"] == (4, 4, False)
    assert [r["new"] for r in conn.execute("SELECT new FROM audit_log WHERE field = 'settings.cycles' ORDER BY id")] == [json.dumps(load_cycle_overrides(conn) or {"deep_every_weeks": 2, "ats_find_every_weeks": 0, "ats_find_per_run": 40, "enrich_retry_every_weeks": 4}), None]


def test_a_cycle_outside_its_range_is_refused_and_a_bad_saved_value_is_ignored(conn, cfg):
    assert save_cycles(conn, {"deep_every_weeks": "27", "ats_find_every_weeks": "8", "ats_find_per_run": "0", "enrich_retry_every_weeks": "4"})                 # two errors
    assert save_cycles(conn, {"deep_every_weeks": "x", "ats_find_every_weeks": "8", "ats_find_per_run": "5", "enrich_retry_every_weeks": "4"})
    assert load_cycle_overrides(conn) == {}
    conn.execute("INSERT INTO settings(key, value_json, updated) VALUES ('cycles', ?, 'x')", (json.dumps({"deep_every_weeks": 999, "bogus": 1, "ats_find_per_run": True}),))
    assert load_cycle_overrides(conn) == {} and apply_overrides(cfg, conn).scan.deep_every_weeks == 4


# ---- a release raises a saved number once, audited, and never touches one you set -------------------------------------------------------------
def test_a_saved_research_number_below_the_new_floor_is_raised_once_and_audited_as_a_release(conn):
    put_update(conn, research=10, segments=10, scan=10, budget=8, rfps=1)
    assert apply_release_changes(conn) == [("research", 10, 25)]
    assert load_update(conn) == {"research": 25, "segments": 10, "scan": 10, "budget": 8, "rfps": 1}          # only that number moved
    rows = conn.execute("SELECT actor, old, new FROM audit_log WHERE field = 'settings.update'").fetchall()
    assert [r["actor"] for r in rows] == ["release"] and json.loads(rows[0]["old"])["research"] == 10 and json.loads(rows[0]["new"])["research"] == 25
    assert apply_release_changes(conn) == []                                                                 # idempotent
    save_update(conn, {"research": "10", "segments": "10", "scan": "10", "budget": "8", "rfps": "1"})        # you lower it again: it stays lowered
    assert apply_release_changes(conn) == [] and load_update(conn)["research"] == 10


def test_no_warning_when_the_scan_number_already_covers_the_cycle(app_env):  # noqa: F811
    import html, re
    e = app_env
    for i in range(30):
        add_account(e.conn, f"Co{i}")
    put_update(e.conn, research=25, segments=2, scan=10, budget=8, rfps=0)
    t = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", e.get("/settings").body)))
    assert "takes at least 3 updates" in t and "only if each is scanned that often" not in t                   # 10 a week is enough for 30 companies in 4 weeks


def test_a_number_you_set_higher_or_a_default_is_left_alone(conn):
    put_update(conn, research=40, segments=2, scan=25, budget=8, rfps=0)
    assert apply_release_changes(conn) == [] and load_update(conn)["research"] == 40
    assert RELEASE_CHANGES and all(c[1] == "research" for c in RELEASE_CHANGES)                                # the only value a release may raise


def test_with_nothing_saved_the_default_already_is_the_new_value_and_nothing_is_written(tmp_path):
    c = make_db(tmp_path)
    assert load_update(c)["research"] == 25 and apply_release_changes(c) == []
    assert c.execute("SELECT COUNT(*) FROM audit_log WHERE actor = 'release'").fetchone()[0] == 0


def test_recent_release_changes_name_the_number_in_words(conn):
    put_update(conn, research=10, segments=10, scan=10, budget=8, rfps=0)
    apply_release_changes(conn)
    got = recent_release_changes(conn)
    assert len(got) == 1 and got[0]["label"] == "Companies to research per update" and (got[0]["old"], got[0]["new"]) == (10, 25) and got[0]["when"] == date.today().isoformat()


# ---- the pages -------------------------------------------------------------------------------------------------------------------------------
def test_settings_page_shows_the_cycles_the_release_change_and_how_long_a_pass_takes(app_env):  # noqa: F811
    import html, re
    e = app_env
    for i in range(100):
        add_account(e.conn, f"Co{i}")
    put_update(e.conn, research=10, segments=10, scan=10, budget=8, rfps=0)
    apply_release_changes(e.conn)
    put_update(e.conn, research=25, segments=10, scan=10, budget=8, rfps=0)
    page = lambda: html.unescape(re.sub(r"\s+([:.,])", r"\1", re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", e.get("/settings").body))))      # noqa: E731
    t = page()
    assert "Scan cycles" in t and "config.yaml default 4" in t and "Look for a company's own job board" in t
    assert "Changed by a release" in t and "Companies to research per update: 10 → 25" in t
    assert "one full pass over the 100 companies on the list takes at least 10 updates" in t                # 100 companies at 10 per update
    assert "gives every company a look every 4 weeks only if each is scanned that often, which needs about 25 companies per update" in t     # 100 companies in 4 weeks
    assert e.post("/settings/cycles", {"deep_every_weeks": "2", "ats_find_every_weeks": "4", "ats_find_per_run": "30", "enrich_retry_every_weeks": "4"}).code == 303
    t = page()
    assert "saved here" in t and "Reset to config.yaml defaults" in t
    assert e.post("/settings/cycles", {"deep_every_weeks": "99", "ats_find_every_weeks": "4", "ats_find_per_run": "30", "enrich_retry_every_weeks": "4"}).code == 303 and "Not saved" in e.get("/settings").body
    assert e.post("/settings/cycles/reset").code == 303 and load_cycle_overrides(e.conn) == {}


def test_admin_home_says_a_release_changed_a_setting_and_the_coverage_follows_the_saved_cycle(app_env):  # noqa: F811
    import re
    e = app_env
    add_account(e.conn, "Deepco")
    put_update(e.conn, research=10, segments=10, scan=10, budget=8, rfps=0)
    apply_release_changes(e.conn)
    body = e.get("/").body
    assert "Settings changed by a release" in body and "Companies to research per update" in body and 'href="/settings"' in body
    assert save_cycles(e.conn, {"deep_every_weeks": "0", "ats_find_every_weeks": "8", "ats_find_per_run": "25", "enrich_retry_every_weeks": "4"}) == []
    assert "scan.deep_every_weeks</code> is 0" in e.get("/").body                                              # the card follows the value saved in Admin
    e.conn.execute("DELETE FROM audit_log")
    e.conn.commit()
    assert "Settings changed by a release" not in e.get("/").body


def test_the_update_description_names_the_new_free_steps_and_the_deep_cycle(app_env):  # noqa: F811
    import re
    e = app_env
    t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", e.get("/").body))
    assert "private equity firm's own portfolio page (free)" in t.replace("&#39;", "'") and "looks on each company's own careers page" in t.replace("&#39;", "'")
    assert "no deep technology and procurement look in 4 weeks" in t
    save_cycles(e.conn, {"deep_every_weeks": "0", "ats_find_every_weeks": "8", "ats_find_per_run": "25", "enrich_retry_every_weeks": "4"})
    assert "no deep technology and procurement look" not in re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", e.get("/").body))
