from __future__ import annotations

from bd.scanners.base import Scanner


class EventsScanner(Scanner):
    name = "events"
    allowed_types = frozenset({
        "restructuring_layoffs", "ma_divestiture", "strategy_rebrand", "ai_commitment",
        "activist", "ops_failure", "cost_program",
    })
