"""The ownership ledger's rules. Every fixture here is synthetic and lives only in these tests."""

from datetime import date, timedelta

import pytest

from bd.pe_ownership import (AmbiguousCompany, Facts, add_alias, add_evidence, company_geography, derive_status, ensure_fund, ensure_ownership,
                             evidence_for, facts_from_evidence, hold_days, is_stale, merge_companies, mirror_legacy, record_add_on,
                             record_location, recompute, resolve_company)
from bd.pe_store import ensure_firm, link_company
from tests.conftest import AS_OF, add_account, make_db

TODAY = AS_OF
URL = "https://firm.example/portfolio"
PR = "https://firm.example/news/acme-completes"


@pytest.fixture
def cfg(cfg):
    return cfg.model_copy(deep=True)


@pytest.fixture
def conn(tmp_path):
    return make_db(tmp_path)


def firm(conn, name="Example Capital", **kw):
    with conn:
        return ensure_firm(conn, name, "manual", "test", **kw)[0]


def ev(conn, oid, claim, url=URL, *, st="firm_site", ver="page_verified", event=None, published=None, h="h1", at="2026-10-01T10:00:00+00:00",
       value=None, excerpt="Acme Corp"):
    with conn:
        return add_evidence(conn, "ownership", oid, claim, url, retrieved_at=at, source_type=st, verification=ver, content_hash=h if ver == "page_verified" else "",
                            excerpt=excerpt if ver == "page_verified" else None, event=event, published=published, value=value)[0]


def rel(conn, fid=None, name="Acme Corp", domain=None):
    fid = fid or firm(conn)
    with conn:
        cid = resolve_company(conn, name, domain, today=TODAY).id
        return ensure_ownership(conn, fid, cid, TODAY)[0]


def status(conn, cfg, oid, today=TODAY, verified=False):
    with conn:
        recompute(conn, oid, today, cfg, verified)
    return conn.execute("SELECT * FROM pe_ownership WHERE id = ?", (oid,)).fetchone()


# ---- evidence ---------------------------------------------------------------------------------------------------------------------------
def test_verified_evidence_needs_the_pages_hash_and_an_excerpt(conn):
    oid = rel(conn)
    with pytest.raises(ValueError):
        add_evidence(conn, "ownership", oid, "completed", URL, retrieved_at="2026-10-01T00:00:00+00:00", source_type="firm_site", verification="page_verified")
    with pytest.raises(ValueError):
        add_evidence(conn, "ownership", oid, "made_up", URL, retrieved_at="x", source_type="firm_site", verification="snippet_only")
    with pytest.raises(ValueError):
        add_evidence(conn, "ownership", oid, "completed", URL, retrieved_at="x", source_type="blog", verification="snippet_only")


def test_evidence_is_idempotent_and_a_re_read_updates_when_it_was_last_seen(conn):
    oid = rel(conn)
    a = ev(conn, oid, "listed_current", at="2026-09-01T00:00:00+00:00")
    b = ev(conn, oid, "listed_current", at="2026-10-01T00:00:00+00:00")
    assert a == b and len(evidence_for(conn, "ownership", oid)) == 1
    assert evidence_for(conn, "ownership", oid)[0]["retrieved_at"] == "2026-10-01T00:00:00+00:00"
    ev(conn, oid, "listed_current", h="h2")                                                       # the page changed: a second observation
    assert len(evidence_for(conn, "ownership", oid)) == 2


def test_unknown_dates_stay_unknown_and_a_malformed_date_is_refused(conn):
    oid = rel(conn)
    ev(conn, oid, "announced")
    row = evidence_for(conn, "ownership", oid)[0]
    assert row["event_date"] is None and row["published_date"] is None and row["retrieved_at"]      # retrieval time is not an event date
    with pytest.raises(ValueError):
        ev(conn, oid, "completed", event="October 2026", h="h9")
    long = ev(conn, oid, "stake", h="h3", excerpt="word " * 200, value="majority")
    assert len(conn.execute("SELECT excerpt FROM pe_evidence WHERE id = ?", (long,)).fetchone()[0]) <= 400


# ---- status rules -----------------------------------------------------------------------------------------------------------------------
def test_an_announcement_is_pending_not_current_and_never_sets_a_closing_date(conn, cfg):
    oid = rel(conn)
    ev(conn, oid, "announced", PR, st="company_release", event=(TODAY - timedelta(days=30)).isoformat())
    o = status(conn, cfg, oid)
    assert (o["status"], o["basis"]) == ("pending", "announcement") and o["closed_date"] is None and o["announced_date"]
    assert o["confidence"] == "Medium" and hold_days(o, TODAY) is None


def test_an_old_announcement_does_not_prove_current_ownership(conn, cfg):
    oid = rel(conn)
    ev(conn, oid, "announced", PR, st="company_release", event="2019-03-01")
    o = status(conn, cfg, oid)
    assert o["status"] == "unclear" and o["basis"] == "announcement_only" and "no source states that it closed" in o["conflict_note"]
    assert o["closed_date"] is None and o["confidence"] == "Low"


def test_a_stated_expected_close_keeps_an_older_announcement_pending(conn, cfg):
    oid = rel(conn)
    ev(conn, oid, "announced", PR, st="company_release", event=(TODAY - timedelta(days=400)).isoformat())
    ev(conn, oid, "expected_close", PR, st="company_release", event=(TODAY + timedelta(days=60)).isoformat(), h="h2")
    o = status(conn, cfg, oid)
    assert o["status"] == "pending" and o["expected_close"] == (TODAY + timedelta(days=60)).isoformat() and o["closed_date"] is None


def test_a_completion_makes_the_holding_current_and_dates_come_from_the_source(conn, cfg):
    oid = rel(conn)
    ev(conn, oid, "announced", PR, st="company_release", event="2025-05-01")
    ev(conn, oid, "completed", PR, st="company_release", event="2025-07-15", h="h2")
    o = status(conn, cfg, oid)
    assert (o["status"], o["basis"], o["confidence"]) == ("current", "completion_notice", "High")
    assert o["announced_date"] == "2025-05-01" and o["closed_date"] == "2025-07-15" and o["as_of_date"] == "2025-07-15"
    assert hold_days(o, TODAY) == (TODAY - date(2025, 7, 15)).days


def test_hold_duration_is_unknown_unless_a_source_states_the_closing(conn, cfg):
    oid = rel(conn)
    ev(conn, oid, "completed", PR, st="secondary_report", event=None)                     # a completion with no stated date
    o = status(conn, cfg, oid)
    assert o["status"] == "current" and o["closed_date"] is None and o["confidence"] == "Medium" and hold_days(o, TODAY) is None


def test_a_firm_page_listing_gives_current_with_unknown_closing(conn, cfg):
    oid = rel(conn)
    ev(conn, oid, "listed_current")
    o = status(conn, cfg, oid, verified=True)
    assert (o["status"], o["basis"], o["confidence"]) == ("current", "firm_listing", "Medium")
    assert o["closed_date"] is None and o["last_verified"] == TODAY.isoformat() and o["as_of_date"] == "2026-10-01"
    assert hold_days(o, TODAY) is None


def test_disappearing_from_the_firms_page_is_not_an_exit(conn, cfg):
    oid = rel(conn)
    ev(conn, oid, "listed_current")
    conn.execute("UPDATE pe_ownership SET listing_absent_since = '2026-10-10' WHERE id = ?", (oid,))
    o = status(conn, cfg, oid)
    assert o["status"] == "unclear" and o["basis"] == "listing_absent" and o["exit_date"] is None and "no source states an exit" in o["conflict_note"]


def test_a_completion_still_stands_when_the_firm_page_drops_the_company(conn, cfg):
    oid = rel(conn)
    ev(conn, oid, "completed", PR, st="company_release", event="2025-07-15")
    conn.execute("UPDATE pe_ownership SET listing_absent_since = '2026-10-10' WHERE id = ?", (oid,))
    o = status(conn, cfg, oid)
    assert o["status"] == "current" and "not listed on the firm's page since 2026-10-10" in o["conflict_note"]


def test_a_stated_exit_makes_it_historical_with_the_exit_date_only_if_stated(conn, cfg):
    oid = rel(conn)
    ev(conn, oid, "completed", PR, st="company_release", event="2021-01-05")
    ev(conn, oid, "exited", "https://firm.example/news/sale", st="company_release", event="2025-11-20", h="h2")
    o = status(conn, cfg, oid)
    assert (o["status"], o["basis"]) == ("historical", "exit_notice") and o["exit_date"] == "2025-11-20" and hold_days(o, TODAY) is None
    oid2 = rel(conn, name="Other Co")
    ev(conn, oid2, "exited", "https://firm.example/news/sale2", st="secondary_report", h="h3")
    o2 = status(conn, cfg, oid2)
    assert o2["status"] == "historical" and o2["exit_date"] is None and o2["confidence"] == "Medium"


def test_the_firms_realized_section_means_historical_but_not_a_date(conn, cfg):
    oid = rel(conn)
    ev(conn, oid, "listed_historical")
    o = status(conn, cfg, oid)
    assert (o["status"], o["basis"]) == ("historical", "firm_listing_historical") and o["exit_date"] is None


def test_a_completion_after_a_stated_exit_is_a_conflict_not_a_guess(conn, cfg):
    oid = rel(conn)
    ev(conn, oid, "exited", "https://firm.example/news/sale", st="company_release", event="2022-01-01")
    ev(conn, oid, "completed", PR, st="company_release", event="2024-06-01", h="h2")
    o = status(conn, cfg, oid)
    assert o["status"] == "unclear" and o["basis"] == "conflict" and "later than the stated exit" in o["conflict_note"] and o["confidence"] == "Low"


def test_pages_that_disagree_leave_it_unclear(conn, cfg):
    oid = rel(conn)
    ev(conn, oid, "listed_current", URL)
    ev(conn, oid, "listed_historical", "https://firm.example/other-page", h="h2")
    o = status(conn, cfg, oid)
    assert o["status"] == "unclear" and o["basis"] == "conflict"


def test_a_newer_reading_of_the_same_page_replaces_an_older_one(conn, cfg):
    oid = rel(conn)
    ev(conn, oid, "listed_current", at="2026-06-01T00:00:00+00:00", h="a")
    ev(conn, oid, "listed_historical", at="2026-09-01T00:00:00+00:00", h="b")
    assert status(conn, cfg, oid)["status"] == "historical"
    ev(conn, oid, "listed_current", at="2026-10-01T00:00:00+00:00", h="a")                 # the page changed back
    assert status(conn, cfg, oid)["status"] == "current"


def test_a_listing_without_a_status_label_is_unclear_not_current(conn, cfg):
    oid = rel(conn)
    ev(conn, oid, "listed")
    o = status(conn, cfg, oid)
    assert (o["status"], o["basis"], o["confidence"]) == ("unclear", "listed_unlabeled", "Low") and "does not say whether" in o["conflict_note"]


def test_current_by_contrast_is_flagged_as_inferred_and_low_confidence(conn, cfg):
    oid = rel(conn)
    ev(conn, oid, "listed_current", value="by_contrast")
    o = status(conn, cfg, oid)
    assert (o["status"], o["basis"], o["confidence"]) == ("current", "firm_listing_by_contrast", "Low") and "outside the page's separate exited section" in o["conflict_note"]


def test_one_reading_that_shows_it_in_two_sections_is_a_conflict(conn, cfg):
    oid = rel(conn)
    ev(conn, oid, "listed_current", h="same")
    ev(conn, oid, "listed_historical", h="same2")
    o = status(conn, cfg, oid)
    assert o["status"] == "unclear" and o["basis"] == "conflict"


def test_an_announced_sale_is_noted_but_does_not_make_it_historical(conn, cfg):
    oid = rel(conn)
    ev(conn, oid, "completed", PR, st="company_release", event="2022-02-01")
    ev(conn, oid, "exit_announced", "https://firm.example/news/sale-agreed", st="company_release", event="2026-09-20", h="h2")
    o = status(conn, cfg, oid)
    assert o["status"] == "current" and o["exit_date"] is None and "a sale was announced on 2026-09-20; no source states that it closed" in o["conflict_note"]


def test_unchecked_snippets_never_support_a_status(conn, cfg):
    oid = rel(conn)
    ev(conn, oid, "completed", "https://news.example/x", st="search_snippet", ver="snippet_only", event="2025-01-01")
    ev(conn, oid, "listed_current", "https://news.example/y", st="search_snippet", ver="snippet_only", h="")
    o = status(conn, cfg, oid)
    assert o["status"] == "unclear" and o["closed_date"] is None and len(evidence_for(conn, "ownership", oid)) == 2    # kept, shown as unverified


def test_status_changes_are_logged_with_the_reason(conn, cfg):
    oid = rel(conn)
    ev(conn, oid, "listed_current")
    status(conn, cfg, oid)
    ev(conn, oid, "exited", "https://firm.example/news/sale", st="company_release", event="2026-09-01", h="h2")
    status(conn, cfg, oid)
    log = [(r["from_status"], r["to_status"], r["reason"]) for r in conn.execute("SELECT * FROM pe_ownership_log WHERE ownership_id=? ORDER BY id", (oid,))]
    assert log == [("unclear", "current", "firm_listing"), ("current", "historical", "exit_notice")]
    status(conn, cfg, oid)
    assert conn.execute("SELECT COUNT(*) FROM pe_ownership_log WHERE ownership_id=?", (oid,)).fetchone()[0] == 2          # no change, no log row


def test_stake_is_recorded_only_when_a_source_states_it(conn, cfg):
    oid = rel(conn)
    ev(conn, oid, "listed_current")
    assert status(conn, cfg, oid)["stake"] == "unspecified"
    ev(conn, oid, "stake", PR, st="company_release", value="majority", h="h2")
    assert status(conn, cfg, oid)["stake"] == "majority"
    ev(conn, oid, "stake", "https://news.example/z", st="secondary_report", value="minority", h="h3")
    o = status(conn, cfg, oid)
    assert o["stake"] == "unspecified" and "sources disagree about the stake" in o["conflict_note"]


def test_derive_status_is_a_pure_function_of_its_facts(cfg):
    f = Facts(announced=[(TODAY - timedelta(days=10)).isoformat()], announced_authoritative=True)
    assert derive_status(f, TODAY, cfg) == derive_status(f, TODAY, cfg)
    assert derive_status(Facts(), TODAY, cfg).status == "unclear"
    assert facts_from_evidence([]).listing_state is None


def test_ownership_staleness_is_shown_not_acted_on(conn, cfg):
    oid = rel(conn)
    ev(conn, oid, "listed_current")
    o = status(conn, cfg, oid, today=date(2026, 5, 1), verified=True)
    assert not is_stale(o, date(2026, 5, 2), cfg)
    assert is_stale(o, date(2026, 5, 1) + timedelta(days=cfg.pe_portfolio.ownership_fresh_days + 1), cfg)
    assert o["status"] == "current"                                                        # age alone never changes the status
    assert is_stale(conn.execute("SELECT * FROM pe_ownership WHERE id = ?", (rel(conn, name="Never Checked"),)).fetchone(), TODAY, cfg)


# ---- companies: one entity ----------------------------------------------------------------------------------------------------------------
def test_a_company_with_two_sponsors_is_one_entity_with_two_relationships(conn):
    a, b = firm(conn, "Alpha Capital"), firm(conn, "Beta Partners")
    with conn:
        c1 = resolve_company(conn, "Eptura", "eptura.com", today=TODAY)
        c2 = resolve_company(conn, "Eptura, Inc.", "eptura.com", today=TODAY)
        ensure_ownership(conn, a, c1.id, TODAY)
        ensure_ownership(conn, b, c2.id, TODAY)
    assert c1.id == c2.id and c2.how == "domain" and not c2.created
    assert conn.execute("SELECT COUNT(*) FROM pe_companies").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM pe_ownership WHERE company_id = ?", (c1.id,)).fetchone()[0] == 2


def test_a_name_change_on_the_same_domain_is_an_alias_not_a_second_company(conn):
    with conn:
        a = resolve_company(conn, "Old Name Software", "oldname.com", today=TODAY)
        b = resolve_company(conn, "New Name", "oldname.com", today=TODAY)
    assert a.id == b.id
    assert conn.execute("SELECT alias FROM pe_aliases WHERE entity_type='company' AND entity_id=?", (a.id,)).fetchall()[0]["alias"] == "New Name"
    with conn:
        c = resolve_company(conn, "New Name", None, today=TODAY)                                 # later, found by the new name alone
    assert c.id == a.id and c.how == "alias"


def test_names_match_apart_from_case_punctuation_and_legal_suffix(conn):
    with conn:
        a = resolve_company(conn, "Acme Holdings, LLC", None, today=TODAY)
        b = resolve_company(conn, "ACME HOLDINGS", None, today=TODAY)
    assert a.id == b.id and b.how == "name"


def test_same_name_different_domains_are_different_companies_and_a_bare_name_is_ambiguous(conn):
    with conn:
        a = resolve_company(conn, "Apex", "apex-software.com", today=TODAY)
        b = resolve_company(conn, "Apex", "apex-steel.com", today=TODAY)
    assert a.id != b.id and b.how == "homonym" and b.created
    with pytest.raises(AmbiguousCompany) as e:
        with conn:
            resolve_company(conn, "Apex", None, today=TODAY)
    assert set(e.value.candidates) == {a.id, b.id}


def test_a_holding_that_is_already_an_account_is_one_record(conn):
    acct = add_account(conn, "Midwest Widgets", domain="midwestwidgets.com")
    with conn:
        m = resolve_company(conn, "Midwest Widgets Inc", "midwestwidgets.com", today=TODAY)
        by_name = resolve_company(conn, "Midwest Widgets", None, today=TODAY)
    assert m.id == by_name.id
    assert conn.execute("SELECT account_id FROM pe_companies WHERE id=?", (m.id,)).fetchone()[0] == acct
    other = add_account(conn, "Plain Co")
    with conn:
        x = resolve_company(conn, "Plain Co", None, today=TODAY)
    assert conn.execute("SELECT account_id FROM pe_companies WHERE id=?", (x.id,)).fetchone()[0] == other and x.how == "account"


def test_merging_duplicates_keeps_every_relationship_and_its_evidence(conn, cfg):
    f, g = firm(conn), firm(conn, "Gamma Partners")
    with conn:
        a = resolve_company(conn, "Beta One", None, today=TODAY).id
        b = resolve_company(conn, "Beta Uno", "betauno.com", today=TODAY).id
        oa, _ = ensure_ownership(conn, f, a, TODAY)
        ob, _ = ensure_ownership(conn, g, b, TODAY, "platform")
    ev(conn, oa, "listed_current", h="x1")
    ev(conn, ob, "completed", PR, st="company_release", event="2025-01-01", h="x2")
    with conn:
        merge_companies(conn, a, b)
    assert conn.execute("SELECT COUNT(*) FROM pe_companies").fetchone()[0] == 1
    assert conn.execute("SELECT domain FROM pe_companies WHERE id=?", (a,)).fetchone()[0] == "betauno.com"
    assert conn.execute("SELECT COUNT(*) FROM pe_ownership WHERE company_id=?", (a,)).fetchone()[0] == 2           # two different sponsors: two relationships
    assert conn.execute("SELECT COUNT(*) FROM pe_evidence").fetchone()[0] == 2
    assert "Beta Uno" in [r["alias"] for r in conn.execute("SELECT alias FROM pe_aliases WHERE entity_type='company' AND entity_id=?", (a,))]


def test_merging_two_relationships_with_the_same_sponsor_keeps_one_with_all_evidence(conn, cfg):
    f = firm(conn)
    with conn:
        a = resolve_company(conn, "Gamma One", None, today=TODAY).id
        b = resolve_company(conn, "Gamma Uno", "gammauno.com", today=TODAY).id
        oa, _ = ensure_ownership(conn, f, a, TODAY)
        ob, _ = ensure_ownership(conn, f, b, TODAY)
    ev(conn, oa, "listed_current", h="y1")
    ev(conn, ob, "completed", PR, st="company_release", event="2025-01-01", h="y2")
    with conn:
        merge_companies(conn, a, b)
    assert conn.execute("SELECT COUNT(*) FROM pe_ownership").fetchone()[0] == 1
    assert {r["claim"] for r in evidence_for(conn, "ownership", oa)} == {"listed_current", "completed"}


# ---- places and geography -----------------------------------------------------------------------------------------------------------------
def test_a_firms_headquarters_is_not_its_portfolio_companys(conn, cfg):
    f = firm(conn, hq_city="Chicago")
    with conn:
        cid = resolve_company(conn, "Faraway Co", None, today=TODAY).id
    g = company_geography(conn, cid, cfg)
    assert g.status == "unknown" and "no headquarters shown" in g.reason
    with conn:
        record_location(conn, "firm", f, "headquarters", "Chicago", "IL", None, TODAY)
    assert company_geography(conn, cid, cfg).status == "unknown"                              # the sponsor's city says nothing about the company


def test_company_geography_follows_the_configured_state_and_says_what_is_missing(conn, cfg):
    with conn:
        out = resolve_company(conn, "Texas Co", None, today=TODAY).id
        inn = resolve_company(conn, "Illinois Co", None, today=TODAY).id
        record_location(conn, "company", out, "headquarters", "Austin", "TX", None, TODAY)
        record_location(conn, "company", inn, "headquarters", "Naperville", "IL", None, TODAY)
        record_location(conn, "company", out, "office", "Chicago", "IL", None, TODAY)           # a branch office does not make it local
    g_out = company_geography(conn, out, cfg)
    assert g_out.status == "out_of_scope" and "outside IL" in g_out.reason
    g_in = company_geography(conn, inn, cfg)
    assert g_in.status == "unknown" and "scope_cities is not configured" in g_in.reason
    cfg.pe_portfolio.scope_cities = ["naperville"]
    assert company_geography(conn, inn, cfg).status == "in_scope"
    cfg.pe_portfolio.scope_cities = ["Chicago"]
    assert company_geography(conn, inn, cfg).status == "unknown" and "county not verified" in company_geography(conn, inn, cfg).reason


def test_an_accounts_verified_facts_decide_geography(conn, cfg):
    add_account(conn, "Local Co", chicagoland="yes", hq_city="Evanston")
    add_account(conn, "Remote Co", chicagoland="no", hq_city="Omaha")
    with conn:
        a = resolve_company(conn, "Local Co", None, today=TODAY).id
        b = resolve_company(conn, "Remote Co", None, today=TODAY).id
    assert company_geography(conn, a, cfg).status == "in_scope" and company_geography(conn, b, cfg).status == "out_of_scope"


# ---- add-ons, funds, legacy -----------------------------------------------------------------------------------------------------------------
def test_an_add_on_is_a_link_between_companies_and_a_completion_is_not_walked_back(conn):
    with conn:
        platform = resolve_company(conn, "Platform Co", None, today=TODAY).id
        target = resolve_company(conn, "Bolt On Inc", None, today=TODAY).id
        lid = record_add_on(conn, platform, target, TODAY, announced="2026-08-01")
    assert conn.execute("SELECT status FROM pe_company_links WHERE id=?", (lid,)).fetchone()[0] == "announced"
    with conn:
        record_add_on(conn, platform, target, TODAY, announced="2026-08-01", closed="2026-09-15")
        record_add_on(conn, platform, target, TODAY, announced="2026-08-01")
    row = conn.execute("SELECT * FROM pe_company_links WHERE id=?", (lid,)).fetchone()
    assert (row["status"], row["closed_date"]) == ("completed", "2026-09-15")
    with pytest.raises(ValueError):
        record_add_on(conn, platform, platform, TODAY)


def test_one_sponsor_and_one_company_is_one_relationship_whose_role_is_refined_not_duplicated(conn, cfg):
    f = firm(conn)
    with conn:
        c = resolve_company(conn, "Acme Corp", None, today=TODAY).id
        a, made = ensure_ownership(conn, f, c, TODAY)                                       # found on the firm's page: role not stated
        b, again = ensure_ownership(conn, f, c, TODAY, "platform", "announcement")          # a press release says it is a platform investment
        z, _ = ensure_ownership(conn, f, c, TODAY, "co_invest")                              # a refinement never overrides a stated role
    assert a == b == z and made and not again
    assert conn.execute("SELECT relationship FROM pe_ownership WHERE id = ?", (a,)).fetchone()[0] == "platform"
    assert conn.execute("SELECT COUNT(*) FROM pe_ownership").fetchone()[0] == 1


def test_the_firms_page_listing_a_company_while_an_announcement_has_no_completion_is_current_with_the_conflict_stated(conn, cfg):
    oid = rel(conn)
    ev(conn, oid, "listed_current")
    ev(conn, oid, "announced", PR, st="company_release", event="2026-09-20", h="h2")
    ev(conn, oid, "expected_close", PR, st="company_release", event="2026-12-31", h="h3")
    o = status(conn, cfg, oid)
    assert (o["status"], o["basis"], o["closed_date"]) == ("current", "firm_listing", None)
    assert "announcement on 2026-09-20 expected the deal to close 2026-12-31" in o["conflict_note"] and "no source states the closing date" in o["conflict_note"]


def test_a_place_shown_on_a_listing_is_named_in_the_geography_reason_but_is_not_a_headquarters(conn, cfg):
    with conn:
        cid = resolve_company(conn, "Office Co", None, today=TODAY).id
        record_location(conn, "company", cid, "office", "Evanston", "IL", None, TODAY)
    g = company_geography(conn, cid, cfg)
    assert g.status == "unknown" and "Evanston, IL is shown by a retrieved page, but no page says it is the headquarters" in g.reason


def test_funds_are_per_firm_and_idempotent(conn):
    a, b = firm(conn, "Alpha Capital"), firm(conn, "Beta Partners")
    with conn:
        f1 = ensure_fund(conn, a, "Alpha Fund IV, L.P.", 2021)
        assert ensure_fund(conn, a, "Alpha Fund IV LP") == f1
        assert ensure_fund(conn, b, "Alpha Fund IV LP") != f1


def test_legacy_links_enter_the_ledger_as_unverified_never_as_current(conn, cfg):
    f = firm(conn)
    acct = add_account(conn, "Legacy Co", ownership="pe-backed", sponsor="Example Capital")
    with conn:
        link_company(conn, f, "Legacy Co", None, "https://news.example/legacy", date(2026, 3, 1), acct, "current", "portfolio")
        link_company(conn, f, "Sold Co", None, None, date(2026, 3, 1), None, "exited", "portfolio")
    assert mirror_legacy(conn, TODAY, cfg) == 2 and mirror_legacy(conn, TODAY, cfg) == 0
    rows = conn.execute("SELECT o.*, c.name, c.account_id FROM pe_ownership o JOIN pe_companies c ON c.id = o.company_id ORDER BY c.name").fetchall()
    assert [(r["name"], r["status"], r["basis"], r["confidence"], r["origin"]) for r in rows] == [
        ("Legacy Co", "unclear", "legacy_unverified", "Low", "legacy"), ("Sold Co", "unclear", "legacy_unverified", "Low", "legacy")]
    assert rows[0]["account_id"] == acct
    ev_rows = evidence_for(conn, "ownership", rows[0]["id"])
    assert len(ev_rows) == 1 and ev_rows[0]["verification"] == "snippet_only" and ev_rows[0]["excerpt"] is None


def test_a_relationship_already_in_the_ledger_is_not_overwritten_by_a_legacy_link(conn, cfg):
    f = firm(conn)
    oid = rel(conn, f, "Verified Co")
    ev(conn, oid, "listed_current")
    status(conn, cfg, oid)
    with conn:
        link_company(conn, f, "Verified Co", None, None, date(2026, 3, 1), None, "exited", "portfolio")
    mirror_legacy(conn, TODAY, cfg)
    assert conn.execute("SELECT status FROM pe_ownership WHERE id=?", (oid,)).fetchone()[0] == "current"
