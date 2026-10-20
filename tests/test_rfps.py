import json
from datetime import date, timedelta

import pytest
from typer.testing import CliRunner

from bd.cli import app
from bd.rfp_store import RfpExtract, list_open, store_rfps
from bd.rfps import RfpFinder, run_rfps
from tests.conftest import AS_OF, FakeLLM, FakeVerifier, make_db
from tests.test_update import app_env, cli_env  # noqa: F401  (fixtures)

runner = CliRunner()
URL = "https://www.chicago.gov/city/en/depts/dps/supp_info/rfp/it-strategy.html"


def d(n):
    return (AS_OF + timedelta(days=n)).isoformat()


def rfp(**over):
    base = {"title": "IT Strategy and Roadmap Services", "agency": "Department of Technology and Innovation", "due_date": d(20),
            "summary": "City seeks a consultant to assess its technology portfolio and prepare a five-year roadmap.",
            "source_url": URL, "service_lines": ["ai & technology", "Bogus Line"]}
    base.update(over)
    return base


@pytest.fixture
def group(cfg):
    return cfg.rfps.groups[0]


def find(cfg, group, items, urls=(URL,), verifier=None):
    llm = FakeLLM([json.dumps({"rfps": items})], urls=urls)
    return RfpFinder(cfg, llm, verifier or FakeVerifier()).find(group, AS_OF), llm


def test_an_official_open_rfp_is_kept_with_canonical_service_lines(cfg, group):
    out, llm = find(cfg, group, [rfp()])
    assert out.error is None and len(out.rfps) == 1 and out.rejections == []
    r = out.rfps[0]
    assert r.due_date == AS_OF + timedelta(days=20) and r.service_lines == ["AI & Technology"]
    assert "Agency group: City of Chicago" in llm.calls[0][1][0]["content"] and "chicago.gov" in llm.calls[0][1][0]["content"]


def test_search_is_limited_to_the_groups_own_domains(cfg, group):
    llm = FakeLLM([json.dumps({"rfps": []})])
    seen = {}
    orig = llm.run

    def spy(system, messages, **kw):
        seen.update(kw)
        return orig(system, messages)
    llm.run = spy
    RfpFinder(cfg, llm, FakeVerifier()).find(group, AS_OF)
    assert seen == {"allowed_domains": ["chicago.gov"], "max_uses": cfg.rfps.max_searches}


@pytest.mark.parametrize("over,urls,reason", [
    ({"source_url": "https://news.example.com/rfp"}, ("https://news.example.com/rfp",), "not_an_agency_page"),
    ({"due_date": d(-1)}, (URL,), "closed"),
    ({"due_date": d(900)}, (URL,), "due_date_implausible"),
    ({}, (), "url_not_retrieved"),
])
def test_rejections(cfg, group, over, urls, reason):
    out, _ = find(cfg, group, [rfp(**over)], urls=urls)
    assert not out.rfps and out.rejections[0][0].startswith(reason)


def test_unreachable_pages_and_duplicates_are_dropped(cfg, group):
    class Down(FakeVerifier):
        def verify(self, url):
            from bd.verify import VerifyResult
            return VerifyResult(False, "http_404")
    out, _ = find(cfg, group, [rfp()], verifier=Down())
    assert not out.rfps and out.rejections[0][0] == "unreachable:http_404"
    out, _ = find(cfg, group, [rfp(), rfp(title="Same page again")])
    assert len(out.rfps) == 1 and out.rejections[0][0] == "duplicate_in_batch"


def test_a_missing_due_date_or_long_summary_is_invalid_json_not_a_record(cfg, group):
    bad = rfp(summary="word " * 40)
    llm = FakeLLM([json.dumps({"rfps": [bad]}), json.dumps({"rfps": [bad]})], urls=(URL,))
    out = RfpFinder(cfg, llm, FakeVerifier()).find(group, AS_OF)
    assert out.error and not out.rfps


def test_store_dedupes_by_url_and_refreshes_the_deadline(cfg, tmp_path):
    conn = make_db(tmp_path)
    r = RfpExtract(**rfp())
    assert store_rfps(conn, "City of Chicago", [r], AS_OF) == (1, 0)
    later = r.model_copy(update={"due_date": AS_OF + timedelta(days=40)})
    assert store_rfps(conn, "City of Chicago", [later], AS_OF + timedelta(days=3)) == (0, 1)
    row = conn.execute("SELECT * FROM solicitations").fetchone()
    assert row["due_date"] == d(40) and row["first_seen"] == AS_OF.isoformat() and row["last_seen"] == d(3)


def test_list_open_hides_closed_filters_and_sorts_by_due_date(cfg, tmp_path):
    conn = make_db(tmp_path)
    store_rfps(conn, "A", [RfpExtract(**rfp(source_url="https://www.chicago.gov/a", due_date=d(30), service_lines=["Data & Analytics"])),
                           RfpExtract(**rfp(source_url="https://www.chicago.gov/b", due_date=d(5), service_lines=["AI & Technology"]))], AS_OF)
    store_rfps(conn, "B", [RfpExtract(**rfp(source_url="https://www.illinois.gov/c", due_date=d(-3)))], AS_OF)
    urls = lambda **k: [r["url"].rsplit("/", 1)[1] for r in list_open(conn, AS_OF, **k)]  # noqa: E731
    assert urls() == ["b", "a"] and urls(include_closed=True) == ["c", "b", "a"]
    assert urls(service_line="Data & Analytics") == ["a"] and urls(grp="B", include_closed=True) == ["c"]


def test_run_rfps_logs_each_group_and_respects_the_limit(cfg, tmp_path):
    conn = make_db(tmp_path)
    groups = cfg.rfps.groups[:3]
    llm = FakeLLM([json.dumps({"rfps": [rfp()]}), json.dumps({"rfps": []}), json.dumps({"rfps": []})], urls=(URL,))
    s = run_rfps(conn, cfg, RfpFinder(cfg, llm, FakeVerifier()), AS_OF, "claude-sonnet-5-5", groups, max_cost=5)
    assert (s.groups, s.new, s.errors, s.budget_hit) == (3, 1, 0, False)
    assert conn.execute("SELECT COUNT(*) FROM runs WHERE step = 'rfps'").fetchone()[0] == 3
    llm2 = FakeLLM([json.dumps({"rfps": []})] * 6, urls=(URL,))
    s2 = run_rfps(conn, cfg, RfpFinder(cfg, llm2, FakeVerifier()), AS_OF, "claude-sonnet-5-5", cfg.rfps.groups, max_cost=0.0000001)
    assert s2.groups == 1 and s2.budget_hit                          # one call is always let through, then the limit holds


def test_cli_dry_run_lists_groups_without_a_key(cli_env):
    r = runner.invoke(app, ["rfps", "--dry-run", "--limit", "2"])
    assert r.exit_code == 0 and "2 agency group(s)" in r.output and "would search: City of Chicago (chicago.gov)" in r.output


def test_jobs_and_the_page(app_env):  # noqa: F811
    from bd.jobs import JobError, validate
    assert validate("rfps", {"limit": "6"}) == ["rfps", "--limit", "6"]
    for bad in ({}, {"limit": "0"}, {"limit": "13"}):
        with pytest.raises(JobError):
            validate("rfps", bad)
    e = app_env
    assert "No open RFPs recorded yet" in e.get("/rfps").body
    store_rfps(e.conn, "City of Chicago", [RfpExtract(**rfp(due_date=(date.today() + timedelta(days=9)).isoformat()))], date.today())
    body = e.get("/rfps").body
    assert "IT Strategy and Roadmap Services" in body and "chicago.gov" in body and "9 days" in body and "AI &amp; Technology" in body
    assert "0 shown of 1 recorded" in e.get("/rfps?service_line=Data+%26+Analytics").body
    assert "Open RFPs" in e.get("/operate").body


def test_the_model_note_explains_an_empty_list_and_reaches_the_log_and_runs_table(cfg, tmp_path):
    conn = make_db(tmp_path)
    reply = json.dumps({"rfps": [], "notes": "Results were news pages; no procurement listings found."})
    llm = FakeLLM([json.dumps({"rfps": [rfp()], "notes": "found 1"}), reply], urls=(URL,))
    lines = []
    s = run_rfps(conn, cfg, RfpFinder(cfg, llm, FakeVerifier()), AS_OF, "claude-sonnet-5-5", cfg.rfps.groups[:2], max_cost=5, log=lines.append)
    assert s.new == 1 and any("model note: Results were news pages" in l for l in lines)
    rows = [r["errors"] for r in conn.execute("SELECT errors FROM runs WHERE step = 'rfps' ORDER BY id")]
    assert rows == [None, "model note: Results were news pages; no procurement listings found."]       # a note is kept only when nothing was found
    assert "notes" in __import__("importlib.resources", fromlist=["files"]).files("bd.prompts").joinpath("rfps.md").read_text()
    llm2 = FakeLLM([json.dumps({"rfps": []})], urls=(URL,))                                           # the note is optional
    assert RfpFinder(cfg, llm2, FakeVerifier()).find(cfg.rfps.groups[0], AS_OF).note is None


def test_a_group_the_search_tool_could_not_search_is_an_error_not_no_rfps(cfg, group):
    llm = FakeLLM([json.dumps({"rfps": []})], urls=(), search_errors=["unavailable"])
    out = RfpFinder(cfg, llm, FakeVerifier()).find(group, AS_OF)
    assert out.error and "search tool errors: unavailable" in out.error and out.rfps == []
    ok = RfpFinder(cfg, FakeLLM([json.dumps({"rfps": []})], urls=(), search_errors=["max_uses_exceeded"]), FakeVerifier()).find(group, AS_OF)
    assert ok.error is None
