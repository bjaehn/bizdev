"""Why is the list thin? Companies that fail the fit gate for want of facts are not scanned or scored, so they never reach This week.

Read-only and deterministic: it groups the fit gate's own reasons for every company on hold, says how many the free SEC step can try, and
how many presses of Update my list it takes to research the rest. No model or network code."""

from __future__ import annotations

import math
import sqlite3
from collections import Counter
from dataclasses import dataclass, field

from bd.accounts import list_accounts, wants_lookup, wants_read
from bd.config import Config
from bd.fit_gate import check_fit, load_exclusions


@dataclass
class Waiting:
    count: int                                                # companies on hold
    reasons: list[tuple[str, int]] = field(default_factory=list)   # (the fit gate's reason, companies), most common first
    sec_can_try: int = 0                                      # no CIK or missing facts that a public filer's SEC record would answer: the free step tries them
    needs_research: int = 0                                   # the rest: a private or PE-owned company the SEC does not list
    presses: int | None = None                                # presses of Update my list to research them all (None when research is off)
    per_press: int = 0
    spend_cap: int = 0


def waiting_for_facts(conn: sqlite3.Connection, cfg: Config, update: dict[str, int]) -> Waiting | None:
    held = [a for a in list_accounts(conn, status=None) if a.status == "hold_review"]
    if not held:
        return None
    exclusions = load_exclusions(conn)
    reasons: Counter[str] = Counter()
    for a in held:
        reasons.update(check_fit(a, exclusions, cfg).reasons)
    sec = sum(1 for a in held if wants_lookup(a) or wants_read(a))
    per = int(update.get("research", 0))
    rest = len(held) - sec
    return Waiting(len(held), sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0])), sec, rest,
                   math.ceil(rest / per) if per and rest else (0 if not rest else None), per, int(update.get("budget", 0)))
