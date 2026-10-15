import copy
import json

import httpx2 as httpx
import pytest

from bd.briefer import Brief, fallback_brief
from bd.card import (CardTooLarge, TYPE_LABELS, build_card, card_bytes, post_card, validate_card, wrap_for_teams, _walk)
from bd.models import SIGNAL_TYPES
from bd.score_store import score_accounts
from bd.shortlist import build_shortlist
from tests.conftest import AS_OF, add_account, add_signal, make_db


@pytest.fixture
def world(cfg, tmp_path):
    conn = make_db(tmp_path)
    a = add_account(conn, "Hotco", industry="Industrial", hq_city="Chicago", ownership="pe-backed", sponsor="Acme Capital")
    for t, age in (("leadership_change", 21), ("hiring_cluster", 10), ("pe_addon", 45)):
        add_signal(conn, cfg, a, t, age, url=f"https://hotco.com/{t}", role="Pat Lee, CIO" if t == "leadership_change" else None)
    b = add_account(conn, "Warmco")
    add_signal(conn, cfg, b, "leadership_change", 5, url="https://www.sec.gov/Archives/x.htm", source_type="sec_filing")
    add_signal(conn, cfg, b, "restructuring_layoffs", 5, url="https://warmco.com/news")
    score_accounts(conn, cfg, AS_OF)
    leads = build_shortlist(conn, cfg, AS_OF).leads
    briefs = {l.account.id: Brief(f"{l.account.name} had a recent leadership change.", "AI & Technology", "llm") for l in leads}
    yield conn, leads, briefs
    conn.close()


def urls(leads):
    return {s.url for l in leads for s in l.signals}


def test_card_envelope_is_adaptive_card_1_4(cfg, world):
    _, leads, briefs = world
    card = build_card(leads, briefs, AS_OF, cfg)
    assert card["type"] == "AdaptiveCard" and card["version"] == "1.4" and card["$schema"].endswith("adaptive-card.json")
    assert validate_card(card, cfg, urls(leads)) == []
    json.dumps(card)   # serialisable


def test_every_action_is_open_url_and_urls_are_stored_https_signal_urls(cfg, world):
    _, leads, briefs = world
    card = build_card(leads, briefs, AS_OF, cfg)
    nodes = list(_walk(card))
    actions = [n for n in nodes if str(n.get("type", "")).startswith("Action.")]
    assert actions and all(a["type"] == "Action.OpenUrl" for a in actions)
    assert all(a["url"].startswith("https://") and a["url"] in urls(leads) for a in actions)
    assert not [n for n in nodes if str(n.get("type", "")).startswith("Input.")]
    assert "actions" not in card                      # no card-level actions; buttons live in ActionSets
    assert all(n.get("type") != "Action.Submit" for n in nodes)


def test_content_leads_ranked_with_band_score_and_service_line(cfg, world):
    _, leads, briefs = world
    card = build_card(leads, briefs, AS_OF, cfg)
    text = json.dumps(card, ensure_ascii=False)
    assert text.index("Hotco") < text.index("Warmco")
    assert "Hot · 91" in text and "Warm · 64" in text
    assert "Best fit" in text and "AI & Technology" in text
    assert "Industrial · Chicago · pe-backed · sponsor: Acme Capital" in text
    assert "Leadership change · Sep 24, 2026 · High — Pat Lee, CIO" in text
    assert "Public sources only. Research only; no outreach drafted." in text
    assert "2 leads: 1 Hot, 1 Warm." in text
    assert "Oct 15, 2026" in text


def test_buttons_show_source_domain_and_respect_per_lead_cap(cfg, world):
    _, leads, briefs = world
    card = build_card(leads, briefs, AS_OF, cfg)
    sets = [n for n in _walk(card) if n.get("type") == "ActionSet"]
    assert len(sets) == 2 and all(1 <= len(s["actions"]) <= cfg.card.max_buttons_per_lead for s in sets)
    titles = [a["title"] for s in sets for a in s["actions"]]
    assert "Leadership change · hotco.com" in titles and "Leadership change · sec.gov" in titles
    assert all(len(t) <= 40 for t in titles)


def test_duplicate_urls_make_one_button(cfg, world):
    _, leads, briefs = world
    for s in leads[0].signals[1:]:
        object.__setattr__(s, "url", leads[0].signals[0].url)
    card = build_card(leads, briefs, AS_OF, cfg)
    first = [n for n in _walk(card) if n.get("type") == "ActionSet"][0]
    assert len(first["actions"]) == 1


def test_no_leads_card_still_valid_with_explanation(cfg):
    card = build_card([], {}, AS_OF, cfg)
    assert validate_card(card, cfg, set()) == []
    assert "No account met the posting gate" in json.dumps(card)
    assert not [n for n in _walk(card) if str(n.get("type", "")).startswith("Action.")]


def test_footer_notes_overflow_and_fallback_briefs(cfg, world):
    _, leads, briefs = world
    briefs[leads[0].account.id] = fallback_brief(leads[0], cfg=cfg)
    text = json.dumps(build_card(leads, briefs, AS_OF, cfg, overflow=3))
    assert "3 more postable leads not shown (cap 10)" in text
    assert "1 why-now line is the plain source summary, not a written brief" in text


def test_detail_shrinks_to_fit_size_limit_then_fails_loudly(cfg, world):
    _, leads, briefs = world
    full = card_bytes(build_card(leads, briefs, AS_OF, cfg))
    tight = cfg.model_copy(update={"card": cfg.card.model_copy(update={"max_bytes": full - 200})})
    small = build_card(leads, briefs, AS_OF, tight)
    assert card_bytes(small) < full and validate_card(small, tight, urls(leads)) == []
    assert "Score" not in [f["title"] for n in _walk(small) if n.get("type") == "FactSet" for f in n["facts"]]
    nothing = cfg.model_copy(update={"card": cfg.card.model_copy(update={"max_bytes": 300})})
    with pytest.raises(CardTooLarge):
        build_card(leads, briefs, AS_OF, nothing)


def test_ten_leads_fit_comfortably(cfg, tmp_path):
    conn = make_db(tmp_path)
    for i in range(10):
        a = add_account(conn, f"Company number {i} International Holdings")
        add_signal(conn, cfg, a, "leadership_change", 5, summary="A very long summary " + "word " * 18)
        add_signal(conn, cfg, a, "restructuring_layoffs", 5)
        add_signal(conn, cfg, a, "ai_commitment", 5)
    score_accounts(conn, cfg, AS_OF)
    leads = build_shortlist(conn, cfg, AS_OF).leads
    assert len(leads) == 10
    briefs = {l.account.id: Brief("Word " * 5, "AI & Technology", "llm") for l in leads}
    assert card_bytes(build_card(leads, briefs, AS_OF, cfg)) < cfg.card.max_bytes


# ---- validator catches violations ---------------------------------------------------------------

def good_card(cfg, world):
    _, leads, briefs = world
    return build_card(leads, briefs, AS_OF, cfg), urls(leads)


@pytest.mark.parametrize("mutate,msg", [
    (lambda c: c.update(version="1.5"), "version 1.4"),
    (lambda c: c["body"].append({"type": "ActionSet", "actions": [{"type": "Action.Submit", "title": "x"}]}), "forbidden action"),
    (lambda c: c["body"].append({"type": "ActionSet", "actions": [{"type": "Action.ShowCard", "title": "x"}]}), "forbidden action"),
    (lambda c: c.update(actions=[{"type": "Action.Execute", "title": "x"}]), "forbidden action"),
    (lambda c: c["body"].append({"type": "Input.Text", "id": "a"}), "forbidden input"),
    (lambda c: c["body"].append({"type": "Container", "selectAction": {"type": "Action.Submit"}, "items": []}), "forbidden action"),
    (lambda c: c["body"].append({"type": "ActionSet", "actions": [{"type": "Action.OpenUrl", "title": "x", "url": "http://insecure.example"}]}), "must be https"),
    (lambda c: c["body"].append({"type": "ActionSet", "actions": [{"type": "Action.OpenUrl", "title": "x", "url": "https://not-stored.example/x"}]}), "not a stored signal URL"),
])
def test_validator_flags_forbidden_content(cfg, world, mutate, msg):
    card, allowed = good_card(cfg, world)
    bad = copy.deepcopy(card)
    mutate(bad)
    assert any(msg in p for p in validate_card(bad, cfg, allowed))


# ---- posting ------------------------------------------------------------------------------------

WEBHOOK = "https://prod-00.example.logic.azure.com/workflows/SECRET-TOKEN/triggers/manual/paths/invoke?sig=SECRETSIG"


def test_envelope_shape(cfg, world):
    card, _ = good_card(cfg, world)
    env = wrap_for_teams(card)
    assert env["type"] == "message" and env["attachments"][0]["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert env["attachments"][0]["content"] is card


def test_post_success_sends_envelope_to_webhook(cfg, world):
    card, _ = good_card(cfg, world)
    seen = []
    def h(req):
        seen.append((str(req.url), json.loads(req.content)))
        return httpx.Response(202)
    res = post_card(WEBHOOK, card, httpx.Client(transport=httpx.MockTransport(h)))
    assert res.ok and res.detail == "HTTP 202"
    assert seen[0][0] == WEBHOOK and seen[0][1]["attachments"][0]["content"]["version"] == "1.4"


@pytest.mark.parametrize("handler,expect", [
    (lambda r: httpx.Response(400), "HTTP 400"),
    (lambda r: httpx.Response(500), "HTTP 500"),
    (lambda r: (_ for _ in ()).throw(httpx.ConnectError("boom", request=r)), "request failed (ConnectError)"),
])
def test_post_failure_never_leaks_the_webhook_url(cfg, world, handler, expect):
    card, _ = good_card(cfg, world)
    res = post_card(WEBHOOK, card, httpx.Client(transport=httpx.MockTransport(handler)))
    assert not res.ok and res.detail == expect
    assert "SECRET" not in res.detail and "logic.azure" not in res.detail


def test_non_https_webhook_refused_without_any_request(cfg, world):
    card, _ = good_card(cfg, world)
    calls = []
    res = post_card("http://hooks.example/x", card, httpx.Client(transport=httpx.MockTransport(lambda r: calls.append(r))))
    assert not res.ok and calls == []


def test_every_signal_type_has_a_label():
    assert set(TYPE_LABELS) == set(SIGNAL_TYPES)
