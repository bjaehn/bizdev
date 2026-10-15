import json
from datetime import timedelta

import pytest

from bd.scanners import SCANNERS, TechProcurementScanner
from tests.conftest import AS_OF, FakeLLM, FakeVerifier, account

URL = "https://acme.com/news/erp"
SEC = "https://www.sec.gov/Archives/edgar/data/1/10k.htm"


def sig(**over):
    base = {"type": "platform_migration", "event_date": "2026-09-01", "summary": "Acme moves its ERP to a cloud platform.",
            "source_url": URL, "source_type": "company_release", "confidence": "High",
            "service_lines": ["AI & Technology"], "role_or_person": None, "due_date": None}
    base.update(over)
    return base


def run(cfg, signals, urls=(URL,)):
    llm = FakeLLM([json.dumps({"signals": signals})], urls=urls)
    return TechProcurementScanner(cfg, llm, FakeVerifier()).scan(account(), AS_OF), llm


def d(n):
    return (AS_OF + timedelta(days=n)).isoformat()


def test_registered_and_prompt_covers_every_type(cfg):
    assert SCANNERS["tech_procurement"] is TechProcurementScanner and TechProcurementScanner.implemented
    system = TechProcurementScanner(cfg, None, None).system_prompt()
    for t in TechProcurementScanner.allowed_types:
        assert f"`{t}`" in system
    assert "due_date" in system and "sec.gov" in system


@pytest.mark.parametrize("t,family", [("platform_migration", "technology"), ("vendor_deal", "technology")])
def test_technology_types(cfg, t, family):
    out, _ = run(cfg, [sig(type=t)])
    assert out.signals and out.signals[0].family == family


def test_a_stray_due_date_on_a_non_rfp_signal_is_harmless_and_the_signal_stores(cfg, tmp_path):
    from bd.scanners import store_signals
    from tests.conftest import make_db
    out, _ = run(cfg, [sig(type="vendor_deal", due_date="2026-12-01")])
    assert len(out.signals) == 1
    conn = make_db(tmp_path)
    conn.execute("INSERT INTO accounts(id, name, domain) VALUES (1, 'Acme Corp', 'acme.com')")
    conn.commit()
    assert store_signals(conn, 1, out.signals, AS_OF) == (1, 0)
    row = conn.execute("SELECT type, family, source_url FROM signals").fetchone()
    assert (row["type"], row["family"], row["source_url"]) == ("vendor_deal", "technology", URL)


@pytest.mark.parametrize("due,reason", [(None, "rfp_no_due_date"), (d(-1), "rfp_closed"), (d(-60), "rfp_closed")])
def test_rfp_needs_an_open_due_date(cfg, due, reason):
    out, _ = run(cfg, [sig(type="rfp_match", due_date=due)])
    assert out.signals == [] and out.rejections[0].reason == reason


def test_open_rfp_is_accepted_including_due_today(cfg):
    for due in (d(0), d(30)):
        out, _ = run(cfg, [sig(type="rfp_match", due_date=due)])
        assert out.signals and out.signals[0].family == "procurement"


def test_rfp_on_a_third_party_portal_is_medium_confidence(cfg):
    u = "https://bids.example.gov/rfp/42"
    out, _ = run(cfg, [sig(type="rfp_match", due_date=d(10), source_url=u, source_type="company_site")], urls=(u,))
    s = out.signals[0].extract
    assert (s.source_type, s.confidence) == ("secondary_report", "Medium")


def test_risk_factor_must_be_on_sec_gov(cfg):
    out, _ = run(cfg, [sig(type="risk_factor", source_url=SEC, source_type="sec_filing")], urls=(SEC,))
    assert out.signals and out.signals[0].family == "filings" and out.signals[0].extract.confidence == "High"
    out, _ = run(cfg, [sig(type="risk_factor", source_type="company_release")])      # acme.com is not sec.gov
    assert out.signals == [] and out.rejections[0].reason == "risk_factor_needs_sec_filing"


def test_window_and_other_scanners_types(cfg):
    out, _ = run(cfg, [sig(event_date="2026-01-01")])
    assert out.rejections[0].reason == "date_out_of_window"
    out, _ = run(cfg, [sig(type="pe_addon")])
    assert out.rejections[0].reason == "type_not_allowed"


def test_invalid_rfp_json_due_date_is_retried(cfg):
    bad = json.dumps({"signals": [sig(type="rfp_match", due_date="next month")]})
    good = json.dumps({"signals": [sig(type="rfp_match", due_date=d(5))]})
    llm = FakeLLM([bad, good], urls=(URL,))
    out = TechProcurementScanner(cfg, llm, FakeVerifier()).scan(account(), AS_OF)
    assert len(out.signals) == 1 and len(llm.calls) == 2
