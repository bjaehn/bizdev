"""The Private equity page: the map, its list twin and the firm table, the filters, the lookup, and the links to and from it. Rows are synthetic and live only in these tests."""

import re

import pytest

from bd.score_store import score_accounts
from tests.test_pe_network import workbook_company
from tests.test_pe_rollup import TODAY, own, signal
from tests.test_update import app_env  # noqa: F401  (fixture)


@pytest.fixture
def world(app_env):  # noqa: F811
    e = app_env
    fid, a_cid, _, a = own(e.conn, e.cfg, "Alpha Capital", "Lakeshore Health")
    own(e.conn, e.cfg, "Beta Partners", "Lakeshore Health")
    own(e.conn, e.cfg, "Alpha Capital", "Quiet Co")
    own(e.conn, e.cfg, "Alpha Capital", "Lead Co", claim="listed")
    workbook_company(e.conn, e.cfg, "Gamma Fund", "Ring Co")
    for t, age in (("leadership_change", 5), ("restructuring_layoffs", 9), ("platform_migration", 12)):
        signal(e.conn, e.cfg, a, t, age)
    score_accounts(e.conn, e.cfg, TODAY)
    return e, fid, a_cid, a


def text(body):
    return " ".join(re.sub(r"<[^>]+>", " ", body.split("<main")[1]).split())


def test_with_nothing_in_the_ledger_it_says_so_and_draws_nothing(app_env):  # noqa: F811
    r = app_env.get("/private-equity")
    t = text(r.body)
    assert r.code == 200 and "Nothing is drawn yet" in t and "<svg" not in r.body.split("<main")[1] and "No firm holds, or is listed with, a company yet" in t


def test_the_page_shows_the_counts_the_map_its_legend_and_the_firm_table(world):
    e, fid, *_ = world
    body = e.get("/private-equity").body
    t = text(body)
    assert "firms hold or are listed with a company" in t and "confirmed holdings, current or pending" in t and "leads, not confirmed by a page" in t
    assert body.count('<svg class="pg"') == 1 and body.count('<svg class="pg pg--phone"') == 1                       # drawn twice: wide, and the phone one
    assert "Prospect now" in t and "Location per the workbook, not confirmed" in t and "Squares are firms, circles are companies" in t
    assert "Alpha Capital" in t and "The same picture as a list" in t and "lead, not confirmed" in t
    assert "A lead is a listing no page has confirmed; leads are never counted as holdings" in t


def test_the_map_and_its_list_agree_about_who_is_drawn(world):
    e, *_ = world
    body = e.get("/private-equity").body
    drawn = set(re.findall(r'<a href="/(?:companies|portfolio)/\d+" class="pg-node">.*?<title>([^<]+?) ·', body.split("<main")[1]))
    listed = set(re.findall(r'<td data-label="Company"><a href="/(?:companies|portfolio)/\d+">([^<]+)</a>', body))
    assert drawn == listed == {"Lakeshore Health", "Quiet Co", "Lead Co", "Ring Co"}


def test_a_firm_view_filters_and_a_bad_value_is_ignored_not_an_error(world):
    e, fid, *_ = world
    one = text(e.get(f"/private-equity?firm={fid}").body)
    assert "The neighbourhood of Alpha Capital" in one and "Beta Partners" in one
    for bad in ("?firm=999", "?firm=abc", "?cat=nonsense", "?past=maybe", "?q="):
        r = e.get("/private-equity" + bad)
        assert r.code == 200 and "Who holds whom" in r.body, bad
    assert "Past ownership" in text(e.get("/private-equity?past=1").body)
    assert 'class="on">Shown' in e.get("/private-equity?past=1").body and 'class="on">Hidden' in e.get("/private-equity").body
    none = e.get("/private-equity?cat=monitor").body
    assert "Show all priorities" in text(none) or "<svg" in none


def test_the_lookup_names_the_investors_marks_a_lead_and_does_not_call_no_record_proof(world):
    e, *_ = world
    hit = text(e.get("/private-equity?q=lakeshore").body)
    assert 'Who owns "lakeshore"?' in hit and "Alpha Capital" in hit and "Beta Partners" in hit and "signals in the last 90 days" in hit
    lead = text(e.get("/private-equity?q=lead+co").body)
    assert "lead, not confirmed" in lead
    miss = text(e.get("/private-equity?q=zzzz+nothing").body)
    assert 'No company called "zzzz nothing" is in the ownership ledger' in miss and "not proof it is not private-equity-backed" in miss
    evil = e.get("/private-equity?q=%3Cscript%3Ealert(1)%3C/script%3E").body
    assert "<script>alert(1)</script>" not in evil and "&lt;script&gt;" in evil


def test_the_page_is_in_the_navigation_and_the_old_directory_is_the_sources_report(world):
    e, fid, *_ = world
    nav = e.get("/opportunities").body.split("</nav>")[0]
    assert 'href="/private-equity">Private equity</a>' in nav
    assert 'class="top on" href="/private-equity"' in e.get("/private-equity").body and 'class="top on" href="/private-equity"' in e.get(f"/firms/{fid}").body
    firms = e.get("/firms").body
    assert "<h1>PE firm sources</h1>" in firms and "Private equity map" in firms
    assert "PE firm sources" in e.get("/reports").body
    assert "/private-equity" in e.get("/opportunities").body.split("Private equity</h2>")[1][:3000] or "/private-equity" in e.get("/opportunities").body


def test_the_firm_page_carries_the_one_firm_map(world):
    e, fid, *_ = world
    body = e.get(f"/firms/{fid}").body
    assert "Its companies and co-investors" in body and body.count('<svg class="pg"') == 1 and "Beta Partners also holds Lakeshore Health" in body
    assert "Its companies and co-investors" not in e.get(f"/firms/{fid}?geo=unknown").body


def test_nothing_the_page_sends_runs_a_script(world):
    e, fid, *_ = world
    for path in ("/private-equity", f"/private-equity?firm={fid}", f"/firms/{fid}"):
        main = e.get(path).body.split("<main")[1]
        assert "<script" not in main and not re.search(r"<[a-z]+[^>]*\son[a-z]+=", main), path
