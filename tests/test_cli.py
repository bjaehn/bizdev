import pytest
from typer.testing import CliRunner

from bd.cli import app
from tests.conftest import ROOT

runner = CliRunner()


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("BD_DB_PATH", str(tmp_path / "t.sqlite"))
    monkeypatch.setenv("BD_CONFIG", str(ROOT / "config.yaml"))
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"):
        monkeypatch.delenv(k, raising=False)


def test_later_stage_commands_are_stubs():
    for cmd in (["score"], ["shortlist"], ["card"]):
        assert runner.invoke(app, cmd).exit_code == 2, cmd


def test_seed_loads_repo_accounts():
    r = runner.invoke(app, ["seed", str(ROOT / "accounts.csv")])
    assert r.exit_code == 0 and "58 inserted" in r.output
    assert "58 held for review" in r.output  # names only: location and size unverified, so nothing is scanned
    assert "0 inserted, 58 updated" in runner.invoke(app, ["seed", str(ROOT / "accounts.csv")]).output


def test_seed_missing_file_and_bad_csv(tmp_path):
    assert runner.invoke(app, ["seed", "nope.csv"]).exit_code == 1
    bad = tmp_path / "b.csv"
    bad.write_text("name,tier\nA,Z\n")
    r = runner.invoke(app, ["seed", str(bad)])
    assert r.exit_code == 1 and "nothing written" in r.output


FIT_CSV = ("name,domain,hq_city,chicagoland,revenue,employees,ownership,sponsor,industry,tier\n"
           "Acme,acme.com,Chicago,yes,500000000,,public,,Industrial,A\n"
           "Beta,beta.com,Chicago,yes,,,,,,B\n"
           "Gamma,gamma.com,Chicago,no,900000000,,private,,Retail,A\n"
           "Weedco,weedco.com,Chicago,yes,900000000,,private,,Cannabis retail,A\n")


def seeded(tmp_path):
    p = tmp_path / "fit.csv"
    p.write_text(FIT_CSV)
    r = runner.invoke(app, ["seed", str(p)])
    assert r.exit_code == 0, r.output
    return r


def test_seed_runs_fit_gate_and_reports_why(tmp_path):
    r = seeded(tmp_path)
    assert "1 pass, 1 held for review, 2 excluded" in r.output
    assert "held: Beta" in r.output and "excluded: Gamma: not in Chicagoland" in r.output
    assert "excluded industry: cannabis" in r.output


def test_seed_with_exclusions_file(tmp_path):
    ex = tmp_path / "ex.csv"
    ex.write_text("name,domain,reason\nAcme,acme.com,client\n")
    p = tmp_path / "fit.csv"
    p.write_text(FIT_CSV)
    r = runner.invoke(app, ["seed", str(p), "--exclusions", str(ex)])
    assert r.exit_code == 0 and "Exclusions: 1 added" in r.output and "0 pass" in r.output
    ex.write_text("name,domain,reason\nAcme,acme.com,because\n")
    assert runner.invoke(app, ["seed", str(p), "--exclusions", str(ex)]).exit_code == 1


def test_scan_dry_run_needs_no_key_and_lists_only_gated_in_accounts(tmp_path):
    seeded(tmp_path)
    r = runner.invoke(app, ["scan", "--dry-run"])
    assert r.exit_code == 0 and "would scan: Acme" in r.output
    assert "Beta" not in r.output.split("due")[1] and "Gamma" not in r.output.split("due")[1]
    assert "1 of 1 accounts due" in r.output and "2 calls" in r.output  # leadership + events
    assert runner.invoke(app, ["scan", "--dry-run", "--tier", "B"]).exit_code == 1  # Beta is held, so none


def test_scan_requires_key_and_model_after_gate(tmp_path):
    seeded(tmp_path)
    r = runner.invoke(app, ["scan"])
    assert r.exit_code == 1 and "ANTHROPIC_API_KEY" in r.output


def test_scan_with_nothing_through_the_gate_and_bad_args(tmp_path):
    r = runner.invoke(app, ["scan", "--dry-run"])
    assert r.exit_code == 1 and "fit gate" in r.output
    assert runner.invoke(app, ["scan", "--tier", "Z"]).exit_code == 1
    assert runner.invoke(app, ["scan", "--scanners", "pe"]).exit_code == 1  # stub: unimplemented
    assert runner.invoke(app, ["scan", "--scanners", "nope"]).exit_code == 1


def test_evaluate_reports_gold_problems_before_needing_a_key(tmp_path):
    r = runner.invoke(app, ["evaluate", "--gold", str(tmp_path / "missing.csv")])
    assert r.exit_code == 1 and "not found" in r.output
    r = runner.invoke(app, ["evaluate", "--gold", str(ROOT / "tests" / "gold.template.csv")])
    assert r.exit_code == 1 and "blank" in r.output
