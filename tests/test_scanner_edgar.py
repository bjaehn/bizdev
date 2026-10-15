import json
from datetime import date

import pytest

from bd.edgar import EdgarError, Filing
from bd.scanners import SCANNERS, EdgarScanner
from tests.conftest import AS_OF, FakeLLM, account

F1 = Filing("0000001234", "0000000001-26-000001", date(2026, 9, 24), date(2026, 9, 22), "8-K", ("5.02",), "d1.htm")
F2 = Filing("0000001234", "0000000001-26-000002", date(2026, 9, 1), None, "8-K", ("5.02",), "d2.htm")

RELEVANT = json.dumps({"relevant": True, "type": "leadership_change", "summary": "Acme CIO Pat Lee resigned.",
                       "role_or_person": "Pat Lee, CIO", "service_lines": ["ai & technology"]})
NOT_RELEVANT = json.dumps({"relevant": False})


class FakeEdgar:
    def __init__(self, filings=(), text="Item 5.02 text", list_error=None, text_error=None):
        self.filings, self.text, self.list_error, self.text_error = list(filings), text, list_error, text_error
        self.calls = []

    def recent_item_502(self, cik, since, until, limit):
        self.calls.append(("list", cik, since, until, limit))
        if self.list_error:
            raise self.list_error
        return self.filings

    def filing_text(self, f):
        self.calls.append(("text", f.accession))
        if self.text_error:
            raise self.text_error
        return self.text


def acct(cik="0000001234"):
    a = account()
    return a.model_copy(update={"cik": cik})


def scan(cfg, replies, edgar=None, cik="0000001234"):
    llm = FakeLLM(replies)
    sc = EdgarScanner(cfg, llm, edgar or FakeEdgar([F1]))
    return sc.scan(acct(cik), AS_OF), llm, sc


def test_registered_and_applies_only_with_a_cik(cfg):
    assert SCANNERS["edgar"] is EdgarScanner and EdgarScanner.implemented
    sc = EdgarScanner(cfg, None, None)
    assert sc.applies_to(acct()) and not sc.applies_to(acct(cik=None))


def test_create_wires_the_text_llm_and_edgar(cfg):
    sc = EdgarScanner.create(cfg, "WEB", "TEXT", "VERIFIER", "EDGAR")
    assert sc.llm == "TEXT" and sc.edgar == "EDGAR"


def test_relevant_filing_becomes_a_high_confidence_sec_signal_with_edgar_supplied_facts(cfg):
    out, llm, _ = scan(cfg, [RELEVANT])
    assert out.error is None and out.candidates == 1 and len(out.signals) == 1
    e = out.signals[0].extract
    assert (e.type, e.source_type, e.confidence) == ("leadership_change", "sec_filing", "High")
    assert e.event_date == date(2026, 9, 22)                      # EDGAR's reportDate, not the model's say-so
    assert e.source_url == F1.url and e.source_url.startswith("https://www.sec.gov/")
    assert e.service_lines == ["AI & Technology"] and e.role_or_person == "Pat Lee, CIO"
    assert out.signals[0].family == "leadership" and "0000000001-26-000001" in out.signals[0].notes[0]
    assert out.usage.total_tokens == 150


def test_prompt_sends_only_the_filing_excerpt_and_company(cfg):
    out, llm, _ = scan(cfg, [RELEVANT], FakeEdgar([F1], text="Item 5.02 Pat Lee resigned."))
    system, msgs = llm.calls[0]
    payload = json.loads(msgs[0]["content"])
    assert payload == {"company": "Acme Corp", "form": "8-K", "filing_date": "2026-09-24",
                       "report_date": "2026-09-22", "text": "Item 5.02 Pat Lee resigned."}
    assert "no tools" in system.lower() and "director" in system.lower()


def test_director_only_filing_is_not_a_signal(cfg):
    out, *_ = scan(cfg, [NOT_RELEVANT])
    assert out.signals == [] and out.rejections[0].reason == "not_officer_change" and out.error is None


def test_window_passed_to_edgar_matches_config(cfg):
    edgar = FakeEdgar([])
    scan(cfg, [], edgar)
    _, cik, since, until, limit = edgar.calls[0]
    assert (cik, since, until, limit) == ("0000001234", date(2026, 4, 18), date(2026, 11, 14), cfg.edgar.max_filings_per_scan)


def test_no_cik_makes_no_calls_and_no_error(cfg):
    edgar = FakeEdgar([F1])
    out, llm, _ = scan(cfg, [], edgar, cik=None)
    assert out.signals == [] and out.error is None and edgar.calls == [] and llm.calls == []


def test_edgar_listing_failure_is_an_error_with_nothing_recorded(cfg):
    out, llm, _ = scan(cfg, [], FakeEdgar(list_error=EdgarError("HTTP 403")))
    assert out.signals == [] and out.error == "EdgarError: HTTP 403" and llm.calls == []


def test_unfetchable_filing_is_a_rejection_not_a_signal(cfg):
    out, llm, _ = scan(cfg, [], FakeEdgar([F1], text_error=EdgarError("HTTP 404")))
    assert out.signals == [] and out.rejections[0].reason == "unreachable:HTTP 404" and llm.calls == []


def test_two_filings_one_failing_llm_call_keeps_the_good_signal_and_reports_error(cfg):
    llm = FakeLLM([RELEVANT, "garbage", "garbage"])
    out = EdgarScanner(cfg, llm, FakeEdgar([F1, F2])).scan(acct(), AS_OF)
    assert len(out.signals) == 1 and out.error and "InvalidJSON" in out.error
    assert out.usage.total_tokens == 150 * 3          # all three calls' spend recorded


def test_same_event_in_two_filings_collapses(cfg):
    same = Filing("0000001234", "0000000001-26-000009", date(2026, 9, 25), date(2026, 9, 22), "8-K/A", ("5.02",), "d9.htm")
    llm = FakeLLM([RELEVANT, RELEVANT])
    out = EdgarScanner(cfg, llm, FakeEdgar([F1, same])).scan(acct(), AS_OF)
    assert len(out.signals) == 1 and out.rejections[-1].reason == "duplicate_in_batch"


def test_relevant_without_summary_fails_validation_then_retries(cfg):
    bad = json.dumps({"relevant": True, "type": "leadership_change"})
    out, llm, _ = scan(cfg, [bad, RELEVANT])
    assert len(out.signals) == 1 and len(llm.calls) == 2


def test_runner_skips_edgar_for_accounts_without_cik(cfg, tmp_path):
    from bd.runner import run_scans
    from tests.conftest import make_db
    conn = make_db(tmp_path)
    conn.execute("INSERT INTO accounts(name, domain, status) VALUES ('NoCik','nocik.com','active')")
    conn.execute("INSERT INTO accounts(name, domain, status, cik) VALUES ('HasCik','hascik.com','active','0000001234')")
    conn.commit()
    from bd.accounts import list_accounts
    edgar = FakeEdgar([])
    sc = EdgarScanner(cfg, FakeLLM([]), edgar)
    s = run_scans(conn, cfg, [sc], list_accounts(conn), AS_OF, "claude-sonnet-5-5")
    assert [c[1] for c in edgar.calls if c[0] == "list"] == ["0000001234"]       # NoCik was never called
    assert len(s.accounts) == 1
    steps = conn.execute("SELECT account_id FROM runs WHERE step='scan:edgar'").fetchall()
    assert len(steps) == 1
