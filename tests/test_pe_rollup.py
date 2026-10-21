"""Signals across a sponsor's portfolio and the sponsors behind a company: holdings and leads stay apart, an unscanned company is never read as quiet, and sharing
a sponsor is never shown as shared need. Rows are synthetic and live only in these tests."""

from datetime import date, timedelta

import pytest

from bd.pe_ownership import add_evidence, ensure_ownership, recompute, resolve_company
from bd.pe_rollup import SIGNAL_DAYS, company_investors, firm_events, firm_rollup
from bd.pe_store import ensure_firm
from bd.score_store import score_accounts
from tests.conftest import add_account, add_signal, make_db
from tests.test_update import app_env  # noqa: F401  (fixture)

TODAY = date.today()


@pytest.fixture
def cfg(cfg):
    return cfg.model_copy(deep=True)


@pytest.fixture
def conn(tmp_path):
    return make_db(tmp_path)


def day(age):
    return (TODAY - timedelta(days=age)).isoformat()


def own(conn, cfg, firm, company, claim="completed", age=20, account=True, scanned=True, chicagoland="yes", verification="page_verified", **kw):
    """A sponsor, a company and one piece of evidence about the deal. The company is the account of the same name when `account`."""
    with conn:
        row = conn.execute("SELECT id FROM accounts WHERE name = ?", (company,)).fetchone()
        aid = (row[0] if row else add_account(conn, company, chicagoland=chicagoland, **kw)) if account else None
        if aid and scanned:
            conn.execute("UPDATE accounts SET last_scanned = ? WHERE id = ?", (TODAY.isoformat(), aid))
        fid = ensure_firm(conn, firm, "manual", "t", f"{firm.split()[0].lower()}.example")[0]
        cid = resolve_company(conn, company, None, today=TODAY).id
        oid, _ = ensure_ownership(conn, fid, cid, TODAY, "platform", "announcement")
        add_evidence(conn, "ownership", oid, claim, f"https://news.example/{company.replace(' ', '-').lower()}", retrieved_at="2026-10-10T00:00:00+00:00",
                     source_type="company_release", verification=verification, content_hash="h" if verification == "page_verified" else "",
                     excerpt=f"{firm} {claim} its acquisition of {company}." if verification == "page_verified" else None, event=day(age))
        recompute(conn, oid, TODAY, cfg, True)
    return fid, cid, oid, aid


def signal(conn, cfg, aid, type_="leadership_change", age=10, **kw):
    add_signal(conn, cfg, aid, type_, age, as_of=TODAY, **kw)


@pytest.fixture
def world(conn, cfg):
    """Alpha holds A (signals), B (scanned, quiet), C (no account), D (lead), E (exited); Beta also holds A."""
    fid, a_cid, _, a = own(conn, cfg, "Alpha Capital", "Acme Foods")
    own(conn, cfg, "Alpha Capital", "Quiet Co")
    own(conn, cfg, "Alpha Capital", "Ledger Only", account=False)
    own(conn, cfg, "Alpha Capital", "Lead Co", claim="listed")                      # a listing that does not say current: unclear
    own(conn, cfg, "Alpha Capital", "Old Co", claim="exited")
    own(conn, cfg, "Beta Partners", "Acme Foods")
    signal(conn, cfg, a, "leadership_change", 10, summary="Names a new chief financial officer.")
    signal(conn, cfg, a, "platform_migration", 25, summary="Announces an ERP migration.")
    score_accounts(conn, cfg, TODAY)
    return fid, a_cid, a


def by_name(group):
    return {h.name: h for h in group.held}


# ---- the portfolio roll-up ---------------------------------------------------------------------------------------------------------------------------
def test_holdings_and_leads_are_apart_and_an_unscanned_company_is_not_taken_for_quiet(conn, cfg, world):
    fid, _, a = world
    roll = firm_rollup(conn, cfg, fid, TODAY)
    c = roll.confirmed
    assert set(by_name(c)) == {"Acme Foods", "Quiet Co", "Ledger Only"} and set(by_name(roll.leads)) == {"Lead Co"} and roll.historical == 1
    assert (c.total, c.on_list, c.scanned, c.not_on_list, c.unscanned) == (3, 2, 2, 1, 0)
    assert [h.name for h in c.with_signals] == ["Acme Foods"] and [h.name for h in c.quiet] == ["Quiet Co"]
    assert by_name(c)["Ledger Only"].signals == [] and not by_name(c)["Ledger Only"].on_list and "Ledger Only" not in [h.name for h in c.quiet]    # not scanned: not quiet


def test_a_holding_carries_its_own_band_theme_and_signals_newest_first(conn, cfg, world):
    fid, _, a = world
    h = by_name(firm_rollup(conn, cfg, fid, TODAY).confirmed)["Acme Foods"]
    assert h.account_id == a and h.score and h.band and h.scanned
    assert [s.type for s in h.signals] == ["leadership_change", "platform_migration"] and h.signals[0].event_date > h.signals[1].event_date
    assert h.signals[0].url.startswith("https://") and h.theme is not None
    assert firm_rollup(conn, cfg, fid, TODAY, themes=False).confirmed.held[0].themes == []


def test_a_signal_outside_the_window_or_of_low_confidence_is_not_shown(conn, cfg):
    fid, _, _, a = own(conn, cfg, "Alpha Capital", "Acme Foods")
    signal(conn, cfg, a, "leadership_change", SIGNAL_DAYS + 30)
    signal(conn, cfg, a, "platform_migration", 5, conf="Low")
    score_accounts(conn, cfg, TODAY)
    assert firm_rollup(conn, cfg, fid, TODAY).confirmed.held[0].signals == []
    assert [s.type for s in firm_rollup(conn, cfg, fid, TODAY, days=SIGNAL_DAYS + 60).confirmed.held[0].signals] == ["leadership_change"]


def test_a_company_shown_to_be_outside_the_area_is_counted_and_a_peer_is_never_listed(conn, cfg):
    fid, *_ = own(conn, cfg, "Alpha Capital", "Faraway Co", chicagoland="no")
    own(conn, cfg, "Alpha Capital", "West Monroe", account=False)
    c = firm_rollup(conn, cfg, fid, TODAY).confirmed
    assert c.total == 0 and c.outside == 1 and c.peers == 1


def test_a_lead_is_never_counted_as_a_holding_even_with_signals(conn, cfg):
    fid, _, _, a = own(conn, cfg, "Alpha Capital", "Lead Co", claim="listed")
    signal(conn, cfg, a, "leadership_change", 5)
    score_accounts(conn, cfg, TODAY)
    roll = firm_rollup(conn, cfg, fid, TODAY)
    assert roll.confirmed.total == 0 and [h.name for h in roll.leads.with_signals] == ["Lead Co"] and roll.leads.held[0].status == "unclear"


def test_themes_are_counted_from_each_companys_own_signals(conn, cfg):
    fid, *_ = own(conn, cfg, "Alpha Capital", "One Co")
    own(conn, cfg, "Alpha Capital", "Two Co")
    for name in ("One Co", "Two Co"):
        aid = conn.execute("SELECT id FROM accounts WHERE name = ?", (name,)).fetchone()[0]
        signal(conn, cfg, aid, "leadership_change", 8)
        signal(conn, cfg, aid, "open_exec_search", 12)
    score_accounts(conn, cfg, TODAY)
    themes = firm_rollup(conn, cfg, fid, TODAY).confirmed.themes
    assert themes and sum(len(t.companies) for t in themes) == 2 and {h.name for h in themes[0].companies} == {"One Co", "Two Co"}
    assert [t.name for t in firm_rollup(conn, cfg, fid, TODAY).confirmed.shared_themes] == [themes[0].name]       # both companies show it


# ---- the firm's own events -----------------------------------------------------------------------------------------------------------------------------
def test_firm_events_are_dated_sourced_and_never_a_lead(conn, cfg, world):
    fid, *_ = world
    with conn:
        conn.execute("INSERT INTO firm_signals(firm_id, type, event_date, detected_date, summary, source_url, source_type, confidence, hash) "
                     "VALUES (?, 'firm_new_fund', ?, ?, 'Closed a new fund.', 'https://news.example/fund', 'secondary_report', 'High', 'h1')", (fid, day(30), day(29)))
        conn.execute("INSERT INTO firm_signals(firm_id, type, event_date, detected_date, summary, source_url, source_type, confidence, hash) "
                     "VALUES (?, 'firm_new_fund', ?, ?, 'A rumour.', 'https://news.example/rumour', 'rumor', 'Low', 'h2')", (fid, day(3), day(3)))
    ev = firm_events(conn, fid, TODAY)
    labels = [e.label for e in ev]
    assert "Deal completed" in labels and any("fund" in l.lower() for l in labels) and all(e.url and e.date for e in ev)
    assert "A rumour." not in " ".join(e.text for e in ev)                                                         # Low confidence is stored, never shown
    assert "Lead Co" not in " ".join((e.company or "") for e in ev)                                                # a 'listed' claim is not a deal
    assert [e.date for e in ev] == sorted((e.date for e in ev), reverse=True)


def test_a_snippet_only_deal_is_not_an_event(conn, cfg):
    fid, *_ = own(conn, cfg, "Alpha Capital", "Snip Co", claim="completed", verification="snippet_only")
    assert firm_events(conn, fid, TODAY) == []


# ---- the sponsors behind a company -------------------------------------------------------------------------------------------------------------------------
def test_a_company_lists_its_investors_with_their_events_and_the_signals_of_their_other_companies(conn, cfg, world):
    fid, a_cid, a = world
    b = conn.execute("SELECT id FROM accounts WHERE name = 'Quiet Co'").fetchone()[0]
    signal(conn, cfg, b, "restructuring_layoffs", 6, summary="Announces a restructuring.")
    score_accounts(conn, cfg, TODAY)
    inv = {i.name: i for i in company_investors(conn, cfg, a_cid, TODAY)}
    assert set(inv) == {"Alpha Capital", "Beta Partners"} and inv["Alpha Capital"].status == "current" and not inv["Alpha Capital"].lead
    alpha = inv["Alpha Capital"]
    assert [h.name for h in alpha.siblings] == ["Quiet Co"] and alpha.siblings[0].signals[0].type == "restructuring_layoffs"
    assert alpha.siblings_total == 2 and alpha.siblings_not_on_list == 1                                           # Quiet Co and Ledger Only; the latter was never scanned
    assert "Acme Foods" not in [h.name for h in alpha.siblings]                                                    # a company is not its own sibling
    assert inv["Beta Partners"].siblings == [] and inv["Beta Partners"].siblings_total == 0


def test_a_lead_investor_shows_the_sponsors_other_leads_and_a_historical_one_shows_none(conn, cfg):
    _, cid, _, a = own(conn, cfg, "Alpha Capital", "Lead Co", claim="listed")
    own(conn, cfg, "Alpha Capital", "Other Lead", claim="listed")
    own(conn, cfg, "Alpha Capital", "Held Co")
    own(conn, cfg, "Gamma Fund", "Lead Co", claim="exited")
    for name in ("Other Lead", "Held Co"):
        aid = conn.execute("SELECT id FROM accounts WHERE name = ?", (name,)).fetchone()[0]
        signal(conn, cfg, aid, "leadership_change", 7)
    score_accounts(conn, cfg, TODAY)
    inv = {i.name: i for i in company_investors(conn, cfg, cid, TODAY)}
    assert inv["Alpha Capital"].lead and [h.name for h in inv["Alpha Capital"].siblings] == ["Other Lead"]       # its other leads, not the confirmed holding
    assert inv["Gamma Fund"].status == "historical" and inv["Gamma Fund"].siblings == []


def test_an_excluded_firm_is_not_listed_as_an_investor(conn, cfg):
    fid, cid, *_ = own(conn, cfg, "Alpha Capital", "Acme Foods")
    conn.execute("UPDATE pe_firms SET status = 'excluded' WHERE id = ?", (fid,))
    conn.commit()
    assert company_investors(conn, cfg, cid, TODAY) == []


# ---- the pages ---------------------------------------------------------------------------------------------------------------------------------------------------
def test_the_firm_page_shows_the_roll_up_leads_and_events_and_hides_it_on_a_filtered_view(app_env, world):  # noqa: F811
    e = app_env
    fid, _, a = world
    html = e.get(f"/firms/{fid}").body
    assert "Signals across this portfolio" in html and "3 confirmed holdings: 2 on the scan list (2 scanned), 1 with a signal in the last 90 days" in " ".join(html.split())
    assert "1 is not on the scan list" in " ".join(html.split()) or "1 not on the scan list" in " ".join(html.split())
    assert "Leads, not confirmed" in html and "Lead Co" in html and "Names a new chief financial officer." in html and f'href="/companies/{a}"' in html
    assert "Recent from this firm and its companies" in html and "Deal completed" in html
    assert "Signals across this portfolio" not in e.get(f"/firms/{fid}?geo=unknown").body


def test_the_company_page_names_its_investors_and_says_the_siblings_are_not_shared_need(app_env, world):  # noqa: F811
    e = app_env
    fid, a_cid, a = world
    b = e.conn.execute("SELECT id FROM accounts WHERE name = 'Quiet Co'").fetchone()[0]
    signal(e.conn, e.cfg, b, "restructuring_layoffs", 6, summary="Announces a restructuring.")
    score_accounts(e.conn, e.cfg, TODAY)
    html = e.get(f"/companies/{a}").body
    text = " ".join(html.split())
    assert "Alpha Capital" in html and "Beta Partners" in html and "Other companies Alpha Capital holds that have signals" in text
    assert "Same sponsor, not evidence of shared needs" in text and "Announces a restructuring." in html and 'href="/companies/%d"' % b in html


def test_a_ledger_only_company_page_shows_its_investors_too(app_env, world):  # noqa: F811
    e = app_env
    fid, *_ = world
    cid = e.conn.execute("SELECT id FROM pe_companies WHERE name = 'Ledger Only'").fetchone()[0]
    html = e.get(f"/portfolio/{cid}").body
    assert "Alpha Capital" in html and "Same sponsor, not evidence of shared needs" in " ".join(html.split())


# ---- the shared holdings pass ------------------------------------------------------------------------------------------------------------------------------
def test_a_summary_built_from_already_ranked_holdings_equals_one_that_ranks_them_itself(conn, cfg, world):
    from bd.opportunity_model import firm_summary, rank_holdings
    fid, *_ = world
    ranked = rank_holdings(conn, cfg, fid, TODAY, statuses=("current", "pending", "unclear", "historical"))
    a, b = firm_summary(conn, cfg, fid, TODAY), firm_summary(conn, cfg, fid, TODAY, ranked)
    assert (a.holdings, a.by_status, a.assessed, a.by_category, a.coverage_note) == (b.holdings, b.by_status, b.assessed, b.by_category, b.coverage_note)
    assert a.assessed == 4                                      # current, current, current and the lead; the exited holding is not assessed
