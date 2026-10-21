"""The network map's data: what is drawn follows a stated rule, what is not drawn is counted, and a lead never looks like a confirmed holding.
Rows are synthetic and live only in these tests."""

import pytest

from bd import pe_network
from bd.pe_network import PER_FIRM_CAP, build_universe, company_lines, lookup, table_lines
from bd.pe_ownership import add_alias, add_evidence, ensure_ownership, resolve_company
from bd.pe_store import ensure_firm
from bd.score_store import score_accounts
from tests.conftest import add_account, make_db
from tests.test_pe_rollup import TODAY, day, own, signal


@pytest.fixture
def cfg(cfg):
    return cfg.model_copy(deep=True)


@pytest.fixture
def conn(tmp_path):
    return make_db(tmp_path)


def workbook_company(conn, cfg, firm, company, location="Chicago, IL"):
    """A company only the researched workbook speaks for: a lead, with its location kept as unverified evidence."""
    with conn:
        fid = ensure_firm(conn, firm, "manual", "t", f"{firm.split()[0].lower()}.example")[0]
        cid = resolve_company(conn, company, None, today=TODAY).id
        oid, _ = ensure_ownership(conn, fid, cid, TODAY, "platform", "manual")
        add_evidence(conn, "ownership", oid, "listed", "https://src.example/p", retrieved_at="2026-10-10T00:00:00+00:00", source_type="workbook", verification="snippet_only")
        add_evidence(conn, "company", cid, "location", "https://src.example/p", retrieved_at="2026-10-10T00:00:00+00:00", source_type="workbook", verification="snippet_only",
                     excerpt=f"Workbook: Chicago-area location {location}", value=location)
    return fid, cid


def names(u):
    return {n.name: n for n in u.companies.values()}


# ---- what is drawn -------------------------------------------------------------------------------------------------------------------------------------
def test_a_company_is_drawn_when_it_is_on_the_list_has_a_signal_or_a_page_places_it_and_the_rest_is_counted(conn, cfg):
    fid, _, _, a = own(conn, cfg, "Alpha Capital", "Listed Co")
    own(conn, cfg, "Alpha Capital", "Placed Co", account=False)                          # a completed deal on a fetched page, no account, no place
    workbook_company(conn, cfg, "Alpha Capital", "Ring Co")                                # only the workbook says Chicago
    own(conn, cfg, "Alpha Capital", "Faraway Co", account=False)
    u = build_universe(conn, cfg, TODAY)
    drawn = names(u)
    assert "Listed Co" in drawn and drawn["Listed Co"].on_list and drawn["Listed Co"].placement == "confirmed"
    assert "Ring Co" in drawn and drawn["Ring Co"].placement == "workbook" and not drawn["Ring Co"].on_list
    assert "Placed Co" not in drawn and "Faraway Co" not in drawn                             # nothing places or scans them
    f = next(f for f in u.all_firms if f.name == "Alpha Capital")
    assert (f.current, f.leads, f.drawn, f.collapsed) == (3, 1, 2, 2)


def test_a_company_shown_to_be_outside_the_area_is_never_drawn(conn, cfg):
    own(conn, cfg, "Alpha Capital", "Faraway Co", chicagoland="no")
    assert names(build_universe(conn, cfg, TODAY)) == {}


def test_at_most_thirty_companies_are_drawn_per_firm_the_strongest_first_and_the_rest_is_a_count(conn, cfg):
    fid, *_ = own(conn, cfg, "Alpha Capital", "Co 00")
    for i in range(1, PER_FIRM_CAP + 5):
        own(conn, cfg, "Alpha Capital", f"Co {i:02d}")
    aid = conn.execute("SELECT id FROM accounts WHERE name = 'Co 33'").fetchone()[0]
    signal(conn, cfg, aid, "leadership_change", 5)
    signal(conn, cfg, aid, "open_exec_search", 6)
    score_accounts(conn, cfg, TODAY)
    u = build_universe(conn, cfg, TODAY)
    f = next(f for f in u.all_firms if f.id == fid)
    assert len(u.companies) == PER_FIRM_CAP and f.drawn == PER_FIRM_CAP and f.collapsed == 5
    assert "Co 33" in names(u)                                                                 # a company with signals outranks quiet ones for a place


def test_a_lead_is_a_dotted_tie_and_past_ownership_appears_only_when_asked_for(conn, cfg):
    own(conn, cfg, "Alpha Capital", "Held Co")
    own(conn, cfg, "Alpha Capital", "Lead Co", claim="listed")
    own(conn, cfg, "Alpha Capital", "Old Co", claim="exited")
    base = build_universe(conn, cfg, TODAY)
    assert {n for n in names(base)} == {"Held Co", "Lead Co"} and {t.status for t in base.ties} == {"current", "unclear"}
    past = build_universe(conn, cfg, TODAY, past=True)
    assert "Old Co" in names(past) and "historical" in {t.status for t in past.ties}


def test_a_peer_is_drawn_as_a_peer_and_never_assessed_as_a_prospect(conn, cfg):
    own(conn, cfg, "Alpha Capital", "West Monroe", account=False)
    u = build_universe(conn, cfg, TODAY)
    peer = names(u)["West Monroe"]
    assert peer.peer and peer.category == "Insufficient evidence"
    assert [ln.company.peer for ln in company_lines(u)] == [True]


def test_a_company_two_firms_hold_has_a_tie_to_each_and_a_one_firm_view_adds_the_co_investors(conn, cfg):
    fid, *_ = own(conn, cfg, "Alpha Capital", "Shared Co")
    own(conn, cfg, "Beta Partners", "Shared Co")
    own(conn, cfg, "Alpha Capital", "Solo Co")
    own(conn, cfg, "Gamma Fund", "Other Co")
    u = build_universe(conn, cfg, TODAY)
    assert sorted(names(u)["Shared Co"].firms) == sorted(f.id for f in u.all_firms if f.name in ("Alpha Capital", "Beta Partners"))
    one = build_universe(conn, cfg, TODAY, firm_id=fid)
    assert set(names(one)) == {"Shared Co", "Solo Co"} and [f.name for f in one.co_firms] == ["Beta Partners"] and [f.name for f in one.firms] == ["Alpha Capital"]
    assert {t.firm_id for t in one.ties} == {fid, next(f.id for f in u.all_firms if f.name == "Beta Partners")}


def test_the_priority_filter_keeps_only_companies_of_that_category(conn, cfg):
    own(conn, cfg, "Alpha Capital", "Quiet Co")
    _, _, _, a = own(conn, cfg, "Alpha Capital", "Busy Co")
    for t, age in (("leadership_change", 5), ("restructuring_layoffs", 8), ("platform_migration", 10)):
        signal(conn, cfg, a, t, age)
    score_accounts(conn, cfg, TODAY)
    everything = names(build_universe(conn, cfg, TODAY))
    key = everything["Busy Co"].key
    assert key != "none" and names(build_universe(conn, cfg, TODAY, cat=key)) == {"Busy Co": everything["Busy Co"]} | {k: v for k, v in everything.items() if v.key == key}
    assert "Busy Co" not in names(build_universe(conn, cfg, TODAY, cat="none"))
    assert names(build_universe(conn, cfg, TODAY, cat="not-a-category")).keys() == everything.keys()          # an unknown value is ignored, not an error


def test_only_a_company_that_something_can_speak_for_is_assessed(conn, cfg, monkeypatch):
    own(conn, cfg, "Alpha Capital", "Listed Co")
    workbook_company(conn, cfg, "Alpha Capital", "Ring Co")
    own(conn, cfg, "Alpha Capital", "Faraway Co", account=False)
    seen = []
    real = pe_network.assess_company
    monkeypatch.setattr(pe_network, "assess_company", lambda c, cfg_, cid, today: seen.append(cid) or real(c, cfg_, cid, today))
    build_universe(conn, cfg, TODAY)
    listed = conn.execute("SELECT id FROM pe_companies WHERE name = 'Listed Co'").fetchone()[0]
    assert seen == [listed]                                                                    # the workbook lead and the unplaced company are not assessed


def test_building_the_picture_does_not_run_a_query_per_company_per_firm(conn, cfg):
    for i in range(120):
        own(conn, cfg, f"Firm {i % 6}", f"Co {i:03d}", account=i % 2 == 0)
    calls = []
    conn.set_trace_callback(calls.append)
    u = build_universe(conn, cfg, TODAY)
    conn.set_trace_callback(None)
    assert len(u.companies) > 0 and len(calls) < 120 * 40                                       # a generous ceiling that an accidental firms-times-companies loop would break


# ---- the table and the lookup -------------------------------------------------------------------------------------------------------------------------
def test_the_table_counts_holdings_and_leads_apart_and_names_what_the_companies_show(conn, cfg):
    fid, _, _, a = own(conn, cfg, "Alpha Capital", "Busy Co")
    own(conn, cfg, "Alpha Capital", "Lead Co", claim="listed")
    signal(conn, cfg, a, "leadership_change", 5)
    signal(conn, cfg, a, "open_exec_search", 8)
    score_accounts(conn, cfg, TODAY)
    u = build_universe(conn, cfg, TODAY)
    ln = next(l for l in table_lines(conn, cfg, TODAY, u) if l.firm.name == "Alpha Capital")
    assert (ln.firm.current, ln.firm.leads, ln.firm.with_signals) == (1, 1, 1) and ln.themes and ln.themes[0][1] == 1
    assert ln.reading == "no portfolio page registered"


def test_the_lookup_finds_a_company_by_any_name_and_says_when_the_ledger_has_no_record(conn, cfg):
    _, cid, _, a = own(conn, cfg, "Alpha Capital", "Acme Foods Inc")
    own(conn, cfg, "Beta Partners", "Acme Foods Inc", claim="listed")
    with conn:
        add_alias(conn, "company", cid, "Acme Brands")
    hit = lookup(conn, TODAY, "acme")[0]
    assert hit.name == "Acme Foods Inc" and [(n, s) for _, n, s in hit.investors] == [("Alpha Capital", "current"), ("Beta Partners", "unclear")] and hit.href == f"/companies/{a}"
    assert [h.name for h in lookup(conn, TODAY, "brands")] == ["Acme Foods Inc"]
    assert lookup(conn, TODAY, "nobody at all") == [] and lookup(conn, TODAY, "a") == []         # no record, or too short to mean anything
    assert lookup(conn, TODAY, "100%") == []                                                    # a wildcard typed in is not a pattern
