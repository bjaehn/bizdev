import json
from datetime import date

import httpx2 as httpx
import pytest

from bd.edgar import EdgarError, facts_url
from bd.financials import annual_values, assess, summary
from bd.scanners import SCANNERS, FinancialsScanner, WarnScanner
from tests.conftest import AS_OF, FakeLLM, FakeVerifier, account
from tests.test_edgar import UA, client as edgar_client


def fy(end, val, filed, form="10-K", accn="0000000001-26-000010", days=364):
    e = date.fromisoformat(end)
    return {"start": (e.replace(year=e.year - 1)).isoformat() if days == 364 else end, "end": end, "val": val, "accn": accn,
            "fy": e.year, "fp": "FY", "form": form, "filed": filed}


def facts(revenue=(), op=(), concept="Revenues"):
    f = {"Revenues": None}
    out = {}
    if revenue:
        out[concept] = {"units": {"USD": list(revenue)}}
    if op:
        out["OperatingIncomeLoss"] = {"units": {"USD": list(op)}}
    return {"cik": 1234, "facts": {"us-gaap": out}}


def pressured():
    return facts(revenue=[fy("2026-06-30", 900e6, "2026-08-25"), fy("2025-06-30", 1000e6, "2025-08-25")],
                 op=[fy("2026-06-30", 45e6, "2026-08-25"), fy("2025-06-30", 120e6, "2025-08-25")])


def acct(cik="0000001234"):
    return account().model_copy(update={"cik": cik})


class FakeEdgar:
    def __init__(self, data=None, error=None):
        self.data, self.error, self.calls = data, error, []

    def company_facts(self, cik):
        self.calls.append(cik)
        if self.error:
            raise self.error
        return self.data


# ---- the arithmetic ---------------------------------------------------------------------------------------------------
def test_assess_flags_revenue_decline_and_margin_drop(cfg):
    a = assess(pressured(), cfg)
    assert a.flags == ("revenue_decline", "margin_drop")
    assert round(a.revenue_growth_pct, 1) == -10.0 and round(a.operating_margin_pct, 1) == 5.0
    assert round(a.margin_change_points, 1) == -7.0 and a.filed == date(2026, 8, 25)
    s = summary(a)
    assert s == "Fiscal 2026 10-K: revenue fell 10% to $900.0M, operating margin fell 7.0 points to 5.0%." and len(s.split()) <= 25


def test_operating_loss_flag_and_no_flags_when_healthy(cfg):
    loss = facts(revenue=[fy("2026-06-30", 500e6, "2026-08-25"), fy("2025-06-30", 510e6, "2025-08-25")],
                 op=[fy("2026-06-30", -20e6, "2026-08-25"), fy("2025-06-30", -15e6, "2025-08-25")])
    assert assess(loss, cfg).flags == ("operating_loss",)
    ok = facts(revenue=[fy("2026-06-30", 1100e6, "2026-08-25"), fy("2025-06-30", 1000e6, "2025-08-25")],
               op=[fy("2026-06-30", 150e6, "2026-08-25"), fy("2025-06-30", 120e6, "2025-08-25")])
    assert assess(ok, cfg).flags == ()


def test_only_annual_year_long_figures_count_and_restatements_use_the_latest_filing(cfg):
    q = {**fy("2025-09-30", 1, "2025-11-01", form="10-Q"), "start": "2025-07-01"}
    quarter_in_10k = {**fy("2026-06-30", 5, "2026-08-25"), "start": "2025-10-01"}           # a quarter inside a 10-K: ignored
    restated = fy("2025-06-30", 990e6, "2026-08-25")                                         # later filing restates 2024
    f = facts(revenue=[q, quarter_in_10k, fy("2026-06-30", 900e6, "2026-08-25"), fy("2025-06-30", 1000e6, "2025-08-25"), restated])
    vals = {a.end: a.value for a in annual_values(f, "Revenues")}
    assert vals == {date(2026, 6, 30): 900e6, date(2025, 6, 30): 990e6}


def test_no_assessment_when_figures_cannot_be_compared(cfg):
    assert assess({"facts": {}}, cfg) is None
    assert assess(facts(revenue=[fy("2026-06-30", 900e6, "2026-08-25")], op=[fy("2026-06-30", 1, "2026-08-25")]), cfg) is None   # one year only
    no_op = facts(revenue=[fy("2026-06-30", 900e6, "2026-08-25"), fy("2025-06-30", 1000e6, "2025-08-25")])
    assert assess(no_op, cfg) is None                                                                                           # e.g. a bank
    assert assess({"facts": {"us-gaap": {"Revenues": {"units": {"USD": "garbage"}}}}}, cfg) is None
    bad = facts(revenue=[{"end": "x"}, 5, {"form": "10-K", "start": "2024-01-01", "end": "2025-06-30", "val": True, "filed": "2025-01-01"}])
    assert assess(bad, cfg) is None


def test_an_alternative_revenue_tag_is_used(cfg):
    f = facts(revenue=[fy("2026-06-30", 900e6, "2026-08-25"), fy("2025-06-30", 1000e6, "2025-08-25")],
              op=[fy("2026-06-30", 45e6, "2026-08-25"), fy("2025-06-30", 120e6, "2025-08-25")],
              concept="RevenueFromContractWithCustomerExcludingAssessedTax")
    assert assess(f, cfg).flags == ("revenue_decline", "margin_drop")


# ---- the scanner ------------------------------------------------------------------------------------------------------
def test_financials_scanner_is_registered_and_needs_a_cik(cfg):
    s = SCANNERS["financials"].create(cfg, None, None, None, FakeEdgar())
    assert isinstance(s, FinancialsScanner) and s.applies_to(acct()) and not s.applies_to(account())


def test_signal_is_sec_high_dated_by_the_filing_and_costs_nothing(cfg):
    out = FinancialsScanner(cfg, FakeEdgar(pressured())).scan(acct(), AS_OF)
    assert out.error is None and len(out.signals) == 1 and out.usage.total_tokens == 0
    e = out.signals[0].extract
    assert (e.type, e.event_date, e.source_url, e.source_type, e.confidence) == \
        ("financial_pressure", date(2026, 8, 25), facts_url("0000001234"), "sec_filing", "High")
    assert out.signals[0].family == "financials" and e.service_lines == ["Strategy & Operations"]
    assert "0000000001-26-000010" in out.signals[0].notes[0]


def test_old_filing_healthy_company_and_missing_data_give_no_signal(cfg):
    old = FinancialsScanner(cfg, FakeEdgar(pressured())).scan(acct(), date(2027, 3, 1))      # filed 6+ months earlier
    assert not old.signals and old.rejections[0].reason == "date_out_of_window"
    healthy = facts(revenue=[fy("2026-06-30", 1100e6, "2026-08-25"), fy("2025-06-30", 1000e6, "2025-08-25")],
                    op=[fy("2026-06-30", 150e6, "2026-08-25"), fy("2025-06-30", 120e6, "2025-08-25")])
    assert FinancialsScanner(cfg, FakeEdgar(healthy)).scan(acct(), AS_OF).rejections[0].reason == "no_pressure_flags"
    assert FinancialsScanner(cfg, FakeEdgar({"facts": {}})).scan(acct(), AS_OF).rejections[0].reason == "no_comparable_figures"


def test_edgar_failure_is_an_error_not_an_empty_result(cfg):
    for failure in ("HTTP 503", "HTTP 429", "HTTP 403", "response was not JSON"):
        out = FinancialsScanner(cfg, FakeEdgar(error=EdgarError(failure))).scan(acct(), AS_OF)
        assert out.error == f"EdgarError: {failure}" and not out.signals


def test_a_cik_the_sec_has_no_data_for_is_a_definite_answer_not_a_weekly_error(cfg):
    out = FinancialsScanner(cfg, FakeEdgar(error=EdgarError("HTTP 404"))).scan(acct(), AS_OF)       # e.g. a subsidiary that reports inside its parent's filing
    assert out.error is None and not out.signals and out.rejections[0].reason == "no_company_facts"
    assert "check the CIK" in out.rejections[0].detail


def test_the_signal_scores_in_its_own_family(cfg):
    assert cfg.scoring.family_of("financial_pressure") == "financials" and cfg.scoring.base_points["financial_pressure"] == 14


def test_company_facts_client_reads_the_documented_url_and_rejects_odd_shapes(cfg):
    seen = []

    def ok(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"facts": {"us-gaap": {}}})
    c = edgar_client(cfg, ok, sleep=lambda s: None)
    assert c.company_facts("1234") == {"facts": {"us-gaap": {}}}
    assert seen == ["https://data.sec.gov/api/xbrl/companyfacts/CIK0000001234.json"]
    bad = edgar_client(cfg, lambda r: httpx.Response(200, json={"nope": 1}), sleep=lambda s: None)
    with pytest.raises(EdgarError):
        bad.company_facts("1234")
    with pytest.raises(EdgarError):
        c.company_facts("abc")


# ---- WARN scanner ---------------------------------------------------------------------------------------------------
WARN_URL = "https://www.illinois.gov/dceo/warn/acme-2026"


def warn_sig(**over):
    base = {"type": "restructuring_layoffs", "event_date": "2026-09-10", "summary": "Acme filed a WARN notice for 120 workers at its Aurora plant.",
            "source_url": WARN_URL, "source_type": "warn_notice", "confidence": "High", "service_lines": ["Strategy & Operations"],
            "role_or_person": None}
    base.update(over)
    return base


def run_warn(cfg, signals, urls=(WARN_URL,)):
    llm = FakeLLM([json.dumps({"signals": signals})], urls=urls)
    return WarnScanner(cfg, llm, FakeVerifier()).scan(account(), AS_OF), llm


def test_warn_scanner_is_registered_and_prompt_matches(cfg):
    assert SCANNERS["warn"] is WarnScanner and WarnScanner.implemented
    system = WarnScanner(cfg, None, None).system_prompt()
    assert "`restructuring_layoffs`" in system and "WARN" in system and "Never substitute today's date" in system


def test_warn_search_is_limited_to_state_domains(cfg):
    assert WarnScanner(cfg, None, None).llm_kwargs(account()) == {"allowed_domains": ["illinois.gov"]}


def test_official_notice_is_accepted_as_high_warn_notice(cfg):
    out, _ = run_warn(cfg, [warn_sig()])
    assert len(out.signals) == 1
    e = out.signals[0].extract
    assert (e.type, e.source_type, e.confidence) == ("restructuring_layoffs", "warn_notice", "High")
    assert out.signals[0].family == "events"


def test_a_non_state_page_is_rejected_even_if_the_model_calls_it_a_notice(cfg):
    out, _ = run_warn(cfg, [warn_sig(source_url="https://news.example.com/acme-layoffs")], urls=("https://news.example.com/acme-layoffs",))
    assert not out.signals and out.rejections[0].reason == "warn_needs_state_page"


def test_warn_rejects_other_types_unretrieved_urls_and_old_dates(cfg):
    out, _ = run_warn(cfg, [warn_sig(type="cost_program"), warn_sig(event_date="2024-01-01")])
    assert {r.reason for r in out.rejections} == {"type_not_allowed", "date_out_of_window"}
    out, _ = run_warn(cfg, [warn_sig()], urls=())
    assert out.rejections[0].reason == "url_not_retrieved"
