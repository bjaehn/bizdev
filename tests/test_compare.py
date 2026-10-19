import json
from datetime import timedelta

import pytest

from bd.accounts import list_accounts
from bd.compare import REPLACED_STEPS, pick_accounts, prior_cost, report, run_compare, stored_signals
from bd.jobs import COMPARE_BUDGET, KINDS, PAID_STEPS, JobError, is_paid, sequence_for, validate
from bd.llm import Usage
from bd.scanners import SweepScanner
from tests.conftest import AS_OF, FakeLLM, FakeVerifier, add_account, add_signal, make_db

MODEL = "claude-sonnet-5-5"


def found(url, type_="leadership_change", date="2026-10-01", summary="Acme names a new chief operating officer."):
    return {"type": type_, "event_date": date, "summary": summary, "source_url": url, "source_type": "company_release",
            "confidence": "High", "service_lines": ["Strategy & Operations"], "role_or_person": None}


def sweep(cfg, replies, urls):
    return SweepScanner(cfg, FakeLLM([json.dumps({"signals": r}) for r in replies], urls=urls, usage=Usage(input_tokens=2000, output_tokens=300, web_searches=3)),
                        FakeVerifier())


@pytest.fixture
def db(tmp_path, cfg):
    conn = make_db(tmp_path)
    acme = add_account(conn, "Acme Corp")
    zed = add_account(conn, "Zed Co")
    quiet = add_account(conn, "Quiet Inc")
    add_signal(conn, cfg, acme, "leadership_change", 10, url="https://acme.com/coo")
    add_signal(conn, cfg, acme, "vendor_deal", 20, url="https://acme.com/erp")
    add_signal(conn, cfg, acme, "hiring_cluster", 5, url="https://acme.com/jobs")       # not a kind the sweep covers: left out of the baseline
    add_signal(conn, cfg, zed, "restructuring_layoffs", 15, url="https://zed.com/layoffs")
    return conn, acme, zed, quiet


def test_the_baseline_is_what_the_replaced_scanners_stored_in_the_window_for_the_kinds_the_sweep_covers(db, cfg):
    conn, acme, zed, quiet = db
    since = (AS_OF - timedelta(days=cfg.scan.window_days)).isoformat()
    assert [(s.type, s.url) for s in stored_signals(conn, acme, since)] == [("leadership_change", "https://acme.com/coo"), ("vendor_deal", "https://acme.com/erp")]
    assert stored_signals(conn, quiet, since) == [] and stored_signals(conn, acme, "2026-10-14") == []
    assert set(REPLACED_STEPS) == {"scan:leadership", "scan:press", "scan:events", "scan:tech_procurement"}


def test_it_picks_the_companies_with_the_most_stored_signals_and_none_without(db, cfg):
    conn, acme, zed, quiet = db
    accounts = list_accounts(conn)
    assert [a.name for a in pick_accounts(conn, accounts, AS_OF, cfg.scan.window_days, 4)] == ["Acme Corp", "Zed Co"]
    assert [a.name for a in pick_accounts(conn, accounts, AS_OF, cfg.scan.window_days, 1)] == ["Acme Corp"]


def test_the_past_cost_is_the_replaced_scanners_on_the_latest_day_they_ran(db):
    conn, acme, zed, quiet = db
    assert prior_cost(conn, acme) is None
    for day, step, cost in (("2026-10-01", "scan:leadership", 9.0), ("2026-10-08", "scan:leadership", 0.30), ("2026-10-08", "scan:press", 0.20),
                            ("2026-10-08", "scan:hiring", 5.0), ("2026-10-08", "scan:events", None)):
        conn.execute("INSERT INTO runs(run_date,step,account_id,est_cost) VALUES (?,?,?,?)", (day, step, acme, cost))
    assert prior_cost(conn, acme) == pytest.approx(0.50)         # hiring is not replaced; an unpriced call is not counted as zero


def test_found_signals_are_matched_to_stored_ones_by_page_or_by_type_and_date_and_the_rest_are_new_or_missed(db, cfg):
    conn, acme, zed, quiet = db
    sc = sweep(cfg, [[found("https://acme.com/coo"),                                                                             # same page
                      found("https://news.example.com/other", "vendor_deal", "2026-09-25", "Signs an ERP deal."),                # same type and date as stored
                      found("https://acme.com/new", "ai_commitment", "2026-10-05", "Announces an AI program.")]],               # not stored
               urls=["https://acme.com/coo", "https://news.example.com/other", "https://acme.com/new"])
    [row] = run_compare(conn, cfg, sc, [list_accounts(conn)[0]], AS_OF, MODEL)
    assert (len(row.stored), len(row.found), row.matched, row.new, row.missed) == (2, 3, 2, 1, [])
    assert row.searches == 3 and row.cost is not None and row.cost > 0 and row.error is None


def test_a_stored_signal_the_sweep_does_not_find_is_reported_missed(db, cfg):
    conn, acme, zed, quiet = db
    sc = sweep(cfg, [[found("https://acme.com/coo")]], urls=["https://acme.com/coo"])
    [row] = run_compare(conn, cfg, sc, [list_accounts(conn)[0]], AS_OF, MODEL)
    assert [m.url for m in row.missed] == ["https://acme.com/erp"] and row.matched == 1 and row.new == 0


def test_nothing_is_written_to_the_database(db, cfg):
    conn, acme, zed, quiet = db
    before = [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("signals", "runs", "accounts")]
    last = conn.execute("SELECT last_scanned FROM accounts WHERE id = ?", (acme,)).fetchone()[0]
    sc = sweep(cfg, [[found("https://acme.com/brand-new", "ai_commitment", "2026-10-05")]], urls=["https://acme.com/brand-new"])
    run_compare(conn, cfg, sc, [list_accounts(conn)[0]], AS_OF, MODEL)
    assert [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("signals", "runs", "accounts")] == before
    assert conn.execute("SELECT last_scanned FROM accounts WHERE id = ?", (acme,)).fetchone()[0] == last


def test_the_spending_limit_stops_the_test_before_the_next_company(db, cfg):
    conn, acme, zed, quiet = db
    sc = sweep(cfg, [[], []], urls=[])
    rows = run_compare(conn, cfg, sc, list_accounts(conn)[:2], AS_OF, MODEL, max_cost=0.0001)
    assert [r.skipped for r in rows] == [False, True] and len(sc.llm.calls) == 1            # the first call always goes through; the second never starts


def test_a_failed_sweep_is_reported_and_does_not_stop_the_others(db, cfg):
    conn, acme, zed, quiet = db
    sc = sweep(cfg, [[found("https://acme.com/coo")]], urls=["https://acme.com/coo"])
    sc.llm.replies.insert(0, "not json at all")
    sc.llm.replies.insert(0, "still not json")
    sc.llm.replies.insert(0, "nor this")
    rows = run_compare(conn, cfg, sc, list_accounts(conn)[:1], AS_OF, MODEL)
    assert rows[0].error and "json" in rows[0].error.lower()
    text = "\n".join(report(rows, MODEL))
    assert "the sweep failed" in text and "nothing to compare" in text


def test_the_report_reads_in_plain_words_with_totals_cost_and_the_new_and_missed_sources(db, cfg):
    conn, acme, zed, quiet = db
    conn.execute("INSERT INTO runs(run_date,step,account_id,est_cost) VALUES ('2026-10-08','scan:leadership',?,5.00)", (acme,))
    sc = sweep(cfg, [[found("https://acme.com/coo"), found("https://acme.com/new", "ai_commitment", "2026-10-05", "Announces an AI program.")]],
               urls=["https://acme.com/coo", "https://acme.com/new"])
    rows = run_compare(conn, cfg, sc, [list_accounts(conn)[0]], AS_OF, MODEL)
    text = "\n".join(report(rows, "claude-haiku-5-5"))
    assert "Combined sweep on claude-haiku-5-5, nothing stored" in text
    assert "Acme Corp: stored 2 · sweep found 2 (1 the same, 1 not stored before) · missed 1" in text
    assert "new: 2026-10-05 ai_commitment: Announces an AI program. <https://acme.com/new>" in text
    assert "missed: 2026-09-25 vendor_deal <https://acme.com/erp>" in text
    assert "sweep $0.0" in text and "against $5.00 for the separate scanners" in text and "found fewer signals than were stored" not in text
    assert "Together: stored 2, sweep found 2" in text and "found at least as many signals as were stored and cost less." in text and "Stored counts build up over several runs" in text


# ---- the Admin job -------------------------------------------------------------------------------------------------------------------
def test_the_test_is_a_capped_paid_job_with_a_count_and_an_optional_exact_company():
    assert validate("compare", {"limit": "4"}) == ["compare", "--count", "4", "--budget", str(COMPARE_BUDGET)]
    assert validate("compare", {"limit": "2", "only": "Acme Corp"}) == ["compare", "--count", "2", "--budget", str(COMPARE_BUDGET), "--only", "Acme Corp"]
    for bad in ({}, {"limit": "0"}, {"limit": "9"}, {"limit": "x"}, {"limit": "3", "only": "--all"}, {"limit": "3", "only": "a;b|c"}):
        with pytest.raises(JobError):
            validate("compare", bad)
    assert KINDS["compare"][1] is True and KINDS["compare"][2] is False and "compare" in PAID_STEPS and COMPARE_BUDGET == 3


def test_the_test_runs_one_paid_step_and_does_not_score_because_nothing_was_stored():
    steps = sequence_for("compare", ["compare", "--count", "4", "--budget", "3"], "2026-10-19")
    assert steps == [["compare", "--count", "4", "--as-of", "2026-10-19"]] and is_paid(steps[0])
    one = sequence_for("compare", ["compare", "--count", "2", "--budget", "3", "--only", "Acme Corp"], "2026-10-19")
    assert one == [["compare", "--count", "2", "--only", "Acme Corp", "--as-of", "2026-10-19"]]
