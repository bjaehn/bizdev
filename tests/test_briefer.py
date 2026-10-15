import json

import pytest
from pydantic import ValidationError

from bd.briefer import Brief, BriefResponse, Briefer, fallback_brief, get_briefs
from bd.llm import LLMError, Usage
from bd.score_store import score_accounts
from bd.shortlist import build_shortlist
from tests.conftest import AS_OF, FakeLLM, add_account, add_signal, make_db

CTX = {"max_words": 30, "banned_phrases": ["reach out", "SEI"], "service_lines": ["AI & Technology", "Strategy & Operations"],
       "grounding": json.dumps({"summary": "Acme names new CIO 21 days ago", "days_ago": 21, "event_date": "2026-09-24"})}


def val(**over):
    d = {"why_now": "Acme named a new CIO 21 days ago.", "best_service_line": "AI & Technology"}
    d.update(over)
    return BriefResponse.model_validate(d, context=CTX)


def test_valid_brief_and_service_line_canonicalised():
    assert val(best_service_line=" ai & technology ").best_service_line == "AI & Technology"


@pytest.mark.parametrize("why,msg", [
    ("Acme named a new CIO. It also hired a CFO.", "ONE sentence"),
    ("Acme named a CIO!\nThen more.", "single non-empty line"),
    (" ".join(["word"] * 31), "at most 30 words"),
    ("Acme named a new CIO, so reach out now.", "outreach or advice"),
    ("Acme named a new CIO; a fit for SEI.", "outreach or advice"),
    ("Acme named a new CIO 45 days ago.", "not in the input"),
    ("Acme invested $300 million after naming a CIO.", "not in the input"),
    ("", "single non-empty line"),
])
def test_why_now_rejections(why, msg):
    with pytest.raises(ValidationError, match=msg):
        val(why_now=why)


def test_figures_cannot_borrow_digits_from_dates():
    ctx = {**CTX, "grounding": json.dumps({"event_date": "2026-10-05", "days_ago": 10, "summary": "Cut 300 jobs."})}
    ok = {"why_now": "Cut 300 jobs on Oct. 5.", "best_service_line": "AI & Technology"}
    assert BriefResponse.model_validate(ok, context=ctx)
    for bad in ("Spent $5 billion on Oct. 5.", "Cut 5 jobs.", "Cut 10% of staff.", "Raised $10 million."):
        with pytest.raises(ValidationError, match="figure"):
            BriefResponse.model_validate({**ok, "why_now": bad}, context=ctx)


def test_numbers_present_in_input_are_allowed_and_leading_zeros_normalised():
    assert val(why_now="Acme named a new CIO on Sept. 24, 21 days ago.")
    ctx = {**CTX, "grounding": json.dumps({"event_date": "2026-09-09"})}
    assert BriefResponse.model_validate({"why_now": "Named on Sept. 9.", "best_service_line": "AI & Technology"}, context=ctx)


def test_abbreviations_do_not_look_like_two_sentences():
    assert val(why_now="Acme Inc. named a new CIO 21 days ago.")


def test_service_line_must_be_configured():
    with pytest.raises(ValidationError, match="exactly one of"):
        val(best_service_line="Cloud Migration")


# ---- Briefer over real leads ---------------------------------------------------------------

@pytest.fixture
def leads(cfg, tmp_path):
    conn = make_db(tmp_path)
    a = add_account(conn, "Hotco", industry="Industrial")
    for t, age in (("leadership_change", 21), ("hiring_cluster", 10), ("pe_addon", 45)):
        add_signal(conn, cfg, a, t, age, url=f"https://hotco.com/{t}", lines=("AI & Technology",) if t == "leadership_change" else ("Strategy & Operations",),
                   summary=f"Hotco {t.replace('_', ' ')} summary.", role="Pat Lee, CIO" if t == "leadership_change" else None)
    score_accounts(conn, cfg, AS_OF)
    yield conn, build_shortlist(conn, cfg, AS_OF).leads
    conn.close()


GOOD = json.dumps({"why_now": "Hotco named Pat Lee as CIO 21 days ago and is hiring.", "best_service_line": "AI & Technology"})


def test_llm_brief_uses_only_stored_facts_in_prompt(cfg, leads):
    conn, ls = leads
    llm = FakeLLM([GOOD])
    b = Briefer(cfg, llm).brief(ls[0], AS_OF)
    assert (b.source, b.best_service_line) == ("llm", "AI & Technology") and "Pat Lee" in b.why_now
    system, msgs = llm.calls[0]
    payload = json.loads(msgs[0]["content"])
    assert payload["company"] == "Hotco" and len(payload["signals"]) == 3
    assert payload["signals"][0]["days_ago"] == 21 and "score" not in payload and "url" not in json.dumps(payload).lower()
    assert "Hotco" not in system and "no tools" in system.lower() and "ONE sentence" in system


def test_invalid_brief_is_retried_once_then_succeeds(cfg, leads):
    conn, ls = leads
    bad = json.dumps({"why_now": "Hotco should be pitched about 99 things.", "best_service_line": "AI & Technology"})
    llm = FakeLLM([bad, GOOD])
    b = Briefer(cfg, llm).brief(ls[0], AS_OF)
    assert b.source == "llm" and len(llm.calls) == 2
    assert "not in the input signals" in llm.calls[1][1][-1]["content"] or "outreach" in llm.calls[1][1][-1]["content"]
    assert b.usage.total_tokens == 300


def test_two_invalid_briefs_fall_back_to_plain_summary_never_invented_text(cfg, leads):
    conn, ls = leads
    bad = json.dumps({"why_now": "Hotco spent $7 billion.", "best_service_line": "AI & Technology"})
    b = Briefer(cfg, FakeLLM([bad, bad])).brief(ls[0], AS_OF)
    assert b.source == "fallback" and b.error and "InvalidJSON" in b.error
    assert b.why_now == "Hotco leadership change summary (Sep 24, 2026)."
    assert b.usage.total_tokens == 300           # spend is still recorded


def test_llm_error_falls_back(cfg, leads):
    conn, ls = leads

    class Boom:
        def run(self, *a):
            raise LLMError("down", Usage(input_tokens=5))
    b = Briefer(cfg, Boom()).brief(ls[0], AS_OF)
    assert b.source == "fallback" and "down" in b.error and b.usage.input_tokens == 5


def test_no_llm_gives_fallback_with_code_derived_service_line(cfg, leads):
    conn, ls = leads
    b = fallback_brief(ls[0], cfg=cfg)
    # AI & Technology: 30 pts (leadership). Strategy & Operations: 26 + 15.4 (hiring, pe). S&O wins.
    assert b.best_service_line == "Strategy & Operations" and b.source == "fallback"


def test_fallback_service_line_is_none_when_signals_have_none(cfg, tmp_path):
    conn = make_db(tmp_path)
    a = add_account(conn, "Bare")
    for ty in ("leadership_change", "restructuring_layoffs", "hiring_cluster", "pe_addon"):
        add_signal(conn, cfg, a, ty, 5, lines=())      # no service match halves points; four families still reach Hot
    score_accounts(conn, cfg, AS_OF)
    lead = build_shortlist(conn, cfg, AS_OF).leads[0]
    assert fallback_brief(lead, cfg=cfg).best_service_line is None


def test_get_briefs_caches_llm_briefs_and_logs_cost(cfg, leads):
    conn, ls = leads
    llm = FakeLLM([GOOD])
    out = get_briefs(conn, cfg, ls, AS_OF, Briefer(cfg, llm), "claude-sonnet-5-5")
    assert out[ls[0].account.id].source == "llm"
    run = conn.execute("SELECT * FROM runs WHERE step='brief'").fetchone()
    assert run["account_id"] == ls[0].account.id and run["tokens"] == 150
    assert run["est_cost"] == pytest.approx(100 * 2 / 1e6 + 50 * 10 / 1e6 + 0.01)
    # second call: served from cache, zero LLM calls, identical text
    llm2 = FakeLLM([])
    again = get_briefs(conn, cfg, ls, AS_OF, Briefer(cfg, llm2), "claude-sonnet-5-5")
    assert llm2.calls == [] and again[ls[0].account.id].why_now == out[ls[0].account.id].why_now
    assert again[ls[0].account.id].source == "cache"
    # --rebrief regenerates
    llm3 = FakeLLM([GOOD])
    get_briefs(conn, cfg, ls, AS_OF, Briefer(cfg, llm3), "claude-sonnet-5-5", rebrief=True)
    assert len(llm3.calls) == 1


def test_fallbacks_are_not_cached_and_no_llm_logs_no_cost(cfg, leads):
    conn, ls = leads
    get_briefs(conn, cfg, ls, AS_OF, Briefer(cfg, None), None)
    assert conn.execute("SELECT COUNT(*) FROM briefs").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM runs WHERE step='brief'").fetchone()[0] == 0
