from datetime import timedelta

import pytest

from bd.config import Sequence
from bd.opportunities import build_opportunities
from bd.score_store import score_accounts
from bd.scoring import score_account
from tests.conftest import AS_OF, add_account, add_signal, make_db, sig

PATTERNS = [
    Sequence(name="new_leader_then_hiring", label="New leader, then hiring", first=["leadership_change", "leadership_transformation_ai_data"],
             then=["hiring_cluster", "hiring_leadership", "hiring_interim"], within_days=120, bonus=8),
    Sequence(name="pressure_then_leadership", label="Pressure, then leadership change", first=["restructuring_layoffs", "cost_program"],
             then=["leadership_change", "open_exec_search"], within_days=120, bonus=8),
    Sequence(name="technology_then_hiring", label="Technology commitment, then hiring", first=["platform_migration", "ai_commitment"],
             then=["hiring_cluster", "hiring_leadership"], within_days=120, bonus=6),
]


@pytest.fixture
def seq_cfg(cfg):
    c = cfg.model_copy(deep=True)
    c.scoring = c.scoring.model_copy(update={"sequences": PATTERNS, "sequence_bonus_max": 15})
    return c


def test_off_by_default_so_the_reference_scores_do_not_move(cfg, as_of):
    assert cfg.scoring.sequences == []
    r = score_account([sig("leadership_change", 21), sig("hiring_cluster", 10), sig("pe_addon", 45)], as_of, cfg)
    assert r.score == 91 and r.sequence_bonus == 0 and r.components["sequences"] == {"bonus": 0, "cap": cfg.scoring.sequence_bonus_max, "matches": []}


def test_new_leader_then_hiring_adds_its_bonus_once(seq_cfg, as_of):
    r = score_account([sig("leadership_change", 21), sig("hiring_cluster", 10), sig("pe_addon", 45)], as_of, seq_cfg)
    assert r.sequence_bonus == 8 and r.score == 99                   # 91.4 + 8
    m = r.components["sequences"]["matches"]
    assert [x["name"] for x in m] == ["new_leader_then_hiring"] and m[0]["gap_days"] == 11
    assert m[0]["first"] == {"type": "leadership_change", "event_date": (AS_OF - timedelta(days=21)).isoformat()}
    assert "pattern bonus 8" in r.rationale


def test_order_matters(seq_cfg, as_of):
    r = score_account([sig("hiring_cluster", 21), sig("leadership_change", 10)], as_of, seq_cfg)      # hiring first, leader second
    assert r.sequence_bonus == 0 and r.components["sequences"]["matches"] == []


def test_the_window_is_enforced(seq_cfg, as_of):
    close = score_account([sig("platform_migration", 60), sig("hiring_leadership", 10)], as_of, seq_cfg)   # 50 days apart
    assert close.sequence_bonus == 6
    cfg2 = seq_cfg.model_copy(deep=True)
    cfg2.scoring = cfg2.scoring.model_copy(update={"sequences": [PATTERNS[2].model_copy(update={"within_days": 30})]})
    assert score_account([sig("platform_migration", 60), sig("hiring_leadership", 10)], as_of, cfg2).sequence_bonus == 0


def test_same_day_counts_and_both_signals_must_score(seq_cfg, as_of):
    assert score_account([sig("leadership_change", 10), sig("hiring_cluster", 10)], as_of, seq_cfg).sequence_bonus == 8
    low = score_account([sig("leadership_change", 20), sig("hiring_cluster", 10, conf="Low")], as_of, seq_cfg)   # Low confidence never scores
    assert low.sequence_bonus == 0
    old = score_account([sig("leadership_change", 400), sig("hiring_cluster", 300)], as_of, seq_cfg)             # outside every recency band
    assert old.sequence_bonus == 0 and old.score == 0


def test_a_pattern_counts_once_and_the_total_is_capped(seq_cfg, as_of):
    many = score_account([sig("leadership_change", 30), sig("leadership_change", 25), sig("hiring_cluster", 10),
                          sig("hiring_leadership", 8), sig("hiring_interim", 5)], as_of, seq_cfg)
    assert [m["name"] for m in many.components["sequences"]["matches"]] == ["new_leader_then_hiring"] and many.sequence_bonus == 8
    three = score_account([sig("restructuring_layoffs", 60), sig("open_exec_search", 40), sig("platform_migration", 30),
                           sig("hiring_leadership", 10), sig("leadership_change", 20), sig("hiring_cluster", 5)], as_of, seq_cfg)
    assert {m["name"] for m in three.components["sequences"]["matches"]} == {"new_leader_then_hiring", "pressure_then_leadership", "technology_then_hiring"}
    assert three.sequence_bonus == 15                                  # 8 + 8 + 6 = 22, capped at 15


def test_deterministic_pair_choice(seq_cfg, as_of):
    sigs = [sig("leadership_change", 50), sig("leadership_transformation_ai_data", 30), sig("hiring_cluster", 10), sig("hiring_interim", 20)]
    a = score_account(sigs, as_of, seq_cfg)
    b = score_account(list(reversed(sigs)), as_of, seq_cfg)
    assert a.components["sequences"] == b.components["sequences"]
    m = a.components["sequences"]["matches"][0]
    assert m["then"]["type"] == "hiring_cluster" and m["first"]["type"] == "leadership_transformation_ai_data" and m["gap_days"] == 20


def test_config_validation(cfg):
    base = cfg.scoring.model_dump()
    def bad(**seq):
        d = {"name": "x", "label": "X", "first": ["leadership_change"], "then": ["hiring_cluster"], "within_days": 30, "bonus": 5, **seq}
        with pytest.raises(ValueError):
            type(cfg.scoring)(**{**base, "sequences": [d]})
    bad(first=["nonsense"])
    bad(then=["leadership_change"])                  # overlaps first
    bad(first=["pe_hold_2_4"])                       # status signals have no date to order by
    bad(within_days=0)
    bad(bonus=0)
    bad(first=[])
    with pytest.raises(ValueError):
        d = {"name": "x", "label": "X", "first": ["leadership_change"], "then": ["hiring_cluster"], "within_days": 30, "bonus": 5}
        type(cfg.scoring)(**{**base, "sequences": [d, d]})                    # duplicate names


def test_pattern_shows_on_the_opportunity_and_is_stored(seq_cfg, tmp_path):
    conn = make_db(tmp_path)
    a = add_account(conn, "Acme")
    add_signal(conn, seq_cfg, a, "leadership_change", 30)
    add_signal(conn, seq_cfg, a, "hiring_cluster", 10)
    score_accounts(conn, seq_cfg, AS_OF)
    import json
    comp = json.loads(conn.execute("SELECT components_json FROM scores").fetchone()[0])
    assert comp["sequences"]["bonus"] == 8
    o = build_opportunities(conn, seq_cfg, AS_OF).items[0]
    pat = [r for r in o.reasons if r.kind == "sequence"]
    assert len(pat) == 1 and pat[0].label == "Pattern" and pat[0].points == 8 and "New leader, then hiring" in pat[0].headline
    assert "Leadership change" in pat[0].headline and "Hiring cluster" in pat[0].headline
