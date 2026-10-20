import pytest
from typer.testing import CliRunner

import bd.sources
from bd.cli import app
from bd.db import connect, init_db
from bd.jobs import JobError, validate
from bd.pe_store import ensure_firm
from tests.conftest import ROOT
from tests.pe_fakes import Clock, FakeClient, OpenHosts, page
from tests.test_pe_collect import A, listing

runner = CliRunner()


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("BD_DB_PATH", str(tmp_path / "t.sqlite"))
    monkeypatch.setenv("BD_CONFIG", str(ROOT / "config.yaml"))
    monkeypatch.setenv("BD_USER_AGENT", "SEI Test ops@example.com")
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"):
        monkeypatch.delenv(k, raising=False)


def add_firm(tmp_path, url=A):
    conn = connect(tmp_path / "t.sqlite")
    init_db(conn)
    with conn:
        ensure_firm(conn, "Alpha Capital", "manual", "test", "alphacap.example", None, url)
    return conn


def fake_fetcher(monkeypatch, routes):
    real = bd.sources.Fetcher
    clock = Clock()

    def make(cfg, ua):
        cfg = cfg.model_copy(deep=True)
        cfg.pe_portfolio.fetch.respect_robots = False
        cfg.pe_portfolio.fetch.min_seconds_between_requests = 0
        return real(cfg, ua, client=FakeClient(routes), verifier=OpenHosts(), sleep=clock.sleep, monotonic=clock.monotonic)
    monkeypatch.setattr(bd.sources, "Fetcher", make)


def test_the_command_needs_a_declared_contact(monkeypatch):
    monkeypatch.delenv("BD_USER_AGENT")
    r = runner.invoke(app, ["pe-collect"])
    assert r.exit_code == 1 and "BD_USER_AGENT" in r.output


def test_with_no_firms_it_says_so_and_invents_nothing(tmp_path):
    r = runner.invoke(app, ["pe-collect"])
    assert r.exit_code == 0 and "No portfolio pages are configured" in r.output and "Nothing was invented" in r.output


def test_it_collects_and_reports(tmp_path, monkeypatch):
    conn = add_firm(tmp_path)
    fake_fetcher(monkeypatch, {A: listing()})
    r = runner.invoke(app, ["pe-collect"])
    assert r.exit_code == 0, r.output
    assert "1 read (1 complete" in r.output and "6 entries" in r.output and "6 new relationship(s)" in r.output
    assert conn.execute("SELECT COUNT(*) FROM pe_ownership WHERE status = 'current'").fetchone()[0] == 3
    again = runner.invoke(app, ["pe-collect"])
    assert "1 not re-read (recent)" in again.output and "0 new relationship(s)" in again.output


def test_when_every_source_fails_it_exits_non_zero_and_says_what_is_kept(tmp_path, monkeypatch):
    add_firm(tmp_path)
    fake_fetcher(monkeypatch, {A: page("denied", 403)})
    r = runner.invoke(app, ["pe-collect"])
    assert r.exit_code == 3 and "FAILED" in r.output and "http_403" in r.output and "kept and shown as stale" in r.output


def test_the_job_is_whitelisted_and_needs_no_paid_confirmation():
    from bd.jobs import KINDS
    assert validate("pe_collect", {}) == ["pe-collect"]
    assert validate("pe_collect", {"firm": "Alpha", "force": "1"}) == ["pe-collect", "--firm", "Alpha", "--force"]
    assert KINDS["pe_collect"][1] is False and KINDS["pe_collect"][2] is False                # free; can be run by a person, not only on a schedule
    with pytest.raises(JobError):
        validate("pe_collect", {"firm": "x" * 200})
