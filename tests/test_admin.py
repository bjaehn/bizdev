import re
from datetime import date

import pytest

from bd.score_store import score_accounts
from bd.ui.app import ADMIN_GROUPS, ADMIN_PAGES
from tests.conftest import add_account, add_signal
from tests.test_hosted import HOST, PW, call
from tests.test_ui import world  # noqa: F401  (fixture: local, read-only app)
from tests.test_update import app_env  # noqa: F401  (fixture: hosted app)


def nav_of(body):
    return body.split("<nav>")[1].split("</nav>")[0]


def test_after_login_the_app_opens_on_leadership(app_env):  # noqa: F811
    r = call(app_env.app, "POST", "/login", {"password": PW})
    assert r.code == 303 and r.h["Location"] == "/leadership" and "bd_session=" in r.h["Set-Cookie"]
    ck = r.h["Set-Cookie"].split(";")[0].split("=", 1)[1]
    assert call(app_env.app, "GET", "/login", cookie=ck).h["Location"] == "/leadership"      # already signed in
    assert call(app_env.app, "GET", "/", cookie=ck).code == 200                              # "/" is still the Update page


def test_the_bar_has_five_plain_links_and_admin_lights_up_on_admin_pages(app_env):  # noqa: F811
    e = app_env
    nav = nav_of(e.get("/leadership").body)
    assert re.findall(r'href="(/[a-z]+)"', nav) == ["/leadership", "/opportunities", "/lookup", "/clients", "/admin"]
    assert nav.count('class="top on"') == 1 and 'class="top on" href="/leadership"' in nav
    for key, href, *_ in (i for _, items in ADMIN_GROUPS for i in items):
        page = e.get(href)
        assert page.code == 200, href
        assert 'class="top on" href="/admin"' in nav_of(page.body), href                      # every admin page keeps Admin lit
    assert 'class="top on" href="/opportunities"' in nav_of(e.get("/opportunities").body)
    assert 'class="top on" href="/lookup"' in nav_of(e.get("/lookup").body)
    assert 'class="top on" href="/clients"' in nav_of(e.get("/clients").body)


def test_the_hub_links_every_page_once_with_a_hint(app_env):  # noqa: F811
    body = app_env.get("/admin").body
    hrefs = re.findall(r'<a class="name"[^>]*href="([^"]+)"', body)
    expected = [i[1] for _, items in ADMIN_GROUPS for i in items]
    assert hrefs == expected and len(set(hrefs)) == len(hrefs)
    for name, _ in ADMIN_GROUPS:
        assert f"<h2" in body and name in body
    assert "Advanced tools" in body and "Update my list" in body
    assert ADMIN_PAGES >= {i[0] for _, items in ADMIN_GROUPS for i in items}


def test_the_local_app_hides_the_hosted_only_items(world):  # noqa: F811
    body = world.c.get("/admin").body
    assert "Update my list" not in body and "Advanced tools" not in body
    assert "All companies" in body and "Data health" in body and "Settings" in body
    assert re.findall(r'href="(/[a-z]+)"', nav_of(body)) == ["/leadership", "/opportunities", "/lookup", "/clients", "/admin"]


def test_a_company_page_leads_with_the_story_and_keeps_every_form(app_env):  # noqa: F811
    e = app_env
    a = add_account(e.conn, "Acme")
    add_signal(e.conn, e.cfg, a, "leadership_change", 5)
    page = e.get(f"/accounts/{a}").body
    assert 'class="top on" href="/admin"' not in nav_of(page)                          # a company is not an admin page
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
