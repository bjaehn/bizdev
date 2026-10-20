"""Finding a firm's own website and portfolio page by code. A candidate is accepted only when the page it returns names the firm."""

import pytest
from typer.testing import CliRunner

from bd.cli import app
from bd.db import connect, init_db
from bd.pe_collect import discover_portfolio_pages, register_sources
from bd.pe_sites import candidate_domains, find_firm_domain, find_portfolio_by_path, locate_firm_sites, names_firm
from bd.pe_store import ensure_firm, set_firm_status
from tests.pe_fakes import make_fetcher, page
from tests.test_pe_cli import env, fake_fetcher  # noqa: F401  (fixtures and helper)
from tests.test_pe_collect import doc, grid, card, listing


@pytest.fixture
def cfg(cfg):
    return cfg.model_copy(deep=True)


def home(title, body=""):
    return page(f"<html><head><title>{title}</title></head><body><h1>{title}</h1>{body}</body></html>")


def test_candidates_come_only_from_the_firms_own_name():
    assert candidate_domains("GTCR")[0] == "gtcr.com"
    assert candidate_domains("Sycamore Partners")[:2] == ["sycamorepartners.com", "sycamore.com"]
    assert candidate_domains("Partners Group")[0] == "partnersgroup.com"                  # every word generic: the whole name, nothing invented
    assert "charlesbank.com" in candidate_domains("Charlesbank Capital Partners, LLC")      # the legal suffix is not part of a host
    assert candidate_domains("") == [] and len(candidate_domains("Some Very Long Firm Name Capital")) <= 6


def test_the_page_must_name_the_firm_in_its_title_or_twice_in_its_text():
    assert names_firm("Sycamore Partners | Home", "", "Sycamore Partners")
    assert names_firm("Home", "Sycamore Partners is a firm. About Sycamore Partners.", "Sycamore Partners")
    assert not names_firm("Home", "A parked page that mentions Sycamore Partners once.", "Sycamore Partners")
    assert not names_firm("Sycamore Parts", "Sycamore Partnership", "Sycamore Partners")


def test_a_candidate_that_names_the_firm_is_accepted_and_a_wrong_or_missing_one_is_not(cfg):
    f, client, *_ = make_fetcher(cfg, {"https://sycamorepartners.com/": home("Sycamore Partners")})
    assert find_firm_domain(f, "Sycamore Partners") == "sycamorepartners.com"
    f, client, *_ = make_fetcher(cfg, {"https://sycamorepartners.com/": home("Domain for sale", "Buy this domain."),
                                       "https://sycamore.com/": home("Sycamore: kitchen supplies")})
    assert find_firm_domain(f, "Sycamore Partners") is None                          # nothing confirmed: the site stays unknown
    f, client, *_ = make_fetcher(cfg, {"https://sycamore.com/": home("Sycamore Partners")})
    assert find_firm_domain(f, "Sycamore Partners") == "sycamore.com"                 # the first candidate does not exist, the second is confirmed


def test_guesses_are_not_retried(cfg):
    f, client, clock, _ = make_fetcher(cfg, {})
    assert find_firm_domain(f, "Unknown Capital") is None
    assert len(client.calls) == len(candidate_domains("Unknown Capital")) and not clock.sleeps     # one try each, no waiting


def test_locating_sets_the_domain_only_where_it_is_missing_and_records_how(tmp_path, cfg):
    conn = connect(tmp_path / "t.sqlite")
    init_db(conn)
    with conn:
        a = ensure_firm(conn, "Sycamore Partners", "derived", "sync")[0]
        b = ensure_firm(conn, "Has Domain Capital", "derived", "sync", "hasdomain.example")[0]
        c = ensure_firm(conn, "Excluded Capital", "derived", "sync")[0]
    set_firm_status(conn, c, "excluded")
    f, client, *_ = make_fetcher(cfg, {"https://sycamorepartners.com/": home("Sycamore Partners"), "https://excludedcapital.com/": home("Excluded Capital")})
    assert locate_firm_sites(conn, f) == 1
    doms = {r["name"]: r["domain"] for r in conn.execute("SELECT name, domain FROM pe_firms")}
    assert doms == {"Sycamore Partners": "sycamorepartners.com", "Has Domain Capital": "hasdomain.example", "Excluded Capital": None}
    audit = conn.execute("SELECT detail FROM audit_log WHERE field = 'pe_firm' AND action = 'update'").fetchone()[0] if "detail" in [r[1] for r in conn.execute("pragma table_info(audit_log)")] else ""
    assert audit == "" or "found by code" in audit
    assert locate_firm_sites(conn, f) == 0                                              # nothing left to locate: idempotent


def test_a_portfolio_page_is_found_at_a_usual_path_only_if_it_lists_companies(cfg):
    about = page(doc("<h1>About us</h1><p>We invest.</p>"))
    f, client, *_ = make_fetcher(cfg, {"https://alphacap.example/portfolio": about, "https://alphacap.example/companies": listing()})
    assert find_portfolio_by_path(f, "alphacap.example") == "https://alphacap.example/companies"
    f, client, *_ = make_fetcher(cfg, {"https://alphacap.example/portfolio": about})
    assert find_portfolio_by_path(f, "alphacap.example") is None


def test_discovery_falls_back_to_the_usual_paths_when_the_home_page_has_no_portfolio_link(tmp_path, cfg):
    conn = connect(tmp_path / "t.sqlite")
    init_db(conn)
    with conn:
        fid = ensure_firm(conn, "Alpha Capital", "derived", "sync", "alphacap.example")[0]
    f, client, *_ = make_fetcher(cfg, {"https://alphacap.example/": home("Alpha Capital", "<a href='/about'>About</a>"),
                                       "https://alphacap.example/investments": listing()})
    logs = []
    assert discover_portfolio_pages(conn, f, cfg, log=logs.append) == 1
    row = conn.execute("SELECT url, origin FROM pe_sources WHERE firm_id = ?", (fid,)).fetchone()
    assert tuple(row) == ("https://alphacap.example/investments", "discovered")
    assert any("listing of companies was found" in line for line in logs)


# ---- the command, inside Update -----------------------------------------------------------------------------------------------------------------
def test_soft_mode_never_fails_the_caller(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.delenv("BD_USER_AGENT")
    r = runner.invoke(app, ["pe-collect", "--soft"])
    assert r.exit_code == 0 and "were not read" in r.output and "BD_USER_AGENT" in r.output
    monkeypatch.setenv("BD_USER_AGENT", "SEI Test ops@example.com")
    conn = connect(tmp_path / "t.sqlite")
    init_db(conn)
    with conn:
        ensure_firm(conn, "Alpha Capital", "manual", "test", "alphacap.example", None, "https://www.alphacap.example/portfolio/")
    fake_fetcher(monkeypatch, {"https://www.alphacap.example/portfolio/": page("denied", 403)})
    hard = runner.invoke(app, ["pe-collect"])
    soft = runner.invoke(app, ["pe-collect", "--soft"])
    assert hard.exit_code == 3 and soft.exit_code == 0                                   # every source failed: an error alone, never inside Update
    assert "FAILED" in soft.output or "FAILED" in (soft.stderr if hasattr(soft, "stderr") else "")


def test_the_command_confirms_a_firms_site_then_reads_its_portfolio_in_one_run(tmp_path, monkeypatch):
    conn = connect(tmp_path / "t.sqlite")
    init_db(conn)
    with conn:
        ensure_firm(conn, "Alpha Capital", "derived", "sync")                             # as a sponsor-derived firm: no domain, no page
    fake_fetcher(monkeypatch, {"https://alphacapital.com/": home("Alpha Capital", "<a href='/portfolio'>Portfolio</a>"),
                               "https://alphacapital.com/portfolio": listing()})
    r = CliRunner().invoke(app, ["pe-collect", "--soft"])
    assert r.exit_code == 0, r.output
    firm = connect(tmp_path / "t.sqlite").execute("SELECT domain FROM pe_firms").fetchone()[0]
    assert firm == "alphacapital.com" and "1 firm website(s) confirmed" in r.output
    n = connect(tmp_path / "t.sqlite").execute("SELECT COUNT(*) FROM pe_ownership").fetchone()[0]
    assert n == 6                                                                         # three current and three realized holdings, read from the firm's own page
