"""The Brief record and the plain fallback brief. No LLM code, so the local UI can use it."""

from __future__ import annotations

from dataclasses import dataclass, field

from bd.config import Config
from bd.labels import fmt_date
from bd.llm_usage import Usage
from bd.shortlist import Lead


@dataclass
class Brief:
    why_now: str
    best_service_line: str | None
    source: str                      # llm | cache | fallback
    usage: Usage = field(default_factory=Usage)
    error: str | None = None


def fallback_brief(lead: Lead, error: str | None = None, usage: Usage | None = None, cfg: Config | None = None) -> Brief:
    """Plain, deterministic text from the strongest stored signal. No model involved."""
    top = lead.signals[0]
    why = f"{top.summary.strip().rstrip('.')} ({fmt_date(top.event_date)})."
    weights: dict[str, float] = {}
    for s in lead.signals:
        for line in s.service_lines:
            weights[line] = weights.get(line, 0.0) + s.points
    order = {l: i for i, l in enumerate(cfg.service_lines)} if cfg else {}
    best = min(weights, key=lambda l: (-weights[l], order.get(l, 99))) if weights else None
    return Brief(why, best, "fallback", usage or Usage(), error)


