import os
import re
import stat
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import yaml
from typer.testing import CliRunner

from bd.cli import _today, app
from tests.conftest import ROOT

SCRIPTS = ROOT / "scripts"
runner = CliRunner()


def sh(args, env=None, cwd=ROOT, check=False):
    e = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp"), **(env or {})}
    return subprocess.run(args, env=e, cwd=cwd, capture_output=True, text=True, check=check)


# ---- weekly.sh ----------------------------------------------------------------------------------

@pytest.fixture
def fakebd(tmp_path):
    log = tmp_path / "calls.log"
    exe = tmp_path / "fake-bd"
    exe.write_text(f'#!/usr/bin/env bash\necho "$*" >> {log}\n[ "$1" = "${{FAIL_ON:-none}}" ] && exit 3\nexit 0\n')
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return exe, log


def weekly(fakebd, **env):
    exe, _ = fakebd
    return sh([str(SCRIPTS / "weekly.sh")], {"ANTHROPIC_API_KEY": "k", "ANTHROPIC_MODEL": "m", "BD_BIN": str(exe), **env})


def test_weekly_runs_the_four_steps_in_order_with_the_chicago_run_date(fakebd):
    r = weekly(fakebd, BD_AS_OF="2026-10-19")
    assert r.returncode == 0, r.stderr
    calls = fakebd[1].read_text().splitlines()
    assert calls == ["seed accounts.csv --exclusions exclusions.csv",
                     "scan --as-of 2026-10-19", "score --as-of 2026-10-19", "card --as-of 2026-10-19"]


def test_weekly_default_run_date_is_the_chicago_date(fakebd):
    r = weekly(fakebd)
    assert r.returncode == 0
    run_date = fakebd[1].read_text().splitlines()[1].split()[-1]
    assert run_date == datetime.now(ZoneInfo("America/Chicago")).date().isoformat()


def test_weekly_dry_run_flag_reaches_bd_card(fakebd):
    weekly(fakebd, BD_AS_OF="2026-10-19", BD_DRY_RUN="1")
    assert fakebd[1].read_text().splitlines()[-1] == "card --as-of 2026-10-19 --dry-run"
    fakebd[1].unlink()
    weekly(fakebd, BD_AS_OF="2026-10-19", BD_DRY_RUN="0")
    assert "--dry-run" not in fakebd[1].read_text()


@pytest.mark.parametrize("step,later", [("seed", ["scan", "score", "card"]), ("scan", ["score", "card"]),
                                        ("score", ["card"])])
def test_a_failing_step_stops_the_run_so_nothing_is_posted(fakebd, step, later):
    r = weekly(fakebd, BD_AS_OF="2026-10-19", FAIL_ON=step)
    assert r.returncode == 3
    ran = [c.split()[0] for c in fakebd[1].read_text().splitlines()]
    assert ran[-1] == step and not set(later) & set(ran)


@pytest.mark.parametrize("missing", ["ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"])
def test_weekly_refuses_to_start_without_credentials(fakebd, missing):
    env = {"ANTHROPIC_API_KEY": "k", "ANTHROPIC_MODEL": "m", "BD_BIN": str(fakebd[0])}
    env.pop(missing)
    r = sh([str(SCRIPTS / "weekly.sh")], env)
    assert r.returncode != 0 and missing in r.stderr and not fakebd[1].exists()


# ---- chicago_gate.sh ----------------------------------------------------------------------------

@pytest.mark.parametrize("now,expect", [
    ("2026-10-19 10:00 UTC", "run"),    # Monday 05:00 CDT
    ("2026-10-19 10:59 UTC", "run"),    # still inside the 05:00 hour
    ("2026-10-19 11:00 UTC", "skip"),   # 06:00 CDT: the CST cron firing in summer is ignored
    ("2026-10-19 09:59 UTC", "skip"),   # 04:59 CDT
    ("2026-11-02 11:00 UTC", "run"),    # Monday 05:00 CST (after DST ends on Nov 1)
    ("2026-11-02 10:00 UTC", "skip"),   # 04:00 CST: the CDT cron firing in winter is ignored
    ("2026-03-09 10:00 UTC", "run"),    # Monday after DST starts (Mar 8): 05:00 CDT
    ("2026-03-02 11:00 UTC", "run"),    # Monday before DST starts: 05:00 CST
    ("2026-10-20 10:00 UTC", "skip"),   # Tuesday
    ("2026-10-18 10:00 UTC", "skip"),   # Sunday
])
def test_gate_lets_exactly_the_chicago_monday_5am_hour_through(now, expect):
    assert sh([str(SCRIPTS / "chicago_gate.sh")], {"GATE_NOW": now}).stdout.strip() == expect


def test_exactly_one_of_the_two_crons_passes_each_monday_all_year():
    from datetime import date, timedelta
    d, passes = date(2026, 1, 5), []
    while d.year == 2026:
        hits = [h for h in (10, 11) if sh([str(SCRIPTS / "chicago_gate.sh")], {"GATE_NOW": f"{d} {h}:00 UTC"}).stdout.strip() == "run"]
        passes.append(len(hits))
        d += timedelta(days=7)
    assert set(passes) == {1}, passes


# ---- state.sh (against a local bare repo standing in for GitHub) -------------------------------------

@pytest.fixture
def remote(tmp_path):
    bare = tmp_path / "srv" / "org" / "repo.git"
    bare.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    env = {"GITHUB_SERVER_URL": f"file://{tmp_path / 'srv'}", "GITHUB_REPOSITORY": "org/repo",
           "GITHUB_TOKEN": "t0ken", "STATE_BRANCH": "bd-state"}
    return bare, env


def test_state_first_run_then_save_restore_roundtrip_with_single_commit_history(tmp_path, remote):
    bare, env = remote
    first = tmp_path / "first"; first.mkdir()
    r = sh([str(SCRIPTS / "state.sh"), "restore"], {**env, "BD_DB_PATH": str(first / "bd.sqlite")})
    assert r.returncode == 0 and "first run" in r.stdout and not (first / "bd.sqlite").exists()

    db = tmp_path / "work" / "bd.sqlite"; db.parent.mkdir()
    db.write_bytes(b"version-1")
    assert sh([str(SCRIPTS / "state.sh"), "save"], {**env, "BD_DB_PATH": str(db)}).returncode == 0
    db.write_bytes(b"version-2")
    assert sh([str(SCRIPTS / "state.sh"), "save"], {**env, "BD_DB_PATH": str(db)}).returncode == 0
    count = subprocess.run(["git", "-C", str(bare), "rev-list", "--count", "bd-state"], capture_output=True, text=True).stdout.strip()
    assert count == "1"                                              # no history growth, no repo bloat

    out = tmp_path / "restored" / "bd.sqlite"; out.parent.mkdir()
    r = sh([str(SCRIPTS / "state.sh"), "restore"], {**env, "BD_DB_PATH": str(out)})
    assert r.returncode == 0 and out.read_bytes() == b"version-2"


def test_restore_refuses_to_continue_when_the_repository_is_unreachable(tmp_path, remote):
    _, env = remote
    env = {**env, "GITHUB_REPOSITORY": "org/does-not-exist", "BD_DB_PATH": str(tmp_path / "bd.sqlite")}
    r = sh([str(SCRIPTS / "state.sh"), "restore"], env)
    assert r.returncode == 1 and "refusing to continue with an empty database" in r.stderr
    assert not (tmp_path / "bd.sqlite").exists()


def test_save_without_a_database_fails_and_token_is_required(tmp_path, remote):
    _, env = remote
    assert sh([str(SCRIPTS / "state.sh"), "save"], {**env, "BD_DB_PATH": str(tmp_path / "missing.sqlite")}).returncode == 1
    env.pop("GITHUB_TOKEN")
    assert sh([str(SCRIPTS / "state.sh"), "restore"], env).returncode != 0


def test_the_token_is_never_printed(tmp_path, remote):
    _, env = remote
    db = tmp_path / "bd.sqlite"; db.write_bytes(b"x")
    r = sh([str(SCRIPTS / "state.sh"), "save"], {**env, "BD_DB_PATH": str(db)})
    import base64
    b64 = base64.b64encode(b"x-access-token:t0ken").decode()
    assert "t0ken" not in r.stdout + r.stderr
    assert r.stdout.count(b64) == 1 and "::add-mask::" + b64 in r.stdout     # only inside the mask directive


# ---- the workflow file ------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def wf():
    return yaml.safe_load((ROOT / ".github" / "workflows" / "weekly.yml").read_text())


def test_workflow_schedule_covers_both_utc_hours_and_nothing_else_triggers_it(wf):
    on = wf[True]            # PyYAML reads the bare key `on` as boolean True
    assert {s["cron"] for s in on["schedule"]} == {"0 10 * * 1", "0 11 * * 1"}
    assert set(on) == {"schedule", "workflow_dispatch"}                      # no push / pull_request triggers
    assert on["workflow_dispatch"]["inputs"]["dry_run"]["default"] is True    # manual runs are dry by default


def test_workflow_serialises_runs_and_limits_permissions(wf):
    assert wf["concurrency"] == {"group": "bd-weekly", "cancel-in-progress": False}
    assert wf["permissions"] == {"contents": "write"}
    assert wf["jobs"]["run"]["timeout-minutes"] <= 120


def test_secrets_are_scoped_to_the_single_step_that_needs_them(wf):
    steps = wf["jobs"]["run"]["steps"]
    holders = [s["name"] for s in steps if "secrets." in yaml.dump(s)]
    assert holders == ["Weekly run (seed, scan, score, card)"]
    weekly_env = next(s for s in steps if s.get("id") == "weekly")["env"]
    assert set(weekly_env) == {"ANTHROPIC_API_KEY", "TEAMS_WEBHOOK_URL", "ANTHROPIC_MODEL", "BD_USER_AGENT", "BD_DRY_RUN"}
    assert "vars.ANTHROPIC_MODEL" in weekly_env["ANTHROPIC_MODEL"]            # the model name is a variable, not a secret
    text = (ROOT / ".github" / "workflows" / "weekly.yml").read_text()
    assert "pull_request_target" not in text and "echo ${{ secrets" not in text


def test_checkout_does_not_leave_a_write_token_in_git_config(wf):
    checkout = wf["jobs"]["run"]["steps"][0]
    assert checkout["uses"].startswith("actions/checkout@") and checkout["with"]["persist-credentials"] is False


def test_gate_runs_the_tested_script_and_everything_after_it_honours_it(wf):
    steps = wf["jobs"]["run"]["steps"]
    gate = next(s for s in steps if s.get("id") == "gate")
    assert "scripts/chicago_gate.sh" in gate["run"]
    after = steps[steps.index(gate) + 1:]
    for s in after:
        if s.get("name") == "Save database":
            continue
        assert "steps.gate.outputs.decision == 'run'" in s["if"], s


def test_database_is_saved_only_after_a_successful_restore(wf):
    save = next(s for s in wf["jobs"]["run"]["steps"] if s.get("name") == "Save database")
    assert save["if"] == "always() && steps.restore.outcome == 'success'"


def test_run_date_is_chicago_local_by_default():
    assert _today() == datetime.now(ZoneInfo("America/Chicago")).date()


# ---- bd scan exit code ---------------------------------------------------------------------------------

def test_scan_exits_3_when_every_call_failed(tmp_path, monkeypatch):
    from bd.runner import RunSummary
    from datetime import date
    monkeypatch.setenv("BD_DB_PATH", str(tmp_path / "t.sqlite"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_MODEL", "m")
    p = tmp_path / "a.csv"
    p.write_text("name,domain,hq_city,chicagoland,revenue,employees,ownership,sponsor,industry,tier,cik\n"
                 "Acme,acme.com,Chicago,yes,500000000,,public,,Industrial,A,\n")
    assert runner.invoke(app, ["seed", str(p)]).exit_code == 0
    monkeypatch.setattr("bd.cli._build_scanners", lambda *a, **k: [])
    monkeypatch.setattr("bd.runner.run_scans", lambda *a, **k: RunSummary(date(2026, 10, 19), calls=4, errors=4))
    r = runner.invoke(app, ["scan", "--scanners", "leadership"])
    assert r.exit_code == 3 and "Every scan call failed" in r.output
    monkeypatch.setattr("bd.runner.run_scans", lambda *a, **k: RunSummary(date(2026, 10, 19), calls=4, errors=3))
    assert runner.invoke(app, ["scan", "--scanners", "leadership"]).exit_code == 0     # partial failure still proceeds
    monkeypatch.setattr("bd.runner.run_scans", lambda *a, **k: RunSummary(date(2026, 10, 19), calls=0, errors=0))
    assert runner.invoke(app, ["scan", "--scanners", "leadership"]).exit_code == 0     # nothing due is not a failure
