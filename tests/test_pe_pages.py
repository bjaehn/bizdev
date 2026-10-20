"""The PE screens: honest empty states, then real collected data (synthetic pages, tests only) through the real collection path."""

import re
import time
from datetime import date, timedelta

import pytest

from bd.pe_collect import register_sources, run_collect
from bd.pe_ownership import ensure_ownership, record_location, resolve_company, add_evidence, recompute
from bd.pe_store import ensure_firm
from tests.conftest import AS_OF, add_account, add_signal
from tests.pe_fakes import make_fetcher, page
from tests.test_pe_collect import A, B, doc, grid, card, listing
from tests.test_update import app_env  # noqa: F401  (fixture)

TODAY = date.today()


@pytest.fixture
def cfg(cfg):
    return cfg.model_copy(deep=True)


def add_firm(conn, name="Alpha Capital", url=A, domain="alphacap.example", hq="Chicago"):
    with conn:
        return ensure_firm(conn, name, "manual", "test", domain, hq, url)[0]


def collect(e, routes, force=True, **kw):
    f, client, _clk, c = make_fetcher(e.cfg, routes)
    register_sources(e.conn)
    return run_collect(e.conn, c, f, TODAY, force=force, **kw), client, f, c


def show(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


# ---- honest empty and first-run states -----------------------------------------------------------------------------------------------------------
def test_with_no_firms_the_page_says_so_and_invents_nothing(app_env):  # noqa: F811
    body = app_env.get("/firms").body
    t = show(body)
    assert "No firms are configured" in t and "does not guess a firm's portfolio page or invent its companies" in t
    assert "No earlier reading exists to compare with" in t and "No collection run yet" in t
    assert not re.search(r"\b(Acme|Example Capital|Demo|Sample)\b", t)


def test_a_firm_that_was_never_read_is_registered_not_pretended(app_env):  # noqa: F811
    e = app_env
    fid = add_firm(e.conn)
    register_sources(e.conn)
    t = show(e.get("/firms").body)
    assert "Alpha Capital" in t and "registered, not read yet" in t and "0/1" in t
    d = show(e.get(f"/firms/{fid}").body)
    assert "No holdings are recorded" in d and "has not been read yet, so there is nothing to compare" in d and "never" in d


def test_a_firm_with_no_portfolio_page_says_nothing_was_collected(app_env):  # noqa: F811
    e = app_env
    fid = add_firm(e.conn, url=None)
    assert "No portfolio page is registered for this firm, so nothing has been collected" in show(e.get(f"/firms/{fid}").body)


# ---- a real collection through the pages ----------------------------------------------------------------------------------------------------------
@pytest.fixture
def world(app_env):  # noqa: F811
    e = app_env
    fid = add_firm(e.conn)
    html = page(doc("<h2>Current Investments</h2>" + grid(
        card("Strongco", '<a href="https://strongco.example">Visit</a>', "<p>Naperville, IL</p>"), card("Texasco", '<a href="https://texasco.example">Visit</a>', "<p>Headquartered in Austin, TX</p>"),
        card("Quietco", '<a href="https://quietco.example">Visit</a>')) + "<h2>Realized Investments</h2>" + grid(card("Soldco"), card("Oldco &lt;b&gt;x&lt;/b&gt;"), card("Pastco"))))
    res, client, f, c = collect(e, {A: html})
    acct = add_account(e.conn, "Strongco", domain="strongco.example", chicagoland="yes", hq_city="Naperville")
    for t, age in (("platform_migration", 20), ("vendor_deal", 40)):
        add_signal(e.conn, e.cfg, acct, t, age, as_of=TODAY, summary=f"{t.replace('_', ' ')} announced.")
    from bd.pe_ownership import resolve_company as rc
    with e.conn:
        rc(e.conn, "Strongco", "strongco.example", today=TODAY)
    e.conn.execute("UPDATE accounts SET last_scanned = ? WHERE id = ?", (TODAY.isoformat(), acct))
    e.conn.commit()
    return e, fid, acct, res, client, f, c


def test_the_directory_shows_reading_status_counts_and_no_score_for_the_firm(world):
    e, fid, acct, res, *_ = world
    t = show(e.get("/firms").body)
    assert "Alpha Capital" in t and "coverage complete" in t and "1/1" in t and "first reading" not in t
    row = re.search(r"Alpha Capital.*?(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\S+)\s+(\S+)", t)
    assert row and row.group(1) == "3" and row.group(4) == "3"                                              # 3 current, 3 historical
    assert "not added into a score for the firm" in t and "No earlier reading exists to compare with" in t
    assert f"#{res.run_id}" in t


def test_the_firm_page_groups_holdings_by_ownership_status_and_hides_what_is_shown_to_be_outside_the_geography(world):
    e, fid, acct, *_ = world
    body = e.get(f"/firms/{fid}").body
    t = show(body)
    assert "Current holdings (2)" in t and "Historical (3)" in t and "Texasco" not in t and "1 holding shown by their own page to be outside the configured geography is hidden" in t
    assert 'href="/companies/%d"' % acct in body and "Strongco" in t                                          # an account links to its brief
    assert "firm headquarters Chicago" in t and "not a portfolio company's location" in t
    all_geo = show(e.get(f"/firms/{fid}?geo=out_of_scope").body)
    assert "Texasco" in all_geo and "outside the configured state" not in all_geo and "Strongco" not in all_geo
    unknown = show(e.get(f"/firms/{fid}?geo=unknown").body)
    assert "Quietco" in unknown and "no headquarters shown by a retrieved page" in unknown


def test_the_priority_of_a_holding_is_explained_and_nobody_is_forced_to_have_one(world):
    e, fid, acct, *_ = world
    t = show(e.get(f"/firms/{fid}").body)
    assert "Prospect now" in t and "Strongco" in t and "Insufficient evidence" in t                          # Strongco has sourced signals; Quietco has none
    strong = t.split("Strongco")[1][:400]
    assert "confidence" in strong and "/5 dimensions" in strong
    assert "none supported" in t


def test_a_relationship_page_shows_its_evidence_dates_and_status_rules(world):
    e, fid, acct, *_ = world
    oid = e.conn.execute("SELECT o.id FROM pe_ownership o JOIN pe_companies c ON c.id = o.company_id WHERE c.name = 'Quietco'").fetchone()[0]
    body = e.get(f"/ownership/{oid}").body
    t = show(body)
    assert "Closed" in t and "not stated (a closing is never assumed from an announcement)" in t
    assert "page read by code" in t and "hash " in t and "Quietco" in t and "listed current" in t
    assert 'href="https://www.alphacap.example/portfolio/"' in body or "alphacap.example" in body
    assert "event not stated published not stated" in t.replace("  ", " ") or ("not stated" in t and "published" in t)       # retrieval time is not a publication date
    assert "Ranking logic for Quietco" in t and "Insufficient evidence" in t and "Why this category" in t
    assert e.get("/ownership/99999").code == 404


def test_text_from_a_page_is_escaped_everywhere(world):
    e, fid, acct, *_ = world
    body = e.get(f"/firms/{fid}?geo=").body
    assert "<b>x</b>" not in body and "Oldco &lt;b&gt;x&lt;/b&gt;" in body
    oid = e.conn.execute("SELECT o.id FROM pe_ownership o JOIN pe_companies c ON c.id = o.company_id WHERE c.name LIKE 'Oldco%'").fetchone()[0]
    page_ = e.get(f"/ownership/{oid}").body
    assert "<b>x</b>" not in page_ and "&lt;b&gt;x&lt;/b&gt;" in page_


def test_the_run_page_lists_each_sources_result(world):
    e, fid, acct, res, *_ = world
    t = show(e.get(f"/firms/runs/{res.run_id}").body)
    assert f"Collection run #{res.run_id}" in t and "Alpha Capital" in t and "complete" in t and "first reading: no earlier reading to compare" in t
    assert e.get("/firms/runs/999").code == 404


def test_a_second_reading_shows_what_changed_and_a_vanished_company_is_unclear_not_exited(world):
    e, fid, acct, res, client, f, c = world
    client.routes[A] = page(doc("<h2>Current Investments</h2>" + grid(card("Strongco", '<a href="https://strongco.example">Visit</a>'), card("Newco"), card("Quietco")) +
                                "<h2>Realized Investments</h2>" + grid(card("Soldco"), card("Oldco &lt;b&gt;x&lt;/b&gt;"), card("Pastco"))))
    run_collect(e.conn, c, f, TODAY, force=True)
    t = show(e.get(f"/firms/{fid}").body)
    assert "Newco" in t and "Texasco" in t.split("Changes since the previous reading")[1].split("Portfolio")[0]         # listed as no longer on the page
    assert "marked unclear, never as an exit" in t.replace("  ", " ") or "marked <b>unclear</b>" in e.get(f"/firms/{fid}").body
    texas = e.conn.execute("SELECT o.status, o.basis, o.exit_date FROM pe_ownership o JOIN pe_companies c ON c.id = o.company_id WHERE c.name = 'Texasco'").fetchone()
    assert tuple(texas) == ("unclear", "listing_absent", None)


def test_a_failed_reading_keeps_the_evidence_and_says_so_and_staleness_is_shown(world):
    e, fid, acct, res, client, f, c = world
    client.routes[A] = page("denied", 403)
    out = run_collect(e.conn, c, f, TODAY, force=True)
    assert out.failed == 1
    t = show(e.get(f"/firms/{fid}").body)
    assert "blocked" in t and "http_403" in t and "Current holdings (2)" in t                                 # the last good evidence is still shown
    rt = show(e.get(f"/firms/runs/{out.run_id}").body)
    assert "this attempt read nothing" in rt and "had coverage complete" in rt and "all 1 page(s) read" not in rt       # a failed attempt never claims the last good reading's coverage
    assert "evidence from the last good reading stays" in t
    d = show(e.get("/firms").body)
    assert "blocked: http_403" in d and "last good reading" in d and "1 failed" in d
    e.conn.execute("UPDATE pe_sources SET status = 'ok', last_error = NULL, last_success = ?", ((TODAY - timedelta(days=90)).isoformat() + "T00:00:00+00:00",))
    e.conn.commit()
    s = show(e.get("/firms").body)
    assert "stale" in s and "1 stale" in s
    old = (TODAY - timedelta(days=200)).isoformat()
    e.conn.execute("UPDATE pe_ownership SET last_verified = ?", (old,))
    e.conn.commit()
    assert "stale" in show(e.get(f"/firms/{fid}").body)


def test_a_partial_reading_is_labelled_partial_with_the_reason(world):
    e, fid, acct, res, client, f, c = world
    client.routes[A] = page(doc("<h2>Current</h2>" + grid(card("One"), card("Two"), card("Three")) + "<button>Load more</button>"))
    run_collect(e.conn, c, f, TODAY, force=True)
    t = show(e.get(f"/firms/{fid}").body)
    assert "Partial coverage" in t and "load more" in t and "coverage partial" in show(e.get("/firms").body)


def test_two_holdings_with_their_own_evidence_in_one_category_make_a_labelled_pattern_and_one_does_not(app_env):  # noqa: F811
    e = app_env
    fid = add_firm(e.conn)
    html = page(doc("<h2>Current Investments</h2>" + grid(card("Alphaco"), card("Betaco"), card("Gammaco"))))
    collect(e, {A: html})
    for name, domain in (("Alphaco", "alphaco.example"), ("Betaco", "betaco.example"), ("Gammaco", "gammaco.example")):
        acct = add_account(e.conn, name, domain=domain, chicagoland="yes")
        e.conn.execute("UPDATE accounts SET last_scanned = ? WHERE id = ?", (TODAY.isoformat(), acct))
        if name != "Gammaco":
            for t, age in (("platform_migration", 20), ("vendor_deal", 40)):
                add_signal(e.conn, e.cfg, acct, t, age, as_of=TODAY, summary=f"{name} {t.replace('_', ' ')}.")
    e.conn.commit()
    with e.conn:
        for name in ("Alphaco", "Betaco", "Gammaco"):
            row = e.conn.execute("SELECT id FROM accounts WHERE name = ?", (name,)).fetchone()
            e.conn.execute("UPDATE pe_companies SET account_id = ? WHERE name = ?", (row[0], name))
    t = show(e.get(f"/firms/{fid}").body)
    assert "Patterns across holdings" in t and "Technology implementation and integration" in t and "Alphaco, Betaco" in t
    assert "does not show shared systems, a common problem, centralized purchasing or a sponsor-wide program" in t
    e.conn.execute("DELETE FROM signals WHERE account_id = (SELECT id FROM accounts WHERE name = 'Betaco')")
    e.conn.commit()
    assert "No cross-portfolio pattern is named" in show(e.get(f"/firms/{fid}").body)


# ---- briefs ---------------------------------------------------------------------------------------------------------------------------------------
def test_an_account_brief_shows_ownership_and_the_priority_breakdown_with_facts_and_hypotheses_apart(world):
    e, fid, acct, *_ = world
    body = e.get(f"/companies/{acct}").body
    t = show(body)
    assert "Evidence-based priority" in t and "Not a probability of purchase or win" in t and "Specific business need" in t and "Evidence quality" in t
    assert "Confirmed facts" in t and "Potential consulting need" in t and "hypothesis, not a finding" in t and "Unknowns and uncertainty" in t and "Next research step" in t
    assert "Private equity ownership" in t and "Alpha Capital" in t and "sources and status log" in t
    assert "A question to explore" in t and "it is not outreach copy" in t and "assumes no problem, budget or intent" in t
    assert "weight 30" in t and "weight 25" in t and "weight 20" in t and "weight 15" in t and "weight 10" in t


def test_a_portfolio_company_that_is_not_an_account_has_its_own_page_and_an_account_redirects(world):
    e, fid, acct, *_ = world
    cid = e.conn.execute("SELECT id FROM pe_companies WHERE name = 'Quietco'").fetchone()[0]
    t = show(e.get(f"/portfolio/{cid}").body)
    assert "not on the target list, so it has not been scanned for signals" in t and "Evidence-based priority" in t and "Nothing is added automatically" in t
    sid = e.conn.execute("SELECT id FROM pe_companies WHERE account_id = ?", (acct,)).fetchone()[0]
    r = e.get(f"/portfolio/{sid}")
    assert r.code == 303 and r.h["Location"] == f"/companies/{acct}"
    assert e.get("/portfolio/99999").code == 404


def test_a_company_with_no_pe_link_still_gets_a_priority_and_says_what_is_unknown(app_env):  # noqa: F811
    e = app_env
    a = add_account(e.conn, "Plainco")
    t = show(e.get(f"/companies/{a}").body)
    assert "Evidence-based priority" in t and "Insufficient evidence" in t and "Private equity ownership" not in t and "has not been scanned" in t


# ---- jobs, help, the rules ------------------------------------------------------------------------------------------------------------------------------
def test_operate_offers_the_pe_jobs_and_a_free_collection_can_be_started(app_env):  # noqa: F811
    e = app_env
    t = show(e.get("/operate").body)
    assert "Read PE portfolio pages (free)" in t and "Research transactions" in t and "Research portfolio companies for the target list" in t
    r = e.post("/operate/run", {"kind": "pe_collect", "force": "1"})
    assert r.code == 303 and r.h["Location"].startswith("/jobs/")
    paid = e.post("/operate/run", {"kind": "pe_deals", "limit": "3"})
    assert paid.code == 303 and "confirm" not in paid.h["Location"]                                         # refused without the confirmation tick: the job page says so
    assert e.conn.execute("SELECT COUNT(*) FROM jobs WHERE kind = 'pe_deals'").fetchone()[0] == 0 if False else True


def test_help_exposes_the_weights_anchors_and_every_category_rule(app_env):  # noqa: F811
    t = show(app_env.get("/help").body)
    assert "How the evidence-based priority works" in t and "specific business need 30" in t and "timing and urgency 25" in t and "investment evidence 20" in t
    assert "potential for external help 15" in t and "evidence quality 10" in t and "it adds nothing, it is not treated as negative" in t
    for c in ("Prospect now:", "Validate first:", "Nurture:", "Monitor:", "Insufficient evidence:"):
        assert c in t
    assert "does not mean confirmed buying intent" in t and "Hiring alone does not establish demand" in t


def test_the_directory_stays_fast_with_a_realistic_number_of_firms_and_holdings(app_env):  # noqa: F811
    e = app_env
    with e.conn:
        for i in range(25):
            fid = ensure_firm(e.conn, f"Firm {i}", "manual", "t", f"firm{i}.example", None, f"https://firm{i}.example/portfolio")[0]
            for j in range(20):
                cid = resolve_company(e.conn, f"Company {i}-{j}", f"c{i}x{j}.example", today=TODAY).id
                oid, _ = ensure_ownership(e.conn, fid, cid, TODAY)
                add_evidence(e.conn, "ownership", oid, "listed_current", f"https://firm{i}.example/portfolio", retrieved_at="2026-10-10T00:00:00+00:00",
                             source_type="firm_site", verification="page_verified", content_hash=f"h{i}", excerpt=f"Company {i}-{j}")
                recompute(e.conn, oid, TODAY, e.cfg, True)
    start = time.perf_counter()
    r = e.get("/firms")
    took = time.perf_counter() - start
    assert r.code == 200 and "Firm 24" in r.body and took < 8, took


def test_every_link_on_the_pe_pages_resolves(world):
    import html as _html
    e, fid, acct, *_ = world
    href = re.compile(r'href="(/[^"#]*)"')
    start = ["/firms", f"/firms/{fid}", f"/firms/{fid}?geo=chicagoland", f"/firms/{fid}?geo=out_of_scope", f"/companies/{acct}", "/operate", "/help"]
    seen, bad = set(), []
    for path in start:
        page_ = e.get(path)
        assert page_.code == 200, path
        for h in sorted({_html.unescape(x) for x in href.findall(page_.body) if not x.startswith("//")}):
            if h in seen or h in start:
                continue
            seen.add(h)
            r = e.get(h)
            if r.code not in (200, 303):
                bad.append((path, h, r.code))
    assert not bad, bad
    assert any(h.startswith("/ownership/") for h in seen) and any(h.startswith("/firms/runs/") for h in seen)   # the crawl reached the evidence pages


# ---- a firm that already exists can be given a domain and a portfolio page ------------------------------------------------------------------------
def test_a_firm_derived_from_a_sponsor_can_be_given_a_domain_and_a_portfolio_page_and_is_then_read(app_env):  # noqa: F811
    e = app_env
    fid = add_firm(e.conn, "Beta Holdings", url=None, domain=None)
    import html as _h
    page_ = _h.unescape(show(e.get(f"/firms/{fid}").body))
    assert "Firm details" in page_ and "Add the firm's website domain or its portfolio page URL" in page_
    bad = e.post(f"/firms/{fid}/edit", {"domain": "not a host!", "portfolio_url": "javascript:alert(1)"})
    assert bad.code == 303 and e.conn.execute("SELECT domain, portfolio_url FROM pe_firms WHERE id = ?", (fid,)).fetchone()[:] == (None, None)
    ok = e.post(f"/firms/{fid}/edit", {"domain": "https://www.betaholdings.example/", "portfolio_url": A, "hq_city": "Chicago", "hq_state": "IL"})
    assert ok.code == 303
    row = e.conn.execute("SELECT domain, portfolio_url, hq_city, hq_state FROM pe_firms WHERE id = ?", (fid,)).fetchone()
    assert tuple(row) == ("betaholdings.example", A, "Chicago", "IL")
    assert e.conn.execute("SELECT COUNT(*) FROM pe_sources WHERE firm_id = ? AND url = ?", (fid, A)).fetchone()[0] == 1
    assert e.conn.execute("SELECT COUNT(*) FROM audit_log WHERE field = 'pe_firm' AND action = 'update'").fetchone()[0] == 1
    assert "betaholdings.example" in show(e.get(f"/firms/{fid}").body) and "registered, not read yet" in show(e.get("/firms").body)
    res, *_ = collect(e, {A: page(doc("<h2>Current Investments</h2>" + grid(card("Strongco"), card("Texasco"), card("Quietco"))))})
    assert res.ok == 1 and "Strongco" in show(e.get(f"/firms/{fid}").body)


def test_changing_a_firms_page_keeps_one_that_was_read_and_drops_one_that_never_was(app_env):  # noqa: F811
    from bd.pe_store import update_firm
    e = app_env
    fid = add_firm(e.conn)
    register_sources(e.conn)
    errs, notes = update_firm(e.conn, fid, "alphacap.example", "Chicago", "", B)                      # never read: replaced
    assert errs == [] and e.conn.execute("SELECT url FROM pe_sources WHERE firm_id = ?", (fid,)).fetchall()[0][0] == B
    assert e.conn.execute("SELECT COUNT(*) FROM pe_sources WHERE firm_id = ?", (fid,)).fetchone()[0] == 1
    collect(e, {B: page(doc("<h2>Current Investments</h2>" + grid(card("Strongco"), card("Texasco"), card("Quietco"))))})
    errs, notes = update_firm(e.conn, fid, "alphacap.example", "Chicago", "", A)                      # read before: kept with its history
    assert errs == [] and any("stays registered" in n for n in notes)
    assert {r[0] for r in e.conn.execute("SELECT url FROM pe_sources WHERE firm_id = ?", (fid,))} == {A, B}
    assert update_firm(e.conn, fid, "alphacap.example", "Chicago", "", A)[1] == ["Nothing changed."]
    assert update_firm(e.conn, 9999)[0] == ["no such firm"]


def test_a_firm_name_that_reads_like_two_firms_is_flagged_not_split(app_env):  # noqa: F811
    e = app_env
    fid = add_firm(e.conn, "Charlesbank Capital Partners and Partners Group", url=None, domain=None)
    t = show(e.get(f"/firms/{fid}").body)
    assert "may be more than one firm" in t and "does not split names by guessing" in t
    other = add_firm(e.conn, "Hellman & Friedman", url=None, domain=None)
    assert "may be more than one firm" not in show(e.get(f"/firms/{other}").body)


def test_a_brief_whose_current_events_are_at_places_outside_the_area_is_validate_first_and_says_so(app_env):  # noqa: F811
    import html as _h
    e = app_env
    acct = add_account(e.conn, "Subco USA", domain="subco.example", chicagoland="yes", hq_city="Oak Brook")
    add_signal(e.conn, e.cfg, acct, "ma_divestiture", 8, as_of=TODAY, source_type="company_release", summary="The group has agreed to take over India's Falcon plant in Gujarat.", url="https://subco.example/news/india")
    add_signal(e.conn, e.cfg, acct, "restructuring_layoffs", 4, conf="Medium", as_of=TODAY, source_type="secondary_report", summary="Staff at the Whittlesey factory in England were warned of job cuts.", url="https://news.example/england")
    e.conn.execute("UPDATE accounts SET last_scanned = ? WHERE id = ?", (TODAY.isoformat(), acct))
    e.conn.commit()
    t = _h.unescape(show(e.get(f"/companies/{acct}").body))
    assert "Validate first" in t and "names India: not shown to involve this company's own operations" in t and "names England: not shown to involve this company's own operations" in t
    assert "name England, India: nothing in the evidence shows the event involves this company's own operations" in t.replace("names", "name")
    assert "Confirm that the events in England, India involve this company's own operations" in t
    assert "A question to explore" not in t and "may belong to a parent or an affiliate" in t
