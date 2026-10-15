"""Briefer: one-sentence why_now and best service line per lead.

Claude writes the sentence; code decides whether it is acceptable. why_now must be a single
sentence within the word limit, free of outreach language, and may only contain numbers that
appear in the stored signals (no invented figures). best_service_line must be one of the
configured service lines. If Claude fails validation twice, or errors, the lead gets a plain
deterministic fallback built from the stored signal summary - never invented text."""

from __future__ import annotations

import json
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from importlib import resources

from pydantic import BaseModel, ValidationInfo, field_validator

from bd.config import Config
from bd.brief_core import Brief, fallback_brief  # noqa: F401  (re-exported)
from bd.cost import estimate_cost
from bd.labels import fmt_date  # noqa: F401  (re-exported)
from bd.llm import LLM, LLMError, Usage, generate_validated
from bd.shortlist import Lead

_ABBREV = re.compile(r"\b(Inc|Corp|Co|Ltd|LLC|Mr|Ms|Mrs|Dr|Jr|Sr|St|vs|No)\.|U\.S\.")
_SENTENCE_BREAK = re.compile(r"[.!?][\"')\]]?\s+[A-Z]")
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
# A figure ("$5", "300 million", "12%", "4,000 employees") must come from the signal text itself,
# not from a coincidental date component such as the 05 in 2026-10-05.
_FIGURE = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?|\d[\d,]*(?:\.\d+)?\s*(?:%|percent|million|billion|thousand|[MBK]\b|"
                     r"employees|jobs|workers|positions|sites|plants|stores|locations)", re.I)


def _norm_num(n: str) -> str:
    """'09' == '9', '1,000' == '1000', '3.' == '3'."""
    n = n.replace(",", "").rstrip(".")
    whole, _, frac = n.partition(".")
    whole = whole.lstrip("0") or "0"
    return f"{whole}.{frac}" if frac else whole


class BriefResponse(BaseModel):
    why_now: str
    best_service_line: str

    @field_validator("why_now")
    @classmethod
    def _why_now(cls, v: str, info: ValidationInfo) -> str:
        ctx = info.context or {}
        v = v.strip()
        if not v or "\n" in v:
            raise ValueError("why_now must be a single non-empty line")
        if _SENTENCE_BREAK.search(_ABBREV.sub("", v)):
            raise ValueError("why_now must be exactly ONE sentence")
        max_words = ctx.get("max_words")
        if max_words and len(v.split()) > max_words:
            raise ValueError(f"why_now must be at most {max_words} words (it has {len(v.split())})")
        low = v.lower()
        for phrase in ctx.get("banned_phrases", []):
            if phrase.lower() in low:
                raise ValueError(f"why_now must not contain {phrase!r}: no outreach or advice language")
        grounding = ctx.get("grounding")
        if grounding is not None:
            known = {_norm_num(n) for n in _NUMBER.findall(grounding)}
            text_only = _ISO_DATE.sub(" ", re.sub(r'"days_ago":\s*\d+', " ", grounding))
            known_text = {_norm_num(n) for n in _NUMBER.findall(text_only)}
            for fig in _FIGURE.findall(v):
                n = _NUMBER.search(fig).group(0)  # type: ignore[union-attr]
                if _norm_num(n) not in known_text:
                    raise ValueError(f"why_now contains the figure {fig!r}, which is not in the input signals")
            for num in _NUMBER.findall(v):
                if _norm_num(num) not in known:
                    raise ValueError(f"why_now contains the number {num!r}, which is not in the input signals")
        return v

    @field_validator("best_service_line")
    @classmethod
    def _line(cls, v: str, info: ValidationInfo) -> str:
        allowed = (info.context or {}).get("service_lines")
        if allowed is None:
            return v
        canon = {a.lower(): a for a in allowed}
        if v.strip().lower() not in canon:
            raise ValueError(f"best_service_line must be exactly one of {allowed}")
        return canon[v.strip().lower()]


def _lead_input(lead: Lead, as_of: date, cfg: Config) -> dict:
    sigs = lead.signals[: cfg.briefer.max_signals_in_prompt]
    return {
        "company": lead.account.name,
        "industry": lead.account.industry,
        "signals": [{
            "type": s.type, "event_date": s.event_date.isoformat(), "days_ago": max(0, (as_of - s.event_date).days),
            "confidence": s.confidence, "summary": s.summary, "role_or_person": s.role_or_person,
            "service_lines": list(s.service_lines),
        } for s in sigs],
    }


class Briefer:
    def __init__(self, cfg: Config, llm: LLM | None) -> None:
        self.cfg, self.llm = cfg, llm

    def system_prompt(self) -> str:
        return resources.files("bd.prompts").joinpath("briefer.md").read_text(encoding="utf-8")

    def brief(self, lead: Lead, as_of: date) -> Brief:
        if self.llm is None:
            return fallback_brief(lead, cfg=self.cfg)
        data = _lead_input(lead, as_of, self.cfg)
        ctx = {
            "max_words": self.cfg.briefer.max_words,
            "banned_phrases": self.cfg.briefer.banned_phrases,
            "service_lines": self.cfg.service_lines,
            "grounding": json.dumps(data, ensure_ascii=False),
        }
        try:
            resp, res = generate_validated(
                self.llm, self.system_prompt(), json.dumps(data, ensure_ascii=False), BriefResponse,
                retries=self.cfg.llm.json_retries, key="why_now", context=ctx)
        except LLMError as e:   # includes InvalidJSON and refusals
            return fallback_brief(lead, f"{type(e).__name__}: {e}", e.usage, self.cfg)
        return Brief(resp.why_now, resp.best_service_line, "llm", res.usage)


def get_briefs(conn: sqlite3.Connection, cfg: Config, leads: list[Lead], as_of: date, briefer: Briefer,
               model: str | None, rebrief: bool = False) -> dict[int, Brief]:
    """Brief every lead. LLM briefs are cached per (account, run_date) so regenerating a card is free
    and identical; fallbacks are never cached. Calls run concurrently; DB writes stay on this thread."""
    out: dict[int, Brief] = {}
    todo: list[Lead] = []
    for lead in leads:
        row = None if rebrief else conn.execute(
            "SELECT why_now, best_service_line FROM briefs WHERE account_id = ? AND run_date = ?",
            (lead.account.id, as_of.isoformat())).fetchone()
        if row:
            out[lead.account.id] = Brief(row["why_now"], row["best_service_line"], "cache")
        else:
            todo.append(lead)
    if todo:
        with ThreadPoolExecutor(max_workers=max(1, cfg.llm.concurrency)) as ex:
            futs = [(lead, ex.submit(briefer.brief, lead, as_of)) for lead in todo]
            results = [(lead, f.result()) for lead, f in futs]
        for lead, b in results:
            out[lead.account.id] = b
            cost = estimate_cost(b.usage, model, cfg) if b.usage.total_tokens else None
            with conn:
                if b.source == "llm":
                    conn.execute("INSERT OR REPLACE INTO briefs(account_id, run_date, why_now, best_service_line, model) "
                                 "VALUES (?,?,?,?,?)", (lead.account.id, as_of.isoformat(), b.why_now,
                                                        b.best_service_line, model))
                if briefer.llm is not None:
                    conn.execute(
                        "INSERT INTO runs(run_date,step,account_id,accounts_scanned,signals_found,tokens,est_cost,errors) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (as_of.isoformat(), "brief", lead.account.id, 0, 0, b.usage.total_tokens, cost, b.error))
    return out
