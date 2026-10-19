import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx2 as httpx
import pytest

from bd.accounts import list_accounts
from bd.ats import AtsClient, AtsError, RawPosting, board_from_url, classify, seniority_of
from bd.jobs import JobError, JobRunner, sequence_for, validate
from bd.postings import known_accessions, learn_board, store_postings
from bd.runner import run_scans
from bd.scanners import SCANNERS, AtsScanner, HiringScanner
from bd.scheduler import Scheduler, due, most_recent_daily_slot, refresh_due
from bd.settings_store import refresh_enabled, save_refresh
from tests.conftest import AS_OF, FakeLLM, FakeVerifier, account, add_account, make_db

CHI = ZoneInfo("America/Chicago")


def d(n):
    return AS_OF - timedelta(days=n)


# ---- boards and parsing ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("url,expect", [
    ("https://boards.greenhouse.io/acme/jobs/123", ("greenhouse", "acme")),
    ("https://job-boards.greenhouse.io/acme-corp", ("greenhouse", "acme-corp")),
    ("https://jobs.lever.co/acme/abc-123", ("lever", "acme")),
    ("https://jobs.ashbyhq.com/Acme", ("ashby", "Acme")),
    ("https://acme.wd1.myworkdayjobs.com/en/jobs", None), ("https://evil.com/boards.greenhouse.io/acme", None),
    ("https://boards.greenhouse.io/", None), ("https://jobs.lever.co/../x", None), ("not a url", None),
    ("https://jobs.lever.co/ac me", None)])
def test_board_from_url(url, expect):
    assert board_from_url(url) == expect


def client(cfg, body, status=200):
    seen = []

    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(status, json=body)
    return AtsClient(cfg, "UA", httpx.Client(transport=httpx.MockTransport(handler))), seen


def test_greenhouse_lever_ashby_parsing_skips_undated_and_unlisted(cfg):
    gh = {"jobs": [{"title": "Data Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/1", "first_published": "2026-09-01T10:00:00-05:00"},
                   {"title": "No Date", "absolute_url": "https://boards.greenhouse.io/acme/jobs/2", "updated_at": "2026-09-01T10:00:00Z"},
                   {"title": "", "absolute_url": "https://boards.greenhouse.io/acme/jobs/3", "first_published": "2026-09-01T10:00:00Z"}]}
    c, seen = client(cfg, gh)
    got = c.fetch("greenhouse", "acme")
    assert got == [RawPosting("Data Engineer", "https://boards.greenhouse.io/acme/jobs/1", date(2026, 9, 1))]
    assert seen == ["https://boards-api.greenhouse.io/v1/boards/acme/jobs"]
    lv = [{"text": "VP Strategy", "hostedUrl": "https://jobs.lever.co/acme/1", "createdAt": 1788000000000}, {"text": "X", "hostedUrl": "ftp://x", "createdAt": 1}]
    got = client(cfg, lv)[0].fetch("lever", "acme")
    assert len(got) == 1 and got[0].title == "VP Strategy" and got[0].posted.year == 2026
    ab = {"jobs": [{"title": "Head of Data", "jobUrl": "https://jobs.ashbyhq.com/acme/1", "publishedAt": "2026-09-02T00:00:00.000Z", "isListed": True},
                   {"title": "Hidden", "jobUrl": "https://jobs.ashbyhq.com/acme/2", "publishedAt": "2026-09-02T00:00:00Z", "isListed": False}]}
    assert [p.title for p in client(cfg, ab)[0].fetch("ashby", "acme")] == ["Head of Data"]


def test_ats_errors_are_errors_not_empty_boards(cfg):
    with pytest.raises(AtsError, match="HTTP 404"):
        client(cfg, {}, 404)[0].fetch("lever", "acme")
    with pytest.raises(AtsError, match="layout"):
        client(cfg, {"unexpected": 1})[0].fetch("greenhouse", "acme")
    with pytest.raises(AtsError, match="layout"):
        client(cfg, {"jobs": []})[0].fetch("lever", "acme")
    with pytest.raises(AtsError, match="bad provider"):
        client(cfg, {})[0].fetch("workday", "acme")
    with pytest.raises(AtsError, match="bad provider"):
        client(cfg, {})[0].fetch("lever", "../etc")


@pytest.mark.parametrize("title,expect", [
    ("Senior Data Engineer", ("data_analytics", "other", False)),
    ("VP, Data & Analytics", ("data_analytics", "svp_vp", False)),
    ("Director of Business Transformation", ("transformation_change", "director", False)),
    ("Interim Chief Operating Officer", ("strategy_operations", "c_suite", True)),
    ("Machine Learning Engineer", ("ai_technology", "other", False)),
    ("Head of AI", ("ai_technology", "director", False)),
    ("Product Manager, Platform", ("product_engineering", "other", False)),
    ("Software Engineer II", None), ("Accountant", None), ("Director of Sales", None), ("Training Specialist", None)])
def test_classify_titles_by_keyword(cfg, title, expect):
    assert classify(title, cfg) == expect


def test_whole_words_only_so_ai_does_not_match_inside_other_words(cfg):
    assert classify("Retail Associate", cfg) is None and classify("Maintenance Technician", cfg) is None
    assert seniority_of("Chief of Staff") == "c_suite" and seniority_of("Vice President, Ops") == "svp_vp"


# ---- ats scanner -------------------------------------------------------------------------------------------------------
class FakeAts:
    def __init__(self, postings=(), error=None):
        self.postings, self.error, self.calls = list(postings), error, []

    def fetch(self, provider, token):
        self.calls.append((provider, token))
        if self.error:
            raise self.error
        return self.postings


def acct(**kw):
    return account().model_copy(update={"ats_provider": "greenhouse", "ats_board": "acme", **kw})


def rp(title, age, n, host="boards.greenhouse.io"):
    return RawPosting(title, f"https://{host}/acme/jobs/{n}", d(age))


def test_ats_scanner_builds_cluster_leadership_and_keeps_every_relevant_posting(cfg):
    feed = [rp("Data Engineer", 40, 1), rp("Senior Data Engineer", 30, 2), rp("Data Scientist", 20, 3), rp("Data Analyst", 5, 4),
            rp("VP, Business Transformation", 3, 5), rp("Accountant", 2, 6)]
    out = AtsScanner(cfg, FakeAts(feed)).scan(acct(), AS_OF)
    assert out.error is None and out.usage.total_tokens == 0 and out.postings_complete
    types = sorted(s.extract.type for s in out.signals)
    assert types == ["hiring_cluster", "hiring_leadership"]
    cluster = next(s for s in out.signals if s.extract.type == "hiring_cluster").extract
    assert cluster.event_date == d(20) and cluster.source_type == "company_site" and cluster.confidence == "High"
    assert len(out.postings) == 5 and all(p.title != "Accountant" for p in out.postings)


def test_ats_scanner_rules_window_foreign_hosts_and_errors(cfg):
    feed = [rp("Data Engineer", 200, 1), rp("Data Scientist", 5, 2, host="evil.example"), rp("Data Analyst", 4, 3)]
    out = AtsScanner(cfg, FakeAts(feed)).scan(acct(), AS_OF)
    assert {r.reason for r in out.rejections} == {"foreign_host", "date_out_of_window"}
    assert out.signals == [] and len(out.postings) == 2          # one in-window junior posting is not a signal; both relevant ones are kept
    bad = AtsScanner(cfg, FakeAts(error=AtsError("HTTP 500"))).scan(acct(), AS_OF)
    assert bad.error == "AtsError: HTTP 500" and bad.signals == [] and not bad.postings_complete


def test_ats_and_hiring_scanners_are_mutually_exclusive_per_account(cfg):
    ats, hiring = AtsScanner(cfg, FakeAts()), HiringScanner(cfg, None, None)
    with_board, without = acct(), account()
    assert ats.applies_to(with_board) and not ats.applies_to(without)
    assert not hiring.applies_to(with_board) and hiring.applies_to(without)
    assert SCANNERS["ats"] is AtsScanner and SCANNERS["ats"].create(cfg, None, None, None, None, ats=FakeAts()).client is not None


def post(i, age, url=None, **kw):
    return {"title": f"Director of Data {i}", "url": url or f"https://boards.greenhouse.io/acme/jobs/{i}", "posted_date": d(age).isoformat(),
            "theme": "data_analytics", "seniority": "director", "interim": False, "source_type": "company_site", **kw}


def test_model_hiring_scanner_learns_the_board_and_returns_its_postings(cfg):
    p = [post(1, 5)]
    llm = FakeLLM([json.dumps({"postings": p})], urls=[p[0]["url"]])
    out = HiringScanner(cfg, llm, FakeVerifier()).scan(account(), AS_OF)
    assert out.learned_board == ("greenhouse", "acme") and len(out.postings) == 1 and not out.postings_complete


def test_leadership_posting_is_its_own_signal_and_director_single_is_kept(cfg):
    vp = [post(1, 5, seniority="svp_vp")]
    out = HiringScanner(cfg, FakeLLM([json.dumps({"postings": vp})], urls=[vp[0]["url"]]), FakeVerifier()).scan(account(), AS_OF)
    assert [s.extract.type for s in out.signals] == ["hiring_leadership"]
    dr = [post(2, 5)]
    out = HiringScanner(cfg, FakeLLM([json.dumps({"postings": dr})], urls=[dr[0]["url"]]), FakeVerifier()).scan(account(), AS_OF)
    assert [s.extract.type for s in out.signals] == ["hiring_single"]


# ---- storage and the runner --------------------------------------------------------------------------------------------
@pytest.fixture
def conn(tmp_path):
    return make_db(tmp_path)


def test_postings_upsert_close_and_never_resurrect_without_seeing_them_again(cfg, conn):
    a = add_account(conn, "Acme", ats_provider="greenhouse", ats_board="acme")
    from bd.scanners.hiring import Posting
    mk = lambda i: Posting(title=f"Data Role {i}", url=f"https://boards.greenhouse.io/acme/jobs/{i}", posted_date=d(5), theme="data_analytics",  # noqa: E731
                           seniority="other")
    with conn:
        assert store_postings(conn, a, [mk(1), mk(2)], d(1), "greenhouse", True) == (2, 0)
        assert store_postings(conn, a, [mk(2)], AS_OF, "greenhouse", True) == (0, 1)
    rows = {r["title"]: r["open"] for r in conn.execute("SELECT title, open FROM postings")}
    assert rows == {"Data Role 1": 0, "Data Role 2": 1}
    with conn:
        store_postings(conn, a, [mk(3)], AS_OF, "search", False)                 # a search result never closes others
    assert conn.execute("SELECT COUNT(*) FROM postings WHERE open = 1").fetchone()[0] == 2


def test_learn_board_never_overwrites(conn):
    a = add_account(conn, "Acme")
    with conn:
        assert learn_board(conn, a, ("greenhouse", "acme")) and not learn_board(conn, a, ("lever", "other"))
    acc = list_accounts(conn)[0]
    assert (acc.ats_provider, acc.ats_board) == ("greenhouse", "acme")
    assert conn.execute("SELECT new FROM audit_log WHERE field='ats_board'").fetchone()[0] == "greenhouse:acme"


def test_runner_stores_postings_closes_gone_ones_and_light_runs_do_not_mark_scanned(cfg, conn):
    add_account(conn, "Acme", ats_provider="greenhouse", ats_board="acme", domain="acme.com")
    sc = AtsScanner(cfg, FakeAts([rp("Data Engineer", 5, 1), rp("Data Analyst", 4, 2), rp("Data Scientist", 3, 3)]))
    run_scans(conn, cfg, [sc], list_accounts(conn), AS_OF, None, workers=1, touch_last_scanned=False)
    assert conn.execute("SELECT COUNT(*) FROM postings WHERE open = 1 AND source = 'greenhouse'").fetchone()[0] == 3
    assert conn.execute("SELECT last_scanned FROM accounts").fetchone()[0] is None
    assert conn.execute("SELECT COUNT(*) FROM signals WHERE type = 'hiring_cluster'").fetchone()[0] == 1
    sc2 = AtsScanner(cfg, FakeAts([rp("Data Engineer", 5, 1)]))
    run_scans(conn, cfg, [sc2], list_accounts(conn), AS_OF + timedelta(days=1), None, workers=1)
    assert conn.execute("SELECT COUNT(*) FROM postings WHERE open = 1").fetchone()[0] == 1
    assert conn.execute("SELECT last_scanned FROM accounts").fetchone()[0] == (AS_OF + timedelta(days=1)).isoformat()


def test_a_failed_board_fetch_closes_nothing(cfg, conn):
    add_account(conn, "Acme", ats_provider="greenhouse", ats_board="acme", domain="acme.com")
    run_scans(conn, cfg, [AtsScanner(cfg, FakeAts([rp("Data Engineer", 5, 1)]))], list_accounts(conn), AS_OF, None, workers=1)
    run_scans(conn, cfg, [AtsScanner(cfg, FakeAts(error=AtsError("HTTP 503")))], list_accounts(conn), AS_OF + timedelta(days=1), None, workers=1)
    assert conn.execute("SELECT COUNT(*) FROM postings WHERE open = 1").fetchone()[0] == 1


# ---- SEC filings are read once --------------------------------------------------------------------------------------
def test_filings_already_read_are_skipped_and_new_ones_remembered(cfg, conn):
    from bd.edgar import Filing
    from tests.test_earnings_press import FakeEdgar, sigs, GUIDANCE
    from bd.scanners import EarningsScanner
    f1 = Filing("0000001234", "0000000001-26-000002", date(2026, 9, 20), date(2026, 9, 19), "8-K", ("2.02",), "d2.htm")
    f2 = Filing("0000001234", "0000000001-26-000009", date(2026, 10, 1), date(2026, 10, 1), "8-K", ("2.02",), "d9.htm")
    a = add_account(conn, "Acme", cik="0000001234")
    llm = FakeLLM([sigs(GUIDANCE)])
    sc = EarningsScanner(cfg, llm, FakeEdgar([f1]))
    run_scans(conn, cfg, [sc], list_accounts(conn), AS_OF, None, workers=1)
    assert known_accessions(conn, a, "earnings") == {f1.accession} and len(llm.calls) == 1
    llm2 = FakeLLM([sigs()])
    run_scans(conn, cfg, [EarningsScanner(cfg, llm2, FakeEdgar([f1, f2]))], list_accounts(conn), AS_OF, None, workers=1, force=True)
    assert len(llm2.calls) == 1 and known_accessions(conn, a, "earnings") == {f1.accession, f2.accession}   # only the new filing cost a call


# ---- scheduler, jobs, settings -----------------------------------------------------------------------------------------
def at(y, m, dd, h, mi=0):
    return datetime(y, m, dd, h, mi, tzinfo=CHI)


def utc(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def test_refresh_runs_every_day_but_monday_with_catch_up_and_no_repeats():
    tue = at(2026, 10, 13, 5, 1)
    assert refresh_due(tue, None) is not None
    assert refresh_due(at(2026, 10, 13, 4, 59), None) is None                            # before 05:00 the latest slot is Monday's
    assert refresh_due(at(2026, 10, 12, 6), None) is None                                # Monday is the weekly run's
    assert refresh_due(at(2026, 10, 13, 5, 1) + timedelta(hours=13), None) is None        # too late
    assert refresh_due(tue + timedelta(hours=2), utc(tue)) is None                       # already ran today
    assert refresh_due(tue + timedelta(hours=2), utc(at(2026, 10, 12, 5, 5))) is not None   # only Monday's weekly ran
    assert most_recent_daily_slot(at(2026, 3, 8, 6)).hour == 5                           # DST day


def test_all_seven_days_covered_exactly_once_per_week():
    kinds = {}
    for day in range(12, 19):                                            # Mon Oct 12 .. Sun Oct 18, 2026
        now = at(2026, 10, day, 5, 30)
        kinds[day] = "weekly" if due(now, None) else "refresh" if refresh_due(now, None) else None
    assert kinds[12] == "weekly" and all(kinds[x] == "refresh" for x in range(13, 19))


@pytest.fixture
def runner(cfg, tmp_path):
    make_db(tmp_path).close()
    return JobRunner(str(tmp_path / "t.sqlite"), tmp_path, ["true"])


def test_scheduler_submits_refresh_on_non_mondays_and_honors_the_setting(runner):
    tue = at(2026, 10, 13, 5, 2).astimezone(timezone.utc)
    s = Scheduler(runner, clock=lambda: tue)
    with runner._conn() as c:
        save_refresh(c, False)
        assert not refresh_enabled(c)
    assert s.check() is None
    with runner._conn() as c:
        save_refresh(c, True)
    jid = s.check()
    assert jid is not None and runner.get(jid).kind == "refresh"
    runner.wait()
    assert s.check() is None                                             # already done today


def test_refresh_jobs(runner, monkeypatch):
    monkeypatch.setenv("BD_USER_AGENT", "SEI test@example.com")
    assert sequence_for("refresh", [], "2026-10-13") == [["scan", "--scanners", "ats,edgar,earnings,financials,sec_events", "--force", "--light", "--as-of", "2026-10-13"],
                                                         ["score", "--as-of", "2026-10-13"]]
    monkeypatch.delenv("BD_USER_AGENT")
    assert sequence_for("refresh_now", [], "2026-10-13")[0][2] == "ats"
    with pytest.raises(JobError):
        validate("refresh", {})                                          # scheduler only
    assert validate("refresh_now", {}) == []
    with pytest.raises(JobError, match="confirm"):
        runner.submit("refresh_now", {})


def test_hiring_page_account_postings_and_settings_toggle(cfg, tmp_path):
    from tests.test_hosted import PW, call
    from bd.ui.app import App
    from bd.ui.auth import Hosted
    c = make_db(tmp_path)
    a = add_account(c, "Acme", ats_provider="greenhouse", ats_board="acme")
    today = date.today()
    for i, (title, sen) in enumerate([("VP, Data", "svp_vp"), ("Data Engineer", "other")]):
        c.execute("INSERT INTO postings(account_id,url,title,posted_date,theme,seniority,interim,source,first_seen,last_seen,open) "
                  "VALUES (?,?,?,?,?,?,0,'greenhouse',?,?,1)",
                  (a, f"https://boards.greenhouse.io/acme/jobs/{i}", title, (today - timedelta(days=3)).isoformat(), "data_analytics", sen,
                   today.isoformat(), today.isoformat()))
    c.commit()
    db = str(tmp_path / "t.sqlite")
    app = App(cfg, db, root=tmp_path, hosted=Hosted(PW), jobs=JobRunner(db, tmp_path, ["true"]))
    app.fail_delay = 0
    ck = call(app, "POST", "/login", {"password": PW}).h["Set-Cookie"].split(";")[0].split("=", 1)[1]
    page = call(app, "GET", "/hiring", cookie=ck).body
    assert "Acme" in page and "VP, Data" in page and "greenhouse board, daily" in page
    assert "Acme" in call(app, "GET", "/hiring?theme=data_analytics", cookie=ck).body
    assert "No postings recorded yet" in call(app, "GET", "/hiring?theme=ai_technology", cookie=ck).body
    acc = call(app, "GET", f"/accounts/{a}", cookie=ck).body
    assert "Open job postings (2)" in acc and "leadership" in acc and "own greenhouse job board" in acc
    assert "Run the light refresh daily" in call(app, "GET", "/settings", cookie=ck).body
    assert call(app, "POST", "/settings/refresh", {"csrf": app._csrf}, cookie=ck).code == 303          # box unticked = off
    with app.jobs._conn() as cc:
        assert not refresh_enabled(cc)
    assert call(app, "POST", "/settings/refresh", {}, cookie=ck).code == 403
    assert "Light refresh now" in call(app, "GET", "/operate", cookie=ck).body
