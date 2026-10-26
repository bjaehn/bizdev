"""Discovery sources, homepages and evidence pages that block our reader. A company whose source page answers 401, 403 or 429 is kept when the search itself returned that
page, and says so; a company whose own homepage answers 401, 403 or 429 keeps its domain, and says so; a fact whose evidence page the search returned and which answers
401, 403 or 429 is kept, and says so; every other failure to resolve still drops the source, the domain or the fact. Nothing is ever shown as page-checked that code could
not read. Pages and companies here are synthetic."""

import pytest

from bd.accounts import list_accounts
from bd.discover import Discoverer, run_discover
from bd.enrich import Enricher
from bd.verify import READER_BLOCKED, VerifyResult
from tests.conftest import AS_OF, FakeVerifier, make_db
from tests.test_discover import LIST_PAGE, RoutingLLM, all_urls, co, county_facts
from tests.test_update import app_env  # noqa: F401  (fixture)

WALL = "https://paywalled.example/directory"


@pytest.fixture
def cfg(cfg):
    return cfg.model_copy(deep=True)


@pytest.fixture
def conn(tmp_path):
    return make_db(tmp_path)


def go(cfg, conn, llm, bad, log=None):
    v = FakeVerifier(bad)
    return run_discover(conn, cfg, Discoverer(cfg, llm, v), Enricher(cfg, llm, v, None), AS_OF, model=None, segments=["manufacturing"], workers=1,
                        log=log or (lambda s: None))


def reason(conn, name):
    return conn.execute("SELECT reason FROM candidates WHERE name = ?", (name,)).fetchone()["reason"]


# ---- the verifier's own answer -------------------------------------------------------------------------------------------------------------------------
def test_only_a_401_403_or_429_answer_counts_as_blocking_our_reader():
    assert READER_BLOCKED == (401, 403, 429)
    for status in (401, 403, 429):
        assert VerifyResult(False, f"http_{status}", status=status).blocks_reader
    for r in (VerifyResult(True, "ok", status=200), VerifyResult(False, "http_404", status=404), VerifyResult(False, "http_410", status=410), VerifyResult(False, "http_500", status=500),
              VerifyResult(False, "timeout"), VerifyResult(False, "dns_failure"), VerifyResult(False, "private_address"), VerifyResult(False, "bad_url"),
              VerifyResult(False, "blocked_domain")):
        assert not r.blocks_reader


# ---- kept ----------------------------------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("status", [401, 403, 429])
def test_a_source_the_search_returned_that_blocks_our_reader_is_kept_marked_and_researched(cfg, conn, status):
    llm = RoutingLLM([co("Walled Co", "walled.com", WALL)], {"Walled Co": county_facts("Walled Co", "walled.com")}, [LIST_PAGE, WALL, *all_urls("walled.com")])
    lines: list[str] = []
    s = go(cfg, conn, llm, {WALL: f"http_{status}"}, lines.append)
    assert s.kept_blocked == ["Walled Co"] and s.skipped_source == 0 and s.added == ["Walled Co"]
    a = next(x for x in list_accounts(conn, status=None) if x.name == "Walled Co")
    assert a.source_url == WALL and a.origin == "discovered" and not a.reviewed                                   # unreviewed, like every discovered company
    assert f"source blocks our reader (http_{status}): the search returned the page, code could not read it" in reason(conn, "Walled Co")
    assert any("kept Walled Co: source blocks our reader" in ln for ln in lines) and not any("dropped Walled Co" in ln for ln in lines)


def test_the_note_stays_with_a_kept_company_that_the_fit_gate_then_rejects_or_queues(cfg, conn):
    cs = [co("Tiny Walled", "tinywalled.com", WALL), co("Far Walled", "farwalled.com", WALL + "/2")]
    llm = RoutingLLM(cs, {"Tiny Walled": county_facts("Tiny Walled", "tinywalled.com", revenue=1e6, employees=10),
                          "Far Walled": county_facts("Far Walled", "farwalled.com", county="Marion County", hq_state="IN")},
                     [LIST_PAGE, WALL, WALL + "/2", *all_urls("tinywalled.com", "farwalled.com")])
    s = go(cfg, conn, llm, {WALL: "http_403", WALL + "/2": "http_403"})
    assert sorted(s.kept_blocked) == ["Far Walled", "Tiny Walled"] and s.added == []
    for name in ("Tiny Walled", "Far Walled"):
        assert "source blocks our reader (http_403)" in reason(conn, name)
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0


# ---- still dropped -------------------------------------------------------------------------------------------------------------------------------------
def test_every_other_failure_still_drops_the_company_and_says_why(cfg, conn):
    gone = {"https://a.example/404": "http_404", "https://b.example/410": "http_410", "https://c.example/500": "http_500", "https://d.example/timeout": "timeout",
            "https://e.example/dns": "dns_failure", "https://f.example/conn": "connect_error:ConnectError"}
    cs = [co(f"Co {i}", f"co{i}.com", url) for i, url in enumerate(gone)]
    llm = RoutingLLM(cs, {}, [LIST_PAGE, *gone])
    lines: list[str] = []
    s = go(cfg, conn, llm, gone, lines.append)
    assert s.skipped_source == len(gone) and s.kept_blocked == [] and s.researched == 0
    assert all(any(f"dropped Co {i}: source unreachable ({r})" in ln for ln in lines) for i, r in enumerate(gone.values()))


def test_a_blocking_source_the_search_did_not_return_or_on_a_blocked_domain_still_drops(cfg, conn):
    cs = [co("Invented Co", "invented.com", "https://never-returned.example/x"), co("Linked Co", "linked.com", "https://www.linkedin.com/company/linked")]
    llm = RoutingLLM(cs, {}, [LIST_PAGE, "https://www.linkedin.com/company/linked"])                 # the first URL is not among the search results
    s = go(cfg, conn, llm, {"https://never-returned.example/x": "http_403", "https://www.linkedin.com/company/linked": "http_403"})
    assert s.skipped_source == 2 and s.kept_blocked == [] and s.researched == 0


def test_an_ordinary_source_that_loads_carries_no_note(cfg, conn):
    llm = RoutingLLM([co("Open Co", "openco.com")], {"Open Co": county_facts("Open Co", "openco.com")}, all_urls("openco.com"))
    s = go(cfg, conn, llm, {})
    assert s.added == ["Open Co"] and s.kept_blocked == [] and "blocks our reader" not in (reason(conn, "Open Co") or "")


# ---- the pages -----------------------------------------------------------------------------------------------------------------------------------------
def test_the_account_and_candidate_pages_say_the_source_blocks_our_reader(app_env, cfg):  # noqa: F811
    e = app_env
    llm = RoutingLLM([co("Walled Co", "walled.com", WALL), co("Open Co", "openco.com")],
                     {"Walled Co": county_facts("Walled Co", "walled.com"), "Open Co": county_facts("Open Co", "openco.com")}, [LIST_PAGE, WALL, *all_urls("walled.com", "openco.com")])
    go(e.cfg, e.conn, llm, {WALL: "http_403"})
    walled = next(a for a in list_accounts(e.conn, status=None) if a.name == "Walled Co")
    open_ = next(a for a in list_accounts(e.conn, status=None) if a.name == "Open Co")
    page = " ".join(e.get(f"/accounts/{walled.id}").body.split())
    assert "Found by market research" in page and "blocks our reader, so it was not page-checked" in page and "Open it yourself to confirm" in page
    other = " ".join(e.get(f"/accounts/{open_.id}").body.split())
    assert "Found by market research" in other and "blocks our reader" not in other
    e.conn.execute("INSERT INTO candidates(name, domain, segment, source_url, found_date, status, reason) VALUES ('Queued Co','queued.com','x',?,?, 'queued', "
                   "'source blocks our reader (http_403): the search returned the page, code could not read it; size unverified')", (WALL, AS_OF.isoformat()))
    e.conn.commit()
    assert "source blocks our reader (http_403)" in e.get("/candidates").body


# ---- the company's own homepage --------------------------------------------------------------------------------------------------------------------
HOME_NOTE = "homepage blocks our reader (http_{status}): a server answered at the domain, code could not read the page"


def walled_home(status="403", name="Walled Co", dom="walled.com", domain_hint=True, **over):
    cs = [co(name, dom if domain_hint else None)]
    return RoutingLLM(cs, {name: county_facts(name, dom, **over)}, all_urls(dom)), {f"https://{dom}": f"http_{status}"}


@pytest.mark.parametrize("status", [401, 403, 429])
def test_a_homepage_that_blocks_our_reader_keeps_its_domain_marked_and_the_company_is_added(cfg, conn, status):
    llm, bad = walled_home(status)
    lines: list[str] = []
    s = go(cfg, conn, llm, bad, lines.append)
    assert s.added == ["Walled Co"] and s.kept_blocked_domain == ["Walled Co"] and s.kept_blocked == [] and not s.queued
    a = next(x for x in list_accounts(conn, status=None) if x.name == "Walled Co")
    assert a.domain == "walled.com" and a.origin == "discovered" and not a.reviewed
    assert HOME_NOTE.format(status=status) in reason(conn, "Walled Co")
    assert any("kept the domain of Walled Co: homepage blocks our reader" in ln for ln in lines)


def test_a_homepage_that_does_not_load_still_leaves_the_company_without_a_domain_and_queued(cfg, conn):
    for i, why in enumerate(("http_404", "http_410", "http_500", "timeout", "dns_failure", "connect_error:ConnectError")):
        llm, _ = walled_home(name=f"Gone Co {i}", dom=f"gone{i}.com")
        s = go(cfg, conn, llm, {f"https://gone{i}.com": why})
        assert s.queued == [f"Gone Co {i}"] and s.added == [] and s.kept_blocked_domain == [], why
        c = conn.execute("SELECT domain, status, reason FROM candidates WHERE name = ?", (f"Gone Co {i}",)).fetchone()
        assert c["domain"] is None and c["status"] == "queued" and "homepage blocks" not in (c["reason"] or ""), why      # no domain: its own site is not known, so facts cited there do not count


def test_a_domain_on_the_blocked_list_is_never_fetched(cfg, conn):
    llm = RoutingLLM([co("Linked Co", "linkedin.com")], {"Linked Co": county_facts("Linked Co", "linkedin.com")}, all_urls("linkedin.com"))
    v = FakeVerifier({"https://linkedin.com": "http_403"})
    run_discover(conn, cfg, Discoverer(cfg, llm, v), Enricher(cfg, llm, v, None), AS_OF, model=None, segments=["manufacturing"], workers=1, log=lambda s: None)
    assert "https://linkedin.com" not in v.checked and conn.execute("SELECT status FROM candidates").fetchone()["status"] == "queued"


def test_a_domain_only_the_researched_facts_name_is_kept_the_same_way(cfg, conn):
    llm, bad = walled_home(domain_hint=False)                                          # discovery gave no domain; the enrich step found it
    s = go(cfg, conn, llm, bad)
    assert s.added == ["Walled Co"] and s.kept_blocked_domain == ["Walled Co"]
    assert HOME_NOTE.format(status=403) in reason(conn, "Walled Co")
    assert next(x for x in list_accounts(conn, status=None) if x.name == "Walled Co").domain == "walled.com"


def test_both_notes_travel_with_the_company_in_order(cfg, conn):
    llm = RoutingLLM([co("Walled Co", "walled.com", WALL)], {"Walled Co": county_facts("Walled Co", "walled.com")}, [LIST_PAGE, WALL, *all_urls("walled.com")])
    s = go(cfg, conn, llm, {WALL: "http_403", "https://walled.com": "http_429"})
    assert s.added == ["Walled Co"] and s.kept_blocked == ["Walled Co"] and s.kept_blocked_domain == ["Walled Co"]
    r = reason(conn, "Walled Co")
    assert r.index("source blocks our reader (http_403)") < r.index("homepage blocks our reader (http_429)")


# ---- the evidence pages ----------------------------------------------------------------------------------------------------------------------------
EVIDENCE_NOTE = "evidence blocks our reader for {facts} ({status}): the search returned the pages, code could not read them"


@pytest.mark.parametrize("status", [401, 403, 429])
def test_facts_resting_on_an_evidence_page_that_blocks_our_reader_count_and_say_so(cfg, conn, status):
    llm, bad = walled_home(status)
    bad["https://walled.com/about"] = f"http_{status}"                                  # location, revenue, employees and ownership all cite this page
    lines: list[str] = []
    s = go(cfg, conn, llm, bad, lines.append)
    assert s.added == ["Walled Co"] and s.kept_blocked_evidence == ["Walled Co"] and s.kept_blocked_domain == ["Walled Co"] and not s.queued
    a = next(x for x in list_accounts(conn, status=None) if x.name == "Walled Co")
    assert a.chicagoland == "yes" and a.revenue == 5e8 and a.employees == 2000 and a.ownership == "public" and not a.reviewed
    r = reason(conn, "Walled Co")
    assert EVIDENCE_NOTE.format(facts="location, revenue, employees, ownership", status=f"http_{status}") in r
    assert any("kept facts of Walled Co: evidence blocks our reader for location, revenue, employees, ownership" in ln for ln in lines)


def test_an_evidence_page_that_is_down_or_missing_still_drops_its_fact_and_the_company_waits(cfg, conn):
    for i, why in enumerate(("http_404", "http_410", "http_500", "timeout", "dns_failure")):
        llm, bad = walled_home(name=f"Gone Co {i}", dom=f"gone{i}.com")
        bad[f"https://gone{i}.com/about"] = why
        s = go(cfg, conn, llm, bad)
        assert s.queued == [f"Gone Co {i}"] and s.added == [] and s.kept_blocked_evidence == [], why
        assert "evidence blocks" not in (reason(conn, f"Gone Co {i}") or ""), why


def test_blocked_evidence_the_search_did_not_return_or_on_a_blocked_domain_still_drops(cfg, conn):
    ev = {k: {"url": "https://www.linkedin.com/company/walled", "source_type": "company_site"} for k in ("il_county", "revenue_usd", "employees", "ownership")}
    llm = RoutingLLM([co("Linked Co", "linked.com")], {"Linked Co": county_facts("Linked Co", "linked.com", evidence=ev)}, all_urls("linked.com") + ["https://www.linkedin.com/company/walled"])
    s = go(cfg, conn, llm, {"https://www.linkedin.com/company/walled": "http_403"})
    assert s.queued == ["Linked Co"] and s.kept_blocked_evidence == []
    ev2 = {k: {"url": "https://walled.com/never-returned", "source_type": "company_site"} for k in ("il_county", "revenue_usd", "employees", "ownership")}
    llm2 = RoutingLLM([co("Walled Co", "walled.com")], {"Walled Co": county_facts("Walled Co", "walled.com", evidence=ev2)}, all_urls("walled.com"))
    s2 = go(cfg, conn, llm2, {"https://walled.com/never-returned": "http_403"})
    assert s2.queued == ["Walled Co"] and s2.kept_blocked_evidence == []


def test_a_fact_that_is_dropped_for_another_reason_is_not_called_unread(cfg, conn):
    news = "https://news.example/walled-revenue"
    ev = {"il_county": {"url": "https://walled.com/about", "source_type": "company_site"}, "revenue_usd": {"url": news, "source_type": "secondary_report"},
          "employees": {"url": "https://walled.com/about", "source_type": "company_site"}, "ownership": {"url": "https://walled.com/about", "source_type": "company_site"}}
    llm = RoutingLLM([co("Walled Co", "walled.com")], {"Walled Co": county_facts("Walled Co", "walled.com", evidence=ev)}, all_urls("walled.com") + [news])
    s = go(cfg, conn, llm, {news: "http_403"})                                          # the secondary source is refused as size evidence whatever its status
    assert s.kept_blocked_evidence == [] and "evidence blocks our reader" not in (reason(conn, "Walled Co") or "")


def test_the_account_and_candidate_pages_say_the_homepage_blocks_our_reader(app_env, cfg):  # noqa: F811
    e = app_env
    llm, bad = walled_home()
    go(e.cfg, e.conn, llm, bad)
    walled = next(a for a in list_accounts(e.conn, status=None) if a.name == "Walled Co")
    page = " ".join(e.get(f"/accounts/{walled.id}").body.split())
    assert "Found by market research" in page and "walled.com answers but blocks our reader, so the domain was not page-checked" in page
    assert "Open it yourself to confirm it is this company's site" in page and "it blocks our reader, so it was not page-checked" not in page
    assert "The facts for" not in page                                                      # its evidence pages loaded: no evidence sentence
    llm2 = RoutingLLM([co("Open Co", "openco.com")], {"Open Co": county_facts("Open Co", "openco.com")}, all_urls("openco.com"))
    go(e.cfg, e.conn, llm2, {})
    open_ = next(a for a in list_accounts(e.conn, status=None) if a.name == "Open Co")
    assert "blocks our reader" not in " ".join(e.get(f"/accounts/{open_.id}").body.split())
    e.conn.execute("INSERT INTO candidates(name, domain, segment, source_url, found_date, status, reason) VALUES ('Queued Co','queued.com','x',?,?, 'queued', ?)",
                   (LIST_PAGE, AS_OF.isoformat(), HOME_NOTE.format(status=403) + "; size unverified"))
    e.conn.commit()
    assert "homepage blocks our reader (http_403)" in e.get("/candidates").body



def test_the_account_and_candidate_pages_say_which_facts_rest_on_unread_pages(app_env, cfg):  # noqa: F811
    e = app_env
    llm, bad = walled_home()
    bad["https://walled.com/about"] = "http_403"
    go(e.cfg, e.conn, llm, bad)
    walled = next(a for a in list_accounts(e.conn, status=None) if a.name == "Walled Co")
    page = " ".join(e.get(f"/accounts/{walled.id}").body.split())
    assert "The facts for location, revenue, employees, ownership were accepted from pages the search returned that answer but block our reader" in page
    assert "Open the sources in the change log to confirm them before marking it reviewed" in page
    e.conn.execute("INSERT INTO candidates(name, domain, segment, source_url, found_date, status, reason) VALUES ('Queued Co','queued.com','x',?,?, 'queued', ?)",
                   (LIST_PAGE, AS_OF.isoformat(), EVIDENCE_NOTE.format(facts="location", status="http_403")))
    e.conn.commit()
    assert "evidence blocks our reader for location (http_403)" in e.get("/candidates").body
