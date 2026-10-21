import json
import threading

import pytest

from bd.accounts import Account, list_accounts
from bd.curate import accept_candidate, mark_reviewed, reject_candidate
from bd.discover import Discoverer, run_discover, select_scan_accounts
from bd.edgar import EdgarError  # noqa: F401
from bd.enrich import Enricher
from bd.llm import LLMResult, Usage
from bd.urls import normalize_url
from tests.conftest import AS_OF, FakeVerifier, add_account, make_db

@pytest.fixture
def cfg(cfg):
    """A private copy: these tests change discovery limits and pricing, and the shared config is session-scoped."""
    return cfg.model_copy(deep=True)


LIST_PAGE = "https://directory.example/chicago-industrials"


def county_facts(name, domain, revenue=5e8, county="Cook County", ownership="public", employees=2000, **over):
    site = f"https://{domain}/about"
    f = {"domain": domain, "hq_city": "Chicago", "hq_state": "IL", "il_county": county, "revenue_usd": revenue,
         "employees": employees, "ownership": ownership, "sponsor": None, "industry": "Industrial", "cik": None, "notes": None,
         "evidence": {"il_county": {"url": site, "source_type": "company_site"},
                      "revenue_usd": {"url": site, "source_type": "company_site"},
                      "employees": {"url": site, "source_type": "company_site"},
                      "ownership": {"url": site, "source_type": "company_site"}}}
    f.update(over)
    return f


class RoutingLLM:
    """Thread-safe fake: discovery calls get `companies`, enrich calls get facts keyed by company name."""

    def __init__(self, companies, facts, urls, fail_discover=False):
        self.companies, self.facts, self.fail_discover = companies, facts, fail_discover
        self.urls = {normalize_url(u) for u in urls}
        self.lock, self.calls = threading.Lock(), []

    def run(self, system, messages, **kw):
        user = messages[-1]["content"]
        with self.lock:
            self.calls.append(user)
        if "Segment:" in user:
            body = {"companies": self.companies}
        else:
            name = user.split("Company: ")[1].splitlines()[0]
            body = {"facts": self.facts[name]}
        text = json.dumps(body)
        return LLMResult(text=text, retrieved_urls=set(self.urls), usage=Usage(input_tokens=100, output_tokens=50, web_searches=1),
                         conversation=list(messages) + [{"role": "assistant", "content": text}])


def co(name, domain, source=LIST_PAGE):
    return {"name": name, "domain": domain, "hq_city": "Chicago", "hq_state": "IL", "source_url": source, "source_type": "secondary_report"}


def go(cfg, conn, llm, bad=None, segments=("manufacturing",), max_cost=None, **cfg_over):
    for k, v in cfg_over.items():
        setattr(cfg.discovery, k, v)
    v = FakeVerifier(bad)
    return run_discover(conn, cfg, Discoverer(cfg, llm, v), Enricher(cfg, llm, v, None), AS_OF, model=None,
                        segments=list(segments), workers=1, max_cost=max_cost)


@pytest.fixture
def conn(tmp_path):
    return make_db(tmp_path)


def all_urls(*domains):
    return [LIST_PAGE, *[f"https://{d}{p}" for d in domains for p in ("", "/about")]]


def test_company_that_clears_the_fit_gate_is_auto_added_unreviewed(cfg, conn):
    llm = RoutingLLM([co("Acme Industrial", "acmeind.com")], {"Acme Industrial": county_facts("Acme Industrial", "acmeind.com")},
                     all_urls("acmeind.com"))
    s = go(cfg, conn, llm)
    assert s.added == ["Acme Industrial"] and not s.queued and not s.rejected
    a = list_accounts(conn, status=None)[0]
    assert (a.origin, a.reviewed, a.tier, a.status, a.chicagoland) == ("discovered", 0, "B", "active", "yes")
    assert a.source_url == LIST_PAGE and a.discovered_at == AS_OF.isoformat() and a.revenue == 5e8
    c = conn.execute("SELECT * FROM candidates").fetchone()
    assert c["status"] == "added" and c["account_id"] == a.id and "evidence_urls" in c["facts_json"]
    assert conn.execute("SELECT actor FROM audit_log WHERE action='create'").fetchone()["actor"] == "discover"


def test_cost_is_logged_for_search_and_for_each_researched_company(cfg, conn):
    llm = RoutingLLM([co("Acme Industrial", "acmeind.com")], {"Acme Industrial": county_facts("Acme Industrial", "acmeind.com")},
                     all_urls("acmeind.com"))
    go(cfg, conn, llm)
    steps = [(r["step"], r["account_id"] is None, r["tokens"]) for r in conn.execute("SELECT * FROM runs ORDER BY id")]
    assert steps[0] == ("discover", True, 150) and steps[1][0] == "discover_enrich" and steps[1][2] == 150


def test_unknown_fit_is_queued_not_added(cfg, conn):
    f = county_facts("Mystery Co", "mystery.com", revenue=None, employees=None)
    f["evidence"].pop("revenue_usd"), f["evidence"].pop("employees")
    llm = RoutingLLM([co("Mystery Co", "mystery.com")], {"Mystery Co": f}, all_urls("mystery.com"))
    s = go(cfg, conn, llm)
    assert s.queued == ["Mystery Co"] and not s.added and list_accounts(conn, status=None) == []
    assert conn.execute("SELECT status FROM candidates").fetchone()["status"] == "queued"


def test_below_floor_is_rejected_and_never_researched_twice(cfg, conn):
    llm = RoutingLLM([co("Tiny Co", "tiny.com")], {"Tiny Co": county_facts("Tiny Co", "tiny.com", revenue=1e7, employees=40)},
                     all_urls("tiny.com"))
    s = go(cfg, conn, llm)
    assert s.rejected == ["Tiny Co"] and list_accounts(conn, status=None) == []
    calls = len(llm.calls)
    s2 = go(cfg, conn, llm)
    assert s2.skipped_known == 1 and s2.researched == 0 and len(llm.calls) == calls + 1   # one more discovery call, no enrich


def test_county_must_be_one_of_the_six_and_evidenced(cfg, conn):
    llm = RoutingLLM([co("Rockford Co", "rockford.com")],
                     {"Rockford Co": county_facts("Rockford Co", "rockford.com", county="Winnebago County")}, all_urls("rockford.com"))
    s = go(cfg, conn, llm)
    assert s.queued == ["Rockford Co"] and list_accounts(conn, status=None) == []


def test_known_accounts_exclusions_and_domains_are_skipped_before_any_research(cfg, conn):
    add_account(conn, "Existing Co", domain="existing.com")
    conn.execute("INSERT INTO exclusions(name, domain, reason) VALUES ('Client Co', 'client.com', 'client')")
    conn.commit()
    llm = RoutingLLM([co("existing co", "other.com"), co("Brand New", "existing.com"), co("Client Co", "x.com"),
                      co("Fresh Inc", "client.com")], {}, all_urls("x.com"))
    s = go(cfg, conn, llm)
    assert s.skipped_known == 4 and s.researched == 0 and len(llm.calls) == 1


def test_discovery_source_must_be_retrieved_allowed_and_reachable(cfg, conn):
    cases = [co("A Co", "a.com", "https://never-returned.example/x"), co("B Co", "b.com", "https://www.linkedin.com/company/b"),
             co("C Co", "c.com", "https://dead.example/c")]
    llm = RoutingLLM(cases, {}, [LIST_PAGE, "https://www.linkedin.com/company/b", "https://dead.example/c"])
    s = go(cfg, conn, llm, bad={"https://dead.example/c": "404"})
    assert s.skipped_source == 3 and s.researched == 0


def test_auto_add_cap_overflow_goes_to_the_queue(cfg, conn):
    cs = [co(f"Co{i}", f"co{i}.com") for i in range(3)]
    llm = RoutingLLM(cs, {c["name"]: county_facts(c["name"], c["domain"]) for c in cs}, all_urls(*[c["domain"] for c in cs]))
    s = go(cfg, conn, llm, max_auto_add_per_run=2)
    assert len(s.added) == 2 and len(s.queued) == 1
    q = conn.execute("SELECT reason FROM candidates WHERE status='queued'").fetchone()["reason"]
    assert "auto-add cap" in q


def test_max_candidates_per_run_limits_research(cfg, conn):
    cs = [co(f"Co{i}", f"co{i}.com") for i in range(5)]
    llm = RoutingLLM(cs, {c["name"]: county_facts(c["name"], c["domain"]) for c in cs}, all_urls(*[c["domain"] for c in cs]))
    s = go(cfg, conn, llm, max_candidates_per_run=2, max_auto_add_per_run=10)
    assert s.researched == 2 and len(s.added) == 2


def test_unverified_domain_is_not_auto_added(cfg, conn):
    llm = RoutingLLM([co("Nodomain Co", "nodomain.com")], {"Nodomain Co": county_facts("Nodomain Co", "nodomain.com")},
                     all_urls("nodomain.com"))
    s = go(cfg, conn, llm, bad={"https://nodomain.com": "dns", "https://nodomain.com/about": "dns"})
    assert not s.added and s.queued == ["Nodomain Co"]


def test_budget_cap_stops_before_research(cfg, conn):
    llm = RoutingLLM([co("Acme Industrial", "acmeind.com")], {"Acme Industrial": county_facts("Acme Industrial", "acmeind.com")},
                     all_urls("acmeind.com"))
    cfg.pricing.models["m"] = type(cfg.pricing.models[next(iter(cfg.pricing.models))])(input=1e6, output=1e6, cache_write_5m=0, cache_read=0)
    v = FakeVerifier()
    s = run_discover(conn, cfg, Discoverer(cfg, llm, v), Enricher(cfg, llm, v, None), AS_OF, model="m",
                     segments=["x"], workers=1, max_cost=0.01)
    assert s.budget_hit and s.researched == 0


def _priced(cfg, per_call):
    """Price the fake model so that each 100-input/50-output-token call costs about `per_call` dollars."""
    cfg.pricing.models["m"] = type(cfg.pricing.models[next(iter(cfg.pricing.models))])(
        input=per_call / 150 * 1e6, output=per_call / 150 * 1e6, cache_write_5m=0, cache_read=0)


def test_segment_searches_stop_before_the_spending_limit_is_passed(cfg, conn):
    _priced(cfg, 0.40)
    llm = RoutingLLM([], {}, all_urls())
    v = FakeVerifier()
    s = run_discover(conn, cfg, Discoverer(cfg, llm, v), Enricher(cfg, llm, v, None), AS_OF, model="m",
                     segments=["a", "b", "c"], workers=1, max_cost=0.5)
    assert len(llm.calls) == 1 and s.segments_skipped == 2 and s.budget_hit        # one search ($0.40) fits; a second would pass $0.5
    assert 0.40 <= s.est_cost < 0.5                                                  # the one search that ran, web-search fee included


def test_running_searches_count_against_the_limit_even_when_they_start_together(cfg, conn):
    _priced(cfg, 0.40)
    llm = RoutingLLM([], {}, all_urls())
    v = FakeVerifier()
    s = run_discover(conn, cfg, Discoverer(cfg, llm, v), Enricher(cfg, llm, v, None), AS_OF, model="m",
                     segments=["a", "b", "c", "d"], workers=4, max_cost=0.5)
    assert len(llm.calls) == 1 and s.segments_skipped == 3                          # four workers must not launch four $0.40 searches at once


def test_with_room_under_the_limit_every_segment_is_searched(cfg, conn):
    _priced(cfg, 0.40)
    llm = RoutingLLM([], {}, all_urls())
    v = FakeVerifier()
    s = run_discover(conn, cfg, Discoverer(cfg, llm, v), Enricher(cfg, llm, v, None), AS_OF, model="m",
                     segments=["a", "b", "c"], workers=2, max_cost=10)
    assert len(llm.calls) == 3 and s.segments_skipped == 0 and not s.budget_hit
    llm2 = RoutingLLM([], {}, all_urls())
    s = run_discover(conn, cfg, Discoverer(cfg, llm2, v), Enricher(cfg, llm2, v, None), AS_OF, model="m",
                     segments=["a", "b"], workers=2, max_cost=None)             # no limit: nothing is held back
    assert len(llm2.calls) == 2 and s.segments_skipped == 0


# ---- review queue and scan gating ------------------------------------------------------------------------------------
def test_accept_and_reject_queued_candidates(cfg, conn):
    f = county_facts("Mystery Co", "mystery.com", revenue=None, employees=None)
    f["evidence"].pop("revenue_usd"), f["evidence"].pop("employees")
    llm = RoutingLLM([co("Mystery Co", "mystery.com"), co("Other Co", "other.com")],
                     {"Mystery Co": f, "Other Co": f | {"domain": "other.com"}}, all_urls("mystery.com", "other.com"))
    go(cfg, conn, llm)
    ids = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM candidates")}
    res = accept_candidate(conn, cfg, ids["Mystery Co"])
    assert res.ok
    a = list_accounts(conn, status=None)[0]
    assert (a.origin, a.reviewed, a.status) == ("discovered", 1, "active")      # size still unknown: scanned anyway, tagged unverified
    assert reject_candidate(conn, ids["Other Co"]) and not reject_candidate(conn, ids["Other Co"])
    assert not accept_candidate(conn, cfg, ids["Mystery Co"]).ok                      # already added


def test_unreviewed_accounts_are_scanned_only_up_to_the_limit_until_reviewed(cfg):
    accts = [Account(id=i, name=f"A{i}", reviewed=1 if i < 3 else 0) for i in range(1, 9)]
    cfg.discovery.scan_unreviewed_per_run = 2
    keep, held = select_scan_accounts(accts, cfg)
    assert [a.id for a in keep] == [1, 2, 3, 4] and held == 4


def test_mark_reviewed_is_audited(cfg, conn):
    llm = RoutingLLM([co("Acme Industrial", "acmeind.com")], {"Acme Industrial": county_facts("Acme Industrial", "acmeind.com")},
                     all_urls("acmeind.com"))
    go(cfg, conn, llm)
    assert mark_reviewed(conn) == 1 and mark_reviewed(conn) == 0
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE field='reviewed'").fetchone()[0] == 1


def test_migration_adds_discovery_columns_to_an_old_database(tmp_path):
    import sqlite3
    from bd.db import init_db
    p = tmp_path / "old.sqlite"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT NOT NULL, domain TEXT, hq_city TEXT, "
              "chicagoland TEXT NOT NULL DEFAULT 'unknown', revenue REAL, employees INTEGER, ownership TEXT, sponsor TEXT, "
              "industry TEXT, cik TEXT, tier TEXT NOT NULL DEFAULT 'A', status TEXT NOT NULL DEFAULT 'active', last_scanned TEXT)")
    c.execute("INSERT INTO accounts(name) VALUES ('Old Co')")
    c.row_factory = sqlite3.Row
    init_db(c)
    r = c.execute("SELECT origin, reviewed FROM accounts").fetchone()
    assert (r["origin"], r["reviewed"]) == ("seed", 1)


def test_a_segment_the_search_tool_could_not_search_is_an_error_not_an_empty_segment(cfg, conn):
    class Failing(RoutingLLM):
        def run(self, system, messages, **kw):
            r = super().run(system, messages, **kw)
            r.search_errors = ["too_many_requests"]
            return r
    out = go(cfg, conn, Failing([], {}, []), segments=("manufacturing",))
    assert out.errors == 1 and out.found == 0
    assert "search tool errors: too_many_requests" in conn.execute("SELECT errors FROM runs WHERE step = 'discover'").fetchone()[0]


# ---- competitors and the segment rotation ------------------------------------------------------------------------------------------------------------
def test_a_competitor_is_skipped_before_any_research_and_counted(cfg, conn):
    llm = RoutingLLM([co("RSM US LLP", "rsmus.com"), co("West Monroe Partners", "westmonroe.com"), co("Fresh Inc", "fresh.com")],
                     {"Fresh Inc": county_facts("Fresh Inc", "fresh.com")}, all_urls("fresh.com", "rsmus.com", "westmonroe.com"))
    s = go(cfg, conn, llm)
    assert s.skipped_competitor == 2 and s.researched == 1 and s.added == ["Fresh Inc"]
    assert len(llm.calls) == 2                                                           # one search, and one research call for the only company that is not a competitor
    assert not [c for c in llm.calls if "RSM" in c and "Company:" in c]


def test_queued_competitors_are_rejected_at_the_start_of_a_run_and_audited(cfg, conn):
    conn.execute("INSERT INTO candidates(name, domain, segment, source_url, found_date, status, reason) VALUES ('ZS','zs.com','x','https://a.example','2026-10-20','queued','size unverified')")
    conn.execute("INSERT INTO candidates(name, domain, segment, source_url, found_date, status) VALUES ('Kirkland & Ellis','kirkland.com','x','https://b.example','2026-10-20','queued')")
    conn.commit()
    s = go(cfg, conn, RoutingLLM([], {}, []))
    assert s.rejected_queued == ["ZS"]
    rows = {r["name"]: (r["status"], r["reason"]) for r in conn.execute("SELECT name, status, reason FROM candidates")}
    assert rows["ZS"][0] == "rejected" and "competitor of the firm" in rows["ZS"][1] and rows["Kirkland & Ellis"][0] == "queued"
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE actor = 'discover' AND field = 'candidate_rejected'").fetchone()[0] == 1


def test_the_discovery_prompt_tells_the_model_not_to_return_competing_firms(cfg):
    from importlib import resources
    text = resources.files("bd.prompts").joinpath("discover.md").read_text(encoding="utf-8")
    assert "compete with the client" in text and "consulting firms" in text


def test_a_limit_below_the_number_of_segments_rotates_through_all_of_them(cfg, conn):
    from datetime import date
    from bd.discover import note_segment, pick_segments
    segs = ["a", "b", "c", "d", "e"]
    assert pick_segments(conn, segs, 3) == ["a", "b", "c"]                                # nothing searched yet: configured order
    for day, s in ((1, "a"), (1, "b"), (1, "c")):
        note_segment(conn, s, date(2026, 10, day), 4)
    assert pick_segments(conn, segs, 3) == ["d", "e", "a"]                                # never searched first, then the oldest, ties in configured order
    note_segment(conn, "d", date(2026, 10, 8), 0)
    note_segment(conn, "e", date(2026, 10, 8), 0)
    note_segment(conn, "a", date(2026, 10, 8), 2)
    assert pick_segments(conn, segs, 3) == ["b", "c", "a"] and pick_segments(conn, segs, None) == ["b", "c", "a", "d", "e"]     # the oldest two, then the 8th by configured order
    assert pick_segments(conn, segs + ["new"], 2) == ["new", "b"]                         # a segment added in Settings is searched first
    row = conn.execute("SELECT searches, last_found, last_searched FROM discovery_segments WHERE segment = 'a'").fetchone()
    assert (row["searches"], row["last_found"], row["last_searched"]) == (2, 2, "2026-10-08")


def test_a_run_records_the_segments_it_searched_but_not_one_that_errored(cfg, conn):
    llm = RoutingLLM([co("Fresh Inc", "fresh.com")], {"Fresh Inc": county_facts("Fresh Inc", "fresh.com")}, all_urls("fresh.com"))
    go(cfg, conn, llm, segments=("manufacturing", "retail"))
    rows = {r["segment"]: r["last_found"] for r in conn.execute("SELECT segment, last_found FROM discovery_segments")}
    assert rows == {"manufacturing": 1, "retail": 1}                                      # last_found is what the search returned
    bad = RoutingLLM([], {}, [], fail_discover=True)
    bad.run = lambda *a, **k: (_ for _ in ()).throw(__import__("bd.llm", fromlist=["LLMError"]).LLMError("boom", Usage()))
    s = go(cfg, conn, bad, segments=("energy",))
    assert s.errors == 1 and conn.execute("SELECT COUNT(*) FROM discovery_segments WHERE segment = 'energy'").fetchone()[0] == 0


def test_the_log_says_when_each_segment_was_last_searched(cfg, conn):
    lines: list[str] = []
    v = FakeVerifier(None)
    llm = RoutingLLM([], {}, [])
    run_discover(conn, cfg, Discoverer(cfg, llm, v), Enricher(cfg, llm, v, None), AS_OF, model=None, segments=["manufacturing"], workers=1, log=lines.append)
    run_discover(conn, cfg, Discoverer(cfg, llm, v), Enricher(cfg, llm, v, None), AS_OF, model=None, segments=["manufacturing"], workers=1, log=lines.append)
    seg = [x for x in lines if "segment 'manufacturing'" in x]
    assert "not searched before" in seg[0] and f"last searched {AS_OF.isoformat()}" in seg[1]
