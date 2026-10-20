import json

import pytest

import bd.cli as cli
from bd.config import Settings
from bd.scanners import SCANNERS, SweepScanner
from tests.conftest import AS_OF, FakeLLM, FakeVerifier, account

URL = "https://acme.com/news/coo"


def sig(**over):
    base = {"type": "leadership_change", "event_date": "2026-10-01", "summary": "Acme names a new chief operating officer.",
            "source_url": URL, "source_type": "company_release", "confidence": "High", "service_lines": ["Strategy & Operations"],
            "role_or_person": "Dana Fox, COO"}
    base.update(over)
    return base


def run(cfg, signals, urls=(URL,)):
    llm = FakeLLM([json.dumps({"signals": signals})], urls=urls)
    return SweepScanner(cfg, llm, FakeVerifier()).scan(account(), AS_OF), llm


def test_the_sweep_is_registered_and_its_prompt_names_every_type_it_can_emit(cfg):
    assert SCANNERS["sweep"] is SweepScanner and SweepScanner.implemented
    system = SweepScanner(cfg, None, None).system_prompt()
    for t in SweepScanner.allowed_types:
        assert f"`{t}`" in system, t
    assert "Search budget" not in system and "At most 8 signals" in system


def test_the_sweep_covers_leadership_press_events_and_technology_but_not_hiring_rfps_or_risk_factors():
    types = SweepScanner.allowed_types
    leadership = SCANNERS["leadership"].allowed_types
    events = SCANNERS["events"].allowed_types
    assert leadership | events <= types and {"platform_migration", "vendor_deal"} <= types
    assert not types & SCANNERS["hiring"].allowed_types and not types & {"rfp_match", "risk_factor", "financial_pressure"}


def test_one_call_per_company_with_the_search_budget_named_and_capped(cfg):
    out, llm = run(cfg, [sig()])
    assert len(llm.calls) == 1 and len(out.signals) == 1 and out.signals[0].family == "leadership"
    system, messages = llm.calls[0]
    assert f"at most {cfg.scan.sweep_searches} web searches" in messages[-1]["content"]
    sc = SweepScanner(cfg, None, None)
    assert sc._call_kwargs(account())["max_uses"] == cfg.scan.sweep_searches                # whatever the account's tier says


def test_the_usual_acceptance_rules_apply_to_the_sweep(cfg):
    out, _ = run(cfg, [sig(type="hiring_cluster"), sig(event_date="2024-01-01", source_url="https://acme.com/old"),
                       sig(source_url="https://acme.com/not-in-results"), sig(source_url="https://www.linkedin.com/x"),
                       sig(type="vendor_deal", source_url="https://acme.com/news/erp", summary="Signs an ERP deal with a named vendor.")],
                  urls=(URL, "https://acme.com/old", "https://www.linkedin.com/x", "https://acme.com/news/erp"))
    reasons = sorted(r.reason for r in out.rejections)
    assert reasons == ["blocked_domain", "date_out_of_window", "type_not_allowed", "url_not_retrieved"]
    assert [s.extract.type for s in out.signals] == ["vendor_deal"] and out.signals[0].family == "technology"


def test_a_cheaper_scan_model_applies_to_the_sweep_only_and_is_priced_by_its_own_model(cfg, monkeypatch):
    settings = Settings(anthropic_api_key="sk-test-123456", anthropic_model="claude-sonnet-5-5", teams_webhook_url=None, db_path="x.sqlite",
                        user_agent="u", anthropic_scan_model="claude-haiku-5-5")
    built = {s.name: s for s in cli._build_scanners(["sweep", "hiring"], cfg, settings)}
    assert built["sweep"].model_name == "claude-haiku-5-5" and built["sweep"].llm._model == "claude-haiku-5-5"
    assert built["hiring"].model_name is None and built["hiring"].llm._model == "claude-sonnet-5-5"
    same = Settings(anthropic_api_key="sk-test-123456", anthropic_model="claude-sonnet-5-5", teams_webhook_url=None, db_path="x.sqlite", user_agent="u")
    assert {s.name: s.model_name for s in cli._build_scanners(["sweep"], cfg, same)} == {"sweep": None}
    monkeypatch.setenv("ANTHROPIC_SCAN_MODEL", "claude-haiku-5-5")
    assert Settings.from_env().anthropic_scan_model == "claude-haiku-5-5"
    monkeypatch.delenv("ANTHROPIC_SCAN_MODEL")
    assert Settings.from_env().anthropic_scan_model is None


def test_all_scanners_leaves_the_sweep_out_because_it_duplicates_four_of_them(cfg):
    names = cli._resolve_scanner_names("all", Settings(anthropic_api_key=None, anthropic_model=None, teams_webhook_url=None, db_path="x", user_agent="u",
                                                       user_agent_configured=True))
    assert "sweep" not in names and {"leadership", "press", "events", "hiring"} <= set(names)
    assert cli._resolve_scanner_names("sweep,hiring", Settings(anthropic_api_key=None, anthropic_model=None, teams_webhook_url=None, db_path="x",
                                                                user_agent="u")) == ["sweep", "hiring"]


def test_the_runner_prices_each_scanner_with_its_own_model(cfg, tmp_path):
    from bd.db import connect, init_db
    from bd.llm import Usage
    from bd.runner import run_scans
    from tests.test_runner import FakeScanner
    conn = connect(tmp_path / "t.sqlite")
    init_db(conn)
    conn.execute("INSERT INTO accounts(name, domain, tier, status) VALUES ('Co0', 'co0.com', 'A', 'active')")
    conn.commit()
    big = Usage(input_tokens=1_000_000, output_tokens=0)
    main = FakeScanner(cfg, name="leadership", usage=big)
    cheap = FakeScanner(cfg, name="hiring", usage=Usage(input_tokens=1_000_000, output_tokens=0))
    cheap.model_name = "claude-haiku-5-5"
    from bd.accounts import list_accounts
    s = run_scans(conn, cfg, [main, cheap], list_accounts(conn), AS_OF, model="claude-sonnet-5-5", workers=1)
    assert s.est_cost == pytest.approx(2.0 + 0.10)                       # one million input tokens on each model


# ---- a scan whose search tool failed is an error, not a quiet company ---------------------------------------------------------------------
def test_nothing_found_while_the_search_tool_failed_is_an_error_but_a_finding_or_a_spent_search_budget_is_not(cfg):
    def scan(signals, errors):
        llm = FakeLLM([json.dumps({"signals": signals})], urls=(URL,), search_errors=errors)
        return SweepScanner(cfg, llm, FakeVerifier()).scan(account(), AS_OF)
    out = scan([], ["too_many_requests"] * 3)
    assert out.error and out.error.startswith("search tool errors: too_many_requests x3") and out.signals == []
    assert scan([sig()], ["too_many_requests"]).error is None                       # something was found: keep it
    assert scan([], ["max_uses_exceeded"]).error is None                            # the model used every search it was allowed
    assert scan([], []).error is None


def test_a_company_whose_search_failed_is_not_marked_scanned_so_it_is_not_taken_for_quiet(cfg, tmp_path):
    from bd.accounts import list_accounts
    from bd.runner import run_scans
    from tests.conftest import add_account, make_db
    conn = make_db(tmp_path)
    add_account(conn, "Acme Corp", domain="acme.com")
    llm = FakeLLM([json.dumps({"signals": []})], urls=(), search_errors=["too_many_requests"])
    s = run_scans(conn, cfg, [SweepScanner(cfg, llm, FakeVerifier())], list_accounts(conn), AS_OF, "claude-sonnet-5-5")
    assert s.errors == 1 and "search tool errors" in s.accounts[1].errors[0]
    assert conn.execute("SELECT last_scanned FROM accounts").fetchone()[0] is None                  # it is tried again next run
    assert "search tool errors" in conn.execute("SELECT errors FROM runs WHERE step = 'scan:sweep'").fetchone()[0]


def test_the_technology_scanner_runs_on_the_scan_model_too_and_hiring_and_the_older_scanners_do_not(cfg):
    from bd.scanners import SCAN_MODEL_SCANNERS
    assert SCAN_MODEL_SCANNERS == {"sweep", "tech_procurement"}
    settings = Settings(anthropic_api_key="sk-test-123456", anthropic_model="claude-sonnet-5-5", teams_webhook_url=None, db_path="x.sqlite",
                        user_agent="u", anthropic_scan_model="claude-haiku-5-5")
    built = {s.name: s for s in cli._build_scanners(["sweep", "tech_procurement", "hiring", "leadership"], cfg, settings)}
    for name in ("sweep", "tech_procurement"):
        assert built[name].model_name == "claude-haiku-5-5" and built[name].llm._model == "claude-haiku-5-5"
    for name in ("hiring", "leadership"):
        assert built[name].model_name is None and built[name].llm._model == "claude-sonnet-5-5"
    same = Settings(anthropic_api_key="sk-test-123456", anthropic_model="claude-sonnet-5-5", teams_webhook_url=None, db_path="x.sqlite", user_agent="u")
    assert {s.name: s.model_name for s in cli._build_scanners(["sweep", "tech_procurement"], cfg, same)} == {"sweep": None, "tech_procurement": None}
