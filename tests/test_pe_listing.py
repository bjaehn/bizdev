"""The portfolio page reader. Pages here are synthetic and exist only in these tests."""

import json

import pytest

from bd.htmltext import page_text, parse_html
from bd.pe_listing import collect_listing, parse_listing
from tests.conftest import make_db
from tests.pe_fakes import make_fetcher, page

URL = "https://www.firm.example/portfolio/"


@pytest.fixture
def cfg(cfg):
    return cfg.model_copy(deep=True)


NAV = '<header><nav><a href="/">Home</a><a href="/about">About</a><a href="/portfolio">Portfolio</a><a href="/team">Team</a></nav></header>'


def card(name, link="", extra="", cls="portfolio-item"):
    return f'<div class="{cls}"><h3>{name}</h3>{extra}{link}</div>'


def doc(body, head=""):
    return f"<html><head><title>Portfolio</title>{head}</head><body>{NAV}<main>{body}</main><footer><a href=\"https://linkedin.com/x\">LinkedIn</a></footer></body></html>"


def grid(*cards):
    return '<div class="portfolio-grid">' + "".join(cards) + "</div>"


def test_entries_get_their_section_state_site_and_excerpt_from_the_page(cfg):
    html = doc("<h1>Our Portfolio</h1><h2>Current Investments</h2>" + grid(
        card("Acme Software", '<a href="https://www.acmesoftware.com">Visit website</a>', '<span class="loc">Chicago, IL</span>'),
        card("Bolt Logistics", '<a href="/companies/bolt">Details</a>', '<span class="loc">Dallas, TX</span>'),
        card("Cedar Foods", '<a href="https://cedarfoods.com">Visit</a>')) + "<h2>Realized Investments</h2>" + grid(
        card("Delta Parts", '<a href="https://deltaparts.com">Visit</a>'), card("Echo Labs", '<a href="https://echolabs.io">Visit</a>'),
        card("Foxtrot Health", '<a href="https://foxtrot.health">Visit</a>')))
    lst = parse_listing(html, URL)
    by = {e.name: e for e in lst.entries}
    assert list(by) == ["Acme Software", "Bolt Logistics", "Cedar Foods", "Delta Parts", "Echo Labs", "Foxtrot Health"]
    assert [by[n].state for n in by] == ["current"] * 3 + ["historical"] * 3
    assert by["Acme Software"].domain == "acmesoftware.com" and by["Bolt Logistics"].domain is None and by["Bolt Logistics"].href == "https://www.firm.example/companies/bolt"
    assert by["Acme Software"].section == "Our Portfolio > Current Investments" and by["Delta Parts"].section == "Our Portfolio > Realized Investments"
    assert (by["Acme Software"].location, by["Acme Software"].location_kind) == ("Chicago, IL", "office")      # a place is shown; it is not said to be the HQ
    assert by["Cedar Foods"].location is None
    text = page_text(parse_html(html))
    assert all(e.excerpt and e.excerpt in text for e in lst.entries)                                        # every excerpt is on the page


def test_navigation_links_and_social_links_are_not_entries(cfg):
    html = doc("<h2>Current</h2>" + grid(card("One Co"), card("Two Co"), card("Three Co")))
    assert [e.name for e in parse_listing(html, URL).entries] == ["One Co", "Two Co", "Three Co"]
    nav_only = doc("<p>Coming soon</p>")
    assert parse_listing(nav_only, URL).entries == []


def test_a_listing_with_no_status_label_stays_unlabeled(cfg):
    lst = parse_listing(doc("<h2>Our Companies</h2>" + grid(card("One Co"), card("Two Co"), card("Three Co"))), URL)
    assert [e.state for e in lst.entries] == ["unlabeled"] * 3 and not any(e.by_contrast for e in lst.entries)


def test_entries_outside_a_realized_section_are_current_only_by_contrast_and_say_so(cfg):
    lst = parse_listing(doc("<h2>Healthcare</h2>" + grid(card("One Co"), card("Two Co"), card("Three Co")) + "<h2>Exits</h2>" +
                            grid(card("Old One"), card("Old Two"), card("Old Three"))), URL)
    by = {e.name: e for e in lst.entries}
    assert by["One Co"].state == "current" and by["One Co"].by_contrast and by["Old One"].state == "historical" and not by["Old One"].by_contrast


def test_card_level_labels_and_class_names_set_the_state(cfg):
    html = doc("<h2>Portfolio</h2>" + grid(
        card("Alpha", '<span class="badge">Exited 2022</span>'), card("Beta", '<span class="status">Current</span>'),
        card("Gamma", "", "", "portfolio-item status-realized"), card("Delta", "<p>Active in 12 states</p>")))
    by = {e.name: e.state for e in parse_listing(html, URL).entries}
    assert by["Alpha"] == "historical" and by["Gamma"] == "historical" and by["Beta"] == "current"
    assert by["Delta"] != "historical"                                                         # the word "active" in running text is not a label


def test_a_headquarters_is_only_a_headquarters_when_the_listing_says_so(cfg):
    html = doc("<h2>Current</h2>" + grid(
        card("Alpha", "", "<p>Headquartered in Chicago, IL</p>"), card("Beta", "", "<p>Naperville, IL</p>"), card("Gamma", "", "<p>Based in Austin, TX</p>")))
    by = {e.name: (e.location, e.location_kind) for e in parse_listing(html, URL).entries}
    assert by == {"Alpha": ("Chicago, IL", "headquarters"), "Beta": ("Naperville, IL", "office"), "Gamma": ("Austin, TX", "headquarters")}


def test_news_headlines_and_generic_link_text_are_not_company_names(cfg):
    html = doc("<h2>News</h2>" + grid(card("Firm announces the closing of its eighth flagship fund after strong investor demand"),
                                       card("Read more"), card("Learn more")))
    assert parse_listing(html, URL).entries == []


def test_a_plain_list_of_links_is_read_when_there_are_no_cards(cfg):
    html = doc('<h2>Current Portfolio</h2><ul><li><a href="https://one.example">One Co</a></li><li><a href="https://two.example">Two Co</a></li>'
               '<li><a href="/companies/three">Three Co</a></li></ul>')
    lst = parse_listing(html, URL)
    assert [e.name for e in lst.entries] == ["One Co", "Two Co", "Three Co"] and all(e.state == "current" for e in lst.entries)


def test_pagination_links_and_stated_totals_are_found(cfg):
    html = doc("<p>Showing 1-3 of 7 companies</p>" + grid(card("One Co"), card("Two Co"), card("Three Co")) +
               '<div class="pagination"><a href="?page=1">1</a><a href="?page=2">2</a><a href="?page=3">3</a></div><a rel="next" href="/portfolio/?page=2">Next</a>',
               head='<link rel="next" href="/portfolio/?page=2">')
    lst = parse_listing(html, URL)
    assert lst.stated_total == 7 and lst.next_urls[0] == "https://www.firm.example/portfolio/?page=2" and len(lst.next_urls) == 3
    assert "https://www.firm.example/portfolio/?page=3" in lst.next_urls


def test_signs_of_script_loaded_listings_are_reported(cfg):
    assert "load more" in parse_listing(doc(grid(card("A Co"), card("B Co"), card("C Co")) + "<button>Load more</button>"), URL).dynamic_hint
    shell = parse_listing('<html><body><div id="root"></div><script>app()</script></body></html>', URL)
    assert shell.entries == [] and "drawn by scripts" in shell.dynamic_hint
    wp = parse_listing(doc("<p>soon</p>", '<link rel="https://api.w.org/" href="https://www.firm.example/wp-json/">'), URL)
    assert wp.wp_api == "https://www.firm.example/wp-json/"


def test_structured_data_lists_are_a_fallback(cfg):
    ld = json.dumps({"@type": "ItemList", "itemListElement": [{"@type": "ListItem", "item": {"name": "Ld One", "url": "https://one.example"}},
                                                               {"@type": "ListItem", "item": {"name": "Ld Two"}}]})
    lst = parse_listing(f'<html><body><script type="application/ld+json">{ld}</script></body></html>', URL)
    assert [e.name for e in lst.entries] == ["Ld One", "Ld Two"] and all(e.state == "unlabeled" for e in lst.entries)


# ---- the crawl ---------------------------------------------------------------------------------------------------------------------------------
def pages(n, total=None, extra=""):
    out = {}
    for i in range(1, n + 1):
        url = URL if i == 1 else f"{URL}?page={i}"
        nxt = f'<a rel="next" href="/portfolio/?page={i + 1}">Next</a>' if i < n else ""
        tot = f"<p>Showing {i}-{i} of {total} companies</p>" if total else ""
        out[url] = page(doc(tot + "<h2>Current</h2>" + grid(card(f"P{i} Alpha"), card(f"P{i} Beta"), card(f"P{i} Gamma")) + nxt + extra))
    return out


def run(cfg, routes, maxp=None):
    conn = make_db_tmp()
    f, client, _clk, c = make_fetcher(cfg, routes)
    if maxp:
        c.pe_portfolio.fetch.max_pages_per_source = maxp
    return conn, collect_listing(conn, f, c, None, URL), client


_TMP = []


def make_db_tmp():
    import tempfile
    from pathlib import Path
    d = tempfile.mkdtemp()
    _TMP.append(d)
    return make_db(Path(d))


def test_a_paginated_listing_read_to_its_end_is_complete(cfg):
    conn, res, client = run(cfg, pages(3, total=9))
    assert [p.ok for p in res.pages] == [True] * 3 and len(res.entries) == 9 and res.coverage == "complete", res.coverage_note
    assert conn.execute("SELECT COUNT(*) FROM pe_snapshots").fetchone()[0] == 3 and all(e.snapshot_id for e in res.entries)


def test_a_stated_total_that_does_not_match_makes_it_partial_and_says_why(cfg):
    _conn, res, _c = run(cfg, pages(2, total=40))
    assert res.coverage == "partial" and "states 40 but 6 were found" in res.coverage_note


def test_hitting_the_page_limit_is_partial_with_the_reason(cfg):
    _conn, res, _c = run(cfg, pages(5), maxp=2)
    assert res.coverage == "partial" and "stopped after 2 pages" in res.coverage_note and len(res.entries) == 6


def test_a_page_that_fails_midway_makes_the_listing_partial_and_keeps_what_was_read(cfg):
    routes = pages(3)
    routes[f"{URL}?page=2"] = page("down", 503)
    _conn, res, _c = run(cfg, routes)
    assert res.coverage == "partial" and "could not be read (http_503)" in res.coverage_note and len(res.entries) == 3
    assert [p.ok for p in res.pages] == [True, False]


def test_when_the_first_page_cannot_be_read_nothing_is_claimed(cfg):
    _conn, res, _c = run(cfg, {URL: page("denied", 403)})
    assert res.error == "http_403" and res.entries == [] and res.coverage == "unknown" and "could not be read" in res.coverage_note


def test_a_load_more_control_keeps_the_listing_from_being_called_complete(cfg):
    _conn, res, _c = run(cfg, pages(1, extra="<button>Load more</button>"))
    assert res.coverage == "partial" and "load more" in res.coverage_note


def test_the_same_company_in_the_same_state_is_one_entry_but_two_states_are_both_kept(cfg):
    html = doc("<h2>Current</h2>" + grid(card("Dup Co"), card("Dup Co"), card("Other Co"), card("Third Co")) + "<h2>Realized</h2>" +
               grid(card("Dup Co"), card("Old A"), card("Old B")))
    _conn, res, _c = run(cfg, {URL: page(html)})
    assert sorted((e.name, e.state) for e in res.entries) == [("Dup Co", "current"), ("Dup Co", "historical"), ("Old A", "historical"), ("Old B", "historical"),
                                                               ("Other Co", "current"), ("Third Co", "current")]


def test_an_empty_html_listing_is_unknown_coverage_not_an_empty_portfolio(cfg):
    _conn, res, _c = run(cfg, {URL: page('<html><body><div id="root"></div></body></html>')})
    assert res.entries == [] and res.coverage == "unknown" and "drawn by scripts" in res.coverage_note and res.error is None


# ---- WordPress public API ------------------------------------------------------------------------------------------------------------------------
def wp_routes(items_by_page, total, types=None, state_classes=None):
    api = "https://www.firm.example/wp-json/"
    types = types or {"post": {"rest_base": "posts", "name": "Posts"}, "portfolio": {"rest_base": "portfolio", "name": "Portfolio"}}
    routes = {URL: page(doc("<p>Loading…</p>", f'<link rel="https://api.w.org/" href="{api}">')),
              api + "wp/v2/types": page(json.dumps(types), ctype="application/json")}
    for i, items in enumerate(items_by_page, 1):
        routes[api + f"wp/v2/portfolio?per_page=100&page={i}&_fields=id,slug,link,title,class_list"] = page(
            json.dumps(items), ctype="application/json", **{"x-wp-total": str(total)})
    return routes


def wp_item(i, name, classes=()):
    return {"id": i, "slug": f"co-{i}", "link": f"https://www.firm.example/portfolio/co-{i}/", "title": {"rendered": name}, "class_list": list(classes)}


def test_a_script_drawn_listing_is_read_through_the_sites_public_wordpress_api(cfg):
    items = [wp_item(1, "Alpha &amp; Co", ["portfolio_status-current"]), wp_item(2, "Café Beta"), wp_item(3, "Old Gamma", ["portfolio_status-exited"])]
    conn, res, _c = run(cfg, wp_routes([items], 3))
    assert res.via == "wordpress_api" and res.coverage == "complete", res.coverage_note
    assert [(e.name, e.state) for e in res.entries] == [("Alpha & Co", "current"), ("Café Beta", "current"), ("Old Gamma", "historical")]
    assert res.entries[1].by_contrast and not res.entries[0].by_contrast                          # Café Beta is current only because Old Gamma is exited
    snap = conn.execute("SELECT text FROM pe_snapshots WHERE url LIKE '%wp/v2/portfolio%'").fetchone()[0]
    assert all(e.excerpt in snap for e in res.entries)                                              # the excerpt is in what was read


def test_wordpress_totals_that_do_not_match_make_it_partial(cfg):
    _conn, res, _c = run(cfg, wp_routes([[wp_item(1, "Alpha Co"), wp_item(2, "Beta Co")]], 5))
    assert res.coverage == "partial" and "API reports 5 items; 2 were read" in res.coverage_note


def test_a_site_whose_api_has_no_portfolio_type_is_left_unknown(cfg):
    _conn, res, _c = run(cfg, wp_routes([], 0, types={"post": {"rest_base": "posts", "name": "Posts"}}))
    assert res.entries == [] and res.coverage == "unknown" and "no portfolio-like content type" in res.coverage_note
