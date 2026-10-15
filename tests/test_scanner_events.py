import json

from bd.scanners import SCANNERS, EventsScanner
from tests.conftest import AS_OF, FakeLLM, FakeVerifier, account

URL = "https://acme.com/investors/restructuring"


def sig(**over):
    base = {"type": "restructuring_layoffs", "event_date": "2026-09-01", "summary": "Acme announces restructuring.",
            "source_url": URL, "source_type": "company_release", "confidence": "High",
            "service_lines": ["Strategy & Operations"], "role_or_person": None}
    base.update(over)
    return base


def run(cfg, signals, urls=(URL,)):
    llm = FakeLLM([json.dumps({"signals": signals})], urls=urls)
    return EventsScanner(cfg, llm, FakeVerifier()).scan(account(), AS_OF), llm


def test_registered_and_implemented(cfg):
    assert SCANNERS["events"] is EventsScanner and EventsScanner.implemented


def test_prompt_covers_every_event_type_and_core_rules(cfg):
    p = EventsScanner(cfg, None, None).system_prompt()
    for t in EventsScanner.allowed_types:
        assert f"`{t}`" in p
    for phrase in ("WARN", "LinkedIn", "untrusted", "never rely on memory".capitalize()[:0] or "memory", "SPECIFIC"):
        assert phrase in p
    assert "Acme" not in p


def test_every_event_type_is_accepted_and_in_events_family(cfg):
    for t in sorted(EventsScanner.allowed_types):
        out, _ = run(cfg, [sig(type=t)])
        assert out.signals and out.signals[0].family == "events", t


def test_leadership_types_rejected_by_events_scanner(cfg):
    out, _ = run(cfg, [sig(type="leadership_change")])
    assert out.rejections[0].reason == "type_not_allowed"


def test_illinois_warn_notice_on_illinois_gov_is_high(cfg):
    u = "https://www.illinois.gov/dceo/warn/acme-notice"
    out, _ = run(cfg, [sig(source_type="warn_notice", source_url=u)], urls=(u,))
    s = out.signals[0].extract
    assert s.source_type == "warn_notice" and s.confidence == "High"


def test_warn_notice_claim_on_a_news_site_is_downgraded(cfg):
    u = "https://news.example/acme-layoffs-warn"
    out, _ = run(cfg, [sig(source_type="warn_notice", source_url=u)], urls=(u,))
    s = out.signals[0].extract
    assert s.source_type == "secondary_report" and s.confidence == "Medium"


def test_user_prompt_uses_same_180_day_window(cfg):
    _, llm = run(cfg, [])
    assert "2026-04-18 through 2026-10-15" in llm.calls[0][1][0]["content"]
