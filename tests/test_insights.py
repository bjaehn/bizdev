from datetime import date, timedelta

import pytest

from bd.insights import RULES, THEME_NAMES, Evidence, best_service_line, evidence_from_scored, insights_for, main_theme
from bd.labels import TYPE_LABELS
from bd.models import SIGNAL_TYPES
from bd.opportunities import build_opportunities
from bd.score_store import score_accounts
from tests.conftest import AS_OF, add_account, add_signal, make_db


def ev(id, type_, points=10.0, age=5, lines=("Strategy & Operations",), family="events"):
    return Evidence(id, type_, family, AS_OF - timedelta(days=age), f"{type_} summary", f"https://example.com/{id}", "company_release", "High",
                    None, tuple(lines), points)


def test_every_signal_type_belongs_to_at_least_one_single_signal_theme():
    singles = {t for r in RULES if len(r.needs) == 1 for t in r.needs[0].types}
    assert set(SIGNAL_TYPES) == singles, f"no theme for {sorted(set(SIGNAL_TYPES) - singles)}"
    assert all(t in TYPE_LABELS for t in singles)


def test_each_lone_signal_type_gives_a_theme_so_any_scored_company_has_one():
    for t in SIGNAL_TYPES:
        theme = main_theme([ev(1, t)])
        assert theme is not None and len(theme.evidence) == 1 and theme.evidence[0].type == t, t


def test_a_leadership_signal_and_a_hiring_signal_make_the_compound_theme_with_both_as_evidence():
    themes = insights_for([ev(1, "leadership_change", 30, family="leadership"), ev(2, "hiring_cluster", 12, family="hiring"),
                           ev(3, "restructuring_layoffs", 5)])
    main = themes[0]
    assert main.key == "leadership_hiring" and main.name == "New leadership and a hiring push" and main.compound
    assert [e.type for e in main.evidence] == ["leadership_change", "hiring_cluster"] and main.kinds == ["Leadership", "Hiring"]
    assert main.rule == "Shown when a leadership signal and a hiring signal are both found"
    assert {t.key for t in themes} >= {"turnaround", "leadership_transition", "team_buildout", "operating_pressure"}   # the others still match


def test_compound_themes_outrank_single_ones_even_when_the_single_signal_is_stronger():
    themes = insights_for([ev(1, "restructuring_layoffs", 60), ev(2, "hiring_cluster", 5, family="hiring"), ev(3, "ma_divestiture", 8)])
    assert themes[0].key == "deal_integration" and {e.id for e in themes[0].evidence} == {3, 1}      # the 60-point single theme ranks below it
    assert themes[1].key == "operating_pressure" and themes[1].points == 60
    assert not any(t.key == "turnaround" for t in themes)           # pressure alone with hiring (not interim) is not a turnaround


def test_two_compound_themes_are_ordered_by_the_points_of_their_evidence():
    themes = insights_for([ev(1, "leadership_change", 30, family="leadership"), ev(2, "hiring_cluster", 12, family="hiring"),
                           ev(3, "restructuring_layoffs", 20)])
    assert [t.key for t in themes[:2]] == ["turnaround", "leadership_hiring"]                  # 50 points against 42


def test_one_signal_cannot_satisfy_two_requirements_of_a_rule():
    # a lone leadership signal is in both sets of "turnaround" but needs a pressure signal too
    assert [t.key for t in insights_for([ev(1, "leadership_change", 30, family="leadership")])] == ["leadership_transition"]
    # a lone interim hire is not enough for "turnaround" either
    assert "turnaround" not in {t.key for t in insights_for([ev(1, "hiring_interim", 9, family="hiring")])}
    both = insights_for([ev(1, "restructuring_layoffs", 20), ev(2, "hiring_interim", 9, family="hiring")])
    assert both[0].key == "turnaround" and {e.id for e in both[0].evidence} == {1, 2}


def test_the_scarcest_requirement_is_filled_first_so_a_shared_signal_is_not_wasted():
    # deal_integration needs a deal AND (leadership|hiring|pressure); only one signal (id 2) fits the second set besides the deal itself
    themes = insights_for([ev(1, "ma_divestiture", 50), ev(2, "restructuring_layoffs", 5)])
    assert themes[0].key == "deal_integration" and {e.id for e in themes[0].evidence} == {1, 2}


def test_more_counts_the_other_signals_of_the_same_kinds_that_are_not_listed():
    themes = insights_for([ev(1, "hiring_cluster", 12, family="hiring"), ev(2, "hiring_single", 6, family="hiring"), ev(3, "hiring_interim", 5, family="hiring")])
    assert themes[0].key == "team_buildout" and themes[0].more == 2 and themes[0].evidence[0].id == 1


def test_service_line_is_the_one_the_evidence_was_tagged_with_most_strongly_and_none_when_untagged():
    e = [ev(1, "platform_migration", 20, lines=("AI & Technology",)), ev(2, "hiring_cluster", 12, lines=("Data & Analytics", "AI & Technology"), family="hiring")]
    assert best_service_line(e) == "AI & Technology"
    assert best_service_line([ev(1, "hiring_single", 5, lines=())]) is None
    # equal weight: the configured order breaks the tie
    assert best_service_line([ev(1, "x", 10, lines=("B",)), ev(2, "y", 10, lines=("A",))], order=("B", "A")) == "B"
    assert main_theme([ev(1, "platform_migration", 20, lines=("AI & Technology",))], ("AI & Technology",)).service_line == "AI & Technology"


def test_no_evidence_means_no_theme():
    assert insights_for([]) == [] and main_theme([]) is None


def test_theme_names_are_unique_and_plain():
    assert len(THEME_NAMES) == len(RULES) and len(set(THEME_NAMES.values())) == len(RULES)
    assert all(r.rule.startswith("Shown when a ") for r in RULES)


# ---- on real stored signals ------------------------------------------------------------------------------------------------------
@pytest.fixture
def cfg(cfg):
    return cfg.model_copy(deep=True)


def test_opportunities_carry_a_theme_built_only_from_scored_stored_signals(cfg, tmp_path):
    conn = make_db(tmp_path)
    a = add_account(conn, "Acme")
    add_signal(conn, cfg, a, "leadership_change", 10, summary="Acme names a new COO.", lines=("Strategy & Operations",))
    add_signal(conn, cfg, a, "hiring_cluster", 12, summary="Acme posts 9 data roles.", lines=("Data & Analytics",))
    add_signal(conn, cfg, a, "restructuring_layoffs", 4, conf="Low", summary="Rumoured layoffs.", url="https://gossip.example/acme")   # stored, never scored
    score_accounts(conn, cfg, AS_OF)
    o = build_opportunities(conn, cfg, AS_OF, include_tracked_only=True).items[0]
    assert o.theme.key == "leadership_hiring"
    assert {e.summary for e in o.theme.evidence} == {"Acme names a new COO.", "Acme posts 9 data roles."}
    assert all(e.url.startswith("https://example-") and e.confidence == "High" for e in o.theme.evidence)
    assert "Rumoured layoffs." not in {e.summary for t in o.themes for e in t.evidence}      # a Low-confidence rumour is never evidence
    assert o.theme.service_line in ("Strategy & Operations", "Data & Analytics")


def test_evidence_from_scored_skips_signals_that_did_not_score(cfg, tmp_path):
    from bd.accounts import list_accounts
    from bd.score_store import score_one
    conn = make_db(tmp_path)
    a = add_account(conn, "Acme")
    add_signal(conn, cfg, a, "leadership_change", 10)
    add_signal(conn, cfg, a, "hiring_cluster", 400)                                          # older than the last recency band: scores nothing
    sa = score_one(conn, cfg, list_accounts(conn)[0], AS_OF)
    assert [e.type for e in evidence_from_scored(sa.result, sa.rows)] == ["leadership_change"]
