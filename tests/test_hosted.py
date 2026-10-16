import io
import stat
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import pytest

from bd import scheduler
from bd.jobs import KINDS, SCANNER_NAMES, JobError, JobRunner, redact, sequence_for, validate
from bd.ui.app import App
from bd.ui.auth import AuthConfigError, Hosted, Throttle
from tests.conftest import ROOT, add_account, make_db

PW = "correct horse battery staple"
HOST = "bd.example.app"
CHI = ZoneInfo("America/Chicago")


def call(app, method, path, data=None, cookie="", headers=None, host=HOST):
    path, _, qs = path.partition("?")
    body = urlencode(data, doseq=True).encode() if data else b""
    env = {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": qs, "HTTP_HOST": host,
           "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body), "REMOTE_ADDR": "9.9.9.9"}
    if cookie:
        env["HTTP_COOKIE"] = f"bd_session={cookie}"
    for k, v in (headers or {}).items():
        env["HTTP_" + k.upper().replace("-", "_")] = v
    out = {}
    chunks = app(env, lambda s, h: out.update(status=s, headers=h))
    return type("R", (), {"code": int(out["status"].split()[0]), "h": dict(out["headers"]), "hl": out["headers"],
                          "body": b"".join(chunks).decode()})


@pytest.fixture
def fake_bd(tmp_path):
    exe = tmp_path / "fakebd.sh"
    exe.write_text('#!/usr/bin/env bash\necho "ARGS: $*"\necho "key=$ANTHROPIC_API_KEY"\n[ "$1" = "${FAIL_ON:-none}" ] && exit 3\nexit 0\n')
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return [str(exe)]


@pytest.fixture
def hosted(cfg, tmp_path, fake_bd):
    make_db(tmp_path).close()
    db = str(tmp_path / "t.sqlite")
    runner = JobRunner(db, tmp_path, bd_cmd=fake_bd)
    app = App(cfg, db, root=tmp_path, hosted=Hosted(PW), jobs=runner)
    app.fail_delay = 0
    return type("H", (), {"app": app, "runner": runner, "tmp": tmp_path, "db": db})


def login(h, password=PW, **kw):
    return call(h.app, "POST", "/login", {"password": password}, **kw)


def session(h):
    r = login(h)
    assert r.code == 303
    return r.h["Set-Cookie"].split(";")[0].split("=", 1)[1]


# ---- auth primitives ---------------------------------------------------------------------------------------------
def test_short_or_missing_password_refuses_to_start():
    for bad in ("", "short", "x" * 15):
        with pytest.raises(AuthConfigError):
            Hosted(bad)
    Hosted("x" * 16)


def test_session_tamper_expiry_and_password_change():
    h = Hosted(PW, session_hours=1)
    s = h.make_session(now=1000)
    assert h.session_ok(s, now=1000 + 3599)
    assert not h.session_ok(s, now=1000 + 3601)
    exp = s.split(".")[1]
    assert not h.session_ok(s.replace(exp, str(int(exp) + 99999)), now=1000)
    assert not h.session_ok("garbage") and not h.session_ok("") and not h.session_ok("s1.1.x")
    assert not Hosted(PW + "!").session_ok(s, now=1000)


def test_throttle_locks_out_with_backoff_and_resets():
    t = [0.0]
    th = Throttle(free=3, base=10, cap=100, global_per_hour=1000, clock=lambda: t[0])
    for _ in range(3):
        assert th.retry_after("a") == 0
        th.failed("a")
    assert th.retry_after("a") > 0 and th.retry_after("b") == 0
    t[0] += 11
    assert th.retry_after("a") == 0
    th.failed("a")
    assert th.retry_after("a") >= 20           # doubled
    th.succeeded("a")
    assert th.retry_after("a") == 0


def test_global_throttle_stops_distributed_guessing():
    th = Throttle(free=100, global_per_hour=3, clock=lambda: 0.0)
    for i in range(3):
        th.failed(f"c{i}")
    assert th.retry_after("brand-new") > 0


# ---- login flow ---------------------------------------------------------------------------------------------------
def test_healthz_is_public_and_leaks_nothing(hosted):
    r = call(hosted.app, "GET", "/healthz")
    assert r.code == 200 and r.body == "ok"


def test_everything_else_needs_a_session(hosted):
    for path in ("/", "/accounts", "/operate", "/runs", "/jobs/1"):
        r = call(hosted.app, "GET", path)
        assert r.code == 303 and r.h["Location"] == "/login"
    assert call(hosted.app, "POST", "/operate/run", {"kind": "score"}).code == 403
    assert call(hosted.app, "GET", "/", cookie="s1.99999999999.deadbeef").code == 303


def test_login_wrong_then_right(hosted):
    bad = login(hosted, "nope")
    assert bad.code == 401 and "not right" in bad.body and "Set-Cookie" not in bad.h
    ok = login(hosted)
    assert ok.code == 303
    c = ok.h["Set-Cookie"]
    assert "HttpOnly" in c and "SameSite=Lax" in c and "Secure" not in c
    assert "Secure" in login(hosted, headers={"X-Forwarded-Proto": "https"}).h["Set-Cookie"]
    page = call(hosted.app, "GET", "/", cookie=session(hosted))
    assert page.code == 200 and "Sign out" in page.body and "Update my list" in page.body
    assert call(hosted.app, "GET", "/login", cookie=session(hosted)).code == 303


def test_login_is_throttled_per_client(hosted):
    for _ in range(5):
        login(hosted, "wrong", headers={"X-Forwarded-For": "1.2.3.4"})
    r = login(hosted, PW, headers={"X-Forwarded-For": "1.2.3.4"})
    assert r.code == 429 and "Set-Cookie" not in r.h
    assert login(hosted, PW, headers={"X-Forwarded-For": "5.6.7.8"}).code == 303


def test_login_rejects_cross_site_posts(hosted):
    assert login(hosted, headers={"Origin": "https://evil.example"}).code == 403
    assert login(hosted, headers={"Sec-Fetch-Site": "cross-site"}).code == 403
    assert login(hosted, headers={"Origin": f"https://{HOST}"}).code == 303


def test_browsers_that_send_origin_null_can_sign_in_only_when_same_origin(hosted):
    assert login(hosted, headers={"Origin": "null", "Sec-Fetch-Site": "same-origin"}).code == 303
    assert login(hosted, headers={"Origin": "null"}).code == 403
    assert login(hosted, headers={"Origin": "null", "Sec-Fetch-Site": "cross-site"}).code == 403
    s = session(hosted)
    r = call(hosted.app, "POST", "/logout", {"csrf": hosted.app._csrf}, cookie=s,
             headers={"Origin": "null", "Sec-Fetch-Site": "same-origin"})
    assert r.code == 303


def test_pages_send_origin_on_same_origin_posts(hosted):
    assert call(hosted.app, "GET", "/login").h["Referrer-Policy"] == "same-origin"


def test_logout_clears_cookie_and_post_guards_apply(hosted):
    s = session(hosted)
    csrf = hosted.app._csrf
    assert call(hosted.app, "POST", "/logout", {}, cookie=s).code == 403            # no CSRF token
    assert call(hosted.app, "POST", "/logout", {"csrf": csrf}, cookie=s, headers={"Origin": "https://evil.example"}).code == 403
    r = call(hosted.app, "POST", "/logout", {"csrf": csrf}, cookie=s)
    assert r.code == 303 and "Max-Age=0" in r.h["Set-Cookie"]


def test_flash_never_leaks_onto_the_login_page(hosted):
    hosted.app.say("ok", "SECRET-FLASH-TEXT")
    assert "SECRET-FLASH-TEXT" not in call(hosted.app, "GET", "/login").body
    assert "SECRET-FLASH-TEXT" not in login(hosted, "wrong").body
    assert "SECRET-FLASH-TEXT" in call(hosted.app, "GET", "/", cookie=session(hosted)).body


def test_login_page_has_no_nav_and_hsts_only_over_https(hosted):
    body = call(hosted.app, "GET", "/login").body
    assert "Accounts" not in body and 'type="password"' in body
    assert "Strict-Transport-Security" in call(hosted.app, "GET", "/login", headers={"X-Forwarded-Proto": "https"}).h
    assert "Strict-Transport-Security" not in call(hosted.app, "GET", "/login").h


def test_any_host_header_is_accepted_when_hosted(hosted):
    assert call(hosted.app, "GET", "/healthz", host="something.up.railway.app").code == 200


# ---- jobs ---------------------------------------------------------------------------------------------------------
def test_there_is_no_posting_job_and_scheduled_only_job_is_not_user_startable():
    assert not any("post" in k for k in KINDS)
    with pytest.raises(JobError):
        validate("weekly", {})
    assert validate("weekly", {}, scheduled=True) == []
    for kind in KINDS:
        for step in sequence_for(kind, validate(kind, {"limit": "5", "research": "5", "segments": "1", "scan": "5", "budget": "5"}, scheduled=True), "2026-01-05"):
            if step[0] == "card" and kind != "weekly":
                assert "--dry-run" in step


def test_weekly_posts_only_by_omitting_dry_run_and_honors_bd_dry_run(monkeypatch):
    monkeypatch.delenv("BD_DRY_RUN", raising=False)
    assert "--dry-run" not in sequence_for("weekly", [], "2026-01-05")[-1]
    monkeypatch.setenv("BD_DRY_RUN", "1")
    assert "--dry-run" in sequence_for("weekly", [], "2026-01-05")[-1]
    assert "--dry-run" in sequence_for("weekly_dry", [], "2026-01-05")[-1]


@pytest.mark.parametrize("kind,form", [
    ("enrich", {"limit": "0"}), ("enrich", {"limit": "101"}), ("enrich", {"limit": "x"}), ("enrich", {}),
    ("enrich", {"limit": "5", "only": "--all"}), ("enrich", {"limit": "5", "only": "a;rm -rf /"}),
    ("scan", {"limit": "5", "tier": "C"}), ("scan", {"limit": "5", "scanners": "evil"}), ("nope", {}),
    ("scan", {"limit": "5", "scanners": "hiring,$(x)"}),
])
def test_job_validation_rejects_bad_input(kind, form):
    with pytest.raises(JobError):
        validate(kind, form)


def test_validated_argv_is_exactly_what_we_expect():
    assert validate("scan", {"limit": "5", "tier": "A", "scanners": "hiring,pe"}) == \
        ["scan", "--limit", "5", "--tier", "A", "--scanners", "hiring,pe"]
    assert validate("enrich", {"limit": "3", "only": "Acme Corp."}) == ["enrich", "--limit", "3", "--only", "Acme Corp."]
    assert validate("card_dry", {"no_llm": "1"}) == ["card", "--dry-run", "--no-llm"]


def test_scanner_names_match_the_real_registry():
    from bd.scanners import SCANNERS
    assert set(SCANNER_NAMES) == {n for n, c in SCANNERS.items() if c.implemented}


def test_spend_jobs_need_confirmation(hosted):
    with pytest.raises(JobError, match="confirm"):
        hosted.runner.submit("scan", {"limit": "5"})
    hosted.runner.submit("score", {})           # free: no confirmation needed
    hosted.runner.wait()


def test_job_runs_logs_and_redacts_secrets(hosted, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-very-secret-1234")
    jid = hosted.runner.submit("scan", {"limit": "2"}, confirmed=True)
    hosted.runner.wait()
    job = hosted.runner.get(jid)
    assert job.status == "ok" and job.exit_code == 0
    log = hosted.runner.log_tail(jid)
    assert "ARGS: scan --limit 2 --tier all --as-of" in log
    assert "sk-test-very-secret-1234" not in log and "[ANTHROPIC_API_KEY hidden]" in log


def test_failing_step_stops_the_sequence(hosted, monkeypatch):
    monkeypatch.setenv("FAIL_ON", "score")
    jid = hosted.runner.submit("weekly_dry", {}, confirmed=True)
    hosted.runner.wait()
    job = hosted.runner.get(jid)
    log = hosted.runner.log_tail(jid)
    assert job.status == "failed" and job.exit_code == 3 and "card" not in log.split("$ bd score")[-1]


def test_one_job_at_a_time(hosted, tmp_path):
    slow = tmp_path / "slow.sh"
    slow.write_text("#!/usr/bin/env bash\nsleep 1\n")
    slow.chmod(0o755)
    r = JobRunner(hosted.db, tmp_path, bd_cmd=[str(slow)])
    r.submit("score", {})
    with pytest.raises(JobError, match="still running"):
        r.submit("score", {})
    r.wait()
    r.submit("score", {})
    r.wait()


def test_timeout_marks_failed(hosted, tmp_path):
    slow = tmp_path / "slow2.sh"
    slow.write_text("#!/usr/bin/env bash\nsleep 5\n")
    slow.chmod(0o755)
    r = JobRunner(hosted.db, tmp_path, bd_cmd=[str(slow)], timeout=1)
    jid = r.submit("score", {})
    r.wait()
    assert r.get(jid).status == "failed" and "Stopped after" in (r.get(jid).note or "")


def test_stale_running_jobs_become_interrupted_on_restart(hosted):
    with hosted.runner._conn() as c:
        c.execute("INSERT INTO jobs(kind,args_json,status,created) VALUES ('scan','[]','running','2026-01-01 00:00:00')")
    again = JobRunner(hosted.db, hosted.tmp, bd_cmd=["true"])
    assert again.get(1).status == "interrupted"
    assert not again.busy()


def test_redact_ignores_short_values():
    assert redact("a b", {"ANTHROPIC_API_KEY": "b"}) == "a b"


# ---- operate pages ------------------------------------------------------------------------------------------------
def test_operate_page_shows_readiness_flags_but_never_values(hosted, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-super-secret-value")
    monkeypatch.setenv("TEAMS_WEBHOOK_URL", "https://hooks.example/webhook-secret")
    monkeypatch.setenv("BD_UI_PASSWORD", PW)
    body = call(hosted.app, "GET", "/operate", cookie=session(hosted)).body
    assert "✓" in body and "✗" in body and "Next run" in body and "post to Teams" in body
    for secret in ("sk-super-secret-value", "webhook-secret", PW):
        assert secret not in body


def test_operate_run_flow_end_to_end(hosted):
    s, csrf = session(hosted), hosted.app._csrf
    r = call(hosted.app, "POST", "/operate/run", {"csrf": csrf, "kind": "score"}, cookie=s)
    assert r.code == 303 and r.h["Location"].startswith("/jobs/")
    hosted.runner.wait()
    page = call(hosted.app, "GET", r.h["Location"], cookie=s)
    assert page.code == 200 and "ARGS: score" in page.body and "ok" in page.body
    unconfirmed = call(hosted.app, "POST", "/operate/run", {"csrf": csrf, "kind": "scan", "limit": "5"}, cookie=s)
    assert unconfirmed.h["Location"] == "/operate"
    assert "confirm" in call(hosted.app, "GET", "/operate", cookie=s).body
    assert call(hosted.app, "POST", "/operate/run", {"csrf": csrf, "kind": "weekly"}, cookie=s).h["Location"] == "/operate"
    assert call(hosted.app, "GET", "/jobs/999", cookie=s).code == 404


def test_local_mode_has_no_operate_routes(cfg, tmp_path):
    make_db(tmp_path).close()
    app = App(cfg, str(tmp_path / "t.sqlite"), "tok", root=tmp_path)
    app.port = 8765
    env = {"REQUEST_METHOD": "GET", "PATH_INFO": "/operate", "QUERY_STRING": "", "HTTP_HOST": "127.0.0.1:8765",
           "HTTP_COOKIE": "bd_session=tok", "CONTENT_LENGTH": "0", "wsgi.input": io.BytesIO()}
    out = {}
    app(env, lambda s, h: out.update(s=s))
    assert out["s"].startswith("404")


# ---- scheduler ----------------------------------------------------------------------------------------------------
def test_next_run_is_monday_5am_chicago_across_dst():
    for start in (datetime(2026, 3, 5, tzinfo=CHI), datetime(2026, 11, 2, tzinfo=CHI), datetime(2026, 7, 1, tzinfo=CHI)):
        for i in range(0, 60):
            now = start + timedelta(days=i, hours=7)
            n = scheduler.next_run(now).astimezone(CHI)
            assert n > now and n.weekday() == 0 and (n.hour, n.minute) == (5, 0)
            assert n - now <= timedelta(days=7, hours=1)


def test_due_runs_once_per_monday_with_catch_up():
    mon = datetime(2026, 10, 12, 5, 0, tzinfo=CHI)
    utc = lambda dt: dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")   # noqa: E731
    assert scheduler.due(mon - timedelta(minutes=1), None) is None
    assert scheduler.due(mon, None) is not None
    assert scheduler.due(mon + timedelta(hours=11), None) is not None             # catch-up window
    assert scheduler.due(mon + timedelta(hours=13), None) is None                 # too late: wait for next week
    assert scheduler.due(mon + timedelta(hours=1), utc(mon + timedelta(minutes=2))) is None   # already ran this week
    assert scheduler.due(mon + timedelta(hours=1), utc(mon - timedelta(days=7))) is not None


def test_scheduler_submits_weekly_once_and_waits_when_busy(hosted):
    mon = datetime(2026, 10, 12, 5, 1, tzinfo=CHI).astimezone(timezone.utc)
    s = scheduler.Scheduler(hosted.runner, clock=lambda: mon)
    jid = s.check()
    assert jid is not None
    hosted.runner.wait()
    assert hosted.runner.get(jid).kind == "weekly"
    assert s.check() is None                       # already ran for this Monday


# ---- discovery in the app -----------------------------------------------------------------------------------------
def test_discover_job_is_validated_and_dry_run_free(hosted):
    assert validate("discover", {"limit": "2"}) == ["discover", "--limit", "2"]
    for bad in ({}, {"limit": "0"}, {"limit": "31"}, {"limit": "x"}):
        with pytest.raises(JobError):
            validate("discover", bad)
    with pytest.raises(JobError, match="confirm"):
        hosted.runner.submit("discover", {"limit": "2"})
    assert sequence_for("discover", ["discover", "--limit", "2"], "2026-01-05") == [["discover", "--limit", "2", "--as-of", "2026-01-05"]]


def test_discovery_page_queue_and_review_buttons(hosted):
    import sqlite3
    c = sqlite3.connect(hosted.db)
    c.execute("INSERT INTO accounts(name, domain, chicagoland, ownership, revenue, origin, reviewed, tier) "
              "VALUES ('Auto Co','auto.com','yes','public',5e8,'discovered',0,'B')")
    c.execute("INSERT INTO candidates(name,domain,segment,source_url,found_date,status,reason,facts_json) VALUES "
              "('Queued Co','q.com','mfg','https://src.example/a','2026-10-16','queued','size unverified','{\"chicagoland\":\"yes\"}')")
    c.commit()
    s, csrf = session(hosted), hosted.app._csrf
    page = call(hosted.app, "GET", "/candidates", cookie=s).body
    assert "Queued Co" in page and "size unverified" in page and "Add</button>" in page
    accts = call(hosted.app, "GET", "/accounts", cookie=s).body
    assert "auto-added" in accts and "Mark all 1 as reviewed" in accts
    assert "Not yet reviewed" in call(hosted.app, "GET", "/accounts/1", cookie=s).body
    r = call(hosted.app, "POST", "/accounts/1/reviewed", {"csrf": csrf}, cookie=s)
    assert r.code == 303
    assert "auto-added" not in call(hosted.app, "GET", "/accounts", cookie=s).body
    assert call(hosted.app, "POST", "/candidates/1/accept", {"csrf": csrf}, cookie=s).code == 303
    assert "Queued Co" in call(hosted.app, "GET", "/accounts", cookie=s).body
    assert call(hosted.app, "POST", "/candidates/1/accept", {}, cookie=s).code == 403      # CSRF still required


def test_settings_page_saves_validates_and_resets(hosted):
    from bd.db import connect
    from bd.settings_store import apply_overrides
    s, csrf = session(hosted), hosted.app._csrf
    page = call(hosted.app, "GET", "/settings", cookie=s).body
    assert "manufacturing and industrial products" in page and "Reset to config.yaml" not in page
    good = {"csrf": csrf, "segments": "pharma distribution\n\n Pharma Distribution \nlogistics in Illinois", "per_segment": "5",
            "max_candidates_per_run": "10", "max_auto_add_per_run": "3", "scan_unreviewed_per_run": "4"}
    assert call(hosted.app, "POST", "/settings/discovery", good, cookie=s).code == 303
    page = call(hosted.app, "GET", "/settings", cookie=s).body
    assert "logistics in Illinois" in page and "Reset to config.yaml" in page
    d = apply_overrides(hosted.app.cfg, connect(hosted.db)).discovery
    assert d.segments == ["pharma distribution", "logistics in Illinois"] and (d.per_segment, d.max_auto_add_per_run) == (5, 3)
    assert hosted.app.cfg.discovery.per_segment == 8                       # the config object itself is untouched
    bad = call(hosted.app, "POST", "/settings/discovery", {**good, "segments": "", "per_segment": "999"}, cookie=s)
    assert bad.code == 200 and "Not saved" in bad.body and "at least one segment" in bad.body
    assert call(hosted.app, "POST", "/settings/discovery", {k: v for k, v in good.items() if k != "csrf"}, cookie=s).code == 403
    assert call(hosted.app, "POST", "/settings/discovery/reset", {"csrf": csrf}, cookie=s).code == 303
    assert "pharma distribution" not in call(hosted.app, "GET", "/settings", cookie=s).body
    assert connect(hosted.db).execute("SELECT COUNT(*) FROM audit_log WHERE field='settings.discovery'").fetchone()[0] == 2


def test_enrich_page_offers_apply_all_and_applies_only_new_facts(hosted):
    import sqlite3
    c = sqlite3.connect(hosted.db)
    c.execute("INSERT INTO accounts(name, domain, chicagoland) VALUES ('Acme', 'acme.com', 'unknown')")
    c.commit()
    (hosted.tmp / "p.csv").write_text("name,chicagoland,revenue,evidence_urls\nAcme,yes,500000000,il_county=https://acme.com/loc; revenue_usd=https://acme.com/ir\n")
    s, csrf = session(hosted), hosted.app._csrf
    page = call(hosted.app, "GET", "/enrich?file=p.csv", cookie=s).body
    assert "Apply all new facts" in page and "2 new facts" in page
    r = call(hosted.app, "POST", "/enrich/apply-all", {"csrf": csrf, "file": "p.csv"}, cookie=s)
    assert r.code == 303
    assert "Applied 2 new fact(s) to 1 account(s)" in call(hosted.app, "GET", "/enrich?file=p.csv", cookie=s).body
    assert c.execute("SELECT chicagoland, revenue FROM accounts").fetchone() == ("yes", 5e8)
    assert call(hosted.app, "POST", "/enrich/apply-all", {"csrf": csrf, "file": "../../etc/passwd"}, cookie=s).code == 303
    assert call(hosted.app, "POST", "/enrich/apply-all", {"file": "p.csv"}, cookie=s).code == 403
