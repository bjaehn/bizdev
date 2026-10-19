"""How much of the list has actually been scanned for signals, and what that found. Shown on the Admin Home so a thin list is explained by
a number you can act on (companies not scanned yet, updates it takes to reach them) and never by missing company facts.

Read-only and deterministic: counts from the accounts and signals tables and the fit gate's own verdicts. No model or network code."""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from datetime import date

from bd.accounts import list_accounts
from bd.config import Config
from bd.feed import GROUPS, load_items
from bd.fit_gate import check_fit, load_exclusions

WINDOW_DAYS = 90


@dataclass
class SignalCoverage:
    on_list: int = 0                                          # companies scanned for signals (everything not known out of scope)
    out_of_scope: int = 0
    scanned: int = 0                                          # scanned at least once by the paid scan
    unscanned: int = 0
    unverified: int = 0                                       # on the list with location or size unconfirmed: scanned all the same
    with_signals: int = 0                                     # companies with at least one signal in the window
    by_group: list[tuple[str, int]] = field(default_factory=list)      # (label, signals in the window), in feed order
    per_update: int = 0
    updates_needed: int = 0                                   # presses of Update my list to scan every company not yet scanned
    last_scanned: str | None = None


def signal_coverage(conn: sqlite3.Connection, cfg: Config, today: date, update: dict[str, int]) -> SignalCoverage:
    c = SignalCoverage(per_update=int(update.get("scan", 0)))
    exclusions = load_exclusions(conn)
    for a in list_accounts(conn, status=None):
        if a.status == "excluded":
            c.out_of_scope += 1
            continue
        c.on_list += 1
        c.scanned += bool(a.last_scanned)
        c.unverified += check_fit(a, exclusions, cfg).unverified
        if a.last_scanned and (c.last_scanned is None or a.last_scanned > c.last_scanned):
            c.last_scanned = a.last_scanned
    c.unscanned = c.on_list - c.scanned
    items = load_items(conn, today, WINDOW_DAYS)
    c.with_signals = len({i.account_id for i in items})
    c.by_group = [(label, sum(i.group == key for i in items)) for key, label in GROUPS]
    c.updates_needed = math.ceil(c.unscanned / c.per_update) if c.unscanned and c.per_update else 0
    return c
