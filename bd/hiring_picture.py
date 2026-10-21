"""A company's hiring picture, as plain data: what its own job board showed on the last reading, how that compares with about four weeks earlier, which
themes the relevant roles fall into, and which programs the titles name. No HTML, no network, no model.

Everything is a count or a date taken from stored readings of the company's own Greenhouse, Lever or Ashby board (bd.scanners.ats); a company with no board
found has no picture, and says so instead of showing zeros. The change is only stated when a reading 3 to 6 weeks older exists."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

from bd.config import Config
from bd.labels import THEME_LABEL, fmt_date

COMPARE_MIN_DAYS, COMPARE_MAX_DAYS, COMPARE_TARGET_DAYS = 21, 42, 28
RECENT_DAYS = 30


@dataclass
class ProgramLine:
    name: str
    n: int
    examples: list[dict]


@dataclass
class HiringPicture:
    has_board: bool
    provider: str | None = None
    read_date: date | None = None
    total: int | None = None
    relevant: int | None = None
    change: int | None = None                      # open roles now minus open roles at the earlier reading
    change_text: str = ""
    themes: list[tuple[str, int, int]] = field(default_factory=list)      # (theme label, open relevant roles, of which senior)
    recent: int = 0                                # open relevant roles dated inside the last RECENT_DAYS days
    programs: list[ProgramLine] = field(default_factory=list)
    note: str = ""


def hiring_picture(conn: sqlite3.Connection, cfg: Config, account_id: int, today: date) -> HiringPicture:
    a = conn.execute("SELECT ats_provider, ats_board FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if a is None or not (a["ats_provider"] and a["ats_board"]):
        return HiringPicture(False, note="No job board of this company's own has been found, so its open roles are not counted here. "
                                         "The job-board search runs in every Update and reads only the company's own careers page.")
    pic = HiringPicture(True, provider=a["ats_provider"])
    now = conn.execute("SELECT * FROM board_readings WHERE account_id = ? ORDER BY seen_date DESC LIMIT 1", (account_id,)).fetchone()
    if now is None:
        pic.note = f"The company's {a['ats_provider'].title()} board is known but has not been read yet. The next Update reads it."
        return pic
    pic.read_date, pic.total, pic.relevant = date.fromisoformat(now["seen_date"]), now["total"], now["relevant"]
    lo, hi = pic.read_date - timedelta(days=COMPARE_MAX_DAYS), pic.read_date - timedelta(days=COMPARE_MIN_DAYS)
    before = conn.execute("SELECT * FROM board_readings WHERE account_id = ? AND seen_date BETWEEN ? AND ? "
                          "ORDER BY abs(julianday(?) - julianday(seen_date)) LIMIT 1",
                          (account_id, lo.isoformat(), hi.isoformat(), (pic.read_date - timedelta(days=COMPARE_TARGET_DAYS)).isoformat())).fetchone()
    if before is None:
        pic.change_text = "No reading 3 to 6 weeks older exists yet, so the change shows from a later reading."
    else:
        pic.change = pic.total - before["total"]
        when = fmt_date(date.fromisoformat(before["seen_date"]))
        pic.change_text = (f"Unchanged since {when} ({before['total']})." if pic.change == 0
                           else f"{'Up' if pic.change > 0 else 'Down'} {abs(pic.change)} from {before['total']} on {when}.")
    senior = set(cfg.hiring.senior_levels)
    counts: dict[str, list[int]] = {}
    for r in conn.execute("SELECT theme, seniority, posted_date FROM postings WHERE account_id = ? AND source = ? AND open = 1", (account_id, a["ats_provider"])):
        c = counts.setdefault(r["theme"], [0, 0])
        c[0] += 1
        c[1] += r["seniority"] in senior
        pic.recent += r["posted_date"] >= (today - timedelta(days=RECENT_DAYS)).isoformat()
    pic.themes = sorted(((THEME_LABEL.get(t, t.replace("_", " ")), n, s) for t, (n, s) in counts.items()), key=lambda x: (-x[1], x[0]))
    try:
        progs = json.loads(now["programs"] or "{}")
    except ValueError:
        progs = {}
    pic.programs = sorted((ProgramLine(name, int(v.get("n", 0)), list(v.get("examples", []))) for name, v in progs.items() if isinstance(v, dict) and v.get("n")),
                          key=lambda p: (-p.n, p.name))
    return pic
