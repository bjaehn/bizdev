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
    assert "0 inserted, 58 updated" in runner.invoke(app, ["seed", str(ROOT / "accounts.csv")]).output


def test_seed_missing_file_and_bad_csv(tmp_path):
    assert runner.invoke(app, ["seed", "nope.csv"]).exit_code == 1
    bad = tmp_path / "b.csv"
    bad.write_text("name,tier\nA,Z\n")
    r = runner.invoke(app, ["seed", str(bad)])
    assert r.exit_code == 1 and "nothing written" in r.output


def test_scan_requires_key_and_model():
    r = runner.invoke(app, ["scan"])
    assert r.exit_code == 1 and "ANTHROPIC_API_KEY" in r.output


def test_scan_needs_accounts(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_MODEL", "m")
    r = runner.invoke(app, ["scan"])
    assert r.exit_code == 1 and "bd seed" in r.output
    assert runner.invoke(app, ["scan", "--tier", "Z"]).exit_code == 1


def test_evaluate_reports_gold_problems_before_needing_a_key(tmp_path):
    r = runner.invoke(app, ["evaluate", "--gold", str(tmp_path / "missing.csv")])
    assert r.exit_code == 1 and "not found" in r.output
    r = runner.invoke(app, ["evaluate", "--gold", str(ROOT / "tests" / "gold.template.csv")])
    assert r.exit_code == 1 and "blank" in r.output
