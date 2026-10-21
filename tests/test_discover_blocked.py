"""Discovery sources that block our reader: a company whose source page answers 401, 403 or 429 is kept when the search itself returned that page, and says so; every
other failure to resolve still drops it. Nothing is ever shown as page-checked that code could not read. Pages and companies here are synthetic."""

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
