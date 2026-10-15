from datetime import timedelta

import pytest

from bd.score_store import score_accounts
from bd.shortlist import ShortlistError, build_shortlist, mark_posted
from tests.conftest import AS_OF, add_account, add_signal, make_db


@pytest.fixture
def conn(tmp_path):
    c = make_db(tmp_path)
    yield c
    c.close()


def hot(conn, cfg, name="Hotco", **kw):
    a = add_account(conn, name, **kw)
    for t, age in (("leadership_change", 21), ("hiring_cluster", 10), ("pe_addon", 45)):
        add_signal(conn, cfg, a, t, age, url=f"https://{name.lower()}.com/{t}")
    return a


def warm(conn, cfg, name="Warmco"):
    a = add_account(conn, name)
    add_signal(conn, cfg, a, "leadership_change", 5)
    add_signal(conn, cfg, a, "restructuring_layoffs", 5)    # 30 + 24 + 10 = 64
    return a


def test_requires_scores_first(cfg, conn):
    hot(conn, cfg)
    with pytest.raises(ShortlistError, match="bd score"):
        build_shortlist(conn, cfg, AS_OF)


def test_hot_and_warm_with_high_confidence_are_leads_ranked_by_score(cfg, conn):
    warm(conn, cfg)
    hot(conn, cfg)
    score_accounts(conn, cfg, AS_OF)
    sl = build_shortlist(conn, cfg, AS_OF)
    assert [(l.account.name, l.result.score, l.result.band) for l in sl.leads] == [("Hotco", 91, "Hot"), ("Warmco", 64, "Warm")]
    top = sl.leads[0].signals
    assert [s.type for s in top] == ["leadership_change", "hiring_cluster", "pe_addon"]   # strongest first
    assert top[0].url == "https://hotco.com/leadership_change" and top[0].points == 30


def test_watch_band_and_medium_only_accounts_are_skipped_with_reasons(cfg, conn):
    w = add_account(conn, "Watchco")
    add_signal(conn, cfg, w, "leadership_change", 5)
    add_signal(conn, cfg, w, "risk_factor", 5)                              # 48 -> Watch
    m = add_account(conn, "Mediumco")
    for t, age in (("leadership_change", 5), ("hiring_cluster", 5), ("pe_addon", 5)):
        add_signal(conn, cfg, m, t, age, conf="Medium")                      # Hot/Warm score, no High evidence
    old = add_account(conn, "Oldhighco")
    for t, age in (("leadership_change", 5), ("hiring_cluster", 5), ("pe_addon", 5)):
        add_signal(conn, cfg, old, t, age, conf="Medium")
    add_signal(conn, cfg, old, "restructuring_layoffs", 120, conf="High")    # High, but outside 90 days
    score_accounts(conn, cfg, AS_OF)
    sl = build_shortlist(conn, cfg, AS_OF)
    assert sl.leads == []
    reasons = {s.account.name: s.reason for s in sl.skipped}
    assert reasons["Watchco"] == "band Watch is not posted"
    assert reasons["Mediumco"].startswith("no High-confidence signal in the last 90 days")
    assert reasons["Oldhighco"].startswith("no High-confidence signal")


def test_cap_and_overflow(cfg, conn):
    for i in range(13):
        a = add_account(conn, f"Co{i:02d}")
        add_signal(conn, cfg, a, "leadership_change", 5)
        add_signal(conn, cfg, a, "restructuring_layoffs", 5 + i)             # all Warm, scores vary slightly? same band
    score_accounts(conn, cfg, AS_OF)
    sl = build_shortlist(conn, cfg, AS_OF)
    assert len(sl.leads) == 10 and len(sl.overflow) == 3
    scores = [l.result.score for l in sl.leads]
    assert scores == sorted(scores, reverse=True)
    assert min(scores) >= max(l.result.score for l in sl.overflow)


def test_shortlist_is_read_only_and_dry_runs_do_not_start_cooldowns(cfg, conn):
    hot(conn, cfg)
    score_accounts(conn, cfg, AS_OF)
    build_shortlist(conn, cfg, AS_OF)
    build_shortlist(conn, cfg, AS_OF)
    row = conn.execute("SELECT posted, cooldown_until FROM scores").fetchone()
    assert (row["posted"], row["cooldown_until"]) == (0, None)
    assert len(build_shortlist(conn, cfg, AS_OF).leads) == 1


def test_mark_posted_starts_four_week_cooldown_and_blocks_same_day_repost(cfg, conn):
    hot(conn, cfg)
    score_accounts(conn, cfg, AS_OF)
    sl = build_shortlist(conn, cfg, AS_OF)
    mark_posted(conn, sl.leads, AS_OF, cfg)
    row = conn.execute("SELECT posted, cooldown_until FROM scores").fetchone()
    assert (row["posted"], row["cooldown_until"]) == (1, (AS_OF + timedelta(weeks=4)).isoformat())
    again = build_shortlist(conn, cfg, AS_OF)
    assert again.leads == [] and "in cooldown until 2026-11-12" in again.skipped[0].reason


def test_cooldown_holds_next_week_then_breaks_on_new_family_or_score_rise_or_expiry(cfg, conn):
    a = add_account(conn, "Hotco")
    add_signal(conn, cfg, a, "leadership_change", 5)
    add_signal(conn, cfg, a, "restructuring_layoffs", 5)                      # 64 Warm, families leadership+events
    score_accounts(conn, cfg, AS_OF)
    mark_posted(conn, build_shortlist(conn, cfg, AS_OF).leads, AS_OF, cfg)

    wk2 = AS_OF + timedelta(days=7)
    score_accounts(conn, cfg, wk2)
    sl = build_shortlist(conn, cfg, wk2)
    assert sl.leads == [] and "cooldown" in sl.skipped[0].reason                # nothing new -> held back

    add_signal(conn, cfg, a, "hiring_cluster", 1, as_of=wk2)                    # new family (hiring) appears
    score_accounts(conn, cfg, wk2)
    assert [l.account.name for l in build_shortlist(conn, cfg, wk2).leads] == ["Hotco"]

    wk5 = AS_OF + timedelta(weeks=4)                                            # cooldown expired
    conn.execute("DELETE FROM signals WHERE type='hiring_cluster'")
    conn.commit()
    score_accounts(conn, cfg, wk5)
    # Cooldown has expired (boundary day). The account has simply decayed to Watch by week 5, so the
    # reason it is not listed is its band, not the cooldown.
    sl = build_shortlist(conn, cfg, wk5)
    assert [s.reason for s in sl.skipped] == ["band Watch is not posted"]


def test_score_rise_of_15_breaks_cooldown(cfg, conn):
    a = add_account(conn, "Hotco")
    add_signal(conn, cfg, a, "leadership_change", 5)
    add_signal(conn, cfg, a, "restructuring_layoffs", 5)                       # 64
    score_accounts(conn, cfg, AS_OF)
    mark_posted(conn, build_shortlist(conn, cfg, AS_OF).leads, AS_OF, cfg)
    wk2 = AS_OF + timedelta(days=7)
    add_signal(conn, cfg, a, "leadership_transformation_ai_data", 1, as_of=wk2)   # same family; +12.5 -> 77
    add_signal(conn, cfg, a, "open_exec_search", 1, as_of=wk2)                    # +10 -> 87, rise >= 15
    score_accounts(conn, cfg, wk2)
    assert len(build_shortlist(conn, cfg, wk2).leads) == 1


def test_stale_scores_are_detected(cfg, conn):
    a = hot(conn, cfg)
    score_accounts(conn, cfg, AS_OF)
    add_signal(conn, cfg, a, "restructuring_layoffs", 1)                        # signals changed after bd score
    with pytest.raises(ShortlistError, match="re-run"):
        build_shortlist(conn, cfg, AS_OF)


def test_account_no_longer_active_is_not_listed(cfg, conn):
    a = hot(conn, cfg)
    score_accounts(conn, cfg, AS_OF)
    conn.execute("UPDATE accounts SET status='excluded' WHERE id=?", (a,))
    conn.commit()
    assert build_shortlist(conn, cfg, AS_OF).leads == []
