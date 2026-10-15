import json

import pytest

from bd.score_store import score_accounts
from tests.conftest import AS_OF, add_account, add_signal, make_db


@pytest.fixture
def conn(tmp_path):
    c = make_db(tmp_path)
    yield c
    c.close()


def test_scores_only_active_accounts_and_persists_components(cfg, conn):
    a = add_account(conn, "Hotco")
    held = add_account(conn, "Heldco", status="hold_review")
    for t, age in (("leadership_change", 21), ("hiring_cluster", 10), ("pe_addon", 45)):
        add_signal(conn, cfg, a, t, age)
    add_signal(conn, cfg, held, "leadership_change", 5)
    out = score_accounts(conn, cfg, AS_OF)
    assert [s.account.name for s in out] == ["Hotco"]          # held account is never scored
    row = conn.execute("SELECT * FROM scores").fetchone()
    assert (row["account_id"], row["run_date"], row["score"], row["band"]) == (a, "2026-10-15", 91, "Hot")
    comp = json.loads(row["components_json"])
    refs = {s["ref"] for fam in comp["families"].values() for s in fam["signals"]}
    ids = {r["id"] for r in conn.execute("SELECT id FROM signals WHERE account_id=?", (a,))}
    assert refs == ids                                          # every score component traces to a stored signal
    assert row["posted"] == 0 and row["cooldown_until"] is None
    assert "stacking bonus 20" in row["rationale"]


def test_account_without_signals_scores_zero_stored(cfg, conn):
    add_account(conn, "Quietco")
    score_accounts(conn, cfg, AS_OF)
    row = conn.execute("SELECT score, band, rationale FROM scores").fetchone()
    assert (row["score"], row["band"], row["rationale"]) == (0, "Stored", "No scoreable signals.")


def test_low_confidence_signal_is_stored_but_adds_nothing(cfg, conn):
    a = add_account(conn, "Rumorco")
    add_signal(conn, cfg, a, "leadership_change", 5, conf="Low")
    score_accounts(conn, cfg, AS_OF)
    assert conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 1
    assert conn.execute("SELECT score FROM scores").fetchone()[0] == 0


def test_rescoring_same_day_updates_but_keeps_posted_and_cooldown(cfg, conn):
    a = add_account(conn, "Hotco")
    add_signal(conn, cfg, a, "leadership_change", 5)
    score_accounts(conn, cfg, AS_OF)
    conn.execute("UPDATE scores SET posted=1, cooldown_until='2026-11-12'")
    conn.commit()
    add_signal(conn, cfg, a, "restructuring_layoffs", 5)
    score_accounts(conn, cfg, AS_OF)
    row = conn.execute("SELECT * FROM scores").fetchone()
    assert row["score"] == 64 and row["posted"] == 1 and row["cooldown_until"] == "2026-11-12"
    assert conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 1


def test_different_run_dates_keep_history(cfg, conn):
    from datetime import timedelta
    a = add_account(conn, "Hotco")
    add_signal(conn, cfg, a, "leadership_change", 5)
    score_accounts(conn, cfg, AS_OF)
    score_accounts(conn, cfg, AS_OF + timedelta(days=7))
    rows = conn.execute("SELECT run_date, score FROM scores ORDER BY run_date").fetchall()
    assert [(r["run_date"], r["score"]) for r in rows] == [("2026-10-15", 30), ("2026-10-22", 30)]


def test_same_event_from_two_domains_counts_once_keeping_the_best_source(cfg, conn):
    a = add_account(conn, "Hotco")
    add_signal(conn, cfg, a, "leadership_change", 5, conf="Medium", url="https://news.example/cio", source_type="secondary_report")
    add_signal(conn, cfg, a, "leadership_change", 5, conf="High", url="https://hotco.com/cio")
    out = score_accounts(conn, cfg, AS_OF)[0]
    assert out.result.score == 30                                   # one signal, not 30 + 15
    refs = [s.ref for s in out.result.signals]
    high_id = conn.execute("SELECT id FROM signals WHERE confidence='High'").fetchone()[0]
    assert refs == [high_id]
    assert conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 2   # both stay stored


def test_dedupe_tie_keeps_first_stored_and_other_dates_or_types_do_not_collapse(cfg, conn):
    a = add_account(conn, "Hotco")
    add_signal(conn, cfg, a, "leadership_change", 5, url="https://a.example/1")
    add_signal(conn, cfg, a, "leadership_change", 5, url="https://b.example/1")      # same event, tie -> first
    add_signal(conn, cfg, a, "leadership_change", 6, url="https://c.example/1")      # different date: separate event
    add_signal(conn, cfg, a, "open_exec_search", 5, url="https://d.example/1")       # different type: separate event
    out = score_accounts(conn, cfg, AS_OF)[0]
    assert len(out.result.signals) == 3
    first = conn.execute("SELECT id FROM signals WHERE source_url='https://a.example/1'").fetchone()[0]
    assert first in [s.ref for s in out.result.signals]
