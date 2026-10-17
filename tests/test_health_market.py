from datetime import timedelta

import pytest

from bd.health import build_health
from bd.market import UNKNOWN_INDUSTRY, build_market
from bd.pe_store import sync_from_accounts
from bd.score_store import score_accounts
from tests.conftest import AS_OF, add_account, add_signal, make_db
from tests.test_update import app_env  # noqa: F401  (fixture)


@pytest.fixture
def conn(tmp_path):
    return make_db(tmp_path)


def run_row(conn, step, account_id, signals=0, tokens=1000, cost=0.25, errors=None, day=AS_OF):
    conn.execute("INSERT INTO runs(run_date,step,account_id,accounts_scanned,signals_found,tokens,est_cost,errors) VALUES (?,?,?,?,?,?,?,?)",
                 (day.isoformat(), step, account_id, 1, signals, tokens, cost, errors))
    conn.commit()


# ---- data health ------------------------------------------------------------------------------------------------------
def test_scanner_verdicts_and_costs(conn, cfg):
    a, b = add_account(conn, "Acme"), add_account(conn, "Beta")
    for acc in (a, b):
        run_row(conn, "scan:leadership", acc, signals=1, cost=0.30)
        run_row(conn, "scan:hiring", acc, signals=0, cost=0.20, errors="rejected: unreachable=2")
        run_row(conn, "scan:press", acc, signals=0, cost=0.10, errors="LLMError: api down")
    run_row(conn, "scan:total", None, signals=2, cost=1.2)                       # the whole-run row must not be counted
    h = build_health(conn, cfg, AS_OF)
    y = {s.name: s for s in h.scanners}
    assert (y["leadership"].verdict, y["leadership"].signals, y["leadership"].accounts) == ("producing", 2, 2)
    assert round(y["leadership"].cost_per_signal, 2) == 0.30
    assert y["hiring"].verdict == "ran, found nothing" and y["hiring"].problems == 0          # a rejection is not a failure
    assert y["press"].verdict == "failing" and y["press"].notes == ["LLMError: api down"]
    assert y["warn"].verdict == "never ran" and y["financials"].verdict == "never ran"       # scanners that never ran are listed
    assert round(h.cost_total, 2) == 1.20 and h.accounts_scanned == 2 and round(h.cost_per_scanned_account, 2) == 0.60


def test_unpriced_calls_are_counted_not_zeroed(conn, cfg):
    a = add_account(conn, "Acme")
    run_row(conn, "scan:leadership", a, cost=None)
    h = build_health(conn, cfg, AS_OF)
    assert {s.name: s for s in h.scanners}["leadership"].unpriced == 1 and h.steps[0][3] == 1


def test_window_excludes_old_runs(conn, cfg):
    a = add_account(conn, "Acme")
    run_row(conn, "scan:leadership", a, signals=3, day=AS_OF - timedelta(days=60))
    assert {s.name: s for s in build_health(conn, cfg, AS_OF, days=30).scanners}["leadership"].calls == 0
    assert {s.name: s for s in build_health(conn, cfg, AS_OF, days=90).scanners}["leadership"].calls == 1


def test_coverage_hold_reasons_and_freshness(conn, cfg):
    add_account(conn, "Ready", last_scanned=(AS_OF - timedelta(days=3)).isoformat())
    add_account(conn, "Never")
    add_account(conn, "Old", last_scanned=(AS_OF - timedelta(days=90)).isoformat(), cik="0000001234")
    add_account(conn, "Held1", status="hold_review", chicagoland="unknown", revenue=None, ownership=None)
    add_account(conn, "Held2", status="hold_review", chicagoland="yes", revenue=None, employees=None)
    add_account(conn, "Gone", status="excluded", chicagoland="no")
    add_account(conn, "Auto", origin="discovered", reviewed=0)
    run_row(conn, "enrich", None)
    run_row(conn, "rfps", None)
    h = build_health(conn, cfg, AS_OF)
    assert h.by_status == {"active": 4, "hold_review": 2, "excluded": 1} and h.accounts_total == 7 and h.unreviewed == 1
    assert h.scan_age == {"never": 2, "week": 1, "month": 0, "older": 1}
    reasons = dict(h.hold_reasons)
    assert reasons["Chicagoland status unverified"] == 1 and any("size unverified" in r for r in reasons)
    assert h.with_cik == 1 and h.last_enrich == AS_OF.isoformat() and h.last_rfps == AS_OF.isoformat() and h.last_scan is None


def test_signal_mix_in_the_last_90_days(conn, cfg):
    a = add_account(conn, "Acme")
    add_signal(conn, cfg, a, "leadership_change", 5)
    add_signal(conn, cfg, a, "hiring_cluster", 6, conf="Medium", source_type="secondary_report")
    h = build_health(conn, cfg, AS_OF)
    assert h.signals_90d == 2 and h.high_share == 0.5
    assert dict(h.by_family) == {"leadership": 1, "hiring": 1} and dict(h.by_source) == {"company_release": 1, "secondary_report": 1}


# ---- market view ------------------------------------------------------------------------------------------------------
def test_segments_trend_and_bands(conn, cfg):
    a = add_account(conn, "Medco", industry="Health care")
    b = add_account(conn, "Medtwo", industry="Health care")
    add_account(conn, "Noindustry")
    c = add_account(conn, "Steelco", industry="Manufacturing")
    for age in (3, 10):                                       # 2 in the last 30 days
        add_signal(conn, cfg, a, "leadership_change", age)
    add_signal(conn, cfg, a, "hiring_cluster", 40)           # 1 in the 30 days before
    add_signal(conn, cfg, c, "restructuring_layoffs", 45)
    score_accounts(conn, cfg, AS_OF)
    m = build_market(conn, AS_OF)
    seg = {s.industry: s for s in m.segments}
    h = seg["Health care"]
    assert (h.companies, h.with_signal_90d, h.signals_30d, h.signals_prior_30d, h.trend) == (2, 1, 2, 1, "up")
    assert seg["Manufacturing"].trend == "down" and seg[UNKNOWN_INDUSTRY].companies == 1
    assert h.avg_score is not None and h.avg_score > 0 and b
    assert m.segments[0].industry == "Health care"            # the segment with the most scored companies comes first


def test_monthly_family_counts_and_months(conn, cfg):
    a = add_account(conn, "Acme")
    add_signal(conn, cfg, a, "leadership_change", 3)
    add_signal(conn, cfg, a, "leadership_change", 4, url="https://x.example/2")
    add_signal(conn, cfg, a, "hiring_cluster", 40)
    m = build_market(conn, AS_OF, months=3)
    assert m.months == ["2026-08", "2026-09", "2026-10"]
    assert m.family_by_month["leadership"] == [0, 0, 2] and sum(m.family_by_month["hiring"]) == 1
    assert list(m.family_by_month)[0] == "leadership"         # busiest first


def test_sponsors_count_current_links_only(conn, cfg):
    a = add_account(conn, "Ownedco", ownership="pe-backed", sponsor="Example Capital")
    add_signal(conn, cfg, a, "leadership_change", 5)
    add_account(conn, "Other")
    sync_from_accounts(conn)
    m = build_market(conn, AS_OF)
    assert [(s.name, s.companies, s.signals_90d) for s in m.sponsors] == [("Example Capital", 1, 1)]


def test_quiet_companies_are_the_large_ones_with_no_recent_signal(conn, cfg):
    big = add_account(conn, "Bigco", revenue=9e9, industry="Energy")
    add_account(conn, "Smallco", revenue=3e8)
    loud = add_account(conn, "Loudco", revenue=8e9)
    add_account(conn, "Held", status="hold_review", revenue=9.9e9)       # held companies are not part of the market view
    add_signal(conn, cfg, loud, "leadership_change", 20)
    old = add_account(conn, "Oldnews", revenue=7e9)
    add_signal(conn, cfg, old, "leadership_change", 300)
    m = build_market(conn, AS_OF)
    assert [q.name for q in m.quiet] == ["Bigco", "Oldnews", "Smallco"] and m.quiet_total == 3
    assert m.quiet[1].signals_total == 1 and big
    assert len(build_market(conn, AS_OF, quiet_limit=1).quiet) == 1 and build_market(conn, AS_OF, quiet_limit=1).quiet_total == 3


def test_empty_database_renders(conn, cfg):
    m = build_market(conn, AS_OF)
    assert (m.segments, m.sponsors, m.quiet, m.family_by_month) == ([], [], [], {})
    h = build_health(conn, cfg, AS_OF)
    assert h.accounts_total == 0 and h.cost_total == 0 and h.cost_per_scanned_account is None and h.high_share is None


def test_pages_render_with_and_without_data(app_env):  # noqa: F811
    e = app_env
    for path in ("/health", "/market"):
        assert e.get(path).code == 200
    assert "No signals found in the last 90 days" in e.get("/health").body and "No companies are ready to scan yet" in e.get("/market").body
    from datetime import date
    today = date.today()
    a = add_account(e.conn, "Acme", industry="Health care", revenue=2e9)
    add_signal(e.conn, e.cfg, a, "leadership_change", 3, as_of=today)
    run_row(e.conn, "scan:leadership", a, signals=1, day=today)
    run_row(e.conn, "scan:press", a, signals=0, errors="LLMError: api down", day=today)
    h = e.get("/health").body
    assert "producing" in h and "failing" in h and "LLMError: api down" in h and "never ran" in h and "Why companies are held" not in h
    assert e.get("/health?days=7").code == 200 and e.get("/health?days=abc").code == 200
    m = e.get("/market").body
    assert "Health care" in m and "0 ready-to-scan companies have no signal" in m       # Acme has a recent signal, so it is not quiet
    assert "Data health" in m and "Market view" in m
