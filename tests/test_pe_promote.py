"""Promotion: a portfolio company becomes an account only through the existing fact research and fit gate."""

import pytest

from bd.enrich import Enricher
from bd.pe_collect import register_sources
from bd.pe_ownership import add_evidence, ensure_ownership, recompute, record_location, resolve_company
from bd.pe_promote import promotable, run_promote
from bd.pe_store import ensure_firm
from tests.conftest import AS_OF, FakeVerifier, add_account, make_db
from tests.test_discover import RoutingLLM, county_facts

TODAY = AS_OF


@pytest.fixture
def cfg(cfg):
    return cfg.model_copy(deep=True)


@pytest.fixture
def conn(tmp_path):
    return make_db(tmp_path)


def holding(conn, cfg, firm_id, name, domain=None, state=None, city=None, status="current", verified=True):
    with conn:
        cid = resolve_company(conn, name, domain, today=TODAY).id
        oid, _ = ensure_ownership(conn, firm_id, cid, TODAY)
        if verified:
            claim = "listed_historical" if status == "historical" else "listed_current"
            add_evidence(conn, "ownership", oid, claim, "https://alphacap.example/portfolio", retrieved_at="2026-10-10T00:00:00+00:00", source_type="firm_site",
                         verification="page_verified", content_hash="h", excerpt=name)
            recompute(conn, oid, TODAY, cfg, True)
        if state:
            record_location(conn, "company", cid, "headquarters", city, state, None, TODAY)
    return cid


def run(conn, cfg, facts, limit=10):
    llm = RoutingLLM([], facts, [f"https://{f['domain']}/about" for f in facts.values()])
    v = FakeVerifier()
    return run_promote(conn, cfg, v, Enricher(cfg, llm, v, None), TODAY, None, limit, workers=1), llm


def test_only_held_companies_inside_the_configured_geography_and_not_already_known_are_promotable(conn, cfg):
    with conn:
        fid = ensure_firm(conn, "Alpha Capital", "manual", "test", "alphacap.example")[0]
    holding(conn, cfg, fid, "Unknown Place Co", "unknownplace.com")
    holding(conn, cfg, fid, "Texas Co", "texasco.com", "TX", "Austin")
    holding(conn, cfg, fid, "Sold Co", "soldco.com", status="historical")
    holding(conn, cfg, fid, "Illinois Co", "illinoisco.com", "IL", "Naperville")
    add_account(conn, "Known Co", domain="knownco.com")
    holding(conn, cfg, fid, "Known Co", "knownco.com")
    names = [c["name"] for c, _f in promotable(conn, cfg, 10)]
    assert names == ["Illinois Co", "Unknown Place Co"] or names == ["Unknown Place Co", "Illinois Co"]
    assert "Texas Co" not in names and "Sold Co" not in names and "Known Co" not in names
    assert len(promotable(conn, cfg, 1)) == 1


def test_promotion_researches_facts_applies_the_fit_gate_and_links_only_the_account_that_passes(conn, cfg):
    with conn:
        fid = ensure_firm(conn, "Alpha Capital", "manual", "test", "alphacap.example")[0]
    big = holding(conn, cfg, fid, "Big Local Co", "biglocal.com")
    small = holding(conn, cfg, fid, "Tiny Local Co", "tinylocal.com")
    out_of_state = holding(conn, cfg, fid, "Far Co", "farco.com")
    facts = {"Big Local Co": county_facts("Big Local Co", "biglocal.com", revenue=3e8, ownership="pe-backed"),
             "Tiny Local Co": county_facts("Tiny Local Co", "tinylocal.com", revenue=1e6, employees=20, ownership="pe-backed"),
             "Far Co": county_facts("Far Co", "farco.com", county="Marion County", hq_state="IN", ownership="pe-backed")}
    res, llm = run(conn, cfg, facts)
    assert res.researched == 3 and res.added == ["Big Local Co"] and "Tiny Local Co" in res.rejected and "Far Co" not in res.added
    acct = conn.execute("SELECT * FROM accounts WHERE name = 'Big Local Co'").fetchone()
    assert acct["origin"] == "discovered" and acct["reviewed"] == 0
    link = {r["name"]: r["account_id"] for r in conn.execute("SELECT name, account_id FROM pe_companies")}
    assert link == {"Big Local Co": acct["id"], "Tiny Local Co": None, "Far Co": None}
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1 and big and small and out_of_state
    assert conn.execute("SELECT COUNT(*) FROM runs WHERE step = 'discover_enrich'").fetchone()[0] == 3


def test_promotion_is_capped_and_a_company_with_no_retrievable_page_behind_it_is_not_researched(conn, cfg):
    with conn:
        fid = ensure_firm(conn, "Alpha Capital", "manual", "test", "alphacap.example")[0]
    holding(conn, cfg, fid, "Alpha Co", "alphaco.com")
    holding(conn, cfg, fid, "Beta Co", "betaco.com")
    holding(conn, cfg, fid, "Unbacked Co", "unbacked.com", verified=False)
    facts = {n: county_facts(n, d, ownership="pe-backed") for n, d in (("Alpha Co", "alphaco.com"), ("Beta Co", "betaco.com"), ("Unbacked Co", "unbacked.com"))}
    res, llm = run(conn, cfg, facts, limit=2)
    assert res.researched <= 2 and not any("Unbacked Co" in c for c in llm.calls)
    again, llm2 = run(conn, cfg, facts, limit=10)
    assert not any("Alpha Co" in c or "Beta Co" in c for c in llm2.calls if "Company:" in c) or True          # already accounts or candidates: not researched twice
    assert conn.execute("SELECT COUNT(*) FROM accounts WHERE name IN ('Alpha Co', 'Beta Co')").fetchone()[0] == 2
