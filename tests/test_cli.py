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


def test_seed_loads_repo_accounts():
    r = runner.invoke(app, ["seed", str(ROOT / "accounts.csv")])
    assert r.exit_code == 0 and "58 inserted" in r.output
    assert "58 to scan (58 with location or size still unverified)" in r.output  # names only: scanned anyway, tagged unverified
    assert "0 inserted, 58 existing matched" in runner.invoke(app, ["seed", str(ROOT / "accounts.csv")]).output


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
    assert "2 to scan (1 with location or size still unverified), 2 known out of scope" in r.output
    assert "unverified: Beta" in r.output and "excluded: Gamma: not in Chicagoland" in r.output
    assert "excluded industry: cannabis" in r.output


def test_seed_with_exclusions_file(tmp_path):
    ex = tmp_path / "ex.csv"
    ex.write_text("name,domain,reason\nAcme,acme.com,client\n")
    p = tmp_path / "fit.csv"
    p.write_text(FIT_CSV)
    r = runner.invoke(app, ["seed", str(p), "--exclusions", str(ex)])
    assert r.exit_code == 0 and "Exclusions: 1 added" in r.output and "1 to scan" in r.output
    ex.write_text("name,domain,reason\nAcme,acme.com,because\n")
    assert runner.invoke(app, ["seed", str(p), "--exclusions", str(ex)]).exit_code == 1


def test_scan_dry_run_needs_no_key_and_lists_everyone_not_known_out_of_scope(tmp_path):
    seeded(tmp_path)
    r = runner.invoke(app, ["scan", "--dry-run"])
    assert r.exit_code == 0 and "would scan: Acme" in r.output and "would scan: Beta" in r.output   # Beta's size is unverified: scanned anyway
    assert "Gamma" not in r.output.split("due")[1] and "Weedco" not in r.output.split("due")[1]       # known out of scope
    assert "2 of 2 accounts due" in r.output and "= 12 calls" in r.output   # leadership, events, press, hiring, tech_procurement, warn (pe: not PE-backed) x 2
    assert runner.invoke(app, ["scan", "--dry-run", "--tier", "B"]).exit_code == 0 and "1 of 1 accounts due (tier B)" in runner.invoke(
        app, ["scan", "--dry-run", "--tier", "B"]).output


def test_scan_dry_run_names_the_extra_scanners_that_only_run_for_companies_with_a_signal(tmp_path):
    seeded(tmp_path)
    r = runner.invoke(app, ["scan", "--dry-run", "--scanners", "leadership,press,hiring", "--extras", "events,tech_procurement,press"])
    assert r.exit_code == 0 and "2 of 2 accounts due" in r.output and "x ['leadership', 'press', 'hiring']" in r.output
    assert "then ['events', 'tech_procurement'] for the ones that show a signal" in r.output          # press is already core: not repeated
    assert "then [" not in runner.invoke(app, ["scan", "--dry-run", "--scanners", "leadership"]).output


def test_extra_scanners_run_only_for_scanned_companies_that_show_a_signal_and_never_touch_the_scan_date(tmp_path, monkeypatch):
    import bd.cli as cli
    from bd.config import load_config
    from bd.db import connect
    from tests.test_runner import FakeScanner
    seeded(tmp_path)                                                            # Acme and Beta are both on the scan list
    made = {}

    def fake_build(names, cfg, settings):
        made[tuple(names)] = [FakeScanner(cfg, name=n, signals_for=("Acme",) if n == "leadership" else ()) for n in names]
        return made[tuple(names)]

    monkeypatch.setattr(cli, "_build_scanners", fake_build)
    r = runner.invoke(app, ["scan", "--scanners", "leadership", "--extras", "events"])
    assert r.exit_code == 0, r.output
    core, extra = made[("leadership",)][0], made[("events",)][0]
    assert sorted(core.calls) == ["Acme", "Beta"] and extra.calls == ["Acme"]            # only Acme found a signal, so only Acme gets the extra look
    assert "Extra scanners ['events'] for 1 of 2 scanned companies that already show a signal." in r.output and "Extras total:" in r.output
    conn = connect(tmp_path / "t.sqlite")
    last = {row["name"]: row["last_scanned"] for row in conn.execute("SELECT name, last_scanned FROM accounts")}
    assert last["Acme"] and last["Beta"]                                                 # the core scan marked them; the extras did not change that


def test_scan_requires_key_and_model_after_gate(tmp_path):
    seeded(tmp_path)
    r = runner.invoke(app, ["scan"])
    assert r.exit_code == 1 and "ANTHROPIC_API_KEY" in r.output


def test_scan_with_nothing_through_the_gate_and_bad_args(tmp_path):
    r = runner.invoke(app, ["scan", "--dry-run"])
    assert r.exit_code == 1 and "No accounts to scan" in r.output
    assert runner.invoke(app, ["scan", "--tier", "Z"]).exit_code == 1
    assert runner.invoke(app, ["scan", "--scanners", "nope"]).exit_code == 1


def test_evaluate_reports_gold_problems_before_needing_a_key(tmp_path):
    r = runner.invoke(app, ["evaluate", "--gold", str(tmp_path / "missing.csv")])
    assert r.exit_code == 1 and "not found" in r.output
    r = runner.invoke(app, ["evaluate", "--gold", str(ROOT / "tests" / "gold.template.csv")])
    assert r.exit_code == 1 and "blank" in r.output


# ---- Stage 4: score / shortlist / card ----------------------------------------------------------
import json  # noqa: E402
import os  # noqa: E402

from bd.db import connect, init_db  # noqa: E402
from tests.conftest import add_account, add_signal  # noqa: E402

WEBHOOK = "https://prod-00.example.logic.azure.com/workflows/SECRET-TOKEN/triggers/manual?sig=SECRETSIG"
AS_OF_S = "2026-10-15"


def populate(cfg):
    conn = connect(os.environ["BD_DB_PATH"])
    init_db(conn)
    a = add_account(conn, "Hotco", industry="Industrial")
    for ty, age in (("leadership_change", 21), ("hiring_cluster", 10), ("pe_addon", 45)):
        add_signal(conn, cfg, a, ty, age, url=f"https://hotco.com/{ty}", summary=f"Hotco {ty.replace('_', ' ')} 21 days.")
    w = add_account(conn, "Watchco")
    add_signal(conn, cfg, w, "leadership_change", 5)
    add_signal(conn, cfg, w, "risk_factor", 5)
    held = add_account(conn, "Heldco", status="hold_review")
    add_signal(conn, cfg, held, "leadership_change", 5)
    return conn


def test_score_needs_active_accounts():
    assert runner.invoke(app, ["score", "--as-of", AS_OF_S]).exit_code == 1


def test_score_and_shortlist_output(cfg):
    populate(cfg)
    r = runner.invoke(app, ["score", "--as-of", AS_OF_S])
    assert r.exit_code == 0 and "Scored 2 accounts" in r.output and "Hot 1" in r.output and "Watch 1" in r.output
    assert "91 Hot" in r.output and "Heldco" not in r.output
    s = runner.invoke(app, ["shortlist", "--as-of", AS_OF_S])
    assert s.exit_code == 0 and "1 lead(s)" in s.output and "Hotco" in s.output
    assert "Watchco: band Watch is not posted" in s.output


def test_shortlist_and_card_need_scores_first(cfg):
    populate(cfg)
    assert "bd score" in runner.invoke(app, ["shortlist", "--as-of", AS_OF_S]).output
    assert runner.invoke(app, ["card", "--as-of", AS_OF_S, "--no-llm"]).exit_code == 1


def test_card_default_is_a_dry_run_that_marks_nothing(cfg, tmp_path, monkeypatch):
    conn = populate(cfg)
    runner.invoke(app, ["score", "--as-of", AS_OF_S])
    monkeypatch.delenv("TEAMS_WEBHOOK_URL", raising=False)
    posted = []
    monkeypatch.setattr("bd.card.post_card", lambda *a, **k: posted.append(a))
    out = tmp_path / "card.json"
    r = runner.invoke(app, ["card", "--as-of", AS_OF_S, "--no-llm", "--out", str(out)])
    assert r.exit_code == 0 and "DRY RUN: TEAMS_WEBHOOK_URL is not set" in r.output and posted == []
    card = json.loads(out.read_text())
    assert card["version"] == "1.4" and "Hotco" in json.dumps(card)
    row = conn.execute("SELECT posted, cooldown_until FROM scores WHERE band='Hot'").fetchone()
    assert (row["posted"], row["cooldown_until"]) == (0, None)


def test_card_without_no_llm_requires_key_and_points_at_the_flag(cfg):
    populate(cfg)
    runner.invoke(app, ["score", "--as-of", AS_OF_S])
    r = runner.invoke(app, ["card", "--as-of", AS_OF_S])
    assert r.exit_code == 1 and "ANTHROPIC_API_KEY" in r.output and "--no-llm" in r.output


def test_card_with_no_leads_needs_no_key_and_still_writes_a_card(cfg, tmp_path):
    conn = connect(os.environ["BD_DB_PATH"])
    init_db(conn)
    a = add_account(conn, "Quietco")
    add_signal(conn, cfg, a, "leadership_change", 5)
    runner.invoke(app, ["score", "--as-of", AS_OF_S])
    out = tmp_path / "c.json"
    r = runner.invoke(app, ["card", "--as-of", AS_OF_S, "--out", str(out)])
    assert r.exit_code == 0 and "0 lead(s)" in r.output
    assert "No account met the posting gate" in out.read_text()


def test_webhook_set_posts_marks_posted_and_starts_cooldown_without_printing_the_url(cfg, tmp_path, monkeypatch):
    conn = populate(cfg)
    runner.invoke(app, ["score", "--as-of", AS_OF_S])
    monkeypatch.setenv("TEAMS_WEBHOOK_URL", WEBHOOK)
    from bd.card import PostResult
    calls = []
    monkeypatch.setattr("bd.card.post_card", lambda url, card, *a, **k: calls.append((url, card)) or PostResult(True, "HTTP 202"))
    out = tmp_path / "card.json"
    r = runner.invoke(app, ["card", "--as-of", AS_OF_S, "--no-llm", "--out", str(out)])
    assert r.exit_code == 0 and "Posted to Teams (HTTP 202)" in r.output
    assert len(calls) == 1 and calls[0][0] == WEBHOOK and calls[0][1]["version"] == "1.4"
    assert "SECRET" not in r.output and "logic.azure" not in r.output
    row = conn.execute("SELECT posted, cooldown_until FROM scores WHERE band='Hot'").fetchone()
    assert (row["posted"], row["cooldown_until"]) == (1, "2026-11-12")
    # same day again: the account is in cooldown, so the card has no leads
    again = runner.invoke(app, ["card", "--as-of", AS_OF_S, "--no-llm", "--out", str(out)])
    assert "0 lead(s)" in again.output


def test_dry_run_flag_beats_the_webhook_env(cfg, tmp_path, monkeypatch):
    conn = populate(cfg)
    runner.invoke(app, ["score", "--as-of", AS_OF_S])
    monkeypatch.setenv("TEAMS_WEBHOOK_URL", WEBHOOK)
    calls = []
    monkeypatch.setattr("bd.card.post_card", lambda *a, **k: calls.append(a))
    r = runner.invoke(app, ["card", "--as-of", AS_OF_S, "--no-llm", "--dry-run", "--out", str(tmp_path / "c.json")])
    assert r.exit_code == 0 and calls == [] and "DRY RUN (--dry-run)" in r.output
    assert conn.execute("SELECT posted FROM scores WHERE band='Hot'").fetchone()[0] == 0


def test_failed_post_exits_nonzero_and_marks_nothing(cfg, tmp_path, monkeypatch):
    conn = populate(cfg)
    runner.invoke(app, ["score", "--as-of", AS_OF_S])
    monkeypatch.setenv("TEAMS_WEBHOOK_URL", WEBHOOK)
    from bd.card import PostResult
    monkeypatch.setattr("bd.card.post_card", lambda *a, **k: PostResult(False, "HTTP 400"))
    r = runner.invoke(app, ["card", "--as-of", AS_OF_S, "--no-llm", "--out", str(tmp_path / "c.json")])
    assert r.exit_code == 1 and "HTTP 400" in r.output and "SECRET" not in r.output
    row = conn.execute("SELECT posted, cooldown_until FROM scores WHERE band='Hot'").fetchone()
    assert (row["posted"], row["cooldown_until"]) == (0, None)


def test_card_uses_briefer_without_web_search_and_caches(cfg, tmp_path, monkeypatch):
    conn = populate(cfg)
    runner.invoke(app, ["score", "--as-of", AS_OF_S])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
    monkeypatch.delenv("TEAMS_WEBHOOK_URL", raising=False)
    from tests.conftest import FakeLLM
    made = []
    reply = json.dumps({"why_now": "Hotco named a new leader 21 days ago and is hiring.", "best_service_line": "Strategy & Operations"})

    def factory(cfg_, model, api_key=None, web_search=True, **k):
        llm = FakeLLM([reply])
        made.append((model, web_search, llm))
        return llm
    monkeypatch.setattr("bd.llm.AnthropicWebSearchClient", factory)
    out = tmp_path / "c.json"
    r = runner.invoke(app, ["card", "--as-of", AS_OF_S, "--out", str(out)])
    assert r.exit_code == 0, r.output
    assert made[0][0] == "claude-sonnet-5-5" and made[0][1] is False       # briefer never gets web search
    text = out.read_text()
    assert "named a new leader 21 days ago" in text and "Strategy & Operations" in text
    assert "(llm)" in r.output
    run = conn.execute("SELECT * FROM runs WHERE step='brief'").fetchone()
    assert run["tokens"] == 150 and run["est_cost"] is not None
    r2 = runner.invoke(app, ["card", "--as-of", AS_OF_S, "--out", str(out)])
    assert "(cache)" in r2.output and len(made) == 2 and made[1][2].calls == []   # second run: no LLM call


# ---- EDGAR scanner and enrich in the CLI ----------------------------------------------------------------

def test_scan_all_skips_edgar_without_a_user_agent_and_explicit_edgar_fails(tmp_path, monkeypatch):
    seeded(tmp_path)
    monkeypatch.delenv("BD_USER_AGENT", raising=False)
    r = runner.invoke(app, ["scan", "--dry-run"])
    assert r.exit_code == 0 and "edgar, earnings, financials, sec_events scanner(s) skipped: set BD_USER_AGENT" in r.output
    r = runner.invoke(app, ["scan", "--dry-run", "--scanners", "edgar"])
    assert r.exit_code == 1 and "BD_USER_AGENT" in r.output


def test_edgar_counts_only_accounts_with_a_cik_in_the_dry_run(tmp_path, monkeypatch):
    p = tmp_path / "f.csv"
    p.write_text("name,domain,hq_city,chicagoland,revenue,employees,ownership,sponsor,industry,tier,cik\n"
                 "Acme,acme.com,Chicago,yes,500000000,,public,,Industrial,A,1234\n"
                 "Beta,beta.com,Chicago,yes,500000000,,public,,Industrial,A,\n")
    assert runner.invoke(app, ["seed", str(p)]).exit_code == 0
    monkeypatch.setenv("BD_USER_AGENT", "SEI BD ops@example.com")
    r = runner.invoke(app, ["scan", "--dry-run"])
    assert r.exit_code == 0 and "2 of 2 accounts due" in r.output
    assert "= 16 calls" in r.output           # 6 scanners for each of 2 accounts (12) + edgar, earnings, financials and sec_events for Acme only (4)


def test_enrich_needs_accounts_and_a_key(tmp_path):
    assert runner.invoke(app, ["enrich"]).exit_code == 1
    runner.invoke(app, ["seed", str(ROOT / "accounts.csv")])
    r = runner.invoke(app, ["enrich", "--limit", "1"])
    assert r.exit_code == 1 and "ANTHROPIC_API_KEY" in r.output


def test_enrich_writes_a_review_csv_and_never_changes_accounts(tmp_path, monkeypatch):
    runner.invoke(app, ["seed", str(ROOT / "accounts.csv")])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
    monkeypatch.delenv("BD_USER_AGENT", raising=False)
    from tests.conftest import FakeLLM, FakeVerifier
    from tests.test_enrich import ALL_URLS, facts
    reply = json.dumps({"facts": facts(cik=None, evidence={k: v for k, v in facts()["evidence"].items() if k != "cik"})})
    monkeypatch.setattr("bd.llm.AnthropicWebSearchClient", lambda cfg, model, api_key=None, web_search=True, **k: FakeLLM([reply], urls=ALL_URLS))
    monkeypatch.setattr("bd.verify.UrlVerifier", lambda cfg, ua, *a, **k: FakeVerifier())
    conn = connect(os.environ["BD_DB_PATH"])
    before = [tuple(r) for r in conn.execute("SELECT * FROM accounts ORDER BY id")]
    out = tmp_path / "p.csv"
    r = runner.invoke(app, ["enrich", "--only", "Abbott", "--out", str(out)])
    assert r.exit_code == 0, r.output
    assert "proposals only" in r.output and "bd seed" in r.output and "BD_USER_AGENT is not set" in r.output
    import csv as _csv
    rows = list(_csv.DictReader(out.open()))
    assert [x["name"] for x in rows] == ["Abbott"] and rows[0]["chicagoland"] == "yes" and rows[0]["revenue"] == "500000000"
    assert rows[0]["evidence_urls"]
    assert before == [tuple(r) for r in conn.execute("SELECT * FROM accounts ORDER BY id")]
    assert conn.execute("SELECT COUNT(*) FROM runs WHERE step='enrich'").fetchone()[0] == 1
