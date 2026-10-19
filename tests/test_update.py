import json
import os
import sqlite3
import stat
from datetime import date, timedelta

import pytest
from typer.testing import CliRunner

from bd.accounts import list_accounts
from bd.cli import app
from bd.jobs import JobError, JobRunner, sequence_for, validate
from bd.settings_store import UPDATE_DEFAULTS, load_update, save_update
from bd.setup_check import explain_error, static_problems
from tests.conftest import AS_OF, ROOT, add_account, add_signal, make_db

PW = "correct horse battery staple"
UPD = {"research": "25", "segments": "2", "scan": "25", "budget": "8"}
runner = CliRunner()


@pytest.fixture
def cfg(cfg):
    return cfg.model_copy(deep=True)


# ---- the job ---------------------------------------------------------------------------------------------------------
def test_update_job_is_validated_and_runs_the_steps_in_order():
    args = validate("update", UPD)
    assert args == ["update", "--research", "25", "--segments", "2", "--scan", "25", "--budget", "8", "--rfps", "0"]
    steps = sequence_for("update", args, "2026-10-16")
    assert [s[0] for s in steps] == ["check", "sec-fill", "enrich", "apply-proposals", "discover", "pe-sync", "scan", "scan", "score"]
    assert steps[1] == ["sec-fill", "--as-of", "2026-10-16"]
    assert steps[2] == ["enrich", "--limit", "25", "--allow-empty", "--as-of", "2026-10-16"]
    assert "--allow-empty" in steps[-1] and "--allow-empty" in steps[-2]
    skip = sequence_for("update", validate("update", {**UPD, "research": "0", "segments": "0"}), "2026-10-16")
    assert [s[0] for s in skip] == ["check", "sec-fill", "pe-sync", "scan", "scan", "score"]
    for bad in ({**UPD, "budget": "0"}, {**UPD, "budget": "101"}, {**UPD, "scan": "0"}, {**UPD, "research": "x"}, {}):
        with pytest.raises(JobError):
            validate("update", bad)


def test_the_update_job_never_posts_and_needs_the_paid_calls_confirmation(tmp_path):
    make_db(tmp_path).close()
    r = JobRunner(str(tmp_path / "t.sqlite"), tmp_path, ["true"])
    with pytest.raises(JobError, match="confirm"):
        r.submit("update", UPD)
    for step in sequence_for("update", validate("update", UPD), "2026-10-16"):
        assert step[0] != "card"


@pytest.fixture
def fake(tmp_path):
    """A stand-in for `bd`: records each call and its budget env, and logs the cost the test tells it to."""
    db = tmp_path / "t.sqlite"
    make_db(tmp_path).close()
    exe = tmp_path / "fakebd.py"
    exe.write_text(f'''#!{os.sys.executable}
import json, os, sqlite3, sys
step = sys.argv[1]
with open({str(tmp_path / "calls.jsonl")!r}, "a") as f:
    f.write(json.dumps({{"step": step, "args": sys.argv[1:], "budget": os.environ.get("BD_MAX_COST_USD")}}) + "\\n")
costs = json.loads(os.environ.get("FAKE_COSTS", "{{}}"))
free_scan = step == "scan" and "--scanners" in sys.argv          # the free-sources scan costs nothing
if step in costs and not free_scan:
    c = sqlite3.connect(os.environ["BD_DB_PATH"])
    c.execute("INSERT INTO runs(run_date,step,accounts_scanned,signals_found,tokens,est_cost) VALUES ('2026-10-16', ?, 0, 0, 10, ?)", (step, costs[step]))
    if step == "scan":
        c.execute("INSERT INTO runs(run_date,step,accounts_scanned,signals_found,tokens,est_cost) VALUES ('2026-10-16','scan:total',0,0,10,?)", (costs[step],))
    c.commit()
sys.exit(3 if os.environ.get("FAIL_ON") == step else 0)
''')
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return JobRunner(str(db), tmp_path, [str(exe)]), tmp_path


def calls(tmp):
    return [json.loads(x) for x in (tmp / "calls.jsonl").read_text().splitlines()]


def test_one_spending_limit_covers_the_whole_update(fake, monkeypatch):
    runner_, tmp = fake
    monkeypatch.setenv("FAKE_COSTS", json.dumps({"enrich": 3.0, "discover": 2.5, "scan": 9.0}))
    jid = runner_.submit("update", {**UPD, "budget": "5"}, confirmed=True)
    runner_.wait()
    job = runner_.get(jid)
    by = {c["step"]: c for c in calls(tmp) if "--scanners" not in c["args"]}
    assert float(by["enrich"]["budget"]) == 5.0 and float(by["discover"]["budget"]) == 2.0     # what was left after enrich
    assert "scan" not in by                                                                      # 5.5 spent: limit used up, scan skipped
    free = [c for c in calls(tmp) if c["step"] == "scan"]
    assert len(free) == 1 and "--scanners" in free[0]["args"] and free[0]["budget"] is None      # the free sources still run, with no limit applied
    assert "apply-proposals" in by and "pe-sync" in by and "score" in by                         # the free steps still run
    assert job.status == "ok" and "Spent about $5.50 of the $5 limit" in job.note and "skipped" in job.note
    assert "spending limit for this update is used up" in runner_.log_tail(jid)


def test_scan_total_row_is_not_counted_twice(fake, monkeypatch):
    runner_, tmp = fake
    monkeypatch.setenv("FAKE_COSTS", json.dumps({"scan": 2.0}))
    jid = runner_.submit("update", UPD, confirmed=True)
    runner_.wait()
    assert "Spent about $2.00 of the $8 limit" in runner_.get(jid).note


def test_a_failed_connection_check_stops_before_any_paid_step(fake, monkeypatch):
    runner_, tmp = fake
    monkeypatch.setenv("FAIL_ON", "check")
    jid = runner_.submit("update", UPD, confirmed=True)
    runner_.wait()
    job = runner_.get(jid)
    assert job.status == "failed" and "`bd check` exited with 3" in job.note
    assert [c["step"] for c in calls(tmp)] == ["check"]


# ---- CLI pieces --------------------------------------------------------------------------------------------------------
@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.setenv("BD_DB_PATH", str(tmp_path / "c.sqlite"))
    monkeypatch.setenv("BD_CONFIG", str(ROOT / "config.yaml"))
    monkeypatch.chdir(tmp_path)
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "BD_MAX_COST_USD", "BD_USER_AGENT"):
        monkeypatch.delenv(k, raising=False)
    return tmp_path


def test_apply_proposals_applies_new_facts_and_tolerates_a_missing_file(cli_env):
    assert "not found: nothing to apply" in runner.invoke(app, ["apply-proposals", "nope.csv"]).output
    runner.invoke(app, ["seed", str(ROOT / "accounts.csv")])
    (cli_env / "p.csv").write_text("name,chicagoland,revenue,evidence_urls\nAbbott,yes,40000000000,il_county=https://abbott.com/l; revenue_usd=https://sec.gov/x\n")
    r = runner.invoke(app, ["apply-proposals", "p.csv"])
    assert r.exit_code == 0 and "Applied 2 new fact(s) to 1 account(s)" in r.output
    assert "Applied 0 new fact(s)" in runner.invoke(app, ["apply-proposals", "p.csv"]).output


def test_empty_steps_are_not_errors_when_allowed(cli_env):
    for cmd in (["scan"], ["score"], ["enrich"]):
        assert runner.invoke(app, cmd).exit_code == 1                       # without the flag they still fail loudly
    for cmd in (["scan", "--allow-empty"], ["score", "--allow-empty"], ["enrich", "--allow-empty"]):
        r = runner.invoke(app, cmd)
        assert r.exit_code == 0, (cmd, r.output)


def test_scan_limit_counts_due_accounts_and_never_scanned_go_first(cli_env):
    from bd.db import connect, init_db
    c = connect(cli_env / "c.sqlite")
    init_db(c)
    for i in range(30):                                                      # the first 25 were scanned yesterday, the rest never
        add_account(c, f"Co{i:02d}", last_scanned=(date.today() - timedelta(days=1)).isoformat() if i < 25 else None, tier="A")
    c.close()
    r = runner.invoke(app, ["scan", "--limit", "5", "--dry-run", "--scanners", "leadership"])
    assert r.exit_code == 0 and r.output.count("would scan") == 5
    assert all(f"Co{i}" in r.output for i in range(25, 30))                  # the five never-scanned companies, not the first five by id


def test_check_command_explains_failures_and_reports_success(cli_env, monkeypatch):
    import bd.llm as llm
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")

    class Bad:
        def __init__(self, *a, **k): pass
        def run(self, *a, **k):
            raise llm.LLMError("BadRequestError: Error code: 400 - This API key is not scoped to a workspace")
    monkeypatch.setattr(llm, "AnthropicWebSearchClient", Bad)
    r = runner.invoke(app, ["check"])
    assert r.exit_code == 1 and "not tied to a workspace" in r.output and "ANTHROPIC_WORKSPACE_ID" in r.output

    class Good(Bad):
        def run(self, *a, **k):
            return llm.LLMResult(text="OK")
    monkeypatch.setattr(llm, "AnthropicWebSearchClient", Good)
    import bd.verify as vmod

    class Fetch:
        result = vmod.VerifyResult(True, "ok", status=200)
        def __init__(self, *a, **k): pass
        def verify(self, url): return Fetch.result
        def close(self): pass
    monkeypatch.setattr(vmod, "UrlVerifier", Fetch)
    r = runner.invoke(app, ["check"])
    assert r.exit_code == 0 and "Connection OK" in r.output and "BD_USER_AGENT is not set" in r.output
    Fetch.result = vmod.VerifyResult(False, "bad_request_header (check BD_USER_AGENT)")
    r = runner.invoke(app, ["check"])
    assert r.exit_code == 1 and "fetching web pages fails" in r.output and "BD_USER_AGENT" in r.output
    monkeypatch.delenv("ANTHROPIC_MODEL")
    assert runner.invoke(app, ["check"]).exit_code == 1


def test_error_explanations_and_static_problems(cfg):
    assert "workspace" in explain_error("This API key is not scoped to a workspace")
    assert "rejected" in explain_error("authentication_error invalid x-api-key")
    assert "could not reach Anthropic" in explain_error("APIConnectionError: Connection error.")
    assert "model name" in explain_error("not_found_error model: nope")
    assert "credit" in explain_error("Your credit balance is too low")
    assert explain_error("weird thing").startswith("unexpected error")
    assert len(static_problems(cfg, {})) == 3
    assert static_problems(cfg, {"ANTHROPIC_API_KEY": "k", "ANTHROPIC_MODEL": "claude-sonnet-5-5", "BD_USER_AGENT": "x y@z.com"}) == []
    assert "no price" in static_problems(cfg, {"ANTHROPIC_API_KEY": "k", "ANTHROPIC_MODEL": "claude-haiku-5-5", "BD_USER_AGENT": "u"})[0]


def test_budget_env_overrides_the_config_cap(monkeypatch):
    from bd.config import load_config
    monkeypatch.delenv("BD_MAX_COST_USD", raising=False)
    base = load_config(ROOT / "config.yaml").llm.max_cost_per_run_usd
    monkeypatch.setenv("BD_MAX_COST_USD", "1.25")
    assert load_config(ROOT / "config.yaml").llm.max_cost_per_run_usd == 1.25
    monkeypatch.setenv("BD_MAX_COST_USD", "junk")
    assert load_config(ROOT / "config.yaml").llm.max_cost_per_run_usd == base


# ---- settings and the Home page ---------------------------------------------------------------------------------------
def test_update_settings_defaults_validation_and_audit(tmp_path):
    c = make_db(tmp_path)
    assert load_update(c) == UPDATE_DEFAULTS
    assert save_update(c, {"research": "10", "segments": "1", "scan": "15", "budget": "5"}) == []
    assert load_update(c) == {"research": 10, "segments": 1, "scan": 15, "budget": 5, "rfps": 0}
    for bad in ({"research": "999", "segments": "1", "scan": "15", "budget": "5"}, {"research": "1", "segments": "1", "scan": "0", "budget": "5"},
                {"research": "1", "segments": "1", "scan": "1", "budget": ""}):
        assert save_update(c, bad)
    assert load_update(c)["scan"] == 15
    assert c.execute("SELECT COUNT(*) FROM audit_log WHERE field = 'settings.update'").fetchone()[0] == 1


@pytest.fixture
def app_env(cfg, tmp_path, monkeypatch):
    from bd.ui.app import App
    from bd.ui.auth import Hosted
    from tests.test_hosted import call
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-1234567890")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
    monkeypatch.setenv("BD_USER_AGENT", "SEI Chicago BD ops@example.com")
    conn = make_db(tmp_path)
    db = str(tmp_path / "t.sqlite")
    exe = tmp_path / "ok.sh"
    exe.write_text("#!/usr/bin/env bash\necho \"ARGS: $*\"\n")
    exe.chmod(0o755)
    app = App(cfg, db, root=tmp_path, hosted=Hosted(PW), jobs=JobRunner(db, tmp_path, [str(exe)]))
    app.fail_delay = 0
    ck = call(app, "POST", "/login", {"password": PW}).h["Set-Cookie"].split(";")[0].split("=", 1)[1]
    get = lambda path: call(app, "GET", path, cookie=ck)  # noqa: E731
    post = lambda path, data=None, csrf=True: call(app, "POST", path, {**(data or {}), **({"csrf": app._csrf} if csrf else {})}, cookie=ck)  # noqa: E731
    return type("E", (), {"app": app, "conn": conn, "get": staticmethod(get), "post": staticmethod(post), "cfg": cfg, "tmp": tmp_path})


def test_home_says_what_to_do_next_in_each_state(app_env, monkeypatch):
    e = app_env
    assert "no companies yet" in e.get("/").body and "Press Update my list" in e.get("/").body
    add_account(e.conn, "Needco", status="hold_review", chicagoland="unknown", revenue=None, ownership=None)
    body = e.get("/").body
    assert "1 companies are waiting for facts" in body and "1 need facts" in body
    add_account(e.conn, "Readyco")
    assert "1 companies are ready" in e.get("/").body and "scans them" in e.get("/").body
    add_signal(e.conn, e.cfg, 2, "leadership_change", 5)
    add_signal(e.conn, e.cfg, 2, "hiring_cluster", 5)
    from bd.score_store import score_accounts
    score_accounts(e.conn, e.cfg, date.today())
    body = e.get("/").body
    assert "Open Opportunities" in body and "Top opportunities" in body and "Readyco" in body
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    body = e.get("/").body
    assert "Finish setup" in body and "ANTHROPIC_API_KEY is not set" in body


def test_pressing_update_needs_the_tick_uses_saved_numbers_and_opens_the_job(app_env):
    e = app_env
    r = e.post("/update")
    assert r.code == 303 and r.h["Location"] == "/"
    assert "confirm" in e.get("/").body
    assert e.post("/settings/update", {"research": "7", "segments": "1", "scan": "9", "budget": "4"}).code == 303
    assert "Update my list" in e.get("/settings").body and 'value="9"' in e.get("/settings").body
    r = e.post("/update", {"confirm": "1"})
    assert r.code == 303 and r.h["Location"].startswith("/jobs/")
    e.app.jobs.wait()
    job = e.app.jobs.recent()[0]
    assert job.kind == "update" and job.args == ["update", "--research", "7", "--segments", "1", "--scan", "9", "--budget", "4", "--rfps", "0"]
    body = e.get(r.h["Location"]).body
    assert "ARGS: enrich --limit 7" in body and "ARGS: scan --limit 9" in body
    assert e.post("/update", csrf=False).code == 403
    assert e.post("/settings/update", {"research": "x"}).code == 303 and "Not saved" in e.get("/settings").body


def test_test_connection_button_runs_a_check_job(app_env):
    e = app_env
    r = e.post("/check")
    assert r.code == 303
    e.app.jobs.wait()
    assert e.app.jobs.recent()[0].kind == "check"


def test_a_failed_update_shows_why_on_home(app_env):
    e = app_env
    with e.app.jobs._conn() as c:
        c.execute("INSERT INTO jobs(kind,args_json,status,created,started,finished,exit_code,note) VALUES "
                  "('update','[]','failed','2026-10-16 10:00:00','2026-10-16 10:00:00','2026-10-16 10:00:05',1,'`bd check` exited with 1; later steps were not run.')")
    (e.app.jobs.log_dir / "1.log").write_text("$ bd check\nThe model connection failed: your API key is not tied to a workspace.\n")
    body = e.get("/").body
    assert "Last update" in body and "later steps were not run" in body and "not tied to a workspace" in body


def test_navigation_is_short_and_local_mode_is_unchanged(app_env, cfg, tmp_path):
    body = app_env.get("/").body
    nav = body.split("<nav>")[1].split("</nav>")[0]
    assert nav.count('<a class="top') + nav.count('<div class="menu ') == 5
    for label in ("Home", "Opportunities", "Companies", "Hiring", "More", "Advanced tools"):
        assert label in nav
    from bd.ui.app import App
    local = App(cfg, str(tmp_path / "l.sqlite"), "tok", root=tmp_path)
    local.port = 8765
    env = {"REQUEST_METHOD": "GET", "PATH_INFO": "/", "QUERY_STRING": "", "HTTP_HOST": "127.0.0.1:8765", "HTTP_COOKIE": "bd_session=tok",
           "CONTENT_LENGTH": "0", "wsgi.input": __import__("io").BytesIO()}
    out = {}
    page = b"".join(local(env, lambda s, h: out.update(s=s))).decode()
    assert out["s"].startswith("200") and "Opportunities" in page and "Update my list" not in page


def test_user_agent_is_cleaned_into_a_legal_header():
    from bd.config import DEFAULT_USER_AGENT, clean_user_agent
    assert clean_user_agent(" YourCo BD (a@b.com)\n") == "YourCo BD (a@b.com)"
    assert clean_user_agent('"YourCo BD (a@b.com)"') == "YourCo BD (a@b.com)"
    assert clean_user_agent("YourCo \u2014 a@b.com\r\nX: y") == "YourCo ? a@b.com X: y"
    assert clean_user_agent("  \n") == DEFAULT_USER_AGENT and clean_user_agent(None) == DEFAULT_USER_AGENT


def test_messy_user_agent_is_flagged_and_real_requests_work(cfg):
    import http.server, threading
    from bd.setup_check import static_problems
    from bd.verify import UrlVerifier
    env = {"ANTHROPIC_API_KEY": "k", "ANTHROPIC_MODEL": "claude-sonnet-5-5", "BD_USER_AGENT": " YourCo (a@b.com)\n"}
    assert any("stray spaces" in p for p in static_problems(cfg, env))
    assert any("contact email" in p for p in static_problems(cfg, {**env, "BD_USER_AGENT": "YourCo"}))
    assert not static_problems(cfg, {**env, "BD_USER_AGENT": "YourCo (a@b.com)"})

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self): self.send_response(200); self.end_headers()
        def log_message(self, *a): pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}/"
    try:
        c = cfg.model_copy(deep=True); c.verify.block_private_addresses = False
        bad = UrlVerifier(c, " YourCo (a@b.com)\n")                  # raw, uncleaned: the failure seen live
        assert bad.verify(url).reason.startswith("bad_request_header")
        from bd.config import clean_user_agent
        good = UrlVerifier(c, clean_user_agent(" YourCo (a@b.com)\n"))
        assert good.verify(url).ok
    finally:
        srv.shutdown()


def test_search_depth_follows_the_account_tier(cfg):
    from bd.llm import AnthropicWebSearchClient
    c = AnthropicWebSearchClient(cfg, model="claude-sonnet-5-5", client=object())
    assert c._tools(None)[0]["max_uses"] == cfg.llm.web_search_max_uses
    assert c._tools(None, 3)[0]["max_uses"] == 3
    assert cfg.scan.tiers["A"].max_searches > cfg.scan.tiers["B"].max_searches
    from bd.scanners.leadership import LeadershipScanner
    from bd.accounts import Account
    sc = LeadershipScanner(cfg, None, None)
    a = Account(id=1, name="X", tier="B")
    assert sc._call_kwargs(a)["max_uses"] == cfg.scan.tiers["B"].max_searches


def test_the_rfp_search_can_be_switched_off_for_an_update(app_env):
    from bd.jobs import sequence_for
    on = validate("update", {**UPD, "rfps": "1"})
    off = validate("update", {**UPD, "rfps": "0"})
    assert validate("update", UPD)[-2:] == ["--rfps", "0"]                      # off unless asked for: it has found nothing so far
    assert on[-2:] == ["--rfps", "1"] and off[-2:] == ["--rfps", "0"] and "rfps" in [s[0] for s in sequence_for("update", on, "2026-10-16")]
    assert "rfps" not in [s[0] for s in sequence_for("update", off, "2026-10-16")]
    assert [s[0] for s in sequence_for("update", off, "2026-10-16")][-2:] == ["scan", "score"]
    with pytest.raises(JobError):
        validate("update", {**UPD, "rfps": "2"})
    e = app_env
    assert "open public RFPs" not in e.get("/").body and 'name="rfps"' in e.get("/settings").body
    assert e.post("/settings/update", {"research": "7", "segments": "1", "scan": "9", "budget": "4", "rfps": "1"}).code == 303
    assert "open public RFPs" in e.get("/").body
    assert e.post("/settings/update", {"research": "7", "segments": "1", "scan": "9", "budget": "4", "rfps": "0"}).code == 303
    assert "open public RFPs" not in e.get("/").body and 'name="rfps"' in e.get("/settings").body
    e.post("/update", {"confirm": "1"})
    e.app.jobs.wait()
    assert e.app.jobs.recent()[0].args[-2:] == ["--rfps", "0"]
    assert save_update(e.conn, {"research": "7", "segments": "1", "scan": "9", "budget": "4"}) == [] and load_update(e.conn)["rfps"] == 0   # an old form keeps the default
