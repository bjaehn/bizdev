import json
from datetime import date

import httpx2 as httpx
import pytest

from bd.accounts import list_accounts
from bd.db import connect, init_db
from bd.edgar import EdgarClient, EdgarError
from bd.llm import Usage
from bd.runner import run_scans
from bd.scanners import SCANNERS, SecEventsScanner
from bd.scanners.base import ScanOutcome, Scanner, store_signals
from tests.conftest import AS_OF, account
from tests.test_edgar import UA
from tests.test_runner import FakeScanner

CIK = "0000001234"
DOC_2_05 = "<html><body><p>FORM 8-K</p><p>Item 2.05 Costs Associated with Exit or Disposal Activities</p><p>The Company approved a plan.</p></body></html>"
DOC_2_01 = "<html><body><p>Item 2.01 Completion of Acquisition or Disposition of Assets</p></body></html>"
DOC_BOTH = "<html><body><p>Item 2.01 Completion of Acquisition.</p><p>Item 2.05 Costs Associated with Exit.</p></body></html>"
DOC_NEITHER = "<html><body><p>Item 8.01 Other Events</p></body></html>"


def subs(rows):
    cols = ["accessionNumber", "filingDate", "reportDate", "form", "items", "primaryDocument"]
    recent = {c: [r[i] for r in rows] for i, c in enumerate(cols)}
    return {"name": "ACME CORP", "tickers": ["ACME"], "filings": {"recent": recent}}


ROWS = [
    ("0000001234-26-000001", "2026-09-24", "2026-09-22", "8-K", "2.05,9.01", "restructure.htm"),
    ("0000001234-26-000002", "2026-09-10", "2026-09-09", "8-K", "2.01", "deal.htm"),
    ("0000001234-26-000003", "2026-09-01", "2026-08-31", "8-K", "5.02", "officer.htm"),            # an officer item: its own text decides (the default document has no Item 5.02)
    ("0000001234-26-000004", "2026-08-20", "2026-08-19", "10-Q", "2.05", "q.htm"),                  # not an 8-K
    ("0000001234-25-000005", "2025-01-10", "2025-01-09", "8-K", "2.05", "old.htm"),                 # outside the window
]


class Site:
    """SEC stand-in: the submissions file and the filing documents."""
    def __init__(self, rows=ROWS, docs=None, status=None, subs_status=200):
        self.rows, self.subs_status, self.status = rows, subs_status, status or {}
        self.docs = {"restructure.htm": DOC_2_05, "deal.htm": DOC_2_01, **(docs or {})}
        self.requests = []

    def __call__(self, req):
        self.requests.append(req.url.path)
        if "/submissions/" in req.url.path:
            return httpx.Response(self.subs_status, json=subs(self.rows))
        name = req.url.path.rsplit("/", 1)[-1]
        return httpx.Response(self.status.get(name, 200), text=self.docs.get(name, DOC_NEITHER))


def scanner(cfg, site):
    edgar = EdgarClient(cfg, UA, httpx.Client(transport=httpx.MockTransport(site)), sleep=lambda s: None)
    return SecEventsScanner(cfg, edgar)


def acct(cik=CIK):
    return account().model_copy(update={"cik": cik})


def test_an_item_in_the_filing_becomes_a_dated_sourced_signal_with_no_model_call(cfg):
    site = Site()
    out = scanner(cfg, site).scan(acct(), AS_OF)
    by = {s.extract.type: s for s in out.signals}
    r, d = by["restructuring_layoffs"].extract, by["ma_divestiture"].extract
    assert r.event_date == date(2026, 9, 22) and r.source_type == "sec_filing" and r.confidence == "High"
    assert r.source_url == "https://www.sec.gov/Archives/edgar/data/1234/000000123426000001/restructure.htm"
    assert r.summary == "Filed an 8-K reporting costs associated with exit or disposal activities (Item 2.05)." and len(r.summary.split()) <= 25
    assert r.service_lines == ["Strategy & Operations"] and r.role_or_person is None
    assert d.event_date == date(2026, 9, 9) and d.summary.endswith("(Item 2.01).")
    assert by["restructuring_layoffs"].family == "events" and by["restructuring_layoffs"].notes == ["EDGAR 8-K 0000001234-26-000001 Item 2.05"]
    assert out.usage.total_tokens == 0 and out.usage.web_searches == 0 and out.error is None
    assert sorted(out.read_filings) == ["0000001234-26-000001", "0000001234-26-000002", "0000001234-26-000003"]
    assert len([p for p in site.requests if "/Archives/" in p]) == 3 and len([p for p in site.requests if "/submissions/" in p]) == 1


def test_one_filing_with_two_items_is_fetched_once_and_gives_two_signals(cfg):
    site = Site(rows=[("0000001234-26-000009", "2026-09-24", "2026-09-22", "8-K", "2.01,2.05,9.01", "both.htm")], docs={"both.htm": DOC_BOTH})
    out = scanner(cfg, site).scan(acct(), AS_OF)
    assert sorted(s.extract.type for s in out.signals) == ["ma_divestiture", "restructuring_layoffs"]
    assert len([p for p in site.requests if "/Archives/" in p]) == 1


def test_an_item_the_filing_does_not_carry_is_rejected_but_remembered_as_read(cfg):
    site = Site(rows=[ROWS[0]], docs={"restructure.htm": DOC_NEITHER})
    out = scanner(cfg, site).scan(acct(), AS_OF)
    assert out.signals == [] and [x.reason for x in out.rejections] == ["item_not_in_filing"] and out.read_filings == ["0000001234-26-000001"]


def test_filings_read_before_are_not_fetched_again(cfg):
    site = Site()
    out = scanner(cfg, site).scan(acct(), AS_OF, frozenset({"0000001234-26-000001"}))
    assert [s.extract.type for s in out.signals] == ["ma_divestiture"] and sorted(out.read_filings) == ["0000001234-26-000002", "0000001234-26-000003"]
    assert not [p for p in site.requests if p.endswith("restructure.htm")]


def test_an_unreachable_filing_is_not_marked_read_so_it_is_tried_again(cfg):
    out = scanner(cfg, Site(rows=[ROWS[0]], status={"restructure.htm": 404})).scan(acct(), AS_OF)
    assert out.signals == [] and out.read_filings == [] and out.rejections[0].reason.startswith("unreachable:")


def test_other_items_forms_and_dates_outside_the_window_are_ignored(cfg):
    out = scanner(cfg, Site(rows=ROWS[3:])).scan(acct(), AS_OF)
    assert out.signals == [] and out.candidates == 0 and out.read_filings == []
    future = [("0000001234-26-000010", "2027-06-01", "2027-06-01", "8-K", "2.05", "restructure.htm")]
    assert scanner(cfg, Site(rows=future)).scan(acct(), AS_OF).candidates == 0


def test_sec_trouble_is_reported_not_raised_and_no_cik_means_no_call(cfg):
    out = scanner(cfg, Site(subs_status=503)).scan(acct(), AS_OF)
    assert out.error and "503" in out.error and out.signals == []
    site = Site()
    sc = scanner(cfg, site)
    assert not sc.applies_to(account()) and sc.applies_to(acct()) and sc.scan(account(), AS_OF).signals == [] and site.requests == []


def test_signals_are_stored_once_and_scored_like_any_other(cfg, tmp_path):
    conn = connect(tmp_path / "t.sqlite")
    init_db(conn)
    conn.execute("INSERT INTO accounts(name, domain, status, cik) VALUES ('Acme Corp','acme.com','active',?)", (CIK,))
    conn.commit()
    a = list_accounts(conn)[0]
    out = scanner(cfg, Site()).scan(a, AS_OF)
    assert store_signals(conn, a.id, out.signals, AS_OF)[0] == 2
    assert store_signals(conn, a.id, scanner(cfg, Site()).scan(a, AS_OF).signals, AS_OF)[0] == 0          # same hashes: nothing new
    rows = conn.execute("SELECT family, type, source_type, confidence FROM signals ORDER BY type").fetchall()
    assert [tuple(r) for r in rows] == [("events", "ma_divestiture", "sec_filing", "High"), ("events", "restructuring_layoffs", "sec_filing", "High")]


def test_the_scanner_is_registered_free_and_the_other_free_ones_are_marked(cfg):
    assert SCANNERS["sec_events"] is SecEventsScanner
    assert {n for n, c in SCANNERS.items() if c.free} == {"ats", "financials", "sec_events"}
    assert SecEventsScanner.reads_filings                                                              # remembers filings it has read
    assert scanner(cfg, Site()).allowed_types == {"restructuring_layoffs", "ma_divestiture", "leadership_change", "leadership_transformation_ai_data"}


def test_config_rejects_a_bad_item_map(cfg):
    from bd.config import Config
    raw = cfg.model_dump(mode="json")
    for mutate, msg in ((lambda r: r["sec_events"]["items"].update({"2.5": r["sec_events"]["items"]["2.05"]}), "must look like"),
                        (lambda r: r["sec_events"]["items"]["2.05"].update(type="nope"), "not a signal type"),
                        (lambda r: r["sec_events"]["items"]["2.05"].update(service_lines=["Nope"]), "service_lines"),
                        (lambda r: r["sec_events"]["items"]["2.05"].update(label="word " * 30), "exceed")):
        bad = json.loads(json.dumps(raw))
        mutate(bad)
        with pytest.raises(Exception, match=msg):
            Config.model_validate(bad)


# ---- free scans in the runner -----------------------------------------------------------------------------------------------------
class FreeFake(FakeScanner):
    free = True


def make_accounts(conn, n):
    for i in range(n):
        conn.execute("INSERT INTO accounts(name, domain, tier, status) VALUES (?,?,?,?)", (f"Co{i}", f"co{i}.com", "A", "active"))
    conn.commit()
    return list_accounts(conn)


def test_free_scans_run_first_and_are_never_stopped_by_the_spending_limit(cfg, tmp_path):
    conn = connect(tmp_path / "r.sqlite")
    init_db(conn)
    accts = make_accounts(conn, 4)
    paid = FakeScanner(cfg, name="leadership", usage=Usage(input_tokens=100_000, output_tokens=50_000, web_searches=5))
    free = FreeFake(cfg, name="financials", usage=Usage())
    s = run_scans(conn, cfg, [paid, free], accts, AS_OF, "claude-sonnet-5-5", workers=2, max_cost=0.0001)
    assert len(free.calls) == 4                                  # every company got the free scan even with the limit at ~0
    assert len(paid.calls) == 1 and s.budget_hit and s.skipped_budget == 3     # one paid call through, then the limit holds
    first_two = [r["step"] for r in conn.execute("SELECT step FROM runs WHERE step != 'scan:total' ORDER BY id LIMIT 4")]
    assert first_two == ["scan:financials"] * 4                  # the free scans were done first



# ---- Item 5.02: an officer change, read from the item's own text by code ---------------------------------------------------------------------------
def doc502(body):
    return f"<html><body><p>FORM 8-K</p><p>Item 5.02 Departure of Directors or Certain Officers; Election of Directors; Appointment of Certain Officers; Compensatory Arrangements</p><p>{body}</p><p>Item 9.01 Financial Statements and Exhibits</p><p>Exhibit list.</p></body></html>"


OFFICER_ROW = [("0000001234-26-000020", "2026-10-02", "2026-10-01", "8-K", "5.02,9.01", "officer.htm")]


def officer_scan(cfg, body, **kw):
    return scanner(cfg, Site(rows=OFFICER_ROW, docs={"officer.htm": doc502(body)})).scan(acct(), AS_OF, **kw)


def test_an_appointed_officer_becomes_a_medium_confidence_leadership_signal_with_a_fixed_sentence_and_no_person(cfg):
    out = officer_scan(cfg, "On October 1, 2026, the Board of Directors appointed Jane Doe as Chief Information Officer, effective October 15, 2026.")
    assert [s.extract.type for s in out.signals] == ["leadership_change"]
    e = out.signals[0].extract
    assert e.summary == "Filed an 8-K (Item 5.02) that reports a change involving the Chief Information Officer." and e.role_or_person == "Chief Information Officer"
    assert "Jane" not in e.summary and e.source_type == "sec_filing" and e.confidence == "Medium" and e.event_date == date(2026, 10, 1)
    assert out.signals[0].family == "leadership" and "within 140 characters" in out.signals[0].notes[0] and out.usage.total_tokens == 0


def test_a_transformation_ai_or_data_title_gets_its_own_type_and_service_lines(cfg):
    out = officer_scan(cfg, "The Company announced that John Roe has resigned as Chief Data Officer and will leave the Company on November 30, 2026.")
    e = next(s.extract for s in out.signals if s.extract.type == "leadership_transformation_ai_data")
    assert e.role_or_person == "Chief Data Officer" and e.service_lines == ["AI & Technology", "Data & Analytics", "Strategy & Operations"]


def test_a_director_only_item_and_a_pay_item_are_not_leadership_changes(cfg):
    director = officer_scan(cfg, "On October 1, 2026, the Board elected Maria Lopez as a director of the Company, to serve until the next annual meeting.")
    assert director.signals == [] and [r.reason for r in director.rejections] == ["no_officer_title_in_item"] and director.read_filings == ["0000001234-26-000020"]
    pay = officer_scan(cfg, "The Compensation Committee approved the 2027 annual bonus plan for the named executive officers, including the Chief Executive Officer and the Chief Financial Officer.")
    assert pay.signals == []                                                                          # "named executive officers" is about pay, not a change


def test_a_title_far_from_any_change_word_is_not_enough(cfg):
    far = officer_scan(cfg, "The Board appointed a new independent director. " + ("Background text. " * 30) + "The Chief Executive Officer attended the meeting.")
    assert far.signals == []


def test_one_filing_gives_at_most_one_signal_per_type(cfg):
    out = officer_scan(cfg, "The Board appointed Ann Lee as Chief Financial Officer. The Board also appointed Bo Kim as Chief Operating Officer and promoted Cy Fox to Executive Vice President.")
    assert [s.extract.type for s in out.signals] == ["leadership_change"]


def test_the_item_text_stops_at_the_next_items_heading(cfg):
    doc = ("<html><body><p>Item 5.02 Departure of Directors or Certain Officers</p><p>The Board elected a director.</p>"
           "<p>Item 7.01 Regulation FD Disclosure</p><p>The Company appointed a new Chief Executive Officer.</p></body></html>")
    out = scanner(cfg, Site(rows=OFFICER_ROW, docs={"officer.htm": doc})).scan(acct(), AS_OF)
    assert out.signals == []                                                                          # the appointment is under Item 7.01, not Item 5.02


def test_officer_signals_are_stored_and_the_same_filing_is_not_read_twice(cfg, tmp_path):
    conn = connect(tmp_path / "t.sqlite")
    init_db(conn)
    conn.execute("INSERT INTO accounts(name, domain, status, cik) VALUES ('Acme Corp','acme.com','active',?)", (CIK,))
    conn.commit()
    a = list_accounts(conn)[0]
    site = Site(rows=OFFICER_ROW, docs={"officer.htm": doc502("The Board appointed Jane Doe as Chief Information Officer.")})
    out = scanner(cfg, site).scan(a, AS_OF)
    assert store_signals(conn, a.id, out.signals, AS_OF)[0] == 1
    row = conn.execute("SELECT family, type, source_type, confidence, role_or_person FROM signals").fetchone()
    assert tuple(row) == ("leadership", "leadership_change", "sec_filing", "Medium", "Chief Information Officer")
    again = scanner(cfg, site).scan(a, AS_OF, frozenset(out.read_filings))
    assert again.signals == [] and again.candidates == 0


def test_the_items_own_heading_cannot_supply_the_change_word(cfg):
    # the standard heading says "Departure ... Appointment ..."; a title right after it, with no change word of its own, is not a change
    out = officer_scan(cfg, "The Chief Executive Officer will present the annual plan to the Board in November.")
    assert out.signals == []
    heading_only = "<html><body><p>Item 5.02 Departure of Directors or Certain Officers; Appointment of Certain Officers</p><p>The Chief Financial Officer reviewed the budget.</p></body></html>"
    assert scanner(cfg, Site(rows=OFFICER_ROW, docs={"officer.htm": heading_only})).scan(acct(), AS_OF).signals == []


def test_nouns_from_pay_and_background_text_are_not_change_words(cfg):
    pay = ("The Compensation Committee approved a severance arrangement for the Chief Executive Officer providing payments upon termination without cause and "
           "continued participation in the retirement plan. Her appointment in 2022 is unchanged.")
    assert officer_scan(cfg, pay).signals == []
    assert [s.extract.type for s in officer_scan(cfg, "The Chief Operating Officer resigned effective immediately.").signals] == ["leadership_change"]
