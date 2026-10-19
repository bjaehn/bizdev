import csv
import json
import sqlite3

import pytest

from bd.accounts import Account, list_accounts, seed_csv
from bd.edgar import EdgarError
from bd.enrich import Enricher, run_enrich, select_accounts, write_proposals
from bd.fit_gate import apply_fit_gate
from tests.conftest import AS_OF, FakeLLM, FakeVerifier, make_db

SITE = "https://www.acme.com/about"
SEC_10K = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0000001234&type=10-K"
NEWS = "https://news.example/acme-revenue"
COUNTY_PAGE = "https://www.acme.com/locations"
ALL_URLS = (SITE, SEC_10K, NEWS, COUNTY_PAGE, "https://acme.com/investors")


def facts(**over):
    f = {"domain": "acme.com", "hq_city": "Chicago", "hq_state": "IL", "il_county": "Cook County",
         "revenue_usd": 5.0e8, "employees": 2500, "ownership": "public", "sponsor": None,
         "industry": "Industrial equipment", "cik": "1234", "notes": "FY2025 revenue.",
         "evidence": {"il_county": {"url": COUNTY_PAGE, "source_type": "company_site"},
                      "revenue_usd": {"url": SEC_10K, "source_type": "sec_filing"},
                      "employees": {"url": SITE, "source_type": "company_site"},
                      "ownership": {"url": SEC_10K, "source_type": "sec_filing"},
                      "cik": {"url": SEC_10K, "source_type": "sec_filing"}}}
    f.update(over)
    return f


class FakeEdgar:
    def __init__(self, name="ACME CORP", tickers=("ACME",), error=None):
        self.name, self.tickers, self.error = name, list(tickers), error

    def company(self, cik):
        if self.error:
            raise self.error
        return self.name, self.tickers


def enrich(cfg, f, acct=None, edgar="default", bad=None, urls=ALL_URLS, replies=None):
    llm = FakeLLM(replies or [json.dumps({"facts": f})], urls=urls)
    en = Enricher(cfg, llm, FakeVerifier(bad), FakeEdgar() if edgar == "default" else edgar)
    return en.enrich(acct or Account(id=1, name="Acme Corp"), AS_OF), llm


def test_full_proposal_with_evidence(cfg):
    r, llm = enrich(cfg, facts())
    assert r.error is None
    assert r.row == {"name": "Acme Corp", "domain": "acme.com", "hq_city": "Chicago, IL", "industry": "Industrial equipment",
                     "chicagoland": "yes", "revenue": "500000000", "employees": "2500", "ownership": "public",
                     "cik": "0000001234", "sec_name": "ACME CORP (ACME)",
                     "evidence_urls": f"il_county={COUNTY_PAGE}; revenue_usd={SEC_10K}; employees={SITE}; "
                                      f"ownership={SEC_10K}; cik={SEC_10K}",
                     "notes": "model note: FY2025 revenue."}
    assert r.dropped == []
    assert "ONLY" not in llm.calls[0][1][0]["content"] and "Acme Corp" in llm.calls[0][1][0]["content"]
    assert "never guess" in llm.calls[0][0]


def test_chicagoland_is_derived_from_the_county_by_code(cfg):
    for county, expect in (("Cook", "yes"), ("DuPage County", "yes"), ("mchenry", "yes"), ("Lake County", "yes")):
        r, _ = enrich(cfg, facts(il_county=county))
        assert r.row.get("chicagoland") == expect, county


def test_county_outside_the_six_is_left_unknown_and_no_is_never_proposed(cfg):
    r, _ = enrich(cfg, facts(il_county="Winnebago County"))
    assert "chicagoland" not in r.row
    assert any("Winnebago" in d and "never proposed automatically" in d for d in r.dropped)
    r, _ = enrich(cfg, facts(il_county=None, hq_state="TX", hq_city="Dallas"))
    assert r.row.get("chicagoland") is None and "no" not in r.row.values()


def test_evidence_url_must_come_from_the_search_results(cfg):
    r, _ = enrich(cfg, facts(), urls=(SITE, SEC_10K, NEWS))       # county page was never retrieved
    assert "chicagoland" not in r.row and any("il_county: evidence URL was not in the search results" in d for d in r.dropped)


def test_missing_unreachable_and_blocked_evidence(cfg):
    ev = facts()["evidence"]
    r, _ = enrich(cfg, facts(evidence={k: v for k, v in ev.items() if k != "employees"}))
    assert "employees" not in r.row and any("employees: no evidence URL" in d for d in r.dropped)
    r, _ = enrich(cfg, facts(), bad={SITE: "http_404"})
    assert "employees" not in r.row and any("employees: evidence URL unreachable (http_404)" in d for d in r.dropped)
    li = "https://www.linkedin.com/company/acme"
    ev2 = {**ev, "employees": {"url": li, "source_type": "company_site"}}
    r, _ = enrich(cfg, facts(evidence=ev2), urls=ALL_URLS + (li,))
    assert "employees" not in r.row and any("blocked domain" in d for d in r.dropped)


def test_size_from_a_secondary_source_is_rejected_but_reported(cfg):
    ev = {**facts()["evidence"], "revenue_usd": {"url": NEWS, "source_type": "secondary_report"}}
    r, _ = enrich(cfg, facts(evidence=ev))
    assert "revenue" not in r.row
    assert any("revenue_usd: only a secondary source (news.example)" in d for d in r.dropped)
    assert "secondary source" in r.row["notes"]


def test_company_site_claim_on_a_foreign_domain_does_not_count_as_company_evidence(cfg):
    ev = {**facts()["evidence"], "employees": {"url": NEWS, "source_type": "company_site"}}
    r, _ = enrich(cfg, facts(evidence=ev))
    assert "employees" not in r.row and any("employees: only a secondary source" in d for d in r.dropped)


def test_fields_the_account_already_has_are_never_proposed(cfg):
    known = Account(id=1, name="Acme Corp", domain="acme.com", hq_city="Chicago", chicagoland="no", revenue=1.0,
                    employees=5, ownership="private", sponsor="X", industry="Y", cik="0000009999")
    r, _ = enrich(cfg, facts(sponsor="Someone", evidence={**facts()["evidence"], "sponsor": {"url": SITE, "source_type": "company_site"}}), acct=known)
    assert set(r.row) <= {"name", "notes"}


def test_cik_rules(cfg):
    ev = facts()["evidence"]
    # evidence must be a sec.gov page containing that CIK
    r, _ = enrich(cfg, facts(evidence={**ev, "cik": {"url": SITE, "source_type": "company_site"}}))
    assert "cik" not in r.row and any("sec.gov page containing that CIK" in d for d in r.dropped)
    r, _ = enrich(cfg, facts(cik="9999"))
    assert "cik" not in r.row
    for bad in ("12ab", "0", "123456789012", ""):
        r, _ = enrich(cfg, facts(cik=bad))
        assert "cik" not in r.row
    # EDGAR must confirm it exists; without EDGAR it is not proposed
    r, _ = enrich(cfg, facts(), edgar=FakeEdgar(error=EdgarError("HTTP 404")))
    assert "cik" not in r.row and any("EDGAR could not confirm it (HTTP 404)" in d for d in r.dropped)
    r, _ = enrich(cfg, facts(), edgar=None)
    assert "cik" not in r.row and any("not confirmed with EDGAR" in d for d in r.dropped)


def test_domain_must_resolve_and_not_be_blocked(cfg):
    r, _ = enrich(cfg, facts(), bad={"https://acme.com": "http_404"})
    assert "domain" not in r.row and any("domain: acme.com did not resolve" in d for d in r.dropped)
    r, _ = enrich(cfg, facts(domain="https://www.linkedin.com/company/acme"))
    assert "domain" not in r.row


def test_invalid_output_twice_gives_error_and_name_only_row(cfg):
    r, llm = enrich(cfg, None, replies=["nope", "still nope"])
    assert r.error and r.error.startswith("InvalidJSON") and r.row == {"name": "Acme Corp"} and len(llm.calls) == 2
    assert r.usage.total_tokens == 300


def test_unknown_evidence_key_is_invalid(cfg):
    bad = facts(evidence={"hq_city": {"url": SITE, "source_type": "company_site"}})
    r, _ = enrich(cfg, bad, replies=[json.dumps({"facts": bad})] * 2)
    assert r.error and "evidence keys" in r.error


# ---- CSV round trip: proposals -> human review -> bd seed -> fit gate --------------------------------

def test_proposals_csv_loads_with_seed_and_unblocks_the_fit_gate(cfg, tmp_path):
    conn = make_db(tmp_path)
    seed_csv(conn, _csv(tmp_path, "name\nAcme Corp\nOther Co\n", "names.csv"))
    assert apply_fit_gate(conn, cfg).held                                   # both held: nothing known
    accounts = list_accounts(conn, status=None)
    r, _ = enrich(cfg, facts(), acct=accounts[0])
    out = tmp_path / "proposals.csv"
    n = write_proposals([r, type(r)(account=accounts[1], row={"name": "Other Co"})], out)
    assert n == 1
    header = out.read_text().splitlines()[0].split(",")
    assert header[-3:] == ["evidence_urls", "notes", "sec_name"]            # review columns are present
    res = seed_csv(conn, out)                                               # ...and tolerated by seed
    assert res.errors == [] and (res.inserted, res.updated) == (0, 2)
    a = conn.execute("SELECT * FROM accounts WHERE name='Acme Corp'").fetchone()
    assert (a["domain"], a["chicagoland"], a["revenue"], a["employees"], a["ownership"], a["cik"]) == \
           ("acme.com", "yes", 5e8, 2500, "public", "0000001234")
    g = apply_fit_gate(conn, cfg)
    assert [x.name for x in g.passed] == ["Acme Corp"] and [x.name for x, _ in g.held] == ["Other Co"]


def _csv(tmp_path, body, name):
    p = tmp_path / name
    p.write_text(body)
    return p


def test_seed_cik_normalised_and_validated(tmp_path):
    conn = make_db(tmp_path)
    assert seed_csv(conn, _csv(tmp_path, "name,cik\nA,1234\n", "a.csv")).errors == []
    assert conn.execute("SELECT cik FROM accounts").fetchone()[0] == "0000001234"
    for bad in ("12x", "0", "12345678901"):
        res = seed_csv(conn, _csv(tmp_path, f"name,cik\nB,{bad}\n", "b.csv"))
        assert any("cik" in e for e in res.errors)


def test_old_database_without_cik_column_is_migrated_and_keeps_its_rows(tmp_path):
    from bd.db import init_db
    c = sqlite3.connect(tmp_path / "old.sqlite")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("""CREATE TABLE accounts (
        id INTEGER PRIMARY KEY, name TEXT NOT NULL, domain TEXT, hq_city TEXT,
        chicagoland TEXT NOT NULL DEFAULT 'unknown' CHECK (chicagoland IN ('yes','no','unknown')),
        revenue REAL, employees INTEGER, ownership TEXT, sponsor TEXT, industry TEXT,
        tier TEXT NOT NULL DEFAULT 'A', status TEXT NOT NULL DEFAULT 'active', last_scanned TEXT)""")
    c.execute("INSERT INTO accounts(name, domain) VALUES ('Legacy Co', 'legacy.com')")
    c.commit()
    assert "cik" not in {r[1] for r in c.execute("PRAGMA table_info(accounts)")}
    init_db(c)
    init_db(c)   # idempotent
    assert "cik" in {r[1] for r in c.execute("PRAGMA table_info(accounts)")}
    row = c.execute("SELECT name, domain, cik FROM accounts").fetchone()
    assert (row["name"], row["domain"], row["cik"]) == ("Legacy Co", "legacy.com", None)


# ---- batch runner ----------------------------------------------------------------------------------

def test_select_accounts(cfg, tmp_path):
    conn = make_db(tmp_path)
    seed_csv(conn, _csv(tmp_path, "name\nAlpha\nBeta\nGamma\n", "n.csv"))
    conn.execute("UPDATE accounts SET status='active' WHERE name='Alpha'")
    conn.execute("UPDATE accounts SET status='excluded' WHERE name='Gamma'")
    conn.execute("UPDATE accounts SET status='hold_review' WHERE name='Beta'")
    conn.commit()
    assert [a.name for a in select_accounts(conn, False, None, None)] == ["Beta"]
    assert [a.name for a in select_accounts(conn, True, None, None)] == ["Alpha", "Beta"]   # excluded never enriched
    assert [a.name for a in select_accounts(conn, True, "alp", None)] == ["Alpha"]
    assert len(select_accounts(conn, True, None, 1)) == 1


def test_run_enrich_logs_cost_per_account_and_never_touches_accounts(cfg, tmp_path):
    conn = make_db(tmp_path)
    seed_csv(conn, _csv(tmp_path, "name\nAlpha\nBeta\n", "n.csv"))
    accts = list_accounts(conn, status=None)
    before = [tuple(r) for r in conn.execute("SELECT * FROM accounts ORDER BY id")]
    llm = FakeLLM([json.dumps({"facts": facts()})] * 2, urls=ALL_URLS)
    en = Enricher(cfg, llm, FakeVerifier(), FakeEdgar())
    results, spent, hit = run_enrich(conn, cfg, en, accts, AS_OF, "claude-sonnet-5-5", workers=1)
    assert [r.account.name for r in results] == ["Alpha", "Beta"] and not hit
    per = 100 * 2 / 1e6 + 50 * 10 / 1e6 + 0.01
    rows = conn.execute("SELECT * FROM runs WHERE step='enrich' ORDER BY account_id").fetchall()
    assert len(rows) == 2 and all(r["tokens"] == 150 and r["est_cost"] == pytest.approx(per) for r in rows)
    assert spent == pytest.approx(2 * per)
    assert before == [tuple(r) for r in conn.execute("SELECT * FROM accounts ORDER BY id")]   # proposals only


def test_run_enrich_budget_cap_and_failure_isolation(cfg, tmp_path):
    conn = make_db(tmp_path)
    seed_csv(conn, _csv(tmp_path, "name\nA\nB\nC\nD\n", "n.csv"))
    accts = list_accounts(conn, status=None)
    replies = [json.dumps({"facts": facts()})] * 4
    en = Enricher(cfg, FakeLLM(replies, urls=ALL_URLS), FakeVerifier(), FakeEdgar())
    results, spent, hit = run_enrich(conn, cfg, en, accts, AS_OF, "claude-sonnet-5-5", workers=1, max_cost=0.03)
    assert hit and 1 <= len(results) < 4

    class Boom:
        def enrich(self, a, as_of):
            raise RuntimeError("bug")
    results, *_ = run_enrich(conn, cfg, Boom(), accts[:2], AS_OF, None, workers=1)
    assert all(r.error == "RuntimeError: bug" for r in results) and len(results) == 2


def test_the_prompt_names_only_the_facts_still_needed(cfg):
    from bd.enrich import needed_facts
    known = Account(id=1, name="Acme Corp", domain="acme.com", hq_city="Chicago", industry="Food", chicagoland="yes", revenue=5e8,
                    employees=900, ownership="private", sponsor=None, cik=None)
    assert needed_facts(known) == []                      # private: neither sponsor nor CIK is needed
    assert needed_facts(Account(id=1, name="X", ownership="public", domain="x.com", hq_city="Chicago", industry="Food", revenue=1, employees=1)) == ["il_county", "cik"]
    r, llm = enrich(cfg, {"il_county": None}, acct=Account(id=1, name="HCSC", domain="hcsc.com", hq_city="Chicago", industry="Insurance", revenue=1e10,
                                                           employees=20000, ownership="private"))
    assert "Facts still needed: il_county." in llm.calls[0][1][0]["content"]


def test_facts_the_model_did_not_return_are_noted_not_silent(cfg):
    a = Account(id=1, name="HCSC", domain="hcsc.com", hq_city="Chicago", industry="Insurance", revenue=1e10, employees=20000, ownership="private")
    r, _ = enrich(cfg, {"il_county": None}, acct=a)
    assert r.missing == ["il_county"] and r.dropped == [] and r.row["notes"] == "not found: il_county"
    r, _ = enrich(cfg, facts(il_county="Cook"), acct=a)
    assert r.missing == [] and r.row["chicagoland"] == "yes"
    r, _ = enrich(cfg, {"il_county": "Winnebago", "evidence": {"il_county": {"url": COUNTY_PAGE, "source_type": "company_site"}}}, acct=a)
    assert r.missing == [] and any("Winnebago" in d for d in r.dropped)       # found but refused is a drop, not 'not found'


def test_the_county_may_come_from_the_address_the_page_states():
    from importlib import resources
    p = resources.files("bd.prompts").joinpath("enrich.md").read_text(encoding="utf-8")
    assert "a company page rarely states the county itself" in p
    assert "Cite the page that states the address" in p


def test_companies_never_researched_come_first_and_stuck_ones_do_not_hog_the_batch(tmp_path):
    from bd.enrich import select_accounts
    from tests.conftest import add_account, make_db
    conn = make_db(tmp_path)
    ids = [add_account(conn, n, status="hold_review", chicagoland="unknown", revenue=None) for n in ("Alpha", "Beta", "Gamma", "Delta")]
    assert [a.name for a in select_accounts(conn, False, None, None)] == ["Alpha", "Beta", "Gamma", "Delta"]
    for i, day in ((0, "2026-10-10"), (1, "2026-10-12")):
        conn.execute("INSERT INTO runs(run_date,step,account_id,accounts_scanned,signals_found,tokens,est_cost) VALUES (?,?,?,?,?,?,?)",
                     (day, "enrich", ids[i], 1, 0, 100, 0.1))
    conn.commit()
    assert [a.name for a in select_accounts(conn, False, None, None)] == ["Gamma", "Delta", "Alpha", "Beta"]      # untried, then oldest attempt
    assert [a.name for a in select_accounts(conn, False, None, 2)] == ["Gamma", "Delta"]
