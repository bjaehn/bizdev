"""The collection run end to end over synthetic pages (tests only): page -> entries -> ledger -> status -> history."""

import json
from datetime import date

import pytest

from bd.pe_collect import (discover_portfolio_pages, locate_companies, register_sources, run_collect, run_history, source_is_stale)
from bd.pe_ownership import company_geography, ensure_ownership, evidence_for, resolve_company
from bd.pe_store import ensure_firm
from tests.conftest import AS_OF, add_account, make_db
from tests.pe_fakes import make_fetcher, page
from tests.test_pe_listing import NAV, card, doc, grid

TODAY = AS_OF
A = "https://www.alphacap.example/portfolio/"
B = "https://www.betapartners.example/companies/"


@pytest.fixture
def cfg(cfg):
    return cfg.model_copy(deep=True)


@pytest.fixture
def conn(tmp_path):
    return make_db(tmp_path)


def firm(conn, name="Alpha Capital", url=A, domain="alphacap.example", **kw):
    with conn:
        fid = ensure_firm(conn, name, "manual", "test", domain, kw.get("hq_city"), url)[0]
    return fid


def listing(current=("One Co", "Two Co", "Three Co"), realized=("Old Co", "Gone Co", "Sold Co"), extra=""):
    cur = "<h2>Current Investments</h2>" + grid(*[card(n, f'<a href="https://{n.lower().replace(" ", "")}.example">Visit</a>') for n in current]) if current else ""
    old = "<h2>Realized Investments</h2>" + grid(*[card(n) for n in realized]) if realized else ""
    return page(doc(cur + old + extra))


def collect(conn, cfg, routes, **kw):
    f, client, clock, c = make_fetcher(cfg, routes)
    register_sources(conn)
    return run_collect(conn, c, f, TODAY, **kw), client, c


def rels(conn):
    return {r["name"]: r for r in conn.execute(
        "SELECT o.*, c.name, f.name AS firm_name FROM pe_ownership o JOIN pe_companies c ON c.id = o.company_id JOIN pe_firms f ON f.id = o.firm_id")}


def test_a_first_collection_builds_relationships_with_page_verified_evidence(conn, cfg):
    firm(conn)
    res, _client, _c = collect(conn, cfg, {A: listing()})
    r = rels(conn)
    assert {n: x["status"] for n, x in r.items()} == {"One Co": "current", "Two Co": "current", "Three Co": "current", "Old Co": "historical",
                                                       "Gone Co": "historical", "Sold Co": "historical"}
    assert r["One Co"]["basis"] == "firm_listing" and r["One Co"]["confidence"] == "Medium" and r["Old Co"]["basis"] == "firm_listing_historical"
    assert r["One Co"]["closed_date"] is None and r["One Co"]["exit_date"] is None and r["One Co"]["last_verified"] == TODAY.isoformat()
    ev = evidence_for(conn, "ownership", r["One Co"]["id"])
    snap = conn.execute("SELECT * FROM pe_snapshots").fetchone()
    assert len(ev) == 1 and ev[0]["verification"] == "page_verified" and ev[0]["excerpt"] in snap["text"] and ev[0]["content_hash"] == snap["content_hash"]
    assert ev[0]["source_type"] == "firm_site" and ev[0]["url"] == A and ev[0]["published_date"] is None and ev[0]["event_date"] is None     # no invented dates
    src = conn.execute("SELECT * FROM pe_sources").fetchone()
    assert (src["status"], src["coverage"], src["entries"]) == ("ok", "complete", 6) and src["last_success"]
    assert res.ok == 1 and res.entries == 6 and res.new_relationships == 6 and res.outcomes[0].added is None        # the first reading has nothing to compare with
    assert "1 read (1 complete" in res.line() and run_history(conn)[0]["summary"] == res.line()


def test_collecting_again_is_idempotent_and_a_recent_read_is_not_repeated(conn, cfg):
    firm(conn)
    f, client, _clk, c = make_fetcher(cfg, {A: listing()})
    register_sources(conn)
    run_collect(conn, c, f, TODAY)
    n_calls = len(client.calls)
    cached = run_collect(conn, c, f, TODAY)
    assert cached.cached == 1 and len(client.calls) == n_calls and cached.outcomes[0].status == "cached"            # the cache window, no new request
    before = [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("pe_companies", "pe_ownership", "pe_evidence", "pe_snapshots", "pe_ownership_log")]
    forced = run_collect(conn, c, f, TODAY, force=True)
    after = [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("pe_companies", "pe_ownership", "pe_evidence", "pe_snapshots", "pe_ownership_log")]
    assert before == after and forced.new_relationships == 0 and forced.status_changes == 0
    assert forced.outcomes[0].added == [] and forced.outcomes[0].removed == []                                 # a second reading, no change


def test_changes_since_the_previous_reading_are_reported_and_a_vanished_company_is_unclear_not_exited(conn, cfg):
    firm(conn)
    f, client, _clk, c = make_fetcher(cfg, {A: listing()})
    register_sources(conn)
    run_collect(conn, c, f, TODAY)
    client.routes[A] = listing(current=("One Co", "Two Co", "New Co"))                                          # Three Co disappears, New Co appears
    res = run_collect(conn, c, f, TODAY, force=True)
    o = res.outcomes[0]
    assert o.added == ["New Co"] and o.removed == ["Three Co"] and o.marked_absent == 1
    r = rels(conn)
    gone = r["Three Co"]
    assert (gone["status"], gone["basis"], gone["exit_date"]) == ("unclear", "listing_absent", None) and gone["listing_absent_since"] == TODAY.isoformat()
    assert "no source states an exit" in gone["conflict_note"] and r["New Co"]["status"] == "current"
    client.routes[A] = listing(current=("One Co", "Two Co", "Three Co", "New Co"))                              # it comes back
    run_collect(conn, c, f, TODAY, force=True)
    back = rels(conn)["Three Co"]
    assert back["status"] == "current" and back["listing_absent_since"] is None
    log = [(x["from_status"], x["to_status"]) for x in conn.execute("SELECT * FROM pe_ownership_log WHERE ownership_id = ? ORDER BY id", (gone["id"],))]
    assert log == [("unclear", "current"), ("current", "unclear"), ("unclear", "current")]


def test_a_partial_reading_never_marks_anyone_absent(conn, cfg):
    firm(conn)
    paged = lambda names, nxt="": page(doc("<h2>Current</h2>" + grid(*[card(n) for n in names]) + nxt))
    f, client, _clk, c = make_fetcher(cfg, {A: paged(("One Co", "Two Co", "Three Co"), '<a rel="next" href="/portfolio/?page=2">Next</a>'),
                                            A + "?page=2": paged(("Four Co", "Five Co", "Six Co"))})
    register_sources(conn)
    run_collect(conn, c, f, TODAY)
    client.routes[A + "?page=2"] = page("down", 503)                                                            # page 2 fails next time
    res = run_collect(conn, c, f, TODAY, force=True)
    assert res.outcomes[0].coverage == "partial" and res.outcomes[0].marked_absent == 0
    assert rels(conn)["Four Co"]["status"] == "current" and rels(conn)["Four Co"]["listing_absent_since"] is None


def test_a_failing_source_does_not_stop_the_others_and_keeps_its_last_good_evidence(conn, cfg):
    firm(conn)
    firm(conn, "Beta Partners", B, "betapartners.example")
    f, client, _clk, c = make_fetcher(cfg, {A: listing(), B: listing(current=("Beta One", "Beta Two", "Beta Three"), realized=())})
    register_sources(conn)
    run_collect(conn, c, f, TODAY)
    client.routes[A] = page("denied", 403)
    client.routes[B] = page("<html><body><h2>Current</h2></body></html>")                                       # an empty page is not a portfolio either
    res = run_collect(conn, c, f, TODAY, force=True)
    st = {r["firm_name"]: r for r in conn.execute("SELECT s.*, f.name AS firm_name FROM pe_sources s JOIN pe_firms f ON f.id = s.firm_id")}
    assert res.failed == 1 and st["Alpha Capital"]["status"] == "blocked" and st["Alpha Capital"]["last_error"] == "http_403"
    assert st["Alpha Capital"]["consecutive_failures"] == 1 and st["Alpha Capital"]["last_success"] is not None and st["Alpha Capital"]["entries"] == 6
    assert rels(conn)["One Co"]["status"] == "current"                                                          # nothing was lost by the failure
    assert st["Beta Partners"]["status"] == "ok" and st["Beta Partners"]["coverage"] == "unknown" and st["Beta Partners"]["entries"] == 0
    assert rels(conn)["Beta One"]["status"] == "current" and rels(conn)["Beta One"]["listing_absent_since"] is None  # an unknown-coverage reading marks no one absent
    client.routes[A] = listing()
    run_collect(conn, c, f, TODAY, force=True)
    assert conn.execute("SELECT consecutive_failures, status FROM pe_sources WHERE firm_id = (SELECT id FROM pe_firms WHERE name='Alpha Capital')").fetchone()[:] == (0, "ok")


def test_an_unexpected_error_in_one_source_is_recorded_and_the_run_continues(conn, cfg, monkeypatch):
    firm(conn)
    firm(conn, "Beta Partners", B, "betapartners.example")
    import bd.pe_collect as pc
    real = pc.collect_listing

    def boom(conn_, fetcher, cfg_, sid, url):
        if "alphacap" in url:
            raise RuntimeError("parser bug")
        return real(conn_, fetcher, cfg_, sid, url)
    monkeypatch.setattr(pc, "collect_listing", boom)
    res, _cl, _c = collect(conn, cfg, {A: listing(), B: listing(current=("Beta One", "Beta Two", "Beta Three"), realized=())})
    assert res.failed == 1 and res.ok == 1 and "unexpected RuntimeError" in res.outcomes[0].error
    assert "Beta One" in rels(conn)


def test_a_company_listed_by_two_sponsors_is_one_company_with_two_relationships(conn, cfg):
    firm(conn)
    firm(conn, "Beta Partners", B, "betapartners.example")
    page_a = page(doc("<h2>Current</h2>" + grid(card("Shared Co", '<a href="https://shared.example">Visit</a>'), card("A Two"), card("A Three"))))
    page_b = page(doc("<h2>Current</h2>" + grid(card("Shared Co, Inc.", '<a href="https://www.shared.example">Visit</a>'), card("B Two"), card("B Three"))))
    collect(conn, cfg, {A: page_a, B: page_b})
    assert conn.execute("SELECT COUNT(*) FROM pe_companies WHERE name_key = 'shared'").fetchone()[0] == 1
    cid = conn.execute("SELECT id FROM pe_companies WHERE name_key = 'shared'").fetchone()[0]
    rows = conn.execute("SELECT f.name FROM pe_ownership o JOIN pe_firms f ON f.id = o.firm_id WHERE o.company_id = ? ORDER BY f.name", (cid,)).fetchall()
    assert [r[0] for r in rows] == ["Alpha Capital", "Beta Partners"]


def test_a_holding_that_is_already_an_account_is_not_duplicated(conn, cfg):
    acct = add_account(conn, "One Co", domain="oneco.example")
    firm(conn)
    collect(conn, cfg, {A: listing()})
    assert conn.execute("SELECT account_id FROM pe_companies WHERE name = 'One Co'").fetchone()[0] == acct
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1                                       # collection never creates accounts


def test_a_name_shared_by_two_known_companies_is_left_unresolved_rather_than_guessed(conn, cfg):
    with conn:
        resolve_company(conn, "Apex", "apex-software.com", today=TODAY)
        resolve_company(conn, "Apex", "apex-steel.com", today=TODAY)
    firm(conn)
    page_a = page(doc("<h2>Current</h2>" + grid(card("Apex"), card("Zed Co"), card("Yar Co"))))
    res, _cl, _c = collect(conn, cfg, {A: page_a})
    assert res.outcomes[0].unresolved == ["Apex"] and "Apex" not in {r["name"] for r in rels(conn).values()} and "Zed Co" in rels(conn)


def test_an_excerpt_that_is_not_in_the_stored_page_is_never_stored_as_verified_evidence(conn, cfg):
    firm(conn)
    f, _client, _clk, c = make_fetcher(cfg, {A: listing(current=("One Co", "Two Co", "Three Co"), realized=(), extra="<p>" + "padding " * 400 + "</p>")})
    c.pe_portfolio.text_cap_bytes = 40                                                                         # the stored text cannot hold the later cards
    register_sources(conn)
    res = run_collect(conn, c, f, TODAY)
    assert any("not found in the stored page" in u for u in res.outcomes[0].unresolved)
    assert all(r["verification"] == "page_verified" and r["excerpt"] in conn.execute("SELECT text FROM pe_snapshots").fetchone()[0]
               for r in conn.execute("SELECT * FROM pe_evidence"))


def test_a_listing_with_no_labels_leaves_every_relationship_unclear(conn, cfg):
    firm(conn)
    collect(conn, cfg, {A: page(doc("<h2>Our Companies</h2>" + grid(card("One Co"), card("Two Co"), card("Three Co"))))})
    assert {(r["status"], r["basis"]) for r in rels(conn).values()} == {("unclear", "listed_unlabeled")}


def test_a_location_on_a_listing_is_recorded_as_a_place_not_a_headquarters_unless_the_page_says_so(conn, cfg):
    firm(conn, hq_city="Chicago")
    html = page(doc("<h2>Current</h2>" + grid(card("One Co", "", "<p>Chicago, IL</p>"), card("Two Co", "", "<p>Headquartered in Austin, TX</p>"), card("Three Co"))))
    collect(conn, cfg, {A: html})
    one, two, three = (conn.execute("SELECT id FROM pe_companies WHERE name = ?", (n,)).fetchone()[0] for n in ("One Co", "Two Co", "Three Co"))
    g1, g2, g3 = (company_geography(conn, i, cfg) for i in (one, two, three))
    assert g1.status == "unknown" and g2.status == "out_of_scope" and g3.status == "unknown"                       # the sponsor's Chicago office says nothing about any of them
    kinds = {r["company_id"] if False else r["entity_id"]: r["kind"] for r in conn.execute("SELECT * FROM pe_locations WHERE entity_type = 'company'")}
    assert kinds == {one: "office", two: "headquarters"}
    assert conn.execute("SELECT COUNT(*) FROM pe_locations WHERE entity_type = 'firm'").fetchone()[0] == 0


def test_sources_are_registered_once_and_a_home_page_can_supply_a_missing_portfolio_page(conn, cfg):
    firm(conn)
    firm(conn, "Gamma Equity", None, "gammaequity.example")
    firm(conn, "Delta Equity", None, "deltaequity.example")
    assert register_sources(conn) == 1 and register_sources(conn) == 0
    home_g = page(f'<html><body><a href="/our-investments">Our Investments</a><a href="https://other.example/portfolio">Portfolio</a></body></html>')
    f, _client, _clk, c = make_fetcher(cfg, {"https://gammaequity.example/": home_g, "https://deltaequity.example/": page("<html><body><a href='/x'>Hi</a></body></html>")})
    assert discover_portfolio_pages(conn, f, c) == 1
    row = conn.execute("SELECT s.url, s.origin FROM pe_sources s JOIN pe_firms f ON f.id = s.firm_id WHERE f.name = 'Gamma Equity'").fetchone()
    assert (row["url"], row["origin"]) == ("https://gammaequity.example/our-investments", "discovered")
    assert conn.execute("SELECT COUNT(*) FROM pe_sources").fetchone()[0] == 2                                      # Delta had no portfolio link: nothing invented


def test_source_staleness_is_by_the_last_success(conn, cfg):
    firm(conn)
    register_sources(conn)
    src = conn.execute("SELECT * FROM pe_sources").fetchone()
    assert source_is_stale(src, TODAY, cfg)
    conn.execute("UPDATE pe_sources SET last_success = '2026-10-01T00:00:00+00:00'")
    src = conn.execute("SELECT * FROM pe_sources").fetchone()
    assert not source_is_stale(src, TODAY, cfg) and source_is_stale(src, date(2027, 1, 1), cfg)


def test_the_run_log_records_each_sources_result(conn, cfg):
    firm(conn)
    res, _cl, _c = collect(conn, cfg, {A: listing()})
    row = run_history(conn)[0]
    detail = json.loads(row["detail"])
    assert row["finished"] and detail[0]["firm"] == "Alpha Capital" and detail[0]["coverage"] == "complete" and detail[0]["entries"] == 6 and detail[0]["added"] is None


# ---- locating companies from their own sites ---------------------------------------------------------------------------------------------------------
def test_a_companys_own_page_can_show_where_it_is_and_unseen_structured_data_is_not_trusted(conn, cfg):
    fid = firm(conn)
    with conn:
        a = resolve_company(conn, "Local Co", "localco.example", today=TODAY).id
        b = resolve_company(conn, "Hidden Co", "hiddenco.example", today=TODAY).id
        for cid in (a, b):
            ensure_ownership(conn, fid, cid, TODAY)
    ld = json.dumps({"@type": "Organization", "address": {"addressLocality": "Skokie", "addressRegion": "IL"}})
    routes = {"https://localco.example/": page(f'<html><body><p>Welcome</p><p>Our headquarters: Chicago, IL</p></body></html>'),
              "https://hiddenco.example/": page(f'<html><head><script type="application/ld+json">{ld}</script></head><body><p>Welcome</p></body></html>')}
    f, _client, _clk, c = make_fetcher(cfg, routes)
    cfg_scope = c
    cfg_scope.pe_portfolio.scope_cities = ["Chicago"]
    assert locate_companies(conn, c, f, TODAY) == 1
    assert company_geography(conn, a, c).status == "in_scope"
    assert company_geography(conn, b, c).status == "unknown"
    assert conn.execute("SELECT kind FROM pe_locations WHERE entity_id = ?", (a,)).fetchone()[0] == "headquarters"
    ev = evidence_for(conn, "company", a)[0]
    assert ev["verification"] == "page_verified" and ev["source_type"] == "company_site" and "Chicago, IL" in ev["excerpt"]


def test_a_company_named_only_with_suffix_words_is_kept_and_a_nameless_entry_never_fails_the_source(conn, cfg, monkeypatch):
    """Live: "The Limited" normalised to nothing ("the" and "limited" are both stripped as legal suffixes), which raised and failed the whole source."""
    from bd.pe_listing import Entry
    from bd.pe_store import norm_name
    import bd.pe_collect as pc
    assert norm_name("The Limited") == "the limited" and norm_name("Belk Inc") == "belk" and norm_name("--") == ""
    firm(conn)
    real = pc.collect_listing

    def with_a_nameless_entry(conn_, fetcher, cfg_, sid, url):
        res = real(conn_, fetcher, cfg_, sid, url)
        res.entries.append(Entry(name="--", page_url=url))
        return res
    monkeypatch.setattr(pc, "collect_listing", with_a_nameless_entry)
    res, _client, _c = collect(conn, cfg, {A: listing(current=("The Limited", "Two Co", "Three Co"), realized=())})
    assert res.failed == 0 and res.ok == 1 and rels(conn)["The Limited"]["status"] == "current"
    assert any("nothing to match on" in u for u in res.outcomes[0].unresolved)
    assert conn.execute("SELECT COUNT(*) FROM pe_companies WHERE name_key = ''").fetchone()[0] == 0
