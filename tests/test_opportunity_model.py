"""The priority model. Items and accounts here are synthetic and live only in these tests."""

import random
from datetime import date, timedelta

import pytest

from bd.opportunity_model import (Context, Item, assess, assess_account, assess_company, cluster_events, firm_summary, rank_holdings, rules_text, timing_points)
from bd.pe_ownership import add_evidence, ensure_ownership, recompute, record_add_on, record_location, resolve_company
from bd.pe_store import ensure_firm
from tests.conftest import AS_OF, add_account, add_signal, make_db

TODAY = AS_OF


@pytest.fixture
def cfg(cfg):
    return cfg.model_copy(deep=True)


def it(kind, age=20, st="company_release", conf="High", url=None, n=[0], person=None, summary=None, verified=True, milestone=None, cats=None, unverified=False):
    n[0] += 1
    d = None if age is None else TODAY - timedelta(days=age)
    return Item(f"t:{n[0]}", kind, d, st, conf, url or f"https://src{n[0]}.example/{kind}", summary or f"{kind.replace('_', ' ')} happened.", person, verified,
                milestone, unverified, tuple(cats or ()))


CATS = {"platform_migration": ("Technology implementation and integration",), "vendor_deal": ("Technology implementation and integration",),
        "hiring_cluster": ("Data and analytics",), "deal_completed": ("Acquisition integration and separation", "Operating model and process improvement")}


def items(*specs):
    out = []
    for kind, age, *rest in specs:
        kw = rest[0] if rest else {}
        out.append(it(kind, age, cats=CATS.get(kind, ()), **kw))
    return out


def run(cfg, its, **ctx):
    return assess(its, TODAY, cfg, Context(**{"name": "Acme Corp", "last_scanned": "2026-10-10", **ctx}))


# ---- the arithmetic -----------------------------------------------------------------------------------------------------------------------------
def test_the_score_is_the_weighted_sum_of_anchored_dimensions(cfg):
    a = run(cfg, items(("platform_migration", 20)))
    d = {x.key: (x.points, x.contribution) for x in a.dimensions}
    assert d == {"need": (75, 22.5), "timing": (85, 21.2), "investment": (55, 11.0), "external_help": (60, 9.0), "evidence_quality": (90, 9.0)}
    assert a.score == round(22.5 + 21.25 + 11 + 9 + 9) == 73 and sum(x.weight for x in a.dimensions) == 100
    assert a.known_dimensions == 5 and a.known_weight == 100 and "specific initiative" in a.by_key("need").anchor


def test_a_dimension_nothing_speaks_to_is_unknown_not_zero_and_never_rescaled(cfg):
    a = run(cfg, items(("hiring_single", 5)))                                   # need and timing and quality only: it says nothing about spend or outside help
    assert a.by_key("investment").points is None and a.by_key("external_help").points is None
    assert a.by_key("investment").contribution == 0 and a.known_dimensions == 3 and a.known_weight == 65
    assert a.score == round(30 * 10 / 100 + 25 * 85 / 100 + 10 * 90 / 100) == 33               # not rescaled to 100 over the known weight (which would give 51)
    assert "Funding or procurement evidence: unknown" in a.unknowns and any(u.startswith("No evidence speaks to:") and "potential for external help" in u for u in a.unknowns)


def test_missing_evidence_can_only_lower_never_raise_a_score(cfg):
    base = items(("platform_migration", 20))
    more = base + items(("hiring_single", 20))
    assert run(cfg, more).score >= run(cfg, base).score
    assert run(cfg, []).score == 0 and run(cfg, []).category == "Insufficient evidence"


def test_timing_follows_the_age_the_source_states_and_an_unknown_date_is_unknown(cfg):
    pts = lambda age: timing_points(cluster_events([it("platform_migration", age)], cfg)[0], TODAY, cfg)
    assert [pts(a) for a in (0, 30, 31, 90, 150, 300, 500)] == [85, 85, 65, 65, 40, 15, 0]
    a = run(cfg, items(("platform_migration", None)))
    assert a.by_key("timing").points is None and "no date stated by its source" in " ".join(a.unknowns) or "has no date stated" in " ".join(a.unknowns)
    assert a.by_key("evidence_quality").points == 50                                            # an undated fact is capped


def test_a_stated_expected_closing_is_a_milestone_but_is_never_a_closing(cfg):
    soon = it("deal_announced", 40, milestone=TODAY + timedelta(days=45))
    later = it("deal_announced", 40, milestone=TODAY + timedelta(days=120))
    past = it("deal_announced", 40, milestone=TODAY - timedelta(days=5))
    assert timing_points(cluster_events([soon], cfg)[0], TODAY, cfg) == 100
    assert timing_points(cluster_events([later], cfg)[0], TODAY, cfg) == 80
    assert timing_points(cluster_events([past], cfg)[0], TODAY, cfg) == 65                       # a passed date is not a milestone: only the announcement's age counts


# ---- one event once ---------------------------------------------------------------------------------------------------------------------------
def test_syndicated_coverage_of_one_event_is_one_event_and_does_not_inflate_the_score(cfg):
    one = run(cfg, items(("platform_migration", 20)))
    syndicated = run(cfg, [it("platform_migration", 20, st="company_release"), it("platform_migration", 21, st="secondary_report"),
                           it("platform_migration", 22, st="secondary_report"), it("platform_migration", 20, st="secondary_report")])
    assert syndicated.events == 1 and syndicated.by_key("need").points == one.by_key("need").points
    assert syndicated.by_key("investment").points == one.by_key("investment").points
    assert syndicated.by_key("evidence_quality").points == 100 and one.by_key("evidence_quality").points == 90           # only corroboration raises quality, by a bounded bonus
    assert syndicated.score - one.score <= 1


def test_distinct_events_count_separately_with_a_credit_for_the_second(cfg):
    two = run(cfg, items(("platform_migration", 20), ("platform_migration", 60)))
    assert two.events == 2 and two.by_key("need").points == round(75 + 0.2 * 75)
    near = run(cfg, items(("platform_migration", 20), ("platform_migration", 24)))
    assert near.events == 1


def test_an_unrelated_second_event_adds_nothing_to_a_dimension_but_a_corroborating_one_does(cfg):
    trigger = [it("add_on_completed", 9, cats=("Acquisition integration and separation",))]
    unrelated = trigger + [it("hiring_cluster", 12, cats=("Data and analytics",))]
    assert run(cfg, unrelated).by_key("need").points == run(cfg, trigger).by_key("need").points == 45
    same = trigger + [it("pe_addon", 40, cats=("Acquisition integration and separation",))]
    assert run(cfg, same).by_key("need").points == round(45 + 0.2 * 40)
    a = run(cfg, unrelated)
    assert a.category == "Validate first"                                                         # an acquisition plus unrelated hiring is not a specific hypothesis by itself


def test_the_same_transaction_as_a_signal_and_as_a_ledger_deal_is_one_event(cfg):
    a = run(cfg, [it("pe_platform_or_sponsor_exec", 30), it("deal_completed", 28, cats=CATS["deal_completed"]), it("pe_addon", 10)])
    assert a.events == 2                                                                         # the platform deal once, the add-on once


def test_items_with_unknown_dates_of_one_kind_cannot_be_told_apart_so_they_count_once(cfg):
    assert run(cfg, [it("platform_migration", None), it("platform_migration", None), it("platform_migration", None)]).events == 1


# ---- what must not raise a score -----------------------------------------------------------------------------------------------------------------
def test_hiring_alone_cannot_become_prospect_now(cfg):
    for kind in ("hiring_cluster", "hiring_leadership", "hiring_interim", "hiring_single"):
        a = run(cfg, items((kind, 3)))
        assert a.category != "Prospect now" and a.by_key("need").points <= 45, kind
    a = run(cfg, items(("hiring_cluster", 3)))
    assert any(not g.ok and g.name == "specific need" for g in a.gates)


def test_a_completed_acquisition_is_a_research_trigger_not_a_need(cfg):
    a = run(cfg, [it("deal_completed", 10, cats=CATS["deal_completed"])])
    assert a.by_key("need").points == 30 and a.category == "Monitor" and "generic or weak" in a.rule
    assert a.hypotheses and "not that Acme Corp needs outside help, has a budget, or intends to buy" in a.hypotheses[0].text


def test_holding_duration_low_confidence_and_unverified_evidence_are_excluded_and_listed(cfg):
    a = run(cfg, [it("pe_hold_2_4", 30), it("platform_migration", 10, conf="Low"), it("vendor_deal", 10, verified=False), it("platform_migration", 20)])
    assert a.events == 1 and len(a.excluded) == 3
    assert any("holding" in why or "how long a sponsor" in why for _i, why in a.excluded) and sum("Low confidence" in why for _i, why in a.excluded) == 2


def test_nothing_depends_on_company_size_job_counts_or_pe_ownership(cfg, tmp_path):
    conn = make_db(tmp_path)
    small = add_account(conn, "Small Co", revenue=8e7, employees=120)
    big = add_account(conn, "Big Co", revenue=9e9, employees=90000, ownership="public")
    pe = add_account(conn, "Owned Co", revenue=8e7, ownership="pe-backed", sponsor="Alpha Capital")
    for a in (small, big, pe):
        add_signal(conn, cfg, a, "platform_migration", 20)
    with conn:
        fid = ensure_firm(conn, "Alpha Capital", "manual", "t", "alphacap.example")[0]
        cid = resolve_company(conn, "Owned Co", None, today=TODAY).id
        oid, _ = ensure_ownership(conn, fid, cid, TODAY)
        add_evidence(conn, "ownership", oid, "listed_current", "https://alphacap.example/p", retrieved_at="2026-10-10T00:00:00+00:00", source_type="firm_site",
                     verification="page_verified", content_hash="h", excerpt="Owned Co")
        recompute(conn, oid, TODAY, cfg, True)
    scores = {n: assess_account(conn, cfg, i, TODAY).score for n, i in (("small", small), ("big", big), ("pe", pe))}
    assert len(set(scores.values())) == 1, scores                                                  # identical evidence, identical priority


def test_scoring_is_deterministic_and_independent_of_input_order(cfg):
    its = items(("platform_migration", 20), ("vendor_deal", 40), ("hiring_cluster", 15), ("leadership_change", 70), ("deal_completed", 10))
    ref = run(cfg, its)
    for seed in range(5):
        shuffled = its[:]
        random.Random(seed).shuffle(shuffled)
        b = run(cfg, shuffled)
        assert (b.score, b.category, b.confidence, [(d.key, d.points) for d in b.dimensions]) == (ref.score, ref.category, ref.confidence, [(d.key, d.points) for d in ref.dimensions])


# ---- confidence is not priority --------------------------------------------------------------------------------------------------------------------
def test_a_strong_looking_but_thinly_sourced_opportunity_has_a_high_score_and_low_confidence(cfg):
    a = run(cfg, [it("rfp_match", None, st="secondary_report", cats=("Program and change management",))])
    assert a.score >= 40 and a.confidence == "Low"
    assert a.category == "Insufficient evidence" or a.category in ("Validate first", "Nurture", "Monitor")
    assert a.category != "Prospect now" and any("states no date" in r for r in a.confidence_reasons)


def test_corroboration_and_specificity_raise_confidence(cfg):
    one = run(cfg, items(("platform_migration", 20)))
    two = run(cfg, items(("platform_migration", 20), ("vendor_deal", 50)))
    assert one.confidence in ("High", "Medium") and any("specific" in r for r in one.confidence_reasons)
    assert any("point to the same category" in r for r in two.confidence_reasons)
    assert run(cfg, []).confidence == "Unknown"


# ---- categories and their rules --------------------------------------------------------------------------------------------------------------------
def test_prospect_now_needs_a_specific_current_supported_hypothesis_and_every_gate_is_shown(cfg):
    a = run(cfg, items(("platform_migration", 20), ("vendor_deal", 40)), geography="in_scope")
    assert a.category == "Prospect now" and all(g.ok for g in a.gates) and {g.name for g in a.gates} >= {"score", "specific need", "current", "supported", "coverage", "hypothesis"}
    assert "does not mean" not in a.rule and a.next_step and a.hypotheses[0].category == "Technology implementation and integration"
    stale = run(cfg, items(("platform_migration", 300)))
    assert stale.category == "Nurture" and "specific but not current" in stale.rule


def test_validate_first_names_the_gate_that_failed(cfg):
    a = run(cfg, [it("platform_migration", 20, st="secondary_report", cats=CATS["platform_migration"])])
    assert a.category == "Validate first" and "source quality" in a.rule and any(g.name == "source quality" and not g.ok for g in a.gates)
    assert "second, independent source" in a.next_step
    two = run(cfg, [it("platform_migration", 20, st="secondary_report", cats=CATS["platform_migration"]),
                    it("platform_migration", 21, st="secondary_report", cats=CATS["platform_migration"])])
    assert two.by_key("evidence_quality").points == 65 and two.category == "Validate first"      # corroboration helps, but two secondary sources are still not enough here


def test_counterevidence_as_new_as_the_support_caps_the_category_and_reduces_the_need(cfg):
    base = items(("platform_migration", 20), ("vendor_deal", 40))
    assert run(cfg, base).category == "Prospect now"
    counter = it("program_canceled", 5, summary="The company canceled its migration program.")
    a = run(cfg, base + [counter])
    assert a.category == "Validate first" and a.counter == [counter] and a.by_key("need").points < run(cfg, base).by_key("need").points
    assert any(g.name == "no counterevidence" and not g.ok for g in a.gates) and "Read the counter-signal first" in a.next_step
    old = run(cfg, base + [it("program_canceled", 400)])
    assert old.category == "Prospect now"                                                         # a cancellation older than the support does not undo it


def test_an_unverified_deal_ownership_blocks_prospect_now_and_lowers_confidence(cfg):
    ok = run(cfg, [it("deal_completed", 10, cats=CATS["deal_completed"]), it("platform_migration", 15, cats=CATS["platform_migration"], unverified=False)])
    bad = run(cfg, [it("platform_migration", 15, cats=CATS["platform_migration"], unverified=True)])
    assert not any(g.name == "ownership verified" and not g.ok for g in ok.gates)
    assert any(g.name == "ownership verified" and not g.ok for g in bad.gates) and bad.category != "Prospect now"


def test_the_category_rules_are_exposed_and_follow_the_configuration(cfg):
    text = " ".join(rules_text(cfg))
    assert "score at least 50" in text and "does not mean confirmed buying intent" in text and "Validate first" in text
    base = items(("platform_migration", 20), ("vendor_deal", 40))
    assert run(cfg, base).category == "Prospect now"
    cfg.opportunity_model.categories["prospect_now"]["min_score"] = 99
    assert run(cfg, base).category == "Validate first" and "score at least 99" in " ".join(rules_text(cfg))


def test_an_out_of_scope_company_cannot_be_prospect_now(cfg):
    a = run(cfg, items(("platform_migration", 20), ("vendor_deal", 40)), geography="out_of_scope")
    assert a.category != "Prospect now" and any(g.name == "in configured geography" and not g.ok for g in a.gates)


# ---- the brief's contents --------------------------------------------------------------------------------------------------------------------------
def test_facts_hypotheses_unknowns_and_next_steps_are_kept_apart(cfg):
    a = run(cfg, [it("platform_migration", 20, person="Pat Lee, CIO", summary="Announces a cloud migration of its claims system.", cats=CATS["platform_migration"])])
    assert a.facts[0].summary == "Announces a cloud migration of its claims system." and a.facts[0].event_date == TODAY - timedelta(days=20)
    assert a.hypotheses[0].text.startswith("Hypothesis:") and "technology or IT leadership" == a.owner_function
    assert a.named_people == ["Pat Lee, CIO"] and "Work likely owned by the company itself" in a.hypotheses[0].initiative_owner
    assert "No executive is named" not in " ".join(a.unknowns)
    q = a.discussion_question
    assert "Announces a cloud migration of its claims system" in q and "assumes no problem, budget or intent" in q and "?" in q
    none = run(cfg, items(("platform_migration", 20)))
    assert "No executive is named in the evidence" in " ".join(none.unknowns) and "inferred" in none.next_step


def test_hiring_signals_take_their_category_from_the_area_in_the_summary(cfg, tmp_path):
    conn = make_db(tmp_path)
    a = add_account(conn, "Hirer Co")
    add_signal(conn, cfg, a, "hiring_cluster", 10, summary="4 related data and analytics job postings within 60 days, including Data Engineer.")
    add_signal(conn, cfg, a, "hiring_leadership", 12, summary="Senior mystery leadership role posted: Head of Things.")
    res = assess_account(conn, cfg, a, TODAY)
    assert [h.category for h in res.hypotheses] == ["Data and analytics"]                          # the unknown area suggests no category


# ---- loading from the ledger ------------------------------------------------------------------------------------------------------------------------
def ledger(conn, cfg, claim="completed", event="2026-09-20", url="https://newsroom.example/acme", st="company_release", verification="page_verified", name="Acme Corp",
           expected=None):
    with conn:
        fid = ensure_firm(conn, "Alpha Capital", "manual", "t", "alphacap.example")[0]
        cid = resolve_company(conn, name, None, today=TODAY).id
        oid, _ = ensure_ownership(conn, fid, cid, TODAY, "platform", "announcement")
        add_evidence(conn, "ownership", oid, claim, url, retrieved_at="2026-10-10T00:00:00+00:00", source_type=st, verification=verification,
                     content_hash="h" if verification == "page_verified" else "", excerpt="Alpha Capital completed its acquisition of Acme Corp." if verification == "page_verified" else None,
                     event=event)
        if expected:
            add_evidence(conn, "ownership", oid, "expected_close", url, retrieved_at="2026-10-10T00:00:00+00:00", source_type=st, verification="page_verified",
                         content_hash="h2", excerpt="expected to close", event=expected)
        recompute(conn, oid, TODAY, cfg, True)
    return fid, cid, oid


def test_ledger_deals_become_evidence_but_snippets_do_not(cfg, tmp_path):
    conn = make_db(tmp_path)
    fid, cid, oid = ledger(conn, cfg)
    a = assess_company(conn, cfg, cid, TODAY)
    assert a.events == 1 and a.facts[0].kind == "deal_completed" and a.facts[0].event_date == date(2026, 9, 20) and a.category == "Monitor"
    with conn:
        c2 = resolve_company(conn, "Snippet Co", None, today=TODAY).id
        o2, _ = ensure_ownership(conn, fid, c2, TODAY)
        add_evidence(conn, "ownership", o2, "completed", "https://news.example/s", retrieved_at="2026-10-10T00:00:00+00:00", source_type="search_snippet",
                     verification="snippet_only", event="2026-09-01")
    s = assess_company(conn, cfg, c2, TODAY)
    assert s.events == 0 and s.category == "Insufficient evidence" and "nothing to act on" in s.next_step


def test_a_pending_deal_with_a_stated_expected_close_has_a_milestone(cfg, tmp_path):
    conn = make_db(tmp_path)
    fid, cid, oid = ledger(conn, cfg, claim="announced", event="2026-10-01", expected="2026-11-20")
    a = assess_company(conn, cfg, cid, TODAY)
    assert a.facts[0].kind == "deal_announced" and a.facts[0].milestone == date(2026, 11, 20) and a.by_key("timing").points == 100
    assert conn.execute("SELECT status FROM pe_ownership WHERE id = ?", (oid,)).fetchone()[0] == "pending"


def test_unclear_or_stale_ownership_is_flagged_on_the_deal_evidence_and_in_the_unknowns(cfg, tmp_path):
    conn = make_db(tmp_path)
    fid, cid, oid = ledger(conn, cfg)
    conn.execute("UPDATE pe_ownership SET last_verified = '2025-01-01'")
    a = assess_company(conn, cfg, cid, TODAY)
    assert any("past the 120-day window" in u for u in a.unknowns) and any("ownership behind the deal" in r for r in a.confidence_reasons)


def test_add_ons_are_evidence_for_the_acquiring_company_only(cfg, tmp_path):
    conn = make_db(tmp_path)
    with conn:
        parent = resolve_company(conn, "Platform Co", None, today=TODAY).id
        child = resolve_company(conn, "Bolt Inc", None, today=TODAY).id
        lid = record_add_on(conn, parent, child, TODAY, closed="2026-09-25", completed=True)
        add_evidence(conn, "link", lid, "add_on", "https://newsroom.example/bolt", retrieved_at="2026-10-10T00:00:00+00:00", source_type="company_release",
                     verification="page_verified", content_hash="h", excerpt="Platform Co completed its acquisition of Bolt Inc.", event="2026-09-25")
    p, c = assess_company(conn, cfg, parent, TODAY), assess_company(conn, cfg, child, TODAY)
    assert p.facts[0].kind == "add_on_completed" and p.by_key("need").points == 45 and p.hypotheses[0].category == "Acquisition integration and separation"
    assert c.events == 0


# ---- portfolios ----------------------------------------------------------------------------------------------------------------------------------------
def test_holdings_rank_by_what_the_evidence_supports_and_filters_follow_status_and_geography(cfg, tmp_path):
    conn = make_db(tmp_path)
    with conn:
        fid = ensure_firm(conn, "Alpha Capital", "manual", "t", "alphacap.example")[0]
    names = {}
    for name, sigs in (("Strong Co", [("platform_migration", 20), ("vendor_deal", 40)]), ("Weak Co", [("hiring_single", 20)]), ("Quiet Co", [])):
        acct = add_account(conn, name, chicagoland="yes")
        for t, age in sigs:
            add_signal(conn, cfg, acct, t, age, summary=f"{t} happened.")
        with conn:
            cid = resolve_company(conn, name, None, today=TODAY).id
            oid, _ = ensure_ownership(conn, fid, cid, TODAY)
            add_evidence(conn, "ownership", oid, "listed_current", "https://alphacap.example/p", retrieved_at="2026-10-10T00:00:00+00:00", source_type="firm_site",
                         verification="page_verified", content_hash=name, excerpt=name)
            recompute(conn, oid, TODAY, cfg, True)
        names[name] = cid
    with conn:
        c4 = resolve_company(conn, "Texas Co", None, today=TODAY).id
        o4, _ = ensure_ownership(conn, fid, c4, TODAY)
        add_evidence(conn, "ownership", o4, "listed_historical", "https://alphacap.example/p", retrieved_at="2026-10-10T00:00:00+00:00", source_type="firm_site",
                     verification="page_verified", content_hash="t", excerpt="Texas Co")
        recompute(conn, o4, TODAY, cfg, True)
        record_location(conn, "company", c4, "headquarters", "Austin", "TX", None, TODAY)
    held = rank_holdings(conn, cfg, fid, TODAY)
    assert [h.company["name"] for h in held] == ["Strong Co", "Weak Co", "Quiet Co"] and held[0].assessment.category == "Prospect now"
    assert held[2].assessment.category == "Insufficient evidence"                                    # nobody is forced to have an opportunity
    assert [h.company["name"] for h in rank_holdings(conn, cfg, fid, TODAY, statuses=("historical",))] == ["Texas Co"]
    assert [h.company["name"] for h in rank_holdings(conn, cfg, fid, TODAY, geography="out_of_scope", statuses=("historical",))] == ["Texas Co"]
    assert rank_holdings(conn, cfg, fid, TODAY, geography="out_of_scope") == []
    s = firm_summary(conn, cfg, fid, TODAY)
    assert s.by_status == {"current": 3, "historical": 1} and s.by_category == {"Prospect now": 1, "Monitor": 1, "Insufficient evidence": 1} and s.assessed == 3
    assert "No portfolio page is registered" in s.coverage_note
    assert not hasattr(s, "score") and not hasattr(s, "total_score")                                  # no sponsor-level sum of scores


def test_the_sponsor_summary_discloses_partial_coverage(cfg, tmp_path):
    conn = make_db(tmp_path)
    with conn:
        fid = ensure_firm(conn, "Alpha Capital", "manual", "t", "alphacap.example", None, "https://alphacap.example/portfolio")[0]
        conn.execute("INSERT INTO pe_sources(firm_id, kind, url, status, coverage, coverage_note) VALUES (?, 'portfolio_page', 'https://alphacap.example/portfolio', 'ok', 'partial', 'stopped after 12 pages')", (fid,))
    note = firm_summary(conn, cfg, fid, TODAY).coverage_note
    assert note.startswith("Partial coverage") and "stopped after 12 pages" in note
    conn.execute("UPDATE pe_sources SET coverage = 'complete'")
    assert "read to its end" in firm_summary(conn, cfg, fid, TODAY).coverage_note


def test_an_authoritative_announcement_establishes_the_facts_not_the_need(cfg):
    deal = run(cfg, [it("deal_completed", 10, st="sec_filing", cats=CATS["deal_completed"])])
    assert deal.fact_confidence == "High" and deal.confidence == "Low"                                    # a filing proves the transaction, not a consulting need
    assert any("supports only low confidence in a consulting need" in r for r in deal.confidence_reasons)
    mid = run(cfg, [it("ai_commitment", 10, cats=("AI implementation and adoption",))])                     # need anchor 60
    assert mid.fact_confidence == "High" and mid.confidence == "Medium"
    top = run(cfg, items(("platform_migration", 10)))                                                      # need anchor 75
    assert top.fact_confidence == "High" and top.confidence == "High"
    weak = run(cfg, [it("platform_migration", 10, st="secondary_report", cats=CATS["platform_migration"])])
    assert weak.fact_confidence == "Medium" and weak.confidence == "Medium"                                # a thin source limits both


def test_next_steps_read_as_sentences(cfg):
    a = run(cfg, items(("platform_migration", 20)))
    assert "Identify who is responsible for this work (technology or IT leadership) from the company's own leadership page" in a.next_step
    assert a.next_step.endswith(".") and "; Identify" not in a.next_step


# ---- events at someone else's place: a subsidiary's brief must not rest on the parent's plant in India or its factory in England -----------------------
def mccain_like(cfg, ai_age=96):
    """The shape of a real brief: an acquisition in India (company site) and a restructuring in England (secondary report), both recent, and an older AI program with no place."""
    return [it("ma_divestiture", 8, st="company_site", summary="McCain has reached an agreement to take over India's Falcon Agrifriz frozen potato plant in Gujarat; financial terms were not disclosed.",
               cats=("Acquisition integration and separation",)),
            it("restructuring_layoffs", 4, st="secondary_report", conf="Medium", summary="Staff at McCain's Whittlesey factory in England were warned that jobs could go under a planned restructuring.",
               cats=("Operating model and process improvement",)),
            it("ai_commitment", ai_age, st="secondary_report", conf="Medium", summary="Pilot grower program supported by Ceres AI gives teams and North American growers shared crop insights.",
               cats=("AI implementation and adoption",), person="Jeremy Buchman, director of agronomy")]


def test_outside_places_are_whole_words_and_new_mexico_is_not_mexico(cfg):
    from bd.opportunity_model import outside_places
    assert outside_places("takes over India's plant; staff in England", cfg) == ("India", "England")
    assert outside_places("Indiana and New Mexico and Englewood and Chinatown", cfg) == ()
    assert outside_places("a plant in Mexico", cfg) == ("Mexico",) and outside_places("the UK and Northern Ireland", cfg) == ("UK", "Northern Ireland")


def test_when_every_specific_current_event_is_at_a_place_outside_the_area_it_is_not_prospect_now_and_says_why(cfg):
    a = run(cfg, mccain_like(cfg))
    gate = {g.name: g for g in a.gates}["its own area"]
    assert not gate.ok and "India" in gate.detail and "England" in gate.detail and "parent or an affiliate" in gate.detail
    assert a.category == "Validate first" and "its own area" in a.rule
    assert any(u.startswith("2 of 3 scored events name England, India:") for u in a.unknowns)
    assert a.next_step.startswith("Confirm that the events in England, India involve this company's own operations")
    assert a.discussion_question == ""                                                              # no question built on an event that may concern someone else
    assert all("parent or an affiliate" in h.initiative_owner for h in a.hypotheses if h.category != "AI implementation and adoption")
    assert a.score == run(cfg, [replace_places_none(i) for i in mccain_like(cfg)]).score             # the score itself is untouched: it is the category that changes


def replace_places_none(i):
    from dataclasses import replace
    return replace(i, summary=i.summary.replace("India's", "the").replace("England", "Ohio"))


def test_the_same_events_without_outside_places_are_prospect_now_as_before(cfg):
    clean = [replace_places_none(i) for i in mccain_like(cfg)]
    a = run(cfg, clean)
    assert {g.name: g.ok for g in a.gates}["its own area"] and a.category == "Prospect now" and a.discussion_question


def test_one_specific_current_event_at_home_is_enough(cfg):
    from dataclasses import replace
    its = mccain_like(cfg)
    its.append(it("platform_migration", 12, st="company_release", summary="Acme moves its billing platform to a cloud provider.", cats=("Technology implementation and integration",)))
    a = run(cfg, its)
    assert {g.name: g.ok for g in a.gates}["its own area"] and a.category == "Prospect now"
    assert any(u.startswith("2 of 4 scored events name England, India:") for u in a.unknowns)               # still said: some events name places outside


def test_the_question_is_built_from_the_event_the_hypothesis_rests_on_not_the_newest_fact(cfg):
    its = [it("platform_migration", 25, summary="Acme moves its billing platform to a cloud provider.", cats=("Technology implementation and integration",)),
           it("hiring_cluster", 3, summary="Acme posts 5 data roles.", cats=("Data and analytics",), conf="High")]
    a = run(cfg, its)
    first = a.hypotheses[0]
    assert first.category == "Technology implementation and integration"                                  # the strongest need leads
    assert "billing platform" in a.discussion_question and "data roles" not in a.discussion_question        # not the newest fact (the hiring cluster)
    assert "technology implementation and integration" in a.discussion_question.lower()


def test_the_rules_text_names_the_new_condition(cfg):
    assert any("place outside it" in line and "parent's or an affiliate's" in line for line in rules_text(cfg))
