from datetime import timedelta

import pytest

from bd.models import ScoringSignal
from bd.scoring import (
    PriorPost, cap_leads, cooldown_allows, cooldown_until, is_postable,
    recency_multiplier, score_account, signal_points,
)
from tests.conftest import AS_OF, sig


# ---- the three reference accounts from the spec -------------------------------------

def test_account_a_is_91(cfg, as_of):
    # new C-suite 21d High; hiring cluster; PE add-on 45d High.
    # The spec gives no age/confidence for the hiring cluster: assumed High, <=30 days
    # (the only reading that reproduces 91). 30 + 26 + 22*0.7 = 71.4, +20 bonus = 91.4.
    r = score_account([
        sig("leadership_change", 21),
        sig("hiring_cluster", 10),
        sig("pe_addon", 45),
    ], as_of, cfg)
    assert r.score == 91
    assert r.band == "Hot"
    assert r.families == ("hiring", "leadership", "pe")
    assert r.stacking_bonus == 20


def test_account_b_is_55(cfg, as_of):
    # restructuring 35d High (16.8) + AI commitment 20d High (16 at 50% = 8) + open exec
    # search (assumed High, <=30d: 20) + 10 bonus = 54.8
    r = score_account([
        sig("restructuring_layoffs", 35),
        sig("ai_commitment", 20),
        sig("open_exec_search", 10),
    ], as_of, cfg)
    assert r.score == 55
    assert r.band == "Watch"
    assert r.family_totals["events"] == pytest.approx(24.8)


def test_account_c_is_7(cfg, as_of):
    r = score_account([sig("restructuring_layoffs", 100, "Medium")], as_of, cfg)
    assert r.score == 7  # 24 * 0.4 * 0.7 = 6.72
    assert r.band == "Stored"


# ---- recency ---------------------------------------------------------------------

@pytest.mark.parametrize("age,mult", [
    (0, 1.0), (30, 1.0), (31, 0.7), (90, 0.7), (91, 0.4), (180, 0.4), (181, 0.0), (400, 0.0),
    (-5, 1.0),  # future-dated event clamps to age 0
])
def test_recency_boundaries(cfg, as_of, age, mult):
    s = sig("leadership_change", age)
    assert recency_multiplier(s.type, s.event_date, as_of, cfg.scoring) == mult


def test_status_type_flat_regardless_of_age(cfg, as_of):
    assert signal_points(sig("pe_hold_2_4", 120), as_of, cfg).points == 0           # the holding-duration signal carries no weight by default
    cfg = cfg.model_copy(deep=True)
    cfg.scoring.base_points["pe_hold_2_4"] = 12                                       # the mechanism is still tested with a weight
    for age in (0, 120, 900):
        p = signal_points(sig("pe_hold_2_4", age), as_of, cfg)
        assert p.recency == 0.7
        assert p.points == pytest.approx(12 * 0.7)


# ---- multipliers ------------------------------------------------------------------

def test_service_match_none_halves(cfg, as_of):
    matched = signal_points(sig("leadership_change", 5), as_of, cfg).points
    unmatched = signal_points(sig("leadership_change", 5, lines=()), as_of, cfg).points
    unknown = signal_points(sig("leadership_change", 5, lines=("Accounting",)), as_of, cfg).points
    assert matched == 30 and unmatched == 15 and unknown == 15


def test_low_confidence_never_scored_and_never_counts_as_family(cfg, as_of):
    r = score_account([sig("leadership_change", 5), sig("restructuring_layoffs", 5, "Low")], as_of, cfg)
    assert r.families == ("leadership",)
    assert r.stacking_bonus == 0
    assert r.score == 30


def test_expired_signal_does_not_count_as_family(cfg, as_of):
    r = score_account([sig("leadership_change", 5), sig("restructuring_layoffs", 200)], as_of, cfg)
    assert r.families == ("leadership",)
    assert r.score == 30


def test_empty_and_all_low(cfg, as_of):
    assert score_account([], as_of, cfg).score == 0
    r = score_account([sig("rfp_match", 1, "Low")], as_of, cfg)
    assert r.score == 0 and r.band == "Stored" and r.rationale == "No scoreable signals."


# ---- family rule, bonus, cap -----------------------------------------------------------

def test_within_family_strongest_full_rest_half(cfg, as_of):
    r = score_account([
        sig("leadership_change", 5),                    # 30
        sig("open_exec_search", 5),                     # 20 -> 10
        sig("leadership_transformation_ai_data", 5),    # 25 -> 12.5
    ], as_of, cfg)
    assert r.family_totals["leadership"] == pytest.approx(52.5)
    assert r.score == 53  # 52.5 rounds half up


def test_order_of_input_does_not_matter(cfg, as_of):
    a = [sig("leadership_change", 21), sig("hiring_cluster", 10), sig("pe_addon", 45), sig("ai_commitment", 3)]
    assert score_account(a, as_of, cfg).components == score_account(a[::-1], as_of, cfg).components


def test_bonus_capped_at_30_and_score_capped_at_100(cfg, as_of):
    r = score_account([
        sig("leadership_change", 1), sig("hiring_cluster", 1), sig("pe_platform_or_sponsor_exec", 1),
        sig("restructuring_layoffs", 1), sig("platform_migration", 1), sig("rfp_match", 1),
        sig("risk_factor", 1),
    ], as_of, cfg)
    assert len(r.families) == 7
    assert r.stacking_bonus == 30
    assert r.score == 100
    assert r.raw_total > 100
    assert r.rationale.endswith("(capped)")


def test_bonus_cap_exact_with_five_families(cfg, as_of):
    r = score_account([
        sig("risk_factor", 1), sig("platform_migration", 1), sig("hiring_single", 1),
        sig("activist", 1), sig("leadership_change", 1),
    ], as_of, cfg)
    assert r.stacking_bonus == 30  # 4 extra families = 40, capped


def test_band_edges(cfg):
    from bd.scoring import band_for
    for s, b in [(100, "Hot"), (75, "Hot"), (74, "Warm"), (60, "Warm"), (59, "Watch"),
                 (40, "Watch"), (39, "Stored"), (0, "Stored")]:
        assert band_for(s, cfg.scoring) == b


def test_unknown_type_or_confidence_raises(cfg, as_of):
    with pytest.raises(KeyError):
        score_account([ScoringSignal("made_up", AS_OF, "High", ())], as_of, cfg)
    with pytest.raises(KeyError):
        score_account([ScoringSignal("pe_addon", AS_OF, "Certain", ())], as_of, cfg)


def test_components_json_roundtrip(cfg, as_of):
    import json
    r = score_account([sig("leadership_change", 21), sig("pe_addon", 45)], as_of, cfg)
    d = json.loads(r.components_json())
    assert d["stacking_bonus"] == 10
    assert set(d["families"]) == {"leadership", "pe"}
    assert d["families"]["pe"]["signals"][0]["recency"] == 0.7


# ---- post gate / cooldown ------------------------------------------------------------

def test_post_gate_needs_recent_high_confidence(cfg, as_of):
    # Warm/Hot but only Medium evidence -> not postable
    medium = [sig("leadership_change", 5, "Medium"), sig("hiring_cluster", 5, "Medium"),
              sig("pe_addon", 5, "Medium")]
    r = score_account(medium, as_of, cfg)
    assert r.band in ("Hot", "Warm") and not is_postable(r, medium, as_of, cfg)

    # High signal but 100 days old (outside the 90-day gate) -> not postable
    old_high = medium + [sig("restructuring_layoffs", 100, "High")]
    r = score_account(old_high, as_of, cfg)
    assert r.band in ("Hot", "Warm") and not is_postable(r, old_high, as_of, cfg)

    ok = medium + [sig("restructuring_layoffs", 90, "High")]
    assert is_postable(score_account(ok, as_of, cfg), ok, as_of, cfg)


def test_watch_band_is_not_postable_even_with_high(cfg, as_of):
    s = [sig("open_exec_search", 5)]
    r = score_account(s, as_of, cfg)
    assert r.band == "Stored" and not is_postable(r, s, as_of, cfg)
    s = [sig("leadership_change", 5), sig("risk_factor", 5)]  # 30 + 8 + 10 = 48 -> Watch
    r = score_account(s, as_of, cfg)
    assert r.band == "Watch" and not is_postable(r, s, as_of, cfg)


def test_cooldown_window_and_breaks(cfg, as_of):
    sigs = [sig("leadership_change", 5), sig("hiring_cluster", 5)]
    r = score_account(sigs, as_of, cfg)  # 56 + 10 = 66
    posted = as_of - timedelta(weeks=1)
    prior = PriorPost(score=r.score, families=frozenset(r.families), cooldown_until=cooldown_until(posted, cfg))
    assert prior.cooldown_until == posted + timedelta(weeks=4)

    assert not cooldown_allows(prior, r, as_of, cfg)                       # same families, same score
    assert cooldown_allows(None, r, as_of, cfg)                            # never posted
    assert cooldown_allows(prior, r, prior.cooldown_until, cfg)            # expired (boundary day)
    assert not cooldown_allows(prior, r, prior.cooldown_until - timedelta(days=1), cfg)

    new_family = score_account(sigs + [sig("pe_addon", 5)], as_of, cfg)
    assert cooldown_allows(prior, new_family, as_of, cfg)                  # new family

    lower = PriorPost(score=r.score - 14, families=frozenset(r.families), cooldown_until=prior.cooldown_until)
    assert not cooldown_allows(lower, r, as_of, cfg)                       # +14 is not enough
    lower = PriorPost(score=r.score - 15, families=frozenset(r.families), cooldown_until=prior.cooldown_until)
    assert cooldown_allows(lower, r, as_of, cfg)                           # +15 is


def test_cap_leads(cfg):
    leads = [(i, 60 + (i % 7)) for i in range(1, 25)]
    top = cap_leads(leads, cfg)
    assert len(top) == 10
    assert [s for _, s in top] == sorted((s for _, s in top), reverse=True)
    assert cap_leads([(2, 80), (1, 80)], cfg) == [(1, 80), (2, 80)]  # stable tiebreak
