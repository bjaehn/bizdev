import json
from datetime import date

import pytest

from bd.db import connect, init_db
from bd.hashing import signal_hash
from bd.scanners import LeadershipScanner, store_signals
from tests.conftest import AS_OF, FakeLLM, FakeVerifier, account

URL = "https://acme.com/news/cio"


def sig(**over):
    base = {"type": "leadership_change", "event_date": "2026-09-20", "summary": "Acme names Pat Lee as CIO.",
            "source_url": URL, "source_type": "company_release", "confidence": "High",
            "service_lines": ["AI & Technology"], "role_or_person": "Pat Lee, CIO"}
    base.update(over)
    return base


def run(cfg, signals, urls=(URL,), bad=None, acct=None, replies=None):
    llm = FakeLLM(replies or [json.dumps({"signals": signals})], urls=urls)
    ver = FakeVerifier(bad)
    out = LeadershipScanner(cfg, llm, ver).scan(acct or account(), AS_OF)
    return out, llm, ver


def test_happy_path(cfg):
    out, llm, ver = run(cfg, [sig()])
    assert out.error is None and out.candidates == 1 and not out.rejections
    s = out.signals[0]
    assert s.family == "leadership" and s.extract.confidence == "High"
    assert s.extract.event_date == date(2026, 9, 20)       # from the source, not the run date
    assert s.hash == signal_hash(1, "leadership_change", date(2026, 9, 20), URL)
    assert ver.checked == [URL]
    assert out.usage.total_tokens == 150


def test_prompt_is_static_system_plus_account_specific_user(cfg):
    out, llm, _ = run(cfg, [])
    system, msgs = llm.calls[0]
    assert "Acme" not in system and "leadership_change" in system and "LinkedIn" in system
    user = msgs[0]["content"]
    assert "Company: Acme Corp" in user and "acme.com" in user
    assert "2026-04-18 through 2026-10-15" in user  # 180-day window from config


def test_empty_result_records_nothing(cfg):
    out, *_ = run(cfg, [])
    assert out.signals == [] and out.rejections == [] and out.error is None


def test_url_not_in_search_results_is_fabricated(cfg):
    out, _, ver = run(cfg, [sig(source_url="https://acme.com/invented")], urls=(URL,))
    assert out.signals == [] and out.rejections[0].reason == "url_not_retrieved"
    assert ver.checked == []  # never even fetched


def test_url_match_ignores_www_fragment_trailing_slash(cfg):
    out, *_ = run(cfg, [sig(source_url="https://www.acme.com/news/cio/#top")], urls=("acme.com/news/cio",))
    assert len(out.signals) == 1


def test_unreachable_url_rejected(cfg):
    out, *_ = run(cfg, [sig()], bad={URL: "http_404"})
    assert out.signals == [] and out.rejections[0].reason == "unreachable:http_404"


def test_blocked_domain_rejected_before_anything_else(cfg):
    u = "https://www.linkedin.com/posts/acme-cio"
    out, _, ver = run(cfg, [sig(source_url=u)], urls=(u,))
    assert out.rejections[0].reason == "blocked_domain" and ver.checked == []
    u = "https://www.wsj.com/articles/acme"
    out, *_ = run(cfg, [sig(source_url=u)], urls=(u,))
    assert out.rejections[0].reason == "blocked_domain"


@pytest.mark.parametrize("d,ok", [
    ("2026-04-18", True), ("2026-04-17", False),      # window edge: as_of - 180d
    ("2026-10-15", True), ("2026-11-14", True),       # today / +30d
    ("2026-11-15", False),                            # beyond max_future_days
    ("2025-01-01", False),
])
def test_event_date_window(cfg, d, ok):
    out, *_ = run(cfg, [sig(event_date=d)])
    assert bool(out.signals) is ok
    if not ok:
        assert out.rejections[0].reason == "date_out_of_window"


def test_type_belonging_to_other_scanner_rejected(cfg):
    out, *_ = run(cfg, [sig(type="restructuring_layoffs")])
    assert out.rejections[0].reason == "type_not_allowed"


def test_company_site_on_foreign_domain_downgraded_to_secondary_medium(cfg):
    u = "https://trade-news.example/acme-cio"
    out, *_ = run(cfg, [sig(source_url=u, source_type="company_site", confidence="High")], urls=(u,))
    s = out.signals[0].extract
    assert s.source_type == "secondary_report" and s.confidence == "Medium"
    assert any("source_type" in n for n in out.signals[0].notes)


def test_company_release_on_wire_service_stays_high(cfg):
    u = "https://www.prnewswire.com/news-releases/acme-names-cio.html"
    out, *_ = run(cfg, [sig(source_url=u)], urls=(u,))
    assert out.signals[0].extract.confidence == "High"


def test_account_without_domain_cannot_earn_company_site_high(cfg):
    out, *_ = run(cfg, [sig(source_type="company_site")], acct=account(domain=None))
    assert out.signals[0].extract.source_type == "secondary_report"
    assert out.signals[0].extract.confidence == "Medium"


def test_sec_filing_must_be_on_sec_gov(cfg):
    u = "https://sec.gov/Archives/edgar/data/1/0001-8k.htm"
    out, *_ = run(cfg, [sig(source_url=u, source_type="sec_filing")], urls=(u,))
    assert out.signals[0].extract.source_type == "sec_filing" and out.signals[0].extract.confidence == "High"
    out, *_ = run(cfg, [sig(source_type="sec_filing")])  # acme.com is not sec.gov
    assert out.signals[0].extract.source_type == "secondary_report"


def test_model_confidence_can_be_lowered_never_raised(cfg):
    u = "https://news.example/acme"
    out, *_ = run(cfg, [sig(source_url=u, source_type="secondary_report", confidence="High")], urls=(u,))
    assert out.signals[0].extract.confidence == "Medium"          # capped by source type
    out, *_ = run(cfg, [sig(confidence="Low")])
    assert out.signals[0].extract.confidence == "Low"             # model's lower claim kept
    out, *_ = run(cfg, [sig(source_type="rumor", confidence="High", source_url=u)], urls=(u,))
    assert out.signals[0].extract.confidence == "Low"


def test_service_lines_canonicalised_and_unknown_dropped(cfg):
    out, *_ = run(cfg, [sig(service_lines=["ai & technology", "Accounting", "AI & Technology"])])
    assert out.signals[0].extract.service_lines == ["AI & Technology"]


def test_duplicates_in_batch_collapse(cfg):
    out, *_ = run(cfg, [sig(), sig(summary="Duplicate of the same event.")])
    assert len(out.signals) == 1
    assert [r.reason for r in out.rejections] == ["duplicate_in_batch"]


def test_invalid_json_twice_records_nothing_and_reports_error(cfg):
    out, llm, _ = run(cfg, [], replies=["nope", "still nope"])
    assert out.signals == [] and out.error.startswith("InvalidJSON") and len(llm.calls) == 2
    assert out.usage.total_tokens == 300


def test_llm_failure_is_captured_not_raised(cfg):
    from bd.llm import LLMRefusal

    class Boom:
        def run(self, *a, **kw):
            raise LLMRefusal("model refused (category=None)")
    out = LeadershipScanner(cfg, Boom(), FakeVerifier()).scan(account(), AS_OF)
    assert out.signals == [] and "LLMRefusal" in out.error


def test_tokens_spent_before_a_failure_are_still_logged(cfg):
    from bd.llm import LLMError, LLMResult, Usage

    class FailsOnRetry(FakeLLM):
        def run(self, system, messages, **kw):
            if self.calls:
                self.calls.append(1)
                raise LLMError("api down", Usage(input_tokens=7, output_tokens=3))
            return super().run(system, messages, **kw)
    llm = FailsOnRetry(["garbage"])
    out = LeadershipScanner(cfg, llm, FakeVerifier()).scan(account(), AS_OF)
    assert "api down" in out.error and out.usage.total_tokens == 150 + 10


# ---- storage --------------------------------------------------------------------------

@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.sqlite")
    init_db(c)
    c.execute("INSERT INTO accounts(id, name, domain) VALUES (1, 'Acme Corp', 'acme.com')")
    c.commit()
    yield c
    c.close()


def test_store_keeps_source_event_date_and_run_detected_date_and_dedupes(cfg, conn):
    out, *_ = run(cfg, [sig(), sig(event_date="2026-08-01", source_url="https://acme.com/news/other")],
                  urls=(URL, "https://acme.com/news/other"))
    assert store_signals(conn, 1, out.signals, AS_OF) == (2, 0)
    assert store_signals(conn, 1, out.signals, date(2026, 10, 22)) == (0, 2)  # next week: same hashes
    rows = conn.execute("SELECT event_date, detected_date, family, confidence, service_lines FROM signals "
                        "ORDER BY event_date").fetchall()
    assert [r["event_date"] for r in rows] == ["2026-08-01", "2026-09-20"]
    assert {r["detected_date"] for r in rows} == {"2026-10-15"}
    assert rows[1]["family"] == "leadership" and json.loads(rows[1]["service_lines"]) == ["AI & Technology"]


def test_same_event_on_same_domain_different_page_is_deduped(cfg, conn):
    a, *_ = run(cfg, [sig()])
    b, *_ = run(cfg, [sig(source_url="https://acme.com/other/page")], urls=("https://acme.com/other/page",))
    store_signals(conn, 1, a.signals, AS_OF)
    assert store_signals(conn, 1, b.signals, AS_OF) == (0, 1)


def test_low_confidence_is_stored(cfg, conn):
    u = "https://gossip.example/acme"
    out, *_ = run(cfg, [sig(source_type="rumor", confidence="Low", source_url=u)], urls=(u,))
    assert store_signals(conn, 1, out.signals, AS_OF) == (1, 0)
    assert conn.execute("SELECT confidence FROM signals").fetchone()[0] == "Low"
