import json
from datetime import date

import pytest

from bd.accounts import Account
from bd.curate import load_proposals
from bd.edgar import EdgarError, facts_url, submissions_url
from bd.enrich import Enricher, needed_facts, write_proposals
from bd.fit_gate import check_fit
from bd.sec_facts import business_hq, is_public, latest_revenue, read
from tests.conftest import AS_OF, FakeLLM, FakeVerifier, make_db
from tests.test_enrich import ALL_URLS, COUNTY_PAGE, SEC_10K, facts as model_facts

CIK = "0000001234"


def fy(end, val, filed, form="10-K"):
    e = date.fromisoformat(end)
    return {"start": e.replace(year=e.year - 1).isoformat(), "end": end, "val": val, "accn": "0000000001-26-000010", "fy": e.year,
            "fp": "FY", "form": form, "filed": filed}


def xbrl(revenue=1_200_000_000, concept="Revenues", end="2026-06-30", filed="2026-08-25"):
    return {"facts": {"us-gaap": {concept: {"units": {"USD": [fy(end, revenue, filed)]}}}}}


SUBS = {"name": "ACME CORP", "tickers": ["ACME"], "exchanges": ["NYSE"], "filings": {"recent": {}},
        "addresses": {"business": {"street1": "1 Main St", "city": "CHICAGO", "stateOrCountry": "IL", "zipCode": "60601"}}}


class SecEdgar:
    """The bits of EdgarClient the enricher uses."""
    def __init__(self, subs=SUBS, facts=None, subs_error=None, facts_error=None):
        self.subs, self.facts = subs, xbrl() if facts is None else facts
        self.subs_error, self.facts_error, self.calls = subs_error, facts_error, []

    def company(self, cik):
        return "ACME CORP", ["ACME"]

    def submissions(self, cik):
        self.calls.append(("submissions", cik))
        if self.subs_error:
            raise self.subs_error
        return self.subs

    def company_facts(self, cik):
        self.calls.append(("facts", cik))
        if self.facts_error:
            raise self.facts_error
        return self.facts


def enrich(cfg, model, acct=None, edgar=None):
    llm = FakeLLM([json.dumps({"facts": model})], urls=ALL_URLS)
    en = Enricher(cfg, llm, FakeVerifier(), edgar or SecEdgar())
    return en.enrich(acct or Account(id=1, name="Acme Corp"), AS_OF), llm


# ---- pure reading --------------------------------------------------------------------------------------------------------
def test_latest_revenue_uses_the_newest_tag_and_ignores_stale_or_odd_figures():
    assert latest_revenue(xbrl(), AS_OF) == (1_200_000_000, date(2026, 6, 30))
    assert latest_revenue(xbrl(concept="RevenueFromContractWithCustomerExcludingAssessedTax"), AS_OF)[0] == 1_200_000_000
    old = xbrl(end="2024-06-30", filed="2024-08-25")
    assert latest_revenue(old, AS_OF) is None                          # ended more than ~18 months ago
    assert latest_revenue(xbrl(revenue=-5), AS_OF) is None and latest_revenue({"facts": {}}, AS_OF) is None
    both = xbrl(concept="SalesRevenueNet", end="2025-06-30", filed="2025-08-25")
    both["facts"]["us-gaap"]["Revenues"] = {"units": {"USD": [fy("2026-06-30", 900, "2026-08-25")]}}
    assert latest_revenue(both, AS_OF) == (900, date(2026, 6, 30))     # the tag with the newest year wins


def test_public_means_a_listed_ticker_and_hq_needs_a_us_address():
    assert is_public(SUBS) and is_public({"exchanges": ["Nasdaq"]}) and not is_public({"tickers": [], "exchanges": [""]}) and not is_public({})
    assert business_hq(SUBS) == "Chicago, IL"
    assert business_hq({"addresses": {"business": {"city": "TORONTO", "stateOrCountry": "A6"}}}) is None
    assert business_hq({"addresses": {"business": {"city": "", "stateOrCountry": "IL"}}}) is None and business_hq({}) is None


def test_read_degrades_quietly():
    out = read(SUBS, None, AS_OF)
    assert out.public and out.hq == "Chicago, IL" and out.revenue is None
    assert read(None, {"facts": {}}, AS_OF).notes == ["no recent annual revenue in the SEC data"]


# ---- in the enricher ---------------------------------------------------------------------------------------------------------
def test_revenue_ownership_and_hq_come_from_the_sec_with_its_url_as_evidence(cfg):
    r, _ = enrich(cfg, model_facts(revenue_usd=None, ownership=None, hq_city=None, hq_state=None,
                                   evidence={"il_county": {"url": COUNTY_PAGE, "source_type": "company_site"},
                                             "cik": {"url": SEC_10K, "source_type": "sec_filing"}}))
    assert r.row["revenue"] == "1200000000" and r.row["ownership"] == "public" and r.row["hq_city"] == "Chicago, IL"
    ev = dict(p.split("=", 1) for p in r.row["evidence_urls"].split("; "))
    assert ev["revenue_usd"] == facts_url(CIK) and ev["ownership"] == submissions_url(CIK) and ev["il_county"] == COUNTY_PAGE
    assert "SEC: revenue FY2026 from SEC XBRL" in r.row["notes"]


def test_the_sec_value_replaces_a_model_value_but_nothing_already_on_file(cfg):
    r, _ = enrich(cfg, model_facts(revenue_usd=7e8))                    # model proposed 700M from the 10-K page
    assert r.row["revenue"] == "1200000000"                              # the filing itself wins
    known = Account(id=1, name="Acme Corp", revenue=5e8, ownership="private", hq_city="Elgin")
    edgar = SecEdgar()
    r, _ = enrich(cfg, model_facts(), acct=known, edgar=edgar)
    assert "revenue" not in r.row and "ownership" not in r.row and r.row.get("hq_city") is None
    assert ("facts", CIK) not in edgar.calls                             # revenue is on file: the facts file is not even fetched


def test_an_account_with_a_cik_on_file_is_read_without_asking_the_model_for_those_facts(cfg):
    acct = Account(id=1, name="Acme Corp", cik=CIK, domain="acme.com", hq_city="Chicago", industry="Industrial", chicagoland="yes")
    assert needed_facts(acct, from_sec=True) == ["employees"] and "revenue_usd" in needed_facts(acct)
    r, llm = enrich(cfg, {"employees": None}, acct=acct)
    assert "Facts still needed: employees." in llm.calls[0][1][0]["content"]
    assert r.row["revenue"] == "1200000000" and r.row["ownership"] == "public"
    assert r.missing == ["employees"]


def test_sec_trouble_is_noted_and_never_blocks(cfg):
    r, _ = enrich(cfg, model_facts(revenue_usd=None, ownership=None),
                  edgar=SecEdgar(facts_error=EdgarError("HTTP 500"), subs_error=EdgarError("HTTP 503")))
    assert "revenue" not in r.row and "SEC record not read (HTTP 503)" in r.row["notes"] and "SEC financial data not read (HTTP 500)" in r.row["notes"]
    r, _ = enrich(cfg, model_facts(revenue_usd=None), edgar=SecEdgar(facts={"facts": {}}))
    assert "no recent annual revenue in the SEC data" in r.row["notes"] and "revenue" not in r.row


def test_without_an_sec_client_or_cik_nothing_changes(cfg):
    class OldEdgar:                                       # the test double used elsewhere: company() only
        def company(self, cik):
            return "ACME CORP", ["ACME"]
    r, _ = enrich(cfg, model_facts(revenue_usd=None, evidence={"il_county": {"url": COUNTY_PAGE, "source_type": "company_site"}}),
                  edgar=OldEdgar())
    assert "revenue" not in r.row and "SEC:" not in r.row.get("notes", "")
    llm = FakeLLM([json.dumps({"facts": model_facts(cik=None, evidence={})})], urls=ALL_URLS)
    r = Enricher(cfg, llm, FakeVerifier(), SecEdgar()).enrich(Account(id=1, name="Private Co"), AS_OF)
    assert "revenue" not in r.row                           # a company with no CIK is never looked up


def test_the_fit_gate_passes_once_the_sec_revenue_arrives(cfg, tmp_path):
    r, _ = enrich(cfg, model_facts(revenue_usd=None, employees=None, ownership=None))
    conn = make_db(tmp_path)
    csv_path = tmp_path / "p.csv"
    conn.execute("INSERT INTO accounts(name, status) VALUES ('Acme Corp', 'hold_review')")
    conn.commit()
    write_proposals([r], csv_path)
    props, errs = load_proposals(conn, csv_path)
    assert errs == []
    p = props[0]
    ev = {d.column: d.evidence_url for d in p.diffs}
    assert ev["revenue"] == facts_url(CIK) and ev["ownership"] == submissions_url(CIK) and ev["chicagoland"] == COUNTY_PAGE
    acct = Account(id=1, name="Acme Corp", **{d.column: d.proposed for d in p.diffs if d.column in ("chicagoland", "revenue", "ownership")})
    assert check_fit(acct, [], cfg).verdict == "pass"
