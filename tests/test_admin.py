import re
from datetime import date

import pytest

from bd.score_store import score_accounts
from bd.ui.app import ADMIN_GROUPS, ADMIN_PAGES, REPORT_ITEMS
from tests.conftest import add_account, add_signal
from tests.test_hosted import HOST, PW, call
from tests.test_ui import world  # noqa: F401  (fixture: local, read-only app)
from tests.test_update import app_env  # noqa: F401  (fixture: hosted app)


def nav_of(body):
    return body.split("<nav>")[1].split("</nav>")[0]


def test_after_login_the_app_opens_on_the_ranking(app_env):  # noqa: F811
    r = call(app_env.app, "POST", "/login", {"password": PW})
    assert r.code == 303 and r.h["Location"] == "/opportunities" and "bd_session=" in r.h["Set-Cookie"]
    ck = r.h["Set-Cookie"].split(";")[0].split("=", 1)[1]
    assert call(app_env.app, "GET", "/login", cookie=ck).h["Location"] == "/opportunities"      # already signed in
    assert call(app_env.app, "GET", "/leadership", cookie=ck).h["Location"] == "/opportunities"  # the old page folds into the ranking
    assert call(app_env.app, "GET", "/", cookie=ck).code == 200                                  # "/" is still the Update page (Admin)


ANALYSIS_PAGES = ["/opportunities", "/reports", "/market", "/hiring", "/firms", "/rfps", "/ask", "/monday"]


def test_the_analysis_side_has_two_links_and_no_admin_machinery(app_env):  # noqa: F811
    e = app_env
    for path in ANALYSIS_PAGES:
        body = e.get(path).body
        assert e.get(path).code == 200, path
        assert re.findall(r'href="(/[a-z]+)"', nav_of(body)) == ["/opportunities", "/reports"], path
        assert 'class="adminlink" href="/admin"' in body and 'class="adminnav"' not in body and "<header class=\"admin\">" not in body, path
        assert "Update my list" not in nav_of(body) and "Look up" not in nav_of(body)
    assert 'class="top on" href="/opportunities"' in nav_of(e.get("/opportunities").body)
    assert 'class="top on" href="/reports"' in nav_of(e.get("/market").body)               # a report keeps Reports lit


def test_reports_lists_each_report_once_and_each_opens_on_the_analysis_side(app_env):  # noqa: F811
    body = app_env.get("/reports").body
    hrefs = re.findall(r'<a class="name"[^>]*href="([^"]+)"', body)
    assert hrefs == [i[1] for i in REPORT_ITEMS] and len(set(hrefs)) == len(hrefs)
    assert "Market view" in body and "Ask your data" in body and "Open RFPs" in body


def test_admin_is_a_different_area_with_its_own_shell(app_env):  # noqa: F811
    e = app_env
    pages = ["/admin"] + [i[1] for _, items in ADMIN_GROUPS for i in items]
    for path in pages:
        page = e.get(path)
        assert page.code == 200, path
        body = page.body
        assert '<header class="admin">' in body and '<span class="area">Admin</span>' in body, path
        assert re.findall(r'href="(/[a-z]+)"', nav_of(body)) == ["/opportunities"] and "Back to analysis" in nav_of(body), path
        side = body.split('<aside class="adminnav">')[1].split("</aside>")[0]
        for _, href, *_ in (i for _, items in ADMIN_GROUPS for i in items):
            assert f'href="{href}"' in side, (path, href)                                  # every admin page is one click away
    assert 'href="/accounts" class="on"' in e.get("/accounts").body                       # and the sidebar marks the current page


def test_the_sidebar_marks_where_you_are_and_the_local_app_hides_hosted_only_items(world):  # noqa: F811
    body = world.c.get("/accounts").body
    assert 'href="/accounts" class="on"' in body
    assert "Update my list" not in body.split('<aside class="adminnav">')[1].split("</aside>")[0]
    assert "Advanced tools" not in body and "Data health" in body and "Settings" in body


def test_lookup_clients_and_pursuits_live_in_admin(app_env):  # noqa: F811
    e = app_env
    for path in ("/lookup", "/clients", "/watchlist"):
        assert '<header class="admin">' in e.get(path).body, path
    assert 'href="/lookup"' not in nav_of(e.get("/opportunities").body)


def test_a_company_page_leads_with_the_story_and_keeps_every_form(app_env):  # noqa: F811
    e = app_env
    a = add_account(e.conn, "Acme")
    add_signal(e.conn, e.cfg, a, "leadership_change", 5)
    page = e.get(f"/accounts/{a}").body
    assert '<header class="admin">' not in page                                         # for now the company page is on the analysis side
    story, rest = page.split("<summary>Edit facts</summary>")[0], page.split("<summary>Edit facts</summary>")[1]
    assert "Why now" in story and "Signals (1)" in story and "Leadership change" in story
    assert story.split("<main>")[1].count('<form method="post"') == 3                    # stage, watch, client: the action row only
    for action in ("stage", "watch", "client", "note"):
        assert f'action="/accounts/{a}/{action}"' in page
    assert f'action="/accounts/{a}"' in rest and 'name="revenue"' in rest and 'name="cik"' in rest      # the facts form is still there, folded away
    assert "<details class=\"card\" open>" not in page                                  # closed by default
    for title in ("Edit facts", "Notes", "Open job postings (0)", "History"):
        assert f"<summary>{title}</summary>" in page


def test_a_failed_save_opens_the_edit_block_and_keeps_the_input(app_env):  # noqa: F811
    e = app_env
    a = add_account(e.conn, "Acme")
    r = e.post(f"/accounts/{a}", {"revenue": "lots", "domain": "typed.com"})
    assert r.code == 400 and '<details class="card" open><summary>Edit facts</summary>' in r.body
    assert 'value="lots"' in r.body and "Not saved" in r.body


def test_a_company_waiting_for_facts_says_why_in_plain_words(app_env):  # noqa: F811
    e = app_env
    a = add_account(e.conn, "Waitco", status="hold_review", chicagoland="unknown", revenue=None, ownership=None)
    body = e.get(f"/accounts/{a}").body
    assert "Needs facts" in body and "Not scanned yet:" in body and "Chicagoland status unverified" in body
    assert "Fit gate" not in body and "hold_review" not in body


def test_opportunities_keeps_every_filter_under_more_filters(app_env):  # noqa: F811
    e = app_env
    a = add_account(e.conn, "Acme")
    for t in ("leadership_change", "hiring_cluster"):
        add_signal(e.conn, e.cfg, a, t, 3, as_of=date.today())
    score_accounts(e.conn, e.cfg, date.today())
    body = e.get("/opportunities").body
    form = body.split('<form method="get" action="/opportunities"')[1].split("</form>")[0]
    visible, hidden = form.split('<details class="more">')
    assert 'name="q"' in visible and 'name="band"' in visible and visible.count("<select") == 1
    for name in ("date", "family", "service_line", "status", "stage", "client", "sort", "pe", "new", "tracked"):
        assert f'name="{name}"' in hidden, name
    # every old query string still works (bookmarks and links keep their meaning)
    assert "Acme" in e.get("/opportunities?q=acme&client=no&stage=open&sort=change").body
    assert "No opportunities match" in e.get("/opportunities?q=zzz").body and "No opportunities match" in e.get("/opportunities?client=yes").body
