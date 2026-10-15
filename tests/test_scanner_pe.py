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


@pytest.mark.parametrize("age,ok", [(729, False), (730, True), (1000, True), (1461, True), (1462, False), (3000, False), (90, False)])
def test_hold_signal_needs_the_original_investment_2_to_4_years_ago(cfg, age, ok):
    out, _ = run(cfg, [sig(type="pe_hold_2_4", event_date=days_ago(age), summary="Test Capital acquired Acme.")])
    assert bool(out.signals) is ok
    if not ok:
        assert out.rejections[0].reason == "date_out_of_window" and "hold must be 730-1461 days" in out.rejections[0].detail


def test_hold_signal_is_not_subject_to_the_180_day_window(cfg):
    out, _ = run(cfg, [sig(type="pe_hold_2_4", event_date=days_ago(900))])
    assert len(out.signals) == 1 and out.signals[0].extract.event_date == AS_OF - timedelta(days=900)


def test_hold_hash_is_stable_week_to_week_so_it_is_stored_once(cfg, tmp_path):
    conn = make_db(tmp_path)
    conn.execute("INSERT INTO accounts(id, name, domain, ownership) VALUES (1, 'Acme Corp', 'acme.com', 'pe-backed')")
    conn.commit()
    a, _ = run(cfg, [sig(type="pe_hold_2_4", event_date=days_ago(900))])
    assert store_signals(conn, 1, a.signals, AS_OF) == (1, 0)
    llm = FakeLLM([json.dumps({"signals": [sig(type="pe_hold_2_4", event_date=days_ago(900))]})], urls=(URL,))
    b = PeScanner(cfg, llm, FakeVerifier()).scan(pe_account(), AS_OF)           # the same fact, found again
    assert store_signals(conn, 1, b.signals, AS_OF + timedelta(days=7)) == (0, 1)


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


def test_stored_hold_scores_flat_while_valid_then_drops_out(cfg, tmp_path):
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
