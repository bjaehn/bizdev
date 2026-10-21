"""The private equity section of the Analysis page: companies worth a call and what sponsors did lately. Rows are synthetic and live only in these tests."""

from datetime import date, timedelta

import pytest

from bd.pe_home import CALL_LIMIT, FEED_DAYS, pe_home
from bd.pe_ownership import add_evidence, ensure_ownership, recompute, record_add_on, resolve_company
from bd.pe_store import ensure_firm
from tests.conftest import AS_OF, add_account, add_signal, make_db

TODAY = AS_OF


@pytest.fixture
def cfg(cfg):
    return cfg.model_copy(deep=True)


def day(age):
    return (TODAY - timedelta(days=age)).isoformat()


def own(conn, cfg, firm, company, claim="completed", age=20, st="company_release", verification="page_verified", url=None, account=True, firm_domain=None):
    """A sponsor, a company and one piece of evidence about the deal. The company is the account of the same name when `account`."""
    with conn:
        if account:
            add_account(conn, company)
        fid = ensure_firm(conn, firm, "manual", "t", firm_domain or f"{firm.split()[0].lower()}.example")[0]
        cid = resolve_company(conn, company, None, today=TODAY).id
        oid, _ = ensure_ownership(conn, fid, cid, TODAY, "platform", "announcement")
        add_evidence(conn, "ownership", oid, claim, url or f"https://news.example/{company.replace(' ', '-').lower()}", retrieved_at="2026-10-10T00:00:00+00:00", source_type=st,
                     verification=verification, content_hash="h" if verification == "page_verified" else "",
                     excerpt=f"{firm} {claim} its acquisition of {company}." if verification == "page_verified" else None, event=day(age))
        recompute(conn, oid, TODAY, cfg, True)
    return fid, cid, oid


def account_id(conn, name):
    return conn.execute("SELECT id FROM accounts WHERE name = ?", (name,)).fetchone()[0]


def test_with_no_sponsors_there_is_nothing_to_show_and_it_says_why(cfg, tmp_path):
    h = pe_home(make_db(tmp_path), cfg, TODAY)
    assert "No sponsors are tracked yet" in h.empty and h.calls == [] and h.feed == [] and h.backed == 0


def test_a_sponsor_owned_company_with_strong_evidence_is_on_the_call_list_with_its_sponsor_and_latest_fact(cfg, tmp_path):
    conn = make_db(tmp_path)
    own(conn, cfg, "Alpha Capital", "Acme Foods", age=60)
    a = account_id(conn, "Acme Foods")
    add_signal(conn, cfg, a, "rfp_match", 8, source_type="sec_filing", summary="Open RFP for a finance transformation partner.", url="https://sec.example/rfp")
    add_signal(conn, cfg, a, "platform_migration", 12, source_type="sec_filing", summary="Announces an ERP migration.", url="https://sec.example/erp")
    h = pe_home(conn, cfg, TODAY)
    assert h.backed == 1 and [c.name for c in h.calls] == ["Acme Foods"]
    c = h.calls[0]
    assert c.category in ("Prospect now", "Validate first") and c.sponsors == ["Alpha Capital"] and c.account_id == a and c.score > 0
    assert c.latest_date == TODAY - timedelta(days=8) and c.latest_url == "https://sec.example/rfp" and "RFP" in c.latest_label
    assert c.possible_need and h.by_category[c.category] == 1 and "No sponsors" not in h.empty


def test_a_company_with_thin_evidence_is_counted_but_not_listed_and_an_unclear_lead_is_only_counted(cfg, tmp_path):
    conn = make_db(tmp_path)
    own(conn, cfg, "Alpha Capital", "Quiet Co")                                         # a completed deal, nothing else: Monitor, not a call
    own(conn, cfg, "Beta Partners", "Lead Co", claim="listed", verification="page_verified", age=3)      # a listing that does not say current: unclear
    status = {r["name"]: r["status"] for r in conn.execute("SELECT c.name, o.status FROM pe_ownership o JOIN pe_companies c ON c.id = o.company_id")}
    assert status["Quiet Co"] in ("current", "pending") and status["Lead Co"] == "unclear"
    h = pe_home(conn, cfg, TODAY)
    assert h.backed == 1 and h.calls == [] and h.unclear_leads == 1


def test_the_call_list_is_ranked_by_category_then_score_and_capped(cfg, tmp_path):
    conn = make_db(tmp_path)
    for i in range(CALL_LIMIT + 3):
        own(conn, cfg, "Alpha Capital", f"Co {i}", age=50)
        a = account_id(conn, f"Co {i}")
        add_signal(conn, cfg, a, "rfp_match", 5 + i, source_type="sec_filing", url=f"https://sec.example/{i}")
        add_signal(conn, cfg, a, "platform_migration", 6 + i, source_type="sec_filing", url=f"https://sec.example/m{i}")
    h = pe_home(conn, cfg, TODAY)
    assert len(h.calls) == CALL_LIMIT and h.backed == CALL_LIMIT + 3
    keys = [(["Prospect now", "Validate first"].index(c.category), -c.score) for c in h.calls]
    assert keys == sorted(keys)


def test_a_company_shown_to_be_outside_the_area_is_not_listed_but_is_counted_as_such(cfg, tmp_path):
    conn = make_db(tmp_path)
    from bd.pe_ownership import record_location
    _, cid, _ = own(conn, cfg, "Alpha Capital", "Faraway Co", age=60)
    a = account_id(conn, "Faraway Co")
    add_signal(conn, cfg, a, "rfp_match", 5, source_type="sec_filing")
    _, cid2, _ = own(conn, cfg, "Alpha Capital", "Texas Co", age=60, account=False)                 # not an account: its headquarters comes from a retrieved page
    with conn:
        conn.execute("UPDATE accounts SET chicagoland = 'no' WHERE id = ?", (a,))
        record_location(conn, "company", cid2, "headquarters", "Austin", "TX", None, TODAY)
    h = pe_home(conn, cfg, TODAY)
    assert h.out_of_area == 2 and h.backed == 0 and h.calls == []


def test_the_feed_has_only_dated_sourced_rows_inside_the_window_newest_first(cfg, tmp_path):
    conn = make_db(tmp_path)
    fid, cid, oid = own(conn, cfg, "Alpha Capital", "Acme Foods", claim="completed", age=10)
    own(conn, cfg, "Alpha Capital", "Old Deal Co", claim="completed", age=FEED_DAYS + 20)                              # too old
    own(conn, cfg, "Alpha Capital", "Rumour Co", claim="completed", age=5, st="search_snippet", verification="snippet_only")     # never a fact
    with conn:
        conn.execute("INSERT INTO firm_signals(firm_id, type, event_date, detected_date, summary, source_url, source_type, confidence, hash) VALUES (?,?,?,?,?,?,?,?,?)",
                     (fid, "firm_new_fund", day(3), day(2), "Alpha Capital closed Fund VII.", "https://alpha.example/fund7", "firm_site", "High", "h1"))
        conn.execute("INSERT INTO firm_signals(firm_id, type, event_date, detected_date, summary, source_url, source_type, confidence, hash) VALUES (?,?,?,?,?,?,?,?,?)",
                     (fid, "firm_exit", day(4), day(2), "A low-confidence rumour.", "https://alpha.example/rumour", "secondary_report", "Low", "h2"))
    a = account_id(conn, "Acme Foods")
    add_signal(conn, cfg, a, "leadership_change", 7, source_type="sec_filing", summary="Names a new chief executive.", url="https://sec.example/ceo")
    other = add_account(conn, "Unowned Co")
    add_signal(conn, cfg, other, "leadership_change", 6, source_type="sec_filing", summary="Names a new CFO.", url="https://sec.example/cfo")      # not sponsor-owned: not a sponsor story
    add_signal(conn, cfg, other, "pe_addon", 9, source_type="company_release", summary="Platform buys a rival.", url="https://news.example/addon")  # a PE signal is one anywhere
    feed = pe_home(conn, cfg, TODAY).feed
    assert [f.date for f in feed] == sorted((f.date for f in feed), reverse=True)
    texts = " | ".join(f"{f.label}: {f.who}: {f.text}" for f in feed)
    assert "Sponsor: new fund: Alpha Capital: Alpha Capital closed Fund VII." in texts
    assert "Leadership change: Acme Foods (Alpha Capital): Names a new chief executive." in texts
    assert "Deal completed: Alpha Capital: Acme Foods: Alpha Capital completed its acquisition of Acme Foods." in texts
    assert "PE add-on acquisition: Unowned Co: Platform buys a rival." in texts
    assert "Old Deal Co" not in texts and "Rumour Co" not in texts and "low-confidence rumour" not in texts and "new CFO" not in texts
    assert all(f.url.startswith("https://") for f in feed)


def test_an_add_on_is_a_feed_row_and_the_same_url_and_date_is_listed_once(cfg, tmp_path):
    conn = make_db(tmp_path)
    own(conn, cfg, "Alpha Capital", "Platform Co", age=40)
    with conn:
        parent = resolve_company(conn, "Platform Co", None, today=TODAY).id
        child = resolve_company(conn, "Bolt Inc", None, today=TODAY).id
        lid = record_add_on(conn, parent, child, TODAY, closed=day(9), completed=True)
        for n in (1, 2):
            add_evidence(conn, "link", lid, "completed" if n == 1 else "announced", "https://newsroom.example/bolt", retrieved_at="2026-10-10T00:00:00+00:00",
                         source_type="company_release", verification="page_verified", content_hash=f"h{n}", excerpt="Platform Co completed its acquisition of Bolt Inc.", event=day(9))
    feed = pe_home(conn, cfg, TODAY).feed
    rows = [f for f in feed if f.url == "https://newsroom.example/bolt"]
    assert len(rows) == 1 and rows[0].who == "Platform Co" and rows[0].text.startswith("Bolt Inc:")


def test_coverage_says_how_many_sponsor_portfolios_were_read_in_full(cfg, tmp_path):
    conn = make_db(tmp_path)
    full, _, _ = own(conn, cfg, "Alpha Capital", "A Co")
    part, _, _ = own(conn, cfg, "Beta Partners", "B Co")
    own(conn, cfg, "Gamma Equity", "C Co")
    now = TODAY.isoformat() + "T08:00:00+00:00"
    with conn:
        conn.execute("INSERT INTO pe_sources(firm_id, kind, url, origin, status, last_attempt, last_success, coverage) VALUES (?, 'portfolio_page', 'https://alpha.example/p', 'manual', 'ok', ?, ?, 'complete')",
                     (full, now, now))
        conn.execute("INSERT INTO pe_sources(firm_id, kind, url, origin, status, last_attempt, last_success, coverage) VALUES (?, 'portfolio_page', 'https://beta.example/p', 'manual', 'ok', ?, ?, 'partial')",
                     (part, now, now))
    h = pe_home(conn, cfg, TODAY)
    assert h.coverage == "Sponsor portfolios: 1 of 3 read in full, 1 partly, 1 not read yet." and h.coverage_class == "caution"


# ---- the page --------------------------------------------------------------------------------------------------------------------------------------
from tests.test_update import app_env  # noqa: E402,F401  (fixture)


def test_the_analysis_page_shows_the_pe_section_with_escaped_text_and_an_honest_empty_state(app_env):
    from html import unescape
    e = app_env
    page = unescape(e.get("/opportunities").body)
    assert 'id="pe"' in page and "No sponsors are tracked yet" in page and "PE-backed companies worth a call" not in page
    own(e.conn, e.cfg, "Alpha <i>Capital</i>", "Acme Foods", age=60)
    a = account_id(e.conn, "Acme Foods")
    add_signal(e.conn, e.cfg, a, "rfp_match", 8, source_type="sec_filing", summary="Open RFP <b>now</b>.", url="https://sec.example/rfp")
    add_signal(e.conn, e.cfg, a, "platform_migration", 12, source_type="sec_filing", summary="Announces an ERP migration.", url="https://sec.example/erp")
    raw = e.get("/opportunities").body
    body = unescape(raw)
    assert "PE-backed companies worth a call" in body and "Acme Foods" in body and "Possible need (a hypothesis, not a finding)" in body
    assert "Recent sponsor and portfolio activity" in body and "Sponsor portfolios:" in body and "not read yet" in body
    assert "<i>Capital</i>" not in raw and "&lt;i&gt;Capital&lt;/i&gt;" in raw and "<b>now</b>" not in raw          # sponsor names and sourced text are escaped
    assert f'href="/companies/{a}"' in raw and 'class="ring' in raw
    assert body.index("Is it heating up?") < body.index('id="pe"')                                               # under the heating-up chart, above the ranked list


def test_a_sponsor_owned_consultancy_is_a_peer_and_is_left_off_the_call_list_and_the_counts(cfg, tmp_path):
    conn = make_db(tmp_path)
    own(conn, cfg, "Alpha Capital", "West Monroe", age=60)
    a = account_id(conn, "West Monroe")
    add_signal(conn, cfg, a, "rfp_match", 8, source_type="sec_filing", summary="Open RFP.", url="https://sec.example/rfp")
    add_signal(conn, cfg, a, "platform_migration", 12, source_type="sec_filing", summary="Announces an ERP migration.", url="https://sec.example/erp")
    h = pe_home(conn, cfg, TODAY)
    assert h.calls == [] and h.backed == 0
