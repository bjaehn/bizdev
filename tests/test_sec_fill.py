import json

import pytest
from typer.testing import CliRunner

import httpx2 as httpx

from bd.accounts import Account, list_accounts
from bd.cli import app
from bd.edgar import EdgarClient, EdgarError, facts_url, submissions_url
from bd.enrich import Enricher
from bd.jobs import KINDS, PAID_STEPS, sequence_for, validate
from bd.sec_fill import run_sec_fill, select
from bd.sec_ids import TICKERS_URL, build_index, normalize_name
from tests.conftest import AS_OF, ROOT, FakeLLM, FakeVerifier, add_account, make_db
from tests.test_enrich import ALL_URLS, facts as model_facts
from tests.test_sec_facts import CIK, SUBS, xbrl
from tests.test_update import app_env, cli_env  # noqa: F401  (fixtures)

runner = CliRunner()


def listing(*rows):
    """The SEC file layout: {"0": {"cik_str": 1234, "ticker": "ACME", "title": "ACME CORP"}, ...}"""
    return {str(i): {"cik_str": c, "ticker": t, "title": n} for i, (c, t, n) in enumerate(rows)}


LIST = listing((1234, "ACME", "ACME CORP"), (5678, "CME", "CME Group Inc."), (91, "WTFC", "WINTRUST FINANCIAL CORP"),
               (700, "EDV", "Endeavor Group Holdings, Inc."), (801, "AAA", "Twin Industries Inc"), (802, "BBB", "Twin Industries Corp"),
               (900, "GOOG", "Alphabet Inc."), (900, "GOOGL", "Alphabet Inc."))


class SecEdgar:
    """The calls bd.sec_fill makes: the company list, and a company's two SEC files by CIK."""
    def __init__(self, raw=LIST, subs=None, facts=None, list_error=None, errors=None):
        self.raw, self.list_error, self.errors = raw, list_error, errors or {}
        self.subs, self.facts = subs or {}, facts or {}
        self.calls = []

    def company_tickers(self):
        self.calls.append(("list", None))
        if self.list_error:
            raise self.list_error
        return self.raw

    def submissions(self, cik):
        self.calls.append(("submissions", cik))
        if ("submissions", cik) in self.errors:
            raise self.errors[("submissions", cik)]
        return self.subs.get(cik, SUBS)

    def company_facts(self, cik):
        self.calls.append(("facts", cik))
        if ("facts", cik) in self.errors:
            raise self.errors[("facts", cik)]
        return self.facts.get(cik, xbrl())


def held(conn, name, **kw):
    kw = {"status": "hold_review", "revenue": None, "ownership": None, **kw}
    return add_account(conn, name, **kw)


def audit(conn, account_id):
    return [(r["actor"], r["action"], r["field"], r["new"]) for r in
            conn.execute("SELECT * FROM audit_log WHERE account_id = ? ORDER BY id", (account_id,))]


# ---- name matching -------------------------------------------------------------------------------------------------------
def test_names_match_exactly_apart_from_legal_suffixes_punctuation_and_case():
    for a, b in (("CME Group", "CME Group Inc."), ("Wintrust Financial", "WINTRUST FINANCIAL CORP"),
                 ("The Allstate Corporation", "Allstate Corp"), ("Procter & Gamble Co", "PROCTER and GAMBLE CO /OH/"),
                 ("Acme Holdings, Inc.", "ACME")):
        assert normalize_name(a) == normalize_name(b), (a, b)
    for a, b in (("Endeavor Health", "Endeavor Group Holdings, Inc."), ("CME", "CME Group Inc."), ("Abbott", "Abbott Laboratories"),
                 ("Acme Financial", "Acme Corp")):
        assert normalize_name(a) != normalize_name(b), (a, b)
    assert normalize_name("Holdings Inc") == "" and normalize_name("") == ""


def test_only_a_unique_exact_match_is_a_match():
    idx = build_index(LIST)
    m = idx.lookup("CME Group")
    assert m.listing.cik == "0000005678" and m.listing.ticker == "CME" and m.listing.label == "CME Group Inc. (CME)"
    assert idx.lookup("Wintrust Financial").listing.cik == "0000000091"
    assert idx.lookup("Alphabet").listing.cik == "0000000900"                   # two share classes of one filer are one filer
    assert idx.lookup("Endeavor Health").listing is None and idx.lookup("BMO US").reason == "no match"
    amb = idx.lookup("Twin Industries")
    assert amb.listing is None and amb.reason == "ambiguous (2 different SEC filers)"
    assert idx.lookup("").reason == "empty name"


def test_an_unexpected_layout_gives_an_empty_index_never_an_error():
    for raw in (None, "text", 5, {}, [], {"0": "x"}, {"0": {"ticker": "A"}}, [[1, 2]], {"0": {"cik_str": "abc", "ticker": "A", "title": "A Corp"}},
                {"0": {"cik_str": 0, "ticker": "A", "title": "A Corp"}}, {"0": {"cik_str": True, "ticker": "A", "title": "A Corp"}},
                {"0": {"cik_str": 5, "ticker": "", "title": "A Corp"}}, {"0": {"cik_str": 5, "ticker": "A", "title": ""}}):
        idx = build_index(raw)
        assert idx.size == 0 and idx.lookup("A Corp").listing is None
    mixed = build_index({"0": "junk", "1": {"cik_str": "320193", "ticker": "aapl", "title": "Apple Inc."}})
    assert mixed.lookup("Apple").listing.cik == "0000320193" and mixed.lookup("Apple").listing.ticker == "AAPL"
    assert build_index([{"cik_str": 7, "ticker": "Z", "title": "Zed Corp"}]).lookup("Zed").listing.cik == "0000000007"   # a plain list works too


def test_the_edgar_client_fetches_the_company_list_once_from_the_fixed_url(cfg):
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json=LIST)
    c = EdgarClient(cfg, "SEI BD ops@example.com", httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)
    assert build_index(c.company_tickers()).size == 7                    # seven different filers (Alphabet's two share classes are one)
    assert str(seen[0].url) == TICKERS_URL == "https://www.sec.gov/files/company_tickers.json"
    assert seen[0].headers["user-agent"] == "SEI BD ops@example.com"
    bad = EdgarClient(cfg, "ua", httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>"))), sleep=lambda s: None)
    with pytest.raises(EdgarError):
        bad.company_tickers()
    scalar = EdgarClient(cfg, "ua", httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=5))), sleep=lambda s: None)
    with pytest.raises(EdgarError):
        scalar.company_tickers()


# ---- the step ------------------------------------------------------------------------------------------------------------------
def test_a_listed_company_gets_its_cik_then_revenue_ownership_and_city_and_passes_the_gate(cfg, tmp_path):
    conn = make_db(tmp_path)
    aid = held(conn, "Acme Corp", hq_city=None)
    edgar = SecEdgar()
    logs = []
    s = run_sec_fill(conn, cfg, edgar, AS_OF, log=logs.append)
    a = list_accounts(conn, status=None)[0]
    assert (a.cik, a.revenue, a.ownership, a.hq_city, a.status) == ("0000001234", 1_200_000_000, "public", "Chicago, IL", "active")
    assert s.read == 1 and s.passed_gate == 1 and len(s.filled) == 1 and s.errors == 0
    assert set(s.filled[0].facts) == {"cik", "revenue", "ownership", "hq_city"}
    assert "CIK matched to SEC filer ACME CORP (ACME)" in logs[0] and "now on the scan list" in logs[0]
    rows = audit(conn, aid)
    assert all(r[0] == "sec" for r in rows if r[1] in ("update", "evidence"))
    ev = {r[2]: r[3] for r in rows if r[1] == "evidence"}
    assert ev["cik"] == f"ACME CORP (ACME): {TICKERS_URL}" and ev["revenue"] == facts_url("0000001234")
    assert ev["ownership"] == submissions_url("0000001234") and ev["hq_city"] == submissions_url("0000001234")
    assert ("sec", "status", "status", "active") in rows
    run = conn.execute("SELECT * FROM runs WHERE step = 'sec_fill'").fetchall()
    assert len(run) == 1 and run[0]["account_id"] == aid and run[0]["est_cost"] == 0.0


def test_a_stored_value_is_never_overwritten(cfg, tmp_path):
    conn = make_db(tmp_path)
    aid = held(conn, "Acme Corp", cik=CIK, revenue=5e8, ownership="private", hq_city="Elgin, IL")
    edgar = SecEdgar()
    s = run_sec_fill(conn, cfg, edgar, AS_OF)
    a = list_accounts(conn, status=None)[0]
    assert (a.revenue, a.ownership, a.hq_city) == (5e8, "private", "Elgin, IL") and s.read == 0 and not s.filled   # nothing missing: not even read
    assert edgar.calls == []
    conn.execute("UPDATE accounts SET hq_city = NULL WHERE id = ?", (aid,))
    conn.commit()
    s = run_sec_fill(conn, cfg, edgar, AS_OF)
    a = list_accounts(conn, status=None)[0]
    assert (a.revenue, a.ownership, a.hq_city) == (5e8, "private", "Chicago, IL")              # only the gap is filled
    assert ("facts", CIK) not in edgar.calls                                                  # revenue is on file: the facts file is not fetched
    assert [f.facts for f in s.filled] == [["hq_city"]]


def test_companies_that_cannot_be_the_sec_filer_are_not_looked_up(cfg, tmp_path):
    conn = make_db(tmp_path)
    held(conn, "Acme Corp", ownership="pe-backed", sponsor="Big PE")
    add_account(conn, "CME Group", ownership="private")
    held(conn, "Wintrust Financial", status="excluded")
    held(conn, "Twin Industries")                        # ambiguous
    held(conn, "BMO US")                                  # not listed
    edgar = SecEdgar()
    s = run_sec_fill(conn, cfg, edgar, AS_OF)
    assert not s.filled and s.read == 0
    assert dict(s.unmatched) == {"Twin Industries": "ambiguous (2 different SEC filers)", "BMO US": "no match"}
    assert all(a.cik is None for a in list_accounts(conn, status=None))
    assert not conn.execute("SELECT 1 FROM audit_log WHERE actor = 'sec'").fetchone()
    assert not conn.execute("SELECT 1 FROM runs WHERE step = 'sec_fill'").fetchone()        # nothing was asked of the SEC about them


def test_a_company_list_that_cannot_be_read_is_reported_and_companies_with_a_cik_are_still_filled(cfg, tmp_path):
    conn = make_db(tmp_path)
    held(conn, "Acme Corp", cik=CIK)
    held(conn, "CME Group")
    logs = []
    s = run_sec_fill(conn, cfg, SecEdgar(list_error=EdgarError("HTTP 503")), AS_OF, log=logs.append)
    assert "company list could not be read (HTTP 503)" in logs[0] and "companies with a CIK on file are still read" in logs[0]
    assert [f.account.name for f in s.filled] == ["Acme Corp"]
    logs.clear()
    s = run_sec_fill(conn, cfg, SecEdgar(raw={"unexpected": "layout", "x": 1}), AS_OF, log=logs.append)   # reachable but not the layout we know
    assert "no usable entries" in logs[0] and not s.filled and s.read == 0
    assert next(a for a in list_accounts(conn, status=None) if a.name == "CME Group").cik is None


def test_a_matched_company_keeps_its_cik_even_when_its_sec_files_cannot_be_read(cfg, tmp_path):
    conn = make_db(tmp_path)
    held(conn, "CME Group")
    edgar = SecEdgar(errors={("submissions", "0000005678"): EdgarError("HTTP 503"), ("facts", "0000005678"): EdgarError("HTTP 500")})
    logs = []
    s = run_sec_fill(conn, cfg, edgar, AS_OF, log=logs.append)
    a = list_accounts(conn, status=None)[0]
    assert (a.cik, a.revenue, a.ownership, a.status) == ("0000005678", None, None, "active") and s.errors == 2
    assert "filled cik" in logs[0] and "SEC record not read (HTTP 503)" in logs[0]
    s = run_sec_fill(conn, cfg, SecEdgar(), AS_OF)                         # the next run reads the files it could not before
    assert list_accounts(conn, status=None)[0].revenue == 1_200_000_000 and s.read == 1


def test_sec_trouble_is_noted_per_company_and_never_stops_the_run(cfg, tmp_path):
    conn = make_db(tmp_path)
    held(conn, "Acme Corp", cik=CIK)
    held(conn, "Beta Corp", cik="0000004321")
    edgar = SecEdgar(errors={("submissions", CIK): EdgarError("HTTP 503"), ("facts", CIK): EdgarError("HTTP 500")},
                     subs={"0000004321": SUBS}, facts={"0000004321": {"facts": {}}})
    logs = []
    s = run_sec_fill(conn, cfg, edgar, AS_OF, log=logs.append)
    by = {a.name: a for a in list_accounts(conn, status=None)}
    assert by["Acme Corp"].revenue is None and by["Acme Corp"].ownership is None
    assert any("SEC record not read (HTTP 503)" in x and "SEC financial data not read (HTTP 500)" in x for x in logs)
    assert by["Beta Corp"].ownership == "public" and by["Beta Corp"].revenue is None
    assert any("no recent annual revenue in the SEC data" in x for x in logs)
    assert s.errors == 2 and s.read == 2
    notes = [r["errors"] for r in conn.execute("SELECT errors FROM runs WHERE step = 'sec_fill' ORDER BY id")]
    assert "HTTP 503" in notes[0] and "no recent annual revenue" in notes[1]


def test_the_cap_counts_sec_reads_only_and_the_next_run_starts_with_the_ones_not_reached(cfg, tmp_path):
    conn = make_db(tmp_path)
    for i, name in enumerate(("Aaa", "Bbb", "Ccc"), start=1):
        held(conn, name, cik=f"{i:010d}", hq_city=None)
    held(conn, "Nobody Listed")                                    # a lookup that finds nothing is free and not capped
    edgar = SecEdgar()
    s = run_sec_fill(conn, cfg, edgar, AS_OF, limit=2)
    assert s.read == 2 and s.not_reached == 1 and len(s.filled) == 2 and [n for n, _ in s.unmatched] == ["Nobody Listed"]
    first = {f.account.name for f in s.filled}
    s2 = run_sec_fill(conn, cfg, edgar, AS_OF, limit=2)
    assert {f.account.name for f in s2.filled} == {"Aaa", "Bbb", "Ccc"} - first and s2.not_reached == 0
    assert [a.name for a in select(conn)] == ["Nobody Listed"]


def test_a_cik_a_person_removed_is_not_put_back(cfg, tmp_path):
    from bd.curate import update_account
    from bd.sec_fill import cik_cleared
    conn = make_db(tmp_path)
    aid = held(conn, "Acme Corp")
    run_sec_fill(conn, cfg, SecEdgar(), AS_OF)
    assert list_accounts(conn, status=None)[0].cik == "0000001234" and cik_cleared(conn) == set()
    assert update_account(conn, cfg, aid, {"cik": ""}, actor="ui").ok                      # a person decides it is the wrong company
    assert list_accounts(conn, status=None)[0].cik is None and cik_cleared(conn) == {aid}
    edgar = SecEdgar()
    s = run_sec_fill(conn, cfg, edgar, AS_OF)
    assert list_accounts(conn, status=None)[0].cik is None and s.candidates == 0 and ("list", None) not in edgar.calls
    en = Enricher(cfg, FakeLLM([json.dumps({"facts": model_facts(cik=None, evidence={})})], urls=ALL_URLS), FakeVerifier(), edgar)
    en.no_lookup = cik_cleared(conn)
    assert "cik" not in en.enrich(Account(id=aid, name="Acme Corp"), AS_OF).row


def test_companies_on_hold_come_first_and_the_default_cap_comes_from_config(cfg, tmp_path):
    conn = make_db(tmp_path)
    add_account(conn, "Readyco", cik="0000000010", ownership=None, hq_city=None)       # active, but missing ownership
    held(conn, "Heldco", cik="0000000011")
    assert [a.name for a in select(conn)] == ["Heldco", "Readyco"]
    assert [a.name for a in select(conn, only="ready")] == ["Readyco"]
    assert cfg.sec_fill.max_accounts_per_run == 60


# ---- CLI, job, pages -----------------------------------------------------------------------------------------------------------
def test_the_command_skips_cleanly_without_a_user_agent_and_runs_with_one(cli_env, monkeypatch):    # noqa: F811
    r = runner.invoke(app, ["sec-fill"])
    assert r.exit_code == 0 and "Skipped: BD_USER_AGENT is not set" in r.output
    runner.invoke(app, ["seed", str(ROOT / "accounts.csv")])
    monkeypatch.setenv("BD_USER_AGENT", "SEI BD Research ops@example.com")
    fake = SecEdgar(raw=listing((1, "ABT", "ABBOTT LABORATORIES")))
    monkeypatch.setattr("bd.edgar.EdgarClient", lambda cfg, ua: fake)
    r = runner.invoke(app, ["sec-fill", "--limit", "5"])
    assert r.exit_code == 0 and "SEC fill: read" in r.output and "No cost." in r.output, r.output


def test_sec_fill_is_a_free_job_and_the_first_step_after_the_check():
    assert validate("sec_fill", {}) == ["sec-fill"] and KINDS["sec_fill"][1] is False and "sec-fill" not in PAID_STEPS
    assert sequence_for("sec_fill", ["sec-fill"], "2026-10-16") == [["sec-fill", "--as-of", "2026-10-16"]]
    for research in ("0", "25"):
        upd = validate("update", {"research": research, "segments": "0", "scan": "5", "budget": "5"})
        names = [s[0] for s in sequence_for("update", upd, "2026-10-16")]
        assert names[:2] == ["check", "sec-fill"] and names.index("sec-fill") < (names.index("enrich") if "enrich" in names else 99)


def test_home_and_operate_describe_the_free_step(app_env):
    e = app_env
    assert "Reads the SEC's own data for public companies (free)" in e.get("/").body
    body = e.get("/operate").body
    assert 'value="sec_fill"' in body and "Fill from SEC (free)" in body
    r = e.post("/operate/run", {"kind": "sec_fill"})                  # free: no confirmation tick
    assert r.code == 303 and r.h["Location"].startswith("/jobs/")
    e.app.jobs.wait()
    assert e.app.jobs.recent()[0].args == ["sec-fill"]


# ---- the enricher uses the same lookup ------------------------------------------------------------------------------------------
class ListedEdgar(SecEdgar):
    def company(self, cik):
        return "ACME CORP", ["ACME"]


def enrich_one(cfg, acct, edgar, model=None):
    llm = FakeLLM([json.dumps({"facts": model or model_facts(cik=None, revenue_usd=None, ownership=None, evidence={})})], urls=ALL_URLS)
    return Enricher(cfg, llm, FakeVerifier(), edgar).enrich(acct, AS_OF), llm


def test_enrich_finds_the_cik_in_the_sec_list_when_there_is_none_and_does_not_ask_the_model_for_what_the_sec_has(cfg):
    edgar = ListedEdgar()
    r, llm = enrich_one(cfg, Account(id=1, name="Acme Corp"), edgar)
    ev = dict(p.split("=", 1) for p in r.row["evidence_urls"].split("; "))
    assert r.row["cik"] == "0000001234" and r.row["sec_name"] == "ACME CORP (ACME)" and ev["cik"] == TICKERS_URL
    assert r.row["revenue"] == "1200000000" and r.row["ownership"] == "public" and ev["revenue_usd"] == facts_url("0000001234")
    assert "CIK matched by exact name to the SEC filer ACME CORP (ACME)" in r.row["notes"]
    asked = llm.calls[0][1][0]["content"]
    assert "revenue_usd" not in asked and "ownership" not in asked and "cik" not in asked.split("Facts still needed:")[1].split("\n")[0]
    llm = FakeLLM([json.dumps({"facts": model_facts(cik=None, evidence={})})] * 2, urls=ALL_URLS)
    en = Enricher(cfg, llm, FakeVerifier(), edgar)
    edgar.calls.clear()
    for i, name in enumerate(("Acme Corp", "Other Corp"), start=10):
        en.enrich(Account(id=i, name=name), AS_OF)
    assert edgar.calls.count(("list", None)) == 1                                # the list is fetched once per Enricher, not per company


def test_enrich_leaves_private_companies_and_unlisted_names_to_the_model(cfg):
    edgar = ListedEdgar()
    r, llm = enrich_one(cfg, Account(id=1, name="Acme Corp", ownership="private"), edgar)
    assert "cik" not in r.row and "Facts still needed:" in llm.calls[0][1][0]["content"] and ("list", None) not in edgar.calls
    r, _ = enrich_one(cfg, Account(id=2, name="Unlisted Co"), edgar)
    assert "cik" not in r.row
    broken = ListedEdgar(list_error=EdgarError("HTTP 503"))
    r, _ = enrich_one(cfg, Account(id=3, name="Acme Corp"), broken)
    assert "cik" not in r.row                                                  # no list: the model's own search is the fallback


def test_the_proposal_it_writes_loads_with_the_sec_url_as_evidence_for_the_cik(cfg, tmp_path):
    from bd.curate import apply_all_new, load_proposals
    from bd.enrich import write_proposals
    conn = make_db(tmp_path)
    aid = held(conn, "Acme Corp")
    r, _ = enrich_one(cfg, Account(id=aid, name="Acme Corp"), ListedEdgar())
    path = tmp_path / "p.csv"
    write_proposals([r], path)
    props, errs = load_proposals(conn, path)
    assert errs == [] and {d.column: d.evidence_url for d in props[0].diffs}["cik"] == TICKERS_URL and props[0].sec_name == "ACME CORP (ACME)"
    out = apply_all_new(conn, cfg, path)
    assert out["facts"] >= 3 and list_accounts(conn, status=None)[0].cik == "0000001234"
