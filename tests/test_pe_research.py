"""Capped private-equity research: the limit holds, each step gets its share, the run says why it stopped and what it left, and nothing is spent when the limit is
gone. The three paid steps are replaced by fakes that write cost rows the way the real ones do, so what is tested is the budget logic. Rows are synthetic."""

import json
from datetime import date

import pytest
from typer.testing import CliRunner

from bd import pe_research
from bd.cli import app
from bd.discover import DiscoverSummary
from bd.pe_deals import DealsSummary
from bd.pe_promote import promotable
from bd.pe_research import SHARES, deal_firms, portfolio_firms, record_run, research_cap, run_research
from bd.pe_store import ensure_firm
from bd.pe_view import latest_research
from tests.conftest import add_account, make_db
from tests.test_pe_promote import holding

TODAY = date(2026, 10, 21)


@pytest.fixture
def cfg(cfg):
    return cfg.model_copy(deep=True)


@pytest.fixture
def conn(tmp_path):
    return make_db(tmp_path)


def cost_row(conn, step, cost):
    with conn:
        conn.execute("INSERT INTO runs(run_date,step,account_id,accounts_scanned,signals_found,tokens,est_cost,errors) VALUES (?,?,?,?,?,?,?,?)",
                     (TODAY.isoformat(), step, None, 1, 0, 100, cost, None))


class Fakes:
    """Stand-ins for run_deals, run_promote and run_portfolio that spend `cost` a call and make the same changes the real ones do."""

    def __init__(self, cost=0.30, monkeypatch=None):
        self.cost, self.deals, self.promotes, self.portfolios, self.max_candidates = cost, [], [], [], []
        monkeypatch.setattr(pe_research, "run_deals", self.run_deals)
        monkeypatch.setattr(pe_research, "run_promote", self.run_promote)
        monkeypatch.setattr(pe_research, "run_portfolio", self.run_portfolio)

    def run_deals(self, conn, cfg, researcher, firms, as_of, model, workers=None, max_cost=None, log=lambda s: None):
        self.deals.append(len(firms))
        for f in firms:
            cost_row(conn, "pe_deals", self.cost)
            with conn:
                conn.execute("UPDATE pe_firms SET last_deals = ? WHERE id = ?", (as_of.isoformat(), f["id"]))
        return DealsSummary(firms=len(firms))

    def run_promote(self, conn, cfg, verifier, enricher, as_of, model, limit, workers=None, max_cost=None, log=lambda s: None):
        out = DiscoverSummary()
        rows = promotable(conn, cfg, limit, researchable_only=True)
        self.promotes.append(len(rows))
        for c, _firm in rows:
            cost_row(conn, "discover_enrich", self.cost)
            aid = add_account(conn, c["name"], domain=c["domain"])
            with conn:
                conn.execute("UPDATE pe_companies SET account_id = ? WHERE id = ?", (aid, c["id"]))
            out.researched += 1
            out.added.append(c["name"])
        return out

    def run_portfolio(self, conn, cfg, finder, enricher, firms, as_of, model, workers=None, max_cost=None, log=lambda s: None):
        self.portfolios.append(len(firms))
        self.max_candidates.append(cfg.discovery.max_candidates_per_run)
        for f in firms:
            cost_row(conn, "pe_portfolio", self.cost)
            for _ in range(min(cfg.discovery.max_candidates_per_run, 2)):
                cost_row(conn, "discover_enrich", self.cost)
            with conn:
                conn.execute("UPDATE pe_firms SET last_portfolio = ? WHERE id = ?", (as_of.isoformat(), f["id"]))
        return DiscoverSummary(segments=len(firms))


def world(conn, cfg, firms=10, companies=5):
    with conn:
        fids = [ensure_firm(conn, f"Chicago Fund {i:02d}", "manual", "t", f"cf{i}.example")[0] for i in range(firms)]
        conn.execute("UPDATE pe_firms SET scope = 'chicago'")
    for i in range(companies):
        holding(conn, cfg, fids[0], f"Local Co {i}", f"localco{i}.example", "IL", "Naperville")
    return fids


def go(conn, cfg, pe_budget, update_left=None, **kw):
    return run_research(conn, cfg, None, None, None, None, "claude-sonnet-5-5", TODAY, pe_budget, update_left, **kw)


# ---- the cap -------------------------------------------------------------------------------------------------------------------------------------------------
def test_the_smaller_of_the_pe_limit_and_what_is_left_of_the_update_limit_is_the_cap():
    assert research_cap(5, None) == (5.0, "pe_cap") and research_cap(5, 8) == (5.0, "pe_cap") and research_cap(5, 5) == (5.0, "pe_cap")
    assert research_cap(5, 2.5) == (2.5, "update_limit") and research_cap(5, 0) == (0.0, "update_limit") and research_cap(5, -1) == (0.0, "update_limit")


def test_the_limit_holds_and_the_run_says_it_stopped_at_the_pe_limit_and_what_it_left(conn, cfg, monkeypatch):
    fakes = Fakes(0.30, monkeypatch)
    world(conn, cfg, firms=10, companies=5)
    rep = go(conn, cfg, 1.0)
    assert rep.spent <= 1.0 and rep.spent == pytest.approx(0.30 * (sum(fakes.deals) + sum(fakes.promotes) + sum(fakes.portfolios)) +
                                                           0.30 * 2 * len(fakes.portfolios))
    assert rep.stopped_by == "pe_cap" and rep.binder == "pe_cap" and any(s.left for s in rep.steps)
    line = rep.line()
    assert "stopped at the $1.00 PE research limit" in line and "left" in line and f"${rep.spent:.2f}" in line


def test_when_the_update_limit_is_smaller_that_is_what_the_run_says_stopped_it(conn, cfg, monkeypatch):
    Fakes(0.30, monkeypatch)
    world(conn, cfg)
    rep = go(conn, cfg, 5.0, update_left=0.5)
    assert rep.cap == 0.5 and rep.stopped_by == "update_limit" and rep.spent <= 0.5 + 1e-9 and "what was left of the Update's spending limit" in rep.line()


def test_with_nothing_left_of_the_limit_nothing_is_spent(conn, cfg, monkeypatch):
    fakes = Fakes(0.30, monkeypatch)
    world(conn, cfg)
    rep = go(conn, cfg, 5.0, update_left=0.0)
    assert rep.spent == 0 and fakes.deals == fakes.promotes == fakes.portfolios == [] and rep.stopped_by == "update_limit" and any(s.left for s in rep.steps)


def test_a_generous_limit_finishes_everything_due_and_a_second_run_finds_nothing_due(conn, cfg, monkeypatch):
    fakes = Fakes(0.10, monkeypatch)
    world(conn, cfg, firms=3, companies=2)
    rep = go(conn, cfg, 50.0)
    assert rep.stopped_by == "done" and not any(s.left for s in rep.steps) and sum(fakes.deals) == 3 and sum(fakes.promotes) == 2
    assert [s.step for s in rep.steps] == ["deals", "companies", "portfolios"] and rep.steps[1].added == 2
    again = go(conn, cfg, 50.0)
    assert again.stopped_by == "done" and again.spent == 0 and [s.did for s in again.steps] == [0, 0, 0]          # cadence: nobody is due again so soon


def test_each_step_gets_its_share_so_deals_cannot_starve_the_companies(conn, cfg, monkeypatch):
    Fakes(0.50, monkeypatch)
    world(conn, cfg, firms=40, companies=3)
    rep = go(conn, cfg, 10.0)
    deals, companies = rep.steps[0], rep.steps[1]
    assert deals.left > 0 and companies.did > 0                                       # deals did not finish, yet the companies were still researched
    assert deals.spent <= 10.0 * SHARES["deals"] + 0.50 and rep.spent <= 10.0


def test_a_portfolio_search_is_capped_so_the_find_call_and_the_research_it_leads_to_fit_the_limit(conn, cfg, monkeypatch):
    fakes = Fakes(0.30, monkeypatch)
    world(conn, cfg, firms=2, companies=0)
    with conn:
        conn.execute("UPDATE pe_firms SET last_deals = ?", (TODAY.isoformat(),))              # deals are not due: the budget is for portfolios
    rep = go(conn, cfg, 1.5)
    assert fakes.portfolios and all(1 <= n <= 5 for n in fakes.max_candidates) and rep.spent <= 1.5
    tiny = go(conn, cfg.model_copy(deep=True), 0.4)
    assert tiny.steps[2].did == 0 or tiny.spent <= 0.4 + 0.30                                  # fewer than two calls fit: the find call is not made for nothing


# ---- who is due ---------------------------------------------------------------------------------------------------------------------------------------------
def test_chicago_firms_come_first_and_an_investor_only_firm_is_researched_only_when_it_holds_a_company_on_the_scan_list(conn, cfg):
    with conn:
        inv = ensure_firm(conn, "Big Investor", "manual", "t", "big.example")[0]
        held = ensure_firm(conn, "Holding Investor", "manual", "t", "hold.example")[0]
        chi = ensure_firm(conn, "Chicago Fund", "manual", "t", "chi.example")[0]
        conn.execute("UPDATE pe_firms SET scope = 'investor' WHERE id IN (?, ?)", (inv, held))
        conn.execute("UPDATE pe_firms SET scope = 'chicago' WHERE id = ?", (chi,))
    add_account(conn, "On List Co", domain="onlist.example")
    holding(conn, cfg, held, "On List Co", "onlist.example")
    with conn:
        conn.execute("UPDATE pe_companies SET account_id = (SELECT id FROM accounts WHERE name = 'On List Co') WHERE name = 'On List Co'")
    names = [f["name"] for f in deal_firms(conn, TODAY)]
    assert names == ["Chicago Fund", "Holding Investor"] and "Big Investor" not in names
    assert [f["name"] for f in portfolio_firms(conn, TODAY)] == ["Chicago Fund", "Holding Investor"]


def test_a_firm_whose_portfolio_page_was_read_in_full_is_not_searched_for_its_portfolio(conn, cfg):
    fids = world(conn, cfg, firms=2, companies=0)
    with conn:
        conn.execute("INSERT INTO pe_sources(firm_id, url, kind, status, coverage, origin, last_success, entries) VALUES (?, 'https://cf0.example/portfolio', 'portfolio_page', 'ok', 'complete', "
                     "'manual', '2026-10-20T00:00:00+00:00', 5)", (fids[0],))
    assert [f["name"] for f in portfolio_firms(conn, TODAY)] == ["Chicago Fund 01"]


# ---- the record ---------------------------------------------------------------------------------------------------------------------------------------------
def test_a_run_is_recorded_with_its_cap_spend_and_reason_and_read_back_for_the_page_and_the_job_note(conn, cfg, monkeypatch):
    Fakes(0.30, monkeypatch)
    world(conn, cfg, firms=6, companies=2)
    assert latest_research(conn) is None
    rep = go(conn, cfg, 1.0)
    record_run(conn, rep, "2026-10-21T10:00:00+00:00")
    got = latest_research(conn)
    assert got["stopped_by"] == "pe_cap" and got["cap"] == 1.0 and got["spent"] == pytest.approx(rep.spent, abs=1e-4) and got["summary"] == rep.line()
    assert [s["step"] for s in got["steps"]] == ["deals", "companies", "portfolios"]
    assert conn.execute("SELECT COUNT(*) FROM pe_runs WHERE kind = 'collect'").fetchone()[0] == 0           # research rows never appear as a collection run


def test_the_job_note_carries_the_research_line_only_for_a_run_that_happened_during_the_job(conn, cfg, tmp_path):
    from bd.jobs import JobRunner
    db = str(tmp_path / "t.sqlite")
    runner = JobRunner(db, tmp_path, ["true"])
    before = runner._max_pe_run_id()
    assert runner._pe_research_since(before) == ""
    with conn:
        conn.execute("INSERT INTO pe_runs(kind, started, summary, detail) VALUES ('research', '2026-10-21T10:00:00+00:00', 'PE research spent $0.60; stopped at the $1.00 PE research limit.', '{}')")
    assert "stopped at the $1.00 PE research limit" in runner._pe_research_since(before) and runner._pe_research_since(runner._max_pe_run_id()) == ""


# ---- the command -------------------------------------------------------------------------------------------------------------------------------------------
def test_the_command_can_say_who_is_due_without_spending_and_is_soft_without_a_user_agent(conn, cfg, tmp_path, monkeypatch):
    world(conn, cfg, firms=3, companies=2)
    conn.close()
    monkeypatch.setenv("BD_DB_PATH", str(tmp_path / "t.sqlite"))
    monkeypatch.delenv("BD_USER_AGENT", raising=False)
    runner = CliRunner()
    dry = runner.invoke(app, ["pe-research", "--budget", "5", "--dry-run", "--as-of", "2026-10-21"])
    assert dry.exit_code == 0 and "Cap $5.00" in dry.output and "3 firm(s) for deals" in dry.output and "2 company(ies) a page backs" in dry.output
    soft = runner.invoke(app, ["pe-research", "--budget", "5", "--soft"])
    assert soft.exit_code == 0 and "BD_USER_AGENT" in soft.output
    assert runner.invoke(app, ["pe-research", "--budget", "5"]).exit_code != 0


def test_a_cap_smaller_than_one_call_still_lets_the_first_call_through_and_no_more(conn, cfg, monkeypatch):
    fakes = Fakes(0.30, monkeypatch)
    world(conn, cfg, firms=5, companies=0)
    rep = go(conn, cfg, 0.2)
    assert sum(fakes.deals) == 1 and rep.spent == pytest.approx(0.30) and json.loads(rep.to_json())["stopped_by"] == "pe_cap"


def test_the_command_runs_the_capped_steps_records_the_run_and_reads_the_update_limit_from_the_environment(conn, cfg, tmp_path, monkeypatch):
    import bd.cli
    fakes = Fakes(0.30, monkeypatch)
    world(conn, cfg, firms=6, companies=2)
    conn.close()
    monkeypatch.setenv("BD_DB_PATH", str(tmp_path / "t.sqlite"))
    monkeypatch.setenv("BD_USER_AGENT", "SEI Test ops@example.com")
    monkeypatch.setenv("BD_MAX_COST_USD", "0.9")                                                # what the job runner says is left of the Update's limit
    monkeypatch.setattr(bd.cli, "_pe_deps", lambda settings, cfg: (None, None, None, "claude-sonnet-5-5"))
    out = CliRunner().invoke(app, ["pe-research", "--budget", "5", "--as-of", "2026-10-21"])
    assert out.exit_code == 0, out.output
    assert "PE research spent $" in out.output and "stopped at what was left of the Update's spending limit ($0.90)" in out.output and "Left for the next Update:" in out.output
    assert "The Update's own spending limit was used up first." in out.output
    import sqlite3
    c = sqlite3.connect(str(tmp_path / "t.sqlite"))
    c.row_factory = sqlite3.Row
    got = latest_research(c)
    assert got["cap"] == 0.9 and got["binder"] == "update_limit" and got["spent"] <= 0.9 + 1e-9 and fakes.deals


def test_a_failure_to_start_is_reported_and_costs_nothing_in_soft_mode_and_stops_the_command_otherwise(conn, cfg, tmp_path, monkeypatch):
    import bd.cli
    import typer
    world(conn, cfg, firms=2, companies=0)
    conn.close()
    monkeypatch.setenv("BD_DB_PATH", str(tmp_path / "t.sqlite"))
    monkeypatch.setenv("BD_USER_AGENT", "SEI Test ops@example.com")

    def broken(settings, cfg):
        raise typer.Exit(code=2)
    monkeypatch.setattr(bd.cli, "_pe_deps", broken)
    runner = CliRunner()
    soft = runner.invoke(app, ["pe-research", "--budget", "5", "--soft"])
    assert soft.exit_code == 0 and "did not run" in soft.output and "Nothing was spent" in soft.output
    assert runner.invoke(app, ["pe-research", "--budget", "5"]).exit_code != 0
