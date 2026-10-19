import io
from datetime import date

import pytest

from bd.accounts import list_accounts
from bd.jobs import FREE_SCANNERS, KINDS, JobError, is_paid, sequence_for, validate
from bd.score_store import score_accounts
from tests.conftest import add_account, add_signal
from tests.test_update import app_env  # noqa: F401  (fixture)

AS_OF = "2026-10-19"


# ---- the free check is a job that cannot spend model money ------------------------------------------------------------------------
def test_the_free_check_builds_fixed_argv_and_needs_a_name():
    assert validate("find", {"only": "Acme Corp"}) == ["find", "--only", "Acme Corp"]
    for bad in ({}, {"only": ""}, {"only": "--all"}, {"only": "a;rm -rf /"}, {"only": "x" * 80}):
        with pytest.raises(JobError):
            validate("find", bad)
    assert KINDS["find"][1] is False and KINDS["find"][2] is False           # no confirmation tick, and not schedule-only


def test_the_free_check_reads_the_sec_then_free_sources_then_scores_and_never_calls_a_model(monkeypatch):
    monkeypatch.setenv("BD_USER_AGENT", "SEI ops@example.com")
    steps = sequence_for("find", ["find", "--only", "Acme Corp"], AS_OF)
    assert [s[0] for s in steps] == ["sec-fill", "scan", "score"]
    assert steps[0][1:4] == ["--only", "Acme Corp", "--exact"] and steps[1][1:4] == ["--only", "Acme Corp", "--exact"]
    scanners = set(steps[1][steps[1].index("--scanners") + 1].split(","))
    assert scanners <= FREE_SCANNERS and {"ats", "financials", "sec_events"} <= scanners
    assert "--force" in steps[1] and "--light" in steps[1]
    assert not any(is_paid(s) for s in steps)                                  # nothing here counts against, or needs, the spending limit
    monkeypatch.delenv("BD_USER_AGENT")
    only_jobs = sequence_for("find", ["find", "--only", "Acme Corp"], AS_OF)[1]
    assert only_jobs[only_jobs.index("--scanners") + 1] == "ats"               # without a declared SEC contact only the job-board feed is used


# ---- the page ---------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def world(app_env):  # noqa: F811
    e = app_env
    today = date.today()
    ready = add_account(e.conn, "Acme Corp <b>", industry="Healthcare", hq_city="Chicago", revenue=2e9)
    add_signal(e.conn, e.cfg, ready, "leadership_change", 4, as_of=today, summary="Names a new COO.")
    add_signal(e.conn, e.cfg, ready, "hiring_cluster", 9, as_of=today)
    score_accounts(e.conn, e.cfg, today)
    held = add_account(e.conn, "Heldco", status="hold_review", chicagoland="unknown", revenue=None, ownership=None)
    gone = add_account(e.conn, "Goneco", status="excluded")
    quiet = add_account(e.conn, "Quietco")                                       # ready but never scored
    return e, {"ready": ready, "held": held, "gone": gone, "quiet": quiet}


def test_a_company_on_the_list_shows_where_it_stands_with_a_link_to_its_brief(world):
    e, ids = world
    body = e.get("/find?q=acme").body
    assert "Acme Corp &lt;b&gt;" in body and "Acme Corp <b>" not in body
    assert f'href="/companies/{ids["ready"]}"' in body and "Open brief" in body and "Names a new COO." in body
    assert "Healthcare · Chicago" in body and "$2.0B revenue" in body
    assert "Add and check (free)" in body                                         # "acme" alone is not an exact match of a name, so adding stays possible


def test_an_exact_match_does_not_offer_to_add_the_company_again(world):
    e, ids = world
    body = e.get("/find?q=Acme Corp <b>").body
    assert "Add and check (free)" not in body and "Not on the list" not in body


def test_a_company_waiting_for_facts_says_why_and_offers_the_free_check_again(world):
    e, ids = world
    body = e.get("/find?q=Heldco").body
    assert "waiting for facts" in body and "not scored yet" in body and "Not scored because: Chicagoland status unverified" in body
    assert 'action="/find/run"' in body and "Check again (free)" in body and 'value="Heldco"' in body


def test_a_ready_company_that_was_never_scored_can_be_checked_and_an_excluded_one_cannot(world):
    e, ids = world
    assert "Check again (free)" in e.get("/find?q=Quietco").body
    gone = e.get("/find?q=Goneco").body
    assert "excluded" in gone and "Check again (free)" not in gone


def test_a_company_not_on_the_list_offers_the_free_add_and_check(world):
    e, ids = world
    body = e.get("/find?q=Zeta <i>Holdings").body
    assert "Not on the list: Zeta &lt;i&gt;Holdings" in body and "Add and check (free)" in body and "spends no model money" in body
    assert "<i>Holdings" not in body and 'name="name" value="Zeta &lt;i&gt;Holdings"' in body
    assert "Type a company name above" in e.get("/find").body and e.get("/find?q=").code == 200


def test_a_running_job_disables_the_button_and_says_why(world):
    e, ids = world
    e.app.jobs._busy = True
    try:
        body = e.get("/find?q=Zeta Holdings").body
        assert "Another job is running" in body and "disabled" in body
    finally:
        e.app.jobs._busy = False


def test_a_local_copy_without_jobs_never_offers_the_check(cfg, tmp_path):
    from bd.ui.app import App
    local = App(cfg, str(tmp_path / "l.sqlite"), "tok", root=tmp_path)
    local.port = 8765
    env = {"REQUEST_METHOD": "GET", "PATH_INFO": "/find", "QUERY_STRING": "q=Zeta", "HTTP_HOST": "127.0.0.1:8765", "HTTP_COOKIE": "bd_session=tok",
           "CONTENT_LENGTH": "0", "wsgi.input": io.BytesIO()}
    out = {}
    page = b"".join(local(env, lambda s, h: out.update(s=s))).decode()
    assert out["s"].startswith("200") and "This copy cannot look up new companies" in page and "Add and check (free)" not in page


# ---- running the check --------------------------------------------------------------------------------------------------------------
def test_running_the_free_check_adds_the_company_and_starts_exactly_one_free_job(world):
    e, ids = world
    r = e.post("/find/run", {"name": "Zeta Holdings"})
    assert r.code == 303 and r.h["Location"].startswith("/find/wait/")
    e.app.jobs.wait()
    job = e.app.jobs.recent()[0]
    assert job.kind == "find" and job.args == ["find", "--only", "Zeta Holdings"] and job.status == "ok"
    zeta = [x for x in list_accounts(e.conn, status=None) if x.name == "Zeta Holdings"][0]
    assert zeta.origin == "manual" and zeta.tier == "B" and zeta.status == "hold_review"
    log = e.app.jobs.log_tail(job.id)
    assert "ARGS: sec-fill --only Zeta Holdings --exact" in log and "ARGS: scan --only Zeta Holdings --exact --scanners" in log and "ARGS: score" in log
    assert e.conn.execute("SELECT actor FROM audit_log WHERE account_id = ? ORDER BY id LIMIT 1", (zeta.id,)).fetchone()["actor"] == "ui"
    assert e.post("/find/run", {"name": "zeta holdings, inc."}).code == 303                      # typed differently: found, not added twice
    assert len([x for x in list_accounts(e.conn, status=None) if "zeta" in x.name.lower()]) == 1


def test_an_existing_company_is_checked_without_being_added_again(world):
    e, ids = world
    before = len(list_accounts(e.conn, status=None))
    assert e.post("/find/run", {"name": "Heldco"}).h["Location"].startswith("/find/wait/")
    e.app.jobs.wait()
    assert len(list_accounts(e.conn, status=None)) == before and e.app.jobs.recent()[0].args == ["find", "--only", "Heldco"]


def test_bad_names_and_missing_tokens_are_refused_and_nothing_is_added(world):
    e, ids = world
    for bad in ("", "--all", "a;rm -rf /", "x" * 80):
        before = len(list_accounts(e.conn, status=None))
        r = e.post("/find/run", {"name": bad})
        assert r.code == 303 and r.h["Location"].startswith("/find?q=")
        assert len(list_accounts(e.conn, status=None)) == before
    assert e.post("/find/run", {"name": "Zeta Holdings"}, csrf=False).code == 403
    e.app.jobs.wait()
    assert e.app.jobs.recent() == []


def test_two_companies_with_the_same_name_are_not_guessed(world):
    e, ids = world
    add_account(e.conn, "Twin Industries Inc")
    add_account(e.conn, "Twin Industries Corp")
    r = e.post("/find/run", {"name": "Twin Industries"})
    assert r.code == 303 and r.h["Location"].startswith("/find?q=")
    assert "More than one company" in e.get(r.h["Location"]).body
    e.app.jobs.wait()
    assert e.app.jobs.recent() == []


def test_the_wait_page_refreshes_while_running_then_opens_the_brief_when_done(world):
    e, ids = world
    r = e.post("/find/run", {"name": "Zeta Holdings"})
    e.app.jobs.wait()
    zeta = [x for x in list_accounts(e.conn, status=None) if x.name == "Zeta Holdings"][0]
    done = e.get(r.h["Location"])
    assert done.code == 303 and done.h["Location"] == f"/companies/{zeta.id}"
    job_id = int(r.h["Location"].rsplit("/", 1)[1])
    e.conn.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (job_id,))
    e.conn.commit()
    running = e.get(r.h["Location"]).body
    assert 'http-equiv="refresh"' in running and "Checking Zeta Holdings" in running and "spends no model money" in running
    e.conn.execute("UPDATE jobs SET status = 'failed', note = 'timed out <b>' WHERE id = ?", (job_id,))
    e.conn.commit()
    failed = e.get(r.h["Location"]).body
    assert "The check did not finish" in failed and "timed out &lt;b&gt;" in failed and 'http-equiv="refresh"' not in failed
    assert f'href="/companies/{zeta.id}"' in failed and "Open what we have" in failed


def test_the_wait_page_only_shows_free_checks(world):
    e, ids = world
    e.post("/lookup/run", {"name": "Other Co"})
    e.app.jobs.wait()
    sec = e.app.jobs.recent()[0]
    assert sec.kind == "sec_fill" and e.get(f"/find/wait/{sec.id}").code == 404
    assert e.get("/find/wait/99999").code == 404


# ---- where people find it ---------------------------------------------------------------------------------------------------------
def test_this_week_searches_here_and_the_brief_offers_the_check_when_a_company_is_not_scored(world):
    e, ids = world
    assert 'action="/find"' in e.get("/week").body and 'class="top on" href="/week"' in e.get("/find").body.split("</nav>")[0]
    brief = e.get(f"/companies/{ids['held']}").body
    assert "waiting before it is scanned: Chicagoland status unverified" in brief and "Check now (free)" in brief and 'action="/find/run"' in brief
    assert "Check now (free)" not in e.get(f"/companies/{ids['ready']}").body


def test_a_score_below_the_watch_band_reads_below_watch_not_stored(world):
    e, ids = world
    low = add_account(e.conn, "Lowco")
    add_signal(e.conn, e.cfg, low, "leadership_change", 40, as_of=date.today())
    score_accounts(e.conn, e.cfg, date.today())
    body = e.get("/find?q=Lowco").body
    assert "Below Watch 21" in body and "Stored" not in body
    assert "Below Watch" in e.get(f"/companies/{low}").body and "Stored" not in e.get(f"/companies/{low}").body
    assert "Below Watch" in e.get("/opportunities?tracked=1").body and ">Stored<" not in e.get("/opportunities?tracked=1").body


def test_the_free_check_is_only_offered_where_it_can_help(world):
    e, ids = world
    private = add_account(e.conn, "Privateco", status="hold_review", chicagoland="yes", revenue=None, ownership="private")
    page = e.get("/find?q=Privateco").body
    assert "Check again (free)" not in page and "The SEC does not list this company, so the free check cannot fill it in" in page
    brief = e.get(f"/companies/{private}").body
    assert "Check now (free)" not in brief and "an admin can research it" in brief
    assert "Check now (free)" in e.get(f"/companies/{ids['held']}").body and "an admin can research it" not in e.get(f"/companies/{ids['held']}").body
    assert "Check again (free)" not in e.get("/find?q=Goneco").body                              # excluded: nothing to check
