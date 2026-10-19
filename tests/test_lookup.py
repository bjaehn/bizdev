import json
from datetime import date

import pytest
from typer.testing import CliRunner

from bd.accounts import list_accounts
from bd.cli import app
from bd.jobs import LOOKUP_BUDGET, KINDS, JobError, PAID_STEPS, company_name, is_paid, sequence_for, validate
from bd.lookup import find
from bd.pipeline import set_client
from bd.score_store import score_accounts
from tests.conftest import AS_OF, ROOT, add_account, add_signal, make_db
from tests.test_update import app_env, cli_env, fake  # noqa: F401  (fixtures)

runner = CliRunner()


# ---- the search ---------------------------------------------------------------------------------------------------------------------
def test_find_matches_by_text_or_by_name_apart_from_suffixes_and_orders_exact_first(cfg, tmp_path):
    conn = make_db(tmp_path)
    add_account(conn, "Abbott Laboratories")
    add_account(conn, "CME Group")
    add_account(conn, "Abbott")
    held = add_account(conn, "Heldco", revenue=None, ownership=None, chicagoland="unknown")
    set_client(conn, held, True)
    hits = find(conn, cfg, "abbott", AS_OF.isoformat())
    assert [h.account.name for h in hits] == ["Abbott", "Abbott Laboratories"] and hits[0].exact and not hits[1].exact
    assert [h.account.name for h in find(conn, cfg, "CME Group Inc.", AS_OF.isoformat())] == ["CME Group"]
    assert find(conn, cfg, "CME Group Inc.", AS_OF.isoformat())[0].exact
    assert find(conn, cfg, "", AS_OF.isoformat()) == [] and find(conn, cfg, "zzz", AS_OF.isoformat()) == []
    h = find(conn, cfg, "Heldco", AS_OF.isoformat())[0]
    assert h.state.client and h.account.status == "active" and any("unverified" in r for r in h.reasons)      # scanned, facts unconfirmed


def test_find_reports_score_and_the_newest_signal(cfg, tmp_path):
    conn = make_db(tmp_path)
    a = add_account(conn, "Acme")
    add_signal(conn, cfg, a, "leadership_change", 20)
    add_signal(conn, cfg, a, "hiring_cluster", 3)
    score_accounts(conn, cfg, AS_OF)
    h = find(conn, cfg, "acme", AS_OF.isoformat())[0]
    assert h.score is not None and h.band and h.latest_signal["type"] == "hiring_cluster" and h.signals_90d == 2 and h.reasons == ()


# ---- jobs --------------------------------------------------------------------------------------------------------------------------
def test_company_names_are_checked_before_they_reach_a_command_line():
    assert company_name({"only": "  Acme   Corp, Inc. "}) == "Acme Corp, Inc." and company_name({}) == ""
    for bad in ("--all", "a;rm -rf /", "$(x)", "x" * 61, "Acme\nCorp;x"):
        with pytest.raises(JobError):
            company_name({"only": bad}, required=True)
    with pytest.raises(JobError, match="Type a company name"):
        company_name({}, required=True)


def test_the_free_check_and_the_paid_lookup_build_fixed_argv():
    assert validate("sec_fill", {}) == ["sec-fill"]
    assert validate("sec_fill", {"only": "Acme Corp"}) == ["sec-fill", "--only", "Acme Corp", "--exact"]
    args = validate("lookup", {"only": "Acme Corp"})
    assert args == ["lookup", "--only", "Acme Corp", "--budget", str(LOOKUP_BUDGET)] and KINDS["lookup"][1] is True
    steps = sequence_for("lookup", args, "2026-10-16")
    assert [s[0] for s in steps] == ["sec-fill", "enrich", "apply-proposals", "scan", "score"]
    assert all("--exact" in s and s[s.index("--only") + 1] == "Acme Corp" for s in steps if s[0] in ("sec-fill", "enrich", "scan"))
    assert "--force" in steps[3] and "--limit" in steps[1] and not is_paid(steps[0]) and is_paid(steps[1]) and is_paid(steps[3])
    assert not is_paid(["scan", "--scanners", "ats,financials,sec_events"]) and is_paid(["scan", "--scanners", "ats,leadership"]) and is_paid(["scan"])
    assert "sec-fill" not in PAID_STEPS


def test_a_lookup_cannot_spend_more_than_its_limit(fake, monkeypatch):  # noqa: F811
    runner_, tmp = fake
    monkeypatch.setenv("FAKE_COSTS", json.dumps({"enrich": 2.5, "scan": 2.0}))
    jid = runner_.submit("lookup", {"only": "Acme Corp"}, confirmed=True)
    runner_.wait()
    job = runner_.get(jid)
    calls = [json.loads(x) for x in (tmp / "calls.jsonl").read_text().splitlines()]
    by = {c["step"]: c for c in calls}
    assert by["sec-fill"]["budget"] is None and float(by["enrich"]["budget"]) == 3.0              # the free step has no limit; enrich gets all of it
    assert float(by["scan"]["budget"]) == 0.5                                                       # and scan only what enrich left of the $3
    assert job.status == "ok" and f"${LOOKUP_BUDGET} limit" in job.note
    with pytest.raises(JobError, match="confirm"):
        runner_.submit("lookup", {"only": "Acme Corp"})


# ---- CLI --------------------------------------------------------------------------------------------------------------------------
def test_scan_and_enrich_and_sec_fill_can_target_one_company(cli_env, monkeypatch):  # noqa: F811
    runner.invoke(app, ["seed", str(ROOT / "accounts.csv")])
    r = runner.invoke(app, ["scan", "--only", "Nobody At All", "--allow-empty"])
    assert r.exit_code == 0 and "'Nobody At All' is not on the scan list" in r.output
    r = runner.invoke(app, ["scan", "--only", "Nobody At All", "--dry-run"])
    assert r.exit_code == 1                                                                         # without --allow-empty it still fails loudly
    r = runner.invoke(app, ["enrich", "--only", "Nobody At All", "--exact", "--allow-empty"])
    assert r.exit_code == 0 and "No companies have unverified facts" in r.output
    monkeypatch.setenv("BD_USER_AGENT", "SEI BD Research ops@example.com")
    r = runner.invoke(app, ["sec-fill", "--only", "Nobody At All", "--exact"])
    assert r.exit_code == 0 and "Nothing for the SEC to fill" in r.output


def test_exact_matches_the_whole_name_only(cfg, tmp_path):
    from bd.enrich import select_accounts
    from bd.sec_fill import select
    conn = make_db(tmp_path)
    for n in ("Abbott", "Abbott Laboratories"):
        add_account(conn, n, revenue=None, ownership=None, chicagoland="unknown")
    assert len(select_accounts(conn, False, "abbott", None)) == 2 and [a.name for a in select_accounts(conn, False, "ABBOTT", None, exact=True)] == ["Abbott"]
    assert len(select(conn, "abbott")) == 2 and [a.name for a in select(conn, "abbott", exact=True)] == ["Abbott"]


# ---- pages -----------------------------------------------------------------------------------------------------------------------------
def test_lookup_page_finds_adds_and_starts_the_job(app_env):  # noqa: F811
    e = app_env
    a = add_account(e.conn, "Acme Corp")
    add_signal(e.conn, e.cfg, a, "restructuring_layoffs", 5, as_of=date.today())
    score_accounts(e.conn, e.cfg, date.today())
    assert "Type any company name" in e.get("/lookup").body
    body = e.get("/lookup?q=acme").body
    assert "Acme Corp" in body and "Restructuring / layoffs" in body and "scanned for signals" in body and "Analysis profile" in body
    assert "Not on your list" not in body and "Research and scan now" in body
    miss = e.get("/lookup?q=Zeta <b>Holdings").body
    assert "Not on your list: Zeta &lt;b&gt;Holdings" in miss and "Add and check the SEC (free)" in miss and "<b>Holdings" not in miss
    # free check of a company not on the list: it is added, a sec_fill job starts for exactly that name
    r = e.post("/lookup/run", {"name": "Zeta Holdings"})
    assert r.code == 303 and r.h["Location"].startswith("/jobs/")
    e.app.jobs.wait()
    job = e.app.jobs.recent()[0]
    assert job.kind == "sec_fill" and job.args == ["sec-fill", "--only", "Zeta Holdings", "--exact"]
    zeta = [x for x in list_accounts(e.conn, status=None) if x.name == "Zeta Holdings"][0]
    assert zeta.tier == "B" and zeta.status == "active" and zeta.origin == "manual"
    assert f"open Zeta Holdings" in e.get(r.h["Location"]).body
    # a second request for the same company, typed differently, finds it instead of adding another
    assert e.post("/lookup/run", {"name": "zeta holdings, inc."}).code == 303
    assert len([x for x in list_accounts(e.conn, status=None) if "zeta" in x.name.lower()]) == 1


def test_the_paid_lookup_needs_the_tick_and_bad_names_are_refused(app_env):  # noqa: F811
    e = app_env
    r = e.post("/lookup/run", {"name": "Zeta Holdings", "deep": "1"})
    assert r.code == 303 and r.h["Location"].startswith("/lookup?q=Zeta")
    assert "confirm" in e.get(r.h["Location"]).body.lower()                                   # told to tick the box
    assert any(x.name == "Zeta Holdings" for x in list_accounts(e.conn, status=None))          # the company was still added
    e.app.jobs.wait()
    assert e.app.jobs.recent() == []
    r = e.post("/lookup/run", {"name": "Zeta Holdings", "deep": "1", "confirm": "1"})
    assert r.h["Location"].startswith("/jobs/")
    e.app.jobs.wait()
    job = e.app.jobs.recent()[0]
    assert job.kind == "lookup" and job.args == ["lookup", "--only", "Zeta Holdings", "--budget", str(LOOKUP_BUDGET)]
    for bad in ("", "--all", "a;rm -rf /", "x" * 80):
        before = len(list_accounts(e.conn, status=None))
        assert e.post("/lookup/run", {"name": bad}).code == 303
        assert len(list_accounts(e.conn, status=None)) == before
    assert e.post("/lookup/run", {"name": "Zeta Holdings"}, csrf=False).code == 403


def test_two_companies_with_the_same_name_are_not_guessed(app_env):  # noqa: F811
    e = app_env
    add_account(e.conn, "Twin Industries Inc")
    add_account(e.conn, "Twin Industries Corp")
    r = e.post("/lookup/run", {"name": "Twin Industries"})
    assert r.code == 303 and r.h["Location"].startswith("/lookup")
    assert "More than one company" in e.get(r.h["Location"]).body
    e.app.jobs.wait()
    assert e.app.jobs.recent() == []
