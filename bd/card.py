"""Teams Adaptive Card (version 1.4) for the weekly shortlist.

The card is display-only: the only actions are Action.OpenUrl buttons pointing at source URLs that
are already stored (and were verified) for that lead. No Action.Submit, no Input.* elements.
card.json holds the bare Adaptive Card; the Teams webhook envelope is added only when posting."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import urlparse

import httpx2 as httpx

from bd.briefer import Brief, fmt_date
from bd.config import Config
from bd.models import SIGNAL_TYPES
from bd.shortlist import Lead
from bd.urls import hostname

SCHEMA = "http://adaptivecards.io/schemas/adaptive-card.json"
VERSION = "1.4"

TYPE_LABELS = {
    "leadership_change": "Leadership change",
    "leadership_transformation_ai_data": "New transformation/AI/data leader",
    "open_exec_search": "Open executive search",
    "pe_platform_or_sponsor_exec": "PE platform or sponsor executive",
    "pe_addon": "PE add-on acquisition",
    "pe_hold_2_4": "PE hold 2-4 years",
    "restructuring_layoffs": "Restructuring / layoffs",
    "ma_divestiture": "M&A / divestiture",
    "activist": "Activist pressure",
    "ops_failure": "Operational / tech failure",
    "cost_program": "Cost program / guidance cut",
    "ai_commitment": "AI commitment",
    "strategy_rebrand": "Strategy / rebrand",
    "hiring_cluster": "Hiring cluster",
    "hiring_interim": "Interim hiring",
    "hiring_single": "Single senior hire",
    "platform_migration": "Platform migration",
    "vendor_deal": "Vendor deal",
    "rfp_match": "RFP match",
    "risk_factor": "Risk factor disclosure",
}
assert set(TYPE_LABELS) == set(SIGNAL_TYPES)

_BAND_COLOR = {"Hot": "Attention", "Warm": "Warning"}


class CardTooLarge(Exception):
    pass


def _tb(text: str, **kw: Any) -> dict[str, Any]:
    return {"type": "TextBlock", "text": text, "wrap": True, **kw}


def _short(text: str, n: int = 40) -> str:
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _lead_block(lead: Lead, brief: Brief, cfg: Config, detail: str) -> dict[str, Any]:
    a, r = lead.account, lead.result
    meta = [x for x in (a.industry, a.hq_city, a.ownership if a.ownership != "public" else None,
                        f"sponsor: {a.sponsor}" if a.sponsor else None) if x]
    items: list[dict[str, Any]] = [
        {"type": "ColumnSet", "columns": [
            {"type": "Column", "width": "stretch", "items": [_tb(a.name, weight="Bolder", size="Medium")]},
            {"type": "Column", "width": "auto", "items": [
                _tb(f"{r.band} · {r.score}", weight="Bolder", color=_BAND_COLOR.get(r.band, "Default"),
                    horizontalAlignment="Right")]},
        ]},
        _tb(brief.why_now, spacing="Small"),
    ]
    facts = []
    if brief.best_service_line:
        facts.append({"title": "Best fit", "value": brief.best_service_line})
    if meta:
        facts.append({"title": "Company", "value": " · ".join(meta)})
    if detail == "full":
        facts.append({"title": "Score", "value": r.rationale})
    if facts:
        items.append({"type": "FactSet", "facts": facts})
    if detail in ("full", "compact"):
        for s in lead.signals[: cfg.card.max_buttons_per_lead]:
            line = f"• {TYPE_LABELS[s.type]} · {fmt_date(s.event_date)} · {s.confidence}"
            if s.role_or_person:
                line += f" — {s.role_or_person}"
            items.append(_tb(line, size="Small", isSubtle=True, spacing="None"))
    n_buttons = {"full": cfg.card.max_buttons_per_lead, "compact": 1, "minimal": 1}[detail]
    actions, seen = [], set()
    for s in lead.signals:
        if s.url in seen:
            continue
        seen.add(s.url)
        actions.append({"type": "Action.OpenUrl", "title": _short(f"{TYPE_LABELS[s.type]} · {hostname(s.url)}"),
                        "url": s.url})
        if len(actions) >= n_buttons:
            break
    if actions:
        items.append({"type": "ActionSet", "actions": actions})
    return {"type": "Container", "style": "emphasis", "separator": True, "spacing": "Medium", "items": items}


def card_bytes(card: dict[str, Any]) -> int:
    return len(json.dumps(card, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def build_card(leads: list[Lead], briefs: dict[int, Brief], run_date: date, cfg: Config, overflow: int = 0) -> dict[str, Any]:
    """Build the card, shrinking per-lead detail if needed to fit the size limit."""
    for detail in ("full", "compact", "minimal"):
        card = _build(leads, briefs, run_date, cfg, overflow, detail)
        if card_bytes(card) <= cfg.card.max_bytes:
            return card
    raise CardTooLarge(f"card is {card_bytes(card)} bytes even at minimal detail (limit {cfg.card.max_bytes})")


def _build(leads: list[Lead], briefs: dict[int, Brief], run_date: date, cfg: Config, overflow: int,
           detail: str) -> dict[str, Any]:
    body: list[dict[str, Any]] = [
        _tb(f"{cfg.card.title} · {fmt_date(run_date)}", weight="Bolder", size="Large"),
    ]
    if leads:
        hot = sum(l.result.band == "Hot" for l in leads)
        warm = sum(l.result.band == "Warm" for l in leads)
        body.append(_tb(f"{len(leads)} lead{'s' if len(leads) != 1 else ''}: {hot} Hot, {warm} Warm. "
                        "Public sources only. Research only; no outreach drafted.", isSubtle=True, spacing="Small"))
        for lead in leads:
            body.append(_lead_block(lead, briefs[lead.account.id], cfg, detail))
    else:
        body.append(_tb("No account met the posting gate this week: Hot or Warm, with a High-confidence "
                        "signal in the last 90 days, and not in cooldown.", spacing="Medium"))
    foot = []
    if overflow:
        foot.append(f"{overflow} more postable lead{'s' if overflow != 1 else ''} not shown (cap {cfg.post_gate.max_leads_per_post}).")
    n_fb = sum(1 for l in leads if briefs[l.account.id].source == "fallback")
    if n_fb:
        foot.append(f"{n_fb} why-now line{'s are' if n_fb != 1 else ' is'} the plain source summary, not a written brief.")
    if foot:
        body.append(_tb(" ".join(foot), size="Small", isSubtle=True, spacing="Medium"))
    return {"$schema": SCHEMA, "type": "AdaptiveCard", "version": VERSION, "body": body}


def _walk(node: Any):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def validate_card(card: dict[str, Any], cfg: Config, allowed_urls: set[str] | None = None) -> list[str]:
    """Return a list of problems (empty = valid). Enforces: version 1.4, only Action.OpenUrl, no
    inputs, https URLs, URLs limited to `allowed_urls` (stored signal URLs), size limit."""
    problems: list[str] = []
    if card.get("type") != "AdaptiveCard" or card.get("version") != VERSION or "$schema" not in card:
        problems.append("card must be an AdaptiveCard with $schema and version 1.4")
    for node in _walk(card):
        t = node.get("type")
        if isinstance(t, str) and t.startswith("Action.") and t != "Action.OpenUrl":
            problems.append(f"forbidden action {t}")
        if isinstance(t, str) and t.startswith("Input."):
            problems.append(f"forbidden input element {t}")
        if "selectAction" in node and (node["selectAction"] or {}).get("type") != "Action.OpenUrl":
            problems.append("selectAction must be Action.OpenUrl")
        if t == "Action.OpenUrl":
            url = node.get("url", "")
            if urlparse(url).scheme != "https":
                problems.append(f"OpenUrl must be https: {url!r}")
            if allowed_urls is not None and url not in allowed_urls:
                problems.append(f"OpenUrl not a stored signal URL: {url!r}")
    if card_bytes(card) > cfg.card.max_bytes:
        problems.append(f"card exceeds {cfg.card.max_bytes} bytes")
    return problems


def wrap_for_teams(card: dict[str, Any]) -> dict[str, Any]:
    """Message envelope expected by Teams incoming webhooks / Workflows."""
    return {"type": "message", "attachments": [{
        "contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None, "content": card}]}


@dataclass(frozen=True)
class PostResult:
    ok: bool
    detail: str   # never contains the webhook URL (it is a secret)


def post_card(webhook_url: str, card: dict[str, Any], client: httpx.Client | None = None) -> PostResult:
    if urlparse(webhook_url).scheme != "https":
        return PostResult(False, "webhook URL must be https")
    own = client is None
    client = client or httpx.Client(timeout=15)
    try:
        r = client.post(webhook_url, json=wrap_for_teams(card))
    except httpx.HTTPError as e:
        return PostResult(False, f"request failed ({type(e).__name__})")
    finally:
        if own:
            client.close()
    if 200 <= r.status_code < 300:
        return PostResult(True, f"HTTP {r.status_code}")
    return PostResult(False, f"HTTP {r.status_code}")
