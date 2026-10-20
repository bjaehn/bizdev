import io
from datetime import date

import pytest

from bd.accounts import list_accounts
from bd.jobs import FIND_BUDGET, FIND_SCANNERS, FREE_SCANNERS, KINDS, JobError, is_paid, sequence_for, validate
from bd.score_store import score_accounts
from tests.conftest import add_account, add_signal
from tests.test_update import app_env  # noqa: F401  (fixture)

AS_OF = "2026-10-19"


# ---- the signal scan is a capped job: free job boards first, then leadership, hiring, press and events --------------------------------
def test_the_signal_scan_builds_fixed_argv_with_a_spending_cap_and_needs_a_name():
    assert validate("find", {"only": "Acme Corp"}) == ["find", "--only", "Acme Corp", "--budget", str(FIND_BUDGET)] and FIND_BUDGET == 1
    for bad in ({}, {"only": ""}, {"only": "--all"}, {"only": "a;rm -rf /"}, {"only": "x" * 80}):
        with pytest.raises(JobError):
            validate("find", bad)
    assert KINDS["find"][1] is True and KINDS["find"][2] is False           # spends model tokens, so it needs the confirmation tick; not schedule-only


def test_the_signal_scan_reads_job_boards_free_then_searches_for_signals_then_scores(monkeypatch):
    monkeypatch.setenv("BD_USER_AGENT", "SEI ops@example.com")
    steps = sequence_for("find", ["find", "--only", "Acme Corp", "--budget", "1"], AS_OF)
    assert [s[0] for s in steps] == ["scan", "scan", "score"]               # no SEC fill: facts are not what this is for
    assert all(s[1:4] == ["--only", "Acme Corp", "--exact"] for s in steps[:2])
    free, paid = steps[0], steps[1]
    assert set(free[free.index("--scanners") + 1].split(",")) <= FREE_SCANNERS and "--light" in free and not is_paid(free)
    scanners = set(paid[paid.index("--scanners") + 1].split(","))
    assert scanners == set(FIND_SCANNERS.split(",")) == {"sweep", "hiring"}
    assert is_paid(paid) and "--force" in paid and "--light" not in paid       # the paid scan counts against the cap and records that the company was scanned
    assert not is_paid(steps[2])
    monkeypatch.delenv("BD_USER_AGENT")
    bare = sequence_for("find", ["find", "--only", "Acme Corp", "--budget", "1"], AS_OF)[0]
    assert bare[bare.index("--scanners") + 1] == "ats"                       # without a declared SEC contact only the job-board feed is read for free


# ---- the page ---------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def world(app_env):  # noqa: F811
    e = app_env
    today = date.today()
    ready = add_account(e.conn, "Acme Corp <b>", industry="Healthcare", hq_city="Chicago", revenue=2e9)
    add_signal(e.conn, e.cfg, ready, "leadership_change", 4, as_of=today, summary="Names a new COO.")
    add_signal(e.conn, e.cfg, ready, "hiring_cluster", 9, as_of=today)
    score_accounts(e.conn, e.cfg, today)
    e.conn.execute("UPDATE accounts SET last_scanned = ? WHERE id = ?", (today.isoformat(), ready))
    e.conn.commit()
    held = add_account(e.conn, "Heldco", chicagoland="unknown", revenue=None, ownership=None)     # unverified facts: scanned all the same
    gone = add_account(e.conn, "Goneco", status="excluded")
    quiet = add_account(e.conn, "Quietco")                                       # on the list but never scanned or scored
    return e, {"ready": ready, "held": held, "gone": gone, "quiet": quiet}


def test_a_company_on_the_list_shows_its_signals_with_a_link_to_its_brief(world):
    e, ids = world
    body = e.get("/find?q=acme").body
    assert "Acme Corp &lt;b&gt;" in body and "Acme Corp <b>" not in body
    assert f'href="/companies/{ids["ready"]}"' in body and "Open brief" in body and "Names a new COO." in body
    assert "Healthcare · Chicago" in body and "$2.0B revenue" not in body        # facts are background, not the lead
    assert "Add and scan for signals" in body                                      # "acme" alone is not an exact match of a name, so adding stays possible
    assert "Scan for signals</button>" not in body                                # it was scanned and scored: no rescan offered


def test_an_unverified_company_is_tagged_not_blocked(world):
    e, ids = world
    body = e.get("/find?q=Heldco").body
    assert "unverified" in body and "waiting for facts" not in body and "Not scored because" not in body
    assert "Not scanned yet." in body and "Scan for signals</button>" in body
    assert 'action="/find/run"' in body and 'value="Heldco"' in body and 'name="confirm"' in body


def test_a_never_scanned_company_can_be_scanned_and_an_excluded_one_cannot(world):
    e, ids = world
    assert "Scan for signals</button>" in e.get("/find?q=Quietco").body
    gone = e.get("/find?q=Goneco").body
    assert "out of scope" in gone and "Scan for signals</button>" not in gone and "Not scanned" in gone


def test_a_scanned_company_with_nothing_found_says_when_it_was_scanned(world):
    e, ids = world
    e.conn.execute("UPDATE accounts SET last_scanned = '2026-10-12' WHERE id = ?", (ids["quiet"],))
    e.conn.commit()
    body = e.get("/find?q=Quietco").body
    assert "Scanned Oct 12, 2026: no signals found." in body


def test_a_company_not_on_the_list_offers_to_add_it_and_scan_with_a_confirm_tick_and_the_cap(world):
    e, ids = world
    body = e.get("/find?q=Zeta <i>Holdings").body
    assert "Not on the list: Zeta &lt;i&gt;Holdings" in body and "Add and scan for signals" in body and "at most $1" in body
    assert "up to $1" in body and 'name="confirm"' in body and "SEC" not in body
    assert "<i>Holdings" not in body and 'name="name" value="Zeta &lt;i&gt;Holdings"' in body
    assert "Type a company name above" in e.get("/find").body and e.get("/find?q=").code == 200


def test_an_exact_match_does_not_offer_to_add_the_company_again(world):
    e, ids = world
    body = e.get("/find?q=Acme Corp <b>").body
    assert "Add and scan for signals" not in body and "Not on the list" not in body


def test_a_running_job_disables_the_button_and_says_why(world):
    e, ids = world
    e.app.jobs._busy = True
    try:
        body = e.get("/find?q=Zeta Holdings").body
        assert "Another job is running" in body and "disabled" in body
    finally:
        e.app.jobs._busy = False


def test_a_local_copy_without_jobs_never_offers_the_scan(cfg, tmp_path):
    from bd.ui.app import App
    local = App(cfg, str(tmp_path / "l.sqlite"), "tok", root=tmp_path)
    local.port = 8765
    env = {"REQUEST_METHOD": "GET", "PATH_INFO": "/find", "QUERY_STRING": "q=Zeta", "HTTP_HOST": "127.0.0.1:8765", "HTTP_COOKIE": "bd_session=tok",
           "CONTENT_LENGTH": "0", "wsgi.input": io.BytesIO()}
    out = {}
    page = b"".join(local(env, lambda s, h: out.update(s=s))).decode()
    assert out["s"].startswith("200") and "This copy cannot look up new companies" in page and "Add and scan for signals" not in page


# ---- running the scan ---------------------------------------------------------------------------------------------------------------
def test_running_the_scan_without_the_tick_is_refused_and_adds_nothing_to_run(world):
    e, ids = world
    before = len(list_accounts(e.conn, status=None))
    r = e.post("/find/run", {"name": "Zeta Holdings"})
    assert r.code == 303 and r.h["Location"].startswith("/find?q=")
    assert "Tick the box to confirm" in e.get(r.h["Location"]).body
    e.app.jobs.wait()
    assert e.app.jobs.recent() == []
    assert len(list_accounts(e.conn, status=None)) == before + 1            # the company was added so the next try finds it, as for any lookup


def test_running_the_scan_adds_the_company_and_starts_exactly_one_capped_job(world):
    e, ids = world
    r = e.post("/find/run", {"name": "Zeta Holdings", "confirm": "1"})
    assert r.code == 303 and r.h["Location"].startswith("/find/wait/")
    e.app.jobs.wait()
    job = e.app.jobs.recent()[0]
    assert job.kind == "find" and job.args == ["find", "--only", "Zeta Holdings", "--budget", "1"] and job.status == "ok"
    zeta = [x for x in list_accounts(e.conn, status=None) if x.name == "Zeta Holdings"][0]
    assert zeta.origin == "manual" and zeta.tier == "B" and zeta.status == "active"
    log = e.app.jobs.log_tail(job.id)
    assert "ARGS: scan --only Zeta Holdings --exact --scanners ats" in log and "--scanners sweep,hiring" in log
    assert "ARGS: score" in log and "sec-fill" not in log
    assert e.conn.execute("SELECT actor FROM audit_log WHERE account_id = ? ORDER BY id LIMIT 1", (zeta.id,)).fetchone()["actor"] == "ui"
    assert e.post("/find/run", {"name": "zeta holdings, inc.", "confirm": "1"}).code == 303                      # typed differently: found, not added twice
    assert len([x for x in list_accounts(e.conn, status=None) if "zeta" in x.name.lower()]) == 1


def test_an_existing_company_is_scanned_without_being_added_again(world):
    e, ids = world
    before = len(list_accounts(e.conn, status=None))
    assert e.post("/find/run", {"name": "Heldco", "confirm": "1"}).h["Location"].startswith("/find/wait/")
    e.app.jobs.wait()
    assert len(list_accounts(e.conn, status=None)) == before and e.app.jobs.recent()[0].args == ["find", "--only", "Heldco", "--budget", "1"]


def test_bad_names_and_missing_tokens_are_refused_and_nothing_is_added(world):
    e, ids = world
    for bad in ("", "--all", "a;rm -rf /", "x" * 80):
        before = len(list_accounts(e.conn, status=None))
        r = e.post("/find/run", {"name": bad, "confirm": "1"})
        assert r.code == 303 and r.h["Location"].startswith("/find?q=")
        assert len(list_accounts(e.conn, status=None)) == before
    assert e.post("/find/run", {"name": "Zeta Holdings", "confirm": "1"}, csrf=False).code == 403
    e.app.jobs.wait()
    assert e.app.jobs.recent() == []


def test_two_companies_with_the_same_name_are_not_guessed(world):
    e, ids = world
    add_account(e.conn, "Twin Industries Inc")
    add_account(e.conn, "Twin Industries Corp")
    r = e.post("/find/run", {"name": "Twin Industries", "confirm": "1"})
    assert r.code == 303 and r.h["Location"].startswith("/find?q=")
    assert "More than one company" in e.get(r.h["Location"]).body
    e.app.jobs.wait()
    assert e.app.jobs.recent() == []


def test_the_wait_page_refreshes_while_running_then_opens_the_brief_when_done(world):
    e, ids = world
    r = e.post("/find/run", {"name": "Zeta Holdings", "confirm": "1"})
    e.app.jobs.wait()
    zeta = [x for x in list_accounts(e.conn, status=None) if x.name == "Zeta Holdings"][0]
    done = e.get(r.h["Location"])
    assert done.code == 303 and done.h["Location"] == f"/companies/{zeta.id}"
    job_id = int(r.h["Location"].rsplit("/", 1)[1])
    e.conn.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (job_id,))
    e.conn.commit()
    running = e.get(r.h["Location"]).body
    assert 'http-equiv="refresh"' in running and "Scanning Zeta Holdings" in running and "at most $1" in running
    e.conn.execute("UPDATE jobs SET status = 'failed', note = 'timed out <b>' WHERE id = ?", (job_id,))
    e.conn.commit()
    failed = e.get(r.h["Location"]).body
    assert "The scan did not finish" in failed and "timed out &lt;b&gt;" in failed and 'http-equiv="refresh"' not in failed
    assert f'href="/companies/{zeta.id}"' in failed and "Open what we have" in failed


def test_the_wait_page_only_shows_signal_scans(world):
    e, ids = world
    e.post("/lookup/run", {"name": "Other Co"})
    e.app.jobs.wait()
    sec = e.app.jobs.recent()[0]
    assert sec.kind == "sec_fill" and e.get(f"/find/wait/{sec.id}").code == 404
    assert e.get("/find/wait/99999").code == 404


# ---- where people find it ---------------------------------------------------------------------------------------------------------
def test_analysis_searches_here_and_the_brief_offers_the_scan_when_a_company_has_none(world):
    e, ids = world
    assert 'action="/find"' in e.get("/opportunities").body and 'class="top on" href="/opportunities"' in e.get("/find").body.split("</nav>")[0]
    brief = e.get(f"/companies/{ids['held']}").body
    assert "Not scanned yet" in brief and "Scan for signals now" in brief and 'action="/find/run"' in brief and 'name="confirm"' in brief
    assert "Scan for signals now" not in e.get(f"/companies/{ids['ready']}").body


def test_the_scan_is_offered_for_unverified_and_private_companies_but_not_excluded_ones(world):
    e, ids = world
    private = add_account(e.conn, "Privateco", chicagoland="yes", revenue=None, ownership="private")
    assert "Scan for signals</button>" in e.get("/find?q=Privateco").body
    assert "Scan for signals now" in e.get(f"/companies/{private}").body
    assert "Scan for signals</button>" not in e.get("/find?q=Goneco").body
    assert "Scan for signals now" not in e.get(f"/companies/{ids['gone']}").body


def test_a_score_below_the_watch_band_reads_below_watch_not_stored(world):
    e, ids = world
    low = add_account(e.conn, "Lowco")
    add_signal(e.conn, e.cfg, low, "leadership_change", 40, as_of=date.today())
    score_accounts(e.conn, e.cfg, date.today())
    body = e.get("/find?q=Lowco").body
    assert "Below Watch 21" in body and "Stored" not in body
    assert "Below Watch" in e.get(f"/companies/{low}").body and "Stored" not in e.get(f"/companies/{low}").body
    assert "Below Watch" in e.get("/opportunities?view=matrix&tracked=1").body and ">Stored<" not in e.get("/opportunities?view=matrix&tracked=1").body
