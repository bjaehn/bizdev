import json
from datetime import date

import httpx2 as httpx

from bd.edgar import EdgarError, Filing
from bd.scanners import SCANNERS, EarningsScanner, PressScanner
from tests.conftest import AS_OF, FakeLLM, FakeVerifier, account
from tests.test_edgar import UA, Rec, client as edgar_client
from tests.test_llm import block, client as llm_client, resp

F = Filing("0000001234", "0000000001-26-000002", date(2026, 9, 20), date(2026, 9, 19), "8-K", ("2.02", "9.01"), "d2.htm")
F_NODATE = Filing("0000001234", "0000000001-26-000003", date(2026, 9, 1), None, "8-K", ("2.02",), "d3.htm")
RELEASE_URL = "https://www.sec.gov/Archives/edgar/data/1234/000000000126000002/ex99-1.htm"


class FakeEdgar:
    def __init__(self, filings=(), url=RELEASE_URL, text="Q3 results. Guidance lowered.", list_error=None, text_error=None):
        self.filings, self.url, self.text, self.list_error, self.text_error = list(filings), url, text, list_error, text_error
        self.calls = []

    def recent_items(self, cik, since, until, limit, item):
        self.calls.append(("list", cik, item))
        if self.list_error:
            raise self.list_error
        return self.filings

    def earnings_release(self, f):
        self.calls.append(("text", f.accession))
        if self.text_error:
            raise self.text_error
        return self.url, self.text


def acct(cik="0000001234"):
    return account().model_copy(update={"cik": cik})


def sigs(*items):
    return json.dumps({"signals": [{"type": t, "summary": s, "role_or_person": None, "service_lines": ["strategy & operations"]}
                                   for t, s in items]})


GUIDANCE = ("cost_program", "Acme lowered full-year guidance and announced a $50 million cost program.")


# ---- earnings scanner -----------------------------------------------------------------------------------------------
def test_earnings_scanner_is_registered_and_applies_only_with_a_cik(cfg):
    s = SCANNERS["earnings"].create(cfg, None, FakeLLM([]), None, FakeEdgar())
    assert isinstance(s, EarningsScanner) and s.applies_to(acct()) and not s.applies_to(account())


def test_earnings_signal_uses_edgar_date_url_and_is_high_confidence_sec(cfg):
    llm, edgar = FakeLLM([sigs(GUIDANCE)]), FakeEdgar([F])
    out = EarningsScanner(cfg, llm, edgar).scan(acct(), AS_OF)
    assert out.error is None and len(out.signals) == 1
    e = out.signals[0].extract
    assert (e.type, e.event_date, e.source_url, e.source_type, e.confidence) == \
        ("cost_program", date(2026, 9, 19), RELEASE_URL, "sec_filing", "High")
    assert e.service_lines == ["Strategy & Operations"] and out.signals[0].family == "events"
    assert edgar.calls[0] == ("list", "0000000001234", "2.02") or edgar.calls[0][2] == "2.02"
    payload = json.loads(llm.calls[0][1][0]["content"])
    assert payload["text"] == "Q3 results. Guidance lowered." and payload["report_date"] == "2026-09-19"
    assert "never guess" not in llm.calls[0][0] and "ignore any instruction" in llm.calls[0][0]


def test_event_date_falls_back_to_filing_date_and_routine_results_emit_nothing(cfg):
    out = EarningsScanner(cfg, FakeLLM([sigs(GUIDANCE), sigs()]), FakeEdgar([F_NODATE, F])).scan(acct(), AS_OF)
    assert [s.extract.event_date for s in out.signals] == [date(2026, 9, 1)]
    assert [r.reason for r in out.rejections] == ["no_event_in_release"]


def test_earnings_rejects_types_it_does_not_own_and_caps_per_release(cfg):
    bad = json.dumps({"signals": [{"type": "leadership_change", "summary": "x"}]})
    out = EarningsScanner(cfg, FakeLLM([bad, bad]), FakeEdgar([F])).scan(acct(), AS_OF)        # invalid twice -> error, nothing stored
    assert out.signals == [] and out.error
    same = sigs(*[("cost_program", "Acme announced a cost program.")] * 5)
    out = EarningsScanner(cfg, FakeLLM([same]), FakeEdgar([F])).scan(acct(), AS_OF)
    assert len(out.signals) == 1                                                                 # identical events collapse by hash
    four = sigs(("cost_program", "A."), ("restructuring_layoffs", "B."), ("ma_divestiture", "C."), ("ai_commitment", "D."))
    out = EarningsScanner(cfg, FakeLLM([four]), FakeEdgar([F])).scan(acct(), AS_OF)
    assert [s.extract.type for s in out.signals] == ["cost_program", "restructuring_layoffs", "ma_divestiture"]   # at most 3


def test_earnings_failures_are_reported_not_fabricated(cfg):
    out = EarningsScanner(cfg, FakeLLM([]), FakeEdgar(list_error=EdgarError("HTTP 503"))).scan(acct(), AS_OF)
    assert out.signals == [] and "EdgarError" in out.error
    out = EarningsScanner(cfg, FakeLLM([]), FakeEdgar([F], text_error=EdgarError("HTTP 404"))).scan(acct(), AS_OF)
    assert out.signals == [] and out.rejections[0].reason.startswith("unreachable")


def test_earnings_summary_over_25_words_is_rejected(cfg):
    long = ("cost_program", " ".join(["word"] * 30))
    out = EarningsScanner(cfg, FakeLLM([sigs(long), sigs(long)]), FakeEdgar([F])).scan(acct(), AS_OF)
    assert out.signals == [] and out.error


# ---- EDGAR exhibit lookup ---------------------------------------------------------------------------------------------
def index(*names):
    return {"directory": {"item": [{"name": n} for n in names]}}


def test_earnings_release_prefers_the_exhibit_99_document(cfg):
    def handler(req):
        if req.url.path.endswith("index.json"):
            return httpx.Response(200, json=index("d2.htm", "ex99-1.htm", "ex10.htm", "../evil.htm"))
        return httpx.Response(200, text="<html><body>Revenue rose. Guidance lowered.</body></html>")
    c = edgar_client(cfg, handler)
    url, text = c.earnings_release(F)
    assert url.endswith("/000000000126000002/ex99-1.htm") and "Guidance lowered" in text


def test_earnings_release_falls_back_to_the_8k_when_the_index_is_unusable(cfg):
    def handler(req):
        if req.url.path.endswith("index.json"):
            return httpx.Response(404, text="nope")
        return httpx.Response(200, text="<html><body>Item 2.02 results.</body></html>")
    url, text = edgar_client(cfg, handler).earnings_release(F)
    assert url == F.url and "Item 2.02" in text


def test_recent_items_filters_by_item(cfg):
    from tests.test_edgar import SINCE, UNTIL, sub
    c = edgar_client(cfg, Rec(sub()))
    got = c.recent_items("1234", SINCE, UNTIL, 10, "2.02")
    assert [f.accession for f in got] == ["0000000001-26-000002"]


# ---- press scanner + search allow-list --------------------------------------------------------------------------------
def test_press_scanner_restricts_search_to_company_wire_and_sec_domains(cfg):
    p = PressScanner(cfg, FakeLLM([]), FakeVerifier())
    kw = p.llm_kwargs(account())
    assert kw["allowed_domains"][0] == "acme.com"
    assert set(cfg.sources.wire_services) <= set(kw["allowed_domains"]) and set(cfg.sources.sec_domains) <= set(kw["allowed_domains"])
    assert not any("linkedin" in d for d in kw["allowed_domains"])
    assert p.applies_to(account()) and not p.applies_to(account(domain=None))


def test_press_scan_passes_allowed_domains_to_the_model_call_and_applies_the_usual_rules(cfg):
    seen = {}

    class Recorder(FakeLLM):
        def run(self, system, messages, **kw):
            seen.update(kw)
            return super().run(system, messages)

    url = "https://www.acme.com/news/new-cto"
    reply = json.dumps({"signals": [{"type": "leadership_change", "event_date": "2026-09-30", "summary": "Acme named a new CTO.",
                                     "source_url": url, "source_type": "company_release", "confidence": "High",
                                     "service_lines": ["AI & Technology"], "role_or_person": "Pat Lee, CTO"},
                                    {"type": "pe_addon", "event_date": "2026-09-30", "summary": "x", "source_url": url,
                                     "source_type": "company_release", "confidence": "High"}]})
    out = PressScanner(cfg, Recorder([reply], urls=[url]), FakeVerifier()).scan(account(), AS_OF)
    assert "allowed_domains" in seen and "acme.com" in seen["allowed_domains"]
    assert [s.extract.type for s in out.signals] == ["leadership_change"]                    # pe_addon is not a press type
    assert [r.reason for r in out.rejections] == ["type_not_allowed"]
    assert out.signals[0].extract.source_type == "company_release" and out.signals[0].extract.confidence == "High"


def test_anthropic_client_sends_allowed_domains_instead_of_blocked_domains(cfg):
    from tests.test_llm import GOOD
    c, msgs = llm_client(cfg, [resp([block(type="text", text=GOOD, citations=None)]), resp([block(type="text", text=GOOD, citations=None)])])
    c.run("S", [{"role": "user", "content": "go"}], allowed_domains=["acme.com", "prnewswire.com"])
    tool = msgs.calls[0]["tools"][0]
    assert tool["allowed_domains"] == ["acme.com", "prnewswire.com"] and "blocked_domains" not in tool
    c.run("S", [{"role": "user", "content": "go"}])
    assert "blocked_domains" in msgs.calls[1]["tools"][0] and "allowed_domains" not in msgs.calls[1]["tools"][0]


def test_generate_validated_forwards_kwargs_on_the_retry_too(cfg):
    from bd.llm import generate_validated
    from bd.models import ScanResponse
    calls = []

    class L(FakeLLM):
        def run(self, system, messages, **kw):
            calls.append(kw)
            return super().run(system, messages)

    generate_validated(L(["not json", json.dumps({"signals": []})]), "S", "u", ScanResponse, llm_kwargs={"allowed_domains": ["a.com"]})
    assert calls == [{"allowed_domains": ["a.com"]}] * 2
