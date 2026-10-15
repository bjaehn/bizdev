from datetime import date

import httpx2 as httpx
import pytest

from bd.edgar import EdgarClient, EdgarError, Filing, excerpt, html_to_text

UA = "SEI BD Research ops@example.com"
SINCE, UNTIL = date(2026, 4, 18), date(2026, 11, 14)


def sub(**over):
    recent = {
        "accessionNumber": ["0000000001-26-000001", "0000000001-26-000002", "0000000001-26-000003",
                            "0000000001-26-000004", "0000000001-26-000005", "bad-accession", "0000000001-26-000007"],
        "filingDate": ["2026-09-24", "2026-09-20", "2026-09-10", "2026-10-01", "2025-01-02", "2026-09-01", "2026-09-02"],
        "reportDate": ["2026-09-22", "2026-09-19", "2026-09-09", "", "2025-01-01", "2026-09-01", "2026-09-02"],
        "form": ["8-K", "8-K", "10-Q", "8-K/A", "8-K", "8-K", "8-K"],
        "items": ["5.02,9.01", "2.02,9.01", "", "5.02", "5.02", "5.02", "5.02"],
        "primaryDocument": ["d1.htm", "d2.htm", "q.htm", "d4.htm", "d5.htm", "d6.htm", "../evil.htm"],
    }
    recent.update(over)
    return {"name": "ACME CORP", "tickers": ["ACME"], "filings": {"recent": recent}}


class Rec:
    def __init__(self, body=None, status=200, text=None):
        self.body, self.status, self.text, self.reqs = body, status, text, []

    def __call__(self, req):
        self.reqs.append(req)
        if self.text is not None and "/Archives/" in req.url.path:
            return httpx.Response(self.status, text=self.text)
        return httpx.Response(self.status, json=self.body)


def client(cfg, rec, **kw):
    kw.setdefault("sleep", lambda s: None)
    return EdgarClient(cfg, UA, httpx.Client(transport=httpx.MockTransport(rec)), **kw)


def test_only_8k_item_502_in_window_valid_entries_newest_first(cfg):
    rec = Rec(sub())
    fs = client(cfg, rec).recent_item_502("1234", SINCE, UNTIL, limit=10)
    assert [f.accession for f in fs] == ["0000000001-26-000004", "0000000001-26-000001"]
    a, b = fs
    assert (a.form, a.report_date, a.event_date) == ("8-K/A", None, date(2026, 10, 1))   # no reportDate -> filing date
    assert (b.report_date, b.event_date, b.filing_date) == (date(2026, 9, 22), date(2026, 9, 22), date(2026, 9, 24))
    assert b.items == ("5.02", "9.01") and b.cik == "0000001234"


def test_request_url_and_user_agent(cfg):
    rec = Rec(sub())
    client(cfg, rec).recent_item_502("1234", SINCE, UNTIL, 5)
    req = rec.reqs[0]
    assert str(req.url) == "https://data.sec.gov/submissions/CIK0000001234.json"
    assert req.headers["user-agent"] == UA


def test_filing_url_is_built_from_validated_parts(cfg):
    f = Filing("0000001234", "0000000001-26-000001", date(2026, 9, 24), None, "8-K", ("5.02",), "d1.htm")
    assert f.url == "https://www.sec.gov/Archives/edgar/data/1234/000000000126000001/d1.htm"


def test_limit(cfg):
    fs = client(cfg, Rec(sub())).recent_item_502("1234", SINCE, UNTIL, limit=1)
    assert len(fs) == 1


def test_window_boundaries_use_event_date(cfg):
    fs = client(cfg, Rec(sub())).recent_item_502("1234", date(2026, 9, 23), UNTIL, 10)
    assert [f.accession for f in fs] == ["0000000001-26-000004"]   # 000001's event date is 09-22, before the window


@pytest.mark.parametrize("status", [403, 404, 429, 500])
def test_http_errors_raise_edgar_error(cfg, status):
    with pytest.raises(EdgarError, match=f"HTTP {status}"):
        client(cfg, Rec(sub(), status=status)).submissions("1234")


def test_bad_layouts_are_errors_not_silent_empty(cfg):
    with pytest.raises(EdgarError, match="layout"):
        client(cfg, Rec({"filings": {}})).submissions("1234")
    with pytest.raises(EdgarError, match="layout"):
        client(cfg, Rec([1, 2])).submissions("1234")
    with pytest.raises(EdgarError, match="layout"):
        client(cfg, Rec(sub(form="oops"))).recent_item_502("1234", SINCE, UNTIL, 5)


def test_non_json_and_bad_cik(cfg):
    def h(req):
        return httpx.Response(200, text="<html>not json</html>")
    c = EdgarClient(cfg, UA, httpx.Client(transport=httpx.MockTransport(h)), sleep=lambda s: None)
    with pytest.raises(EdgarError, match="not JSON"):
        c.submissions("1234")
    with pytest.raises(EdgarError, match="bad cik"):
        c.submissions("12/../34")


def test_transport_failure_is_edgar_error_without_url(cfg):
    def h(req):
        raise httpx.ConnectError("boom", request=req)
    c = EdgarClient(cfg, UA, httpx.Client(transport=httpx.MockTransport(h)), sleep=lambda s: None)
    with pytest.raises(EdgarError, match=r"request failed \(ConnectError\)") as e:
        c.submissions("1234")
    assert "sec.gov" not in str(e.value)


def test_requests_are_spaced_by_the_configured_interval(cfg):
    now, sleeps = [100.0], []
    def sleep(s):
        sleeps.append(round(s, 3))
        now[0] += s
    c = client(cfg, Rec(sub()), sleep=sleep, clock=lambda: now[0])
    c.submissions("1")                 # first request: no wait
    now[0] += 0.05
    c.submissions("1")                 # 0.05s later: waits the remaining 0.10s
    now[0] += 1.0
    c.submissions("1")                 # long gap: no wait
    assert sleeps == [0.1]


def test_company_name_and_tickers(cfg):
    assert client(cfg, Rec(sub())).company("1234") == ("ACME CORP", ["ACME"])


def test_filing_text_is_stripped_and_cut_at_item_502(cfg):
    html = ("<html><head><style>.x{}</style><script>var a=1;</script></head><body><p>Cover page &amp; boilerplate</p>"
            "<p>Item 5.02 Departure of Directors.</p><p>Pat Lee resigned as CIO.</p></body></html>")
    f = Filing("0000001234", "0000000001-26-000001", date(2026, 9, 24), None, "8-K", ("5.02",), "d1.htm")
    t = client(cfg, Rec(sub(), text=html)).filing_text(f)
    assert t.startswith("Item 5.02 Departure of Directors. Pat Lee resigned as CIO.")
    assert "boilerplate" not in t and "var a" not in t and ".x{" not in t


def test_html_helpers():
    assert html_to_text("<b>a</b>  <i>b</i>\n\nc") == "a b c"
    assert excerpt("xx ITEM 5.02 yy", 100) == "ITEM 5.02 yy"
    assert excerpt("no heading here", 5) == "no he"
    assert len(excerpt("Item 5.02 " + "z" * 100, 20)) == 20
