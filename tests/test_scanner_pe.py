import json
from datetime import timedelta

import pytest

from bd.scanners import SCANNERS, PeScanner, store_signals
from bd.score_store import drop_lapsed_status_signals, score_accounts
from bd.models import ScoringSignal
from tests.conftest import AS_OF, FakeLLM, FakeVerifier, account, add_account, add_signal, make_db

URL = "https://acme.com/news/sponsor"


def sig(**over):
    base = {"type": "pe_addon", "event_date": "2026-09-01", "summary": "Acme buys a regional competitor.",
            "source_url": URL, "source_type": "company_release", "confidence": "High",
            "service_lines": ["Strategy & Operations"], "role_or_person": "Bolt-On Co"}
    base.update(over)
    return base


def run(cfg, signals, urls=(URL,), acct=None):
    llm = FakeLLM([json.dumps({"signals": signals})], urls=urls)
    return PeScanner(cfg, llm, FakeVerifier()).scan(acct or pe_account(), AS_OF), llm


def pe_account(**kw):
    return account().model_copy(update={"ownership": "pe-backed", "sponsor": "Test Capital", **kw})


def days_ago(n):
    return (AS_OF - timedelta(days=n)).isoformat()


def test_registered_and_runs_only_for_pe_backed_accounts(cfg):
    assert SCANNERS["pe"] is PeScanner and PeScanner.implemented
    sc = PeScanner(cfg, None, None)
    assert sc.applies_to(pe_account())
    for own in ("public", "private", None):
        assert not sc.applies_to(account().model_copy(update={"ownership": own}))


def test_prompt_mentions_each_type_and_user_message_names_the_sponsor(cfg):
    out, llm = run(cfg, [])
    system, msgs = llm.calls[0]
    for t in PeScanner.allowed_types:
        assert f"`{t}`" in system
    assert "Known private equity sponsor: Test Capital" in msgs[0]["content"]
    out, llm = run(cfg, [], acct=pe_account(sponsor=None))
    assert "Known private equity sponsor: unknown" in llm.calls[0][1][0]["content"]


@pytest.mark.parametrize("t", ["pe_platform_or_sponsor_exec", "pe_addon"])
def test_event_types_use_the_180_day_window(cfg, t):
    ok, _ = run(cfg, [sig(type=t, event_date=days_ago(100))])
    assert ok.signals and ok.signals[0].family == "pe"
    old, _ = run(cfg, [sig(type=t, event_date=days_ago(200))])
    assert old.signals == [] and old.rejections[0].reason == "date_out_of_window"


@pytest.mark.parametrize("age", [90, 730, 1000, 1461, 3000])
def test_holding_duration_is_no_longer_requested_or_accepted(cfg, age):
    """How long a sponsor has held a company is not evidence of a need, and a holding period measured from an announcement is not a closing date."""
    out, _ = run(cfg, [sig(type="pe_hold_2_4", event_date=days_ago(age), summary="Test Capital acquired Acme.")])
    assert out.signals == [] and out.rejections[0].reason == "type_not_allowed"
    from importlib import resources
    assert "pe_hold_2_4" not in resources.files("bd.prompts").joinpath("pe.md").read_text(encoding="utf-8")


def test_a_hold_signal_stored_earlier_scores_nothing_and_cannot_make_a_company_look_like_a_lead(cfg, tmp_path):
    conn = make_db(tmp_path)
    a = add_account(conn, "Pecorp", ownership="pe-backed")
    add_signal(conn, cfg, a, "pe_hold_2_4", 900)
    assert cfg.scoring.base_points["pe_hold_2_4"] == 0
    r = score_accounts(conn, cfg, AS_OF)[0].result
    assert r.score == 0 and tuple(r.signals) == () and r.families == ()


def test_other_scanners_types_rejected(cfg):
    out, _ = run(cfg, [sig(type="leadership_change")])
    assert out.rejections[0].reason == "type_not_allowed"


def test_shared_checks_still_apply_to_pe_signals(cfg):
    out, _ = run(cfg, [sig()], urls=("https://acme.com/other",))
    assert out.rejections[0].reason == "url_not_retrieved"
    out, _ = run(cfg, [sig(source_type="company_site", source_url="https://news.example/x")], urls=("https://news.example/x",))
    assert out.signals[0].extract.source_type == "secondary_report" and out.signals[0].extract.confidence == "Medium"


# ---- scoring: a status condition that lapses stops scoring ----------------------------------------------

def test_a_hold_that_passes_four_years_stops_scoring(cfg, tmp_path):
    s = lambda age: ScoringSignal("pe_hold_2_4", AS_OF - timedelta(days=age), "High", ("Strategy & Operations",))
    assert drop_lapsed_status_signals([s(900)], AS_OF, cfg) == [s(900)]
    assert drop_lapsed_status_signals([s(1462)], AS_OF, cfg) == []              # lapsed
    assert drop_lapsed_status_signals([s(729)], AS_OF, cfg) == []               # not yet 2 years
    other = ScoringSignal("leadership_change", AS_OF - timedelta(days=5), "High", ())
    assert drop_lapsed_status_signals([other], AS_OF, cfg) == [other]            # events are never touched


def test_the_status_signal_mechanism_still_works_when_a_weight_is_configured(cfg, tmp_path):
    """The mechanism (flat while valid, then dropped) is kept for any status type; it is just weightless for the hold by default."""
    cfg = cfg.model_copy(deep=True)
    cfg.scoring.base_points["pe_hold_2_4"] = 12
    conn = make_db(tmp_path)
    a = add_account(conn, "Pecorp", ownership="pe-backed")
    add_signal(conn, cfg, a, "pe_hold_2_4", 900)
    r = score_accounts(conn, cfg, AS_OF)[0].result
    assert r.score == 8 and r.signals[0].recency == 0.7                          # 12 * 0.7 * 1 * 1.0 = 8.4
    later = AS_OF + timedelta(days=600)                                          # now 1500 days old
    assert score_accounts(conn, cfg, later)[0].result.score == 0


def test_config_rejects_status_bounds_for_a_non_status_type(cfg):
    import yaml
    from bd.config import Config
    from tests.conftest import ROOT
    raw = yaml.safe_load((ROOT / "config.yaml").read_text())
    raw["scan"]["status_type_age_days"]["leadership_change"] = [1, 2]
    with pytest.raises(Exception, match="not a status type"):
        Config.model_validate(raw)
    raw = yaml.safe_load((ROOT / "config.yaml").read_text())
    raw["scan"]["status_type_age_days"]["pe_hold_2_4"] = [900, 800]
    with pytest.raises(Exception, match="min <= max"):
        Config.model_validate(raw)
