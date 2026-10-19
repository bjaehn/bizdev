from datetime import date

import pytest

from bd.score_store import score_accounts
from tests.conftest import add_account, add_signal
from tests.test_update import app_env  # noqa: F401  (fixture)


@pytest.fixture
def seeded(app_env):  # noqa: F811
    e = app_env
    today = date.today()
    a = add_account(e.conn, "Briefco <i>", industry="Healthcare", hq_city="Chicago", revenue=3.4e9, employees=9000)
    add_signal(e.conn, e.cfg, a, "leadership_change", 5, as_of=today, summary="Names a new chief operating officer.", role="Dana Fox, COO",
               url="https://www.sec.gov/Archives/briefco/coo.htm", source_type="sec_filing")
    add_signal(e.conn, e.cfg, a, "hiring_cluster", 9, as_of=today, summary="4 related operations postings.", url="https://jobs.example.com/briefco")
    add_signal(e.conn, e.cfg, a, "restructuring_layoffs", 14, as_of=today, summary="Filed an 8-K about exit costs.", conf="Medium",
               url="https://www.sec.gov/Archives/briefco/8k.htm")
    bare = add_account(e.conn, "Barecompany", industry=None, revenue=None, ownership=None)
    score_accounts(e.conn, e.cfg, today)
    return e, a, bare


def section_order(body, *names):
    pos = [body.index(n) for n in names]
    return pos == sorted(pos)


def test_the_brief_leads_with_what_the_signals_add_up_to_then_what_happened_and_keeps_facts_and_score_maths_below(seeded):
    e, a, bare = seeded
    body = e.get(f"/companies/{a}").body
    assert section_order(body, "What this adds up to", "What happened", "The last 180 days", "Background", "How the score is built", "Track this company")
    short = body.split("What this adds up to")[1].split("What happened")[0]
    assert "3 kinds of signs agree: leadership, hiring, company events" in short and "Research only" in short and "Why now" not in body
    assert "Shown when a" in short and "Names a new chief operating officer." in short and 'href="https://www.sec.gov/Archives/briefco/coo.htm"' in short
    assert "Also showing" in short                                                       # more than one theme matches this company
    assert "Healthcare · Chicago" in body.split("Background")[1] and "$3.4B revenue" in body and "9,000 employees" in body
    assert "Healthcare · Chicago" not in body.split("Background")[0]                      # company facts are background, not the lead
    assert "Briefco &lt;i&gt;" in body and "Briefco <i>" not in body


def test_what_happened_lists_each_signal_with_its_source_and_marks_less_certain_ones(seeded):
    e, a, bare = seeded
    happened = e.get(f"/companies/{a}").body.split("What happened")[1].split("The last 180 days")[0]
    assert "Names a new chief operating officer." in happened and "Dana Fox, COO" in happened
    assert 'href="https://www.sec.gov/Archives/briefco/coo.htm"' in happened and "www.sec.gov" in happened
    assert "Medium confidence" in happened and happened.count("High confidence") == 0


def test_the_score_maths_is_collapsed_and_the_headline_says_the_band_and_score(seeded):
    e, a, bare = seeded
    body = e.get(f"/companies/{a}").body
    assert '<details class="card"' in body and "More detail" in body.split("<details")[1].split("</summary>")[0]
    detail = body.split("More detail")[1].split("Track this company")[0]
    assert "The last 180 days" in detail and "Background" in detail and "How the score is built" in detail      # timeline, facts and score maths all sit behind one fold
    assert "How the score is built" not in body.split("More detail")[0]


def test_a_company_with_no_signals_says_it_was_not_scanned_or_found_nothing_and_does_not_list_missing_facts(seeded):
    e, a, bare = seeded
    body = e.get(f"/companies/{bare}").body
    assert "Not on file" not in body and "Nothing found yet" in body
    assert "Not scanned yet. The next update looks for leadership changes, hiring and press" in body and "Scan for signals now" in body
    e.conn.execute("UPDATE accounts SET last_scanned = '2026-10-12' WHERE id = ?", (bare,))
    e.conn.commit()
    body = e.get(f"/companies/{bare}").body
    assert "Scanned Oct 12, 2026: no leadership, hiring, press or company-event signals found." in body


def test_tracking_a_company_from_its_brief_returns_to_the_brief_and_shows_it_on_this_week(seeded):
    e, a, bare = seeded
    assert "Companies you track" not in e.get("/week").body
    r = e.post(f"/accounts/{a}/watch", {"on": "1", "back": f"/companies/{a}"})
    assert r.code == 303 and r.h["Location"] == f"/companies/{a}"
    body = e.get(f"/companies/{a}").body
    assert "You are tracking this company" in body
    assert "Stop tracking" in body and '<span class="badge">tracking</span>' in body
    week = e.get("/week").body
    assert "Companies you track" in week and "Briefco &lt;i&gt;" in week.split("Companies you track")[1]
    r = e.post(f"/accounts/{a}/watch", {"on": "0", "back": f"/companies/{a}"})
    assert "Track this company" in e.get(f"/companies/{a}").body and "Companies you track" not in e.get("/week").body


def test_a_stage_and_a_note_set_from_the_brief_come_back_to_the_brief(seeded):
    e, a, bare = seeded
    r = e.post(f"/accounts/{a}/stage", {"stage": "reviewing", "note": "", "back": f"/companies/{a}"})
    assert r.code == 303 and r.h["Location"] == f"/companies/{a}"
    r = e.post(f"/accounts/{a}/note", {"body": "Spoke with the partner <b>today</b>", "back": f"/companies/{a}"})
    assert r.h["Location"] == f"/companies/{a}"
    body = e.get(f"/companies/{a}").body
    assert "Reviewing" in body and "Spoke with the partner &lt;b&gt;today&lt;/b&gt;" in body and "Your notes" in body
    assert "Reviewing" in e.get("/week").body.split("Companies you track")[1]      # a stage of Reviewing counts as tracked


def test_a_back_address_cannot_send_you_off_the_site(seeded):
    e, a, bare = seeded
    for bad in ("https://evil.example/x", "//evil.example/x"):
        assert e.post(f"/accounts/{a}/watch", {"on": "1", "back": bad}).h["Location"] == f"/accounts/{a}"


def test_the_tracking_box_and_toolbar_do_not_print(seeded):
    e, a, bare = seeded
    body = e.get(f"/companies/{a}").body
    assert 'class="meta noprint"' in body and '<h2 class="noprint">Track this company</h2>' in body and 'class="card noprint"' in body


# ---- the plain-text copy ------------------------------------------------------------------------------------------------------------
def test_the_plain_text_version_has_the_same_story_with_sources_and_no_markup(seeded):
    e, a, bare = seeded
    r = e.get(f"/companies/{a}/text")
    assert r.code == 200 and r.h["Content-Type"].startswith("text/plain")
    t = r.body
    assert t.startswith("Briefco <i>\nHealthcare · Chicago") and "WHAT IT ADDS UP TO" in t and "WHAT HAPPENED" in t and "WHY NOW" not in t
    assert "3 kinds of signs agree: leadership, hiring, company events" in t and "Shown when a" in t
    assert "Names a new chief operating officer. (Dana Fox, COO) Source: https://www.sec.gov/Archives/briefco/coo.htm" in t
    assert "Research only" in t and "<b>" not in t and "<div" not in t


def test_the_plain_text_version_never_prints_a_link_that_is_not_a_web_page_and_says_when_nothing_is_on_file(seeded):
    e, a, bare = seeded
    e.conn.execute("UPDATE signals SET source_url = 'javascript:alert(1)' WHERE type = 'hiring_cluster'")
    e.conn.commit()
    t = e.get(f"/companies/{a}/text").body
    assert "javascript:" not in t and "4 related operations postings." in t
    b = e.get(f"/companies/{bare}/text").body
    assert "Not scanned yet." in b and "No signals found." in b and "NOT ON FILE" not in b


def test_the_plain_text_version_of_an_unknown_company_is_a_404(seeded):
    e, a, bare = seeded
    assert e.get("/companies/9999/text").code == 404
