"""Find a company's own job board (Greenhouse, Lever or Ashby) by reading its careers page. Free (no model); a few polite page reads per company.

The board id is taken from a link or embed on the company's OWN site, never guessed from its name, and is saved only when the provider's public feed
for it answers with postings. A company with a board gets the free, complete feed (bd.scanners.ats) instead of a model search for postings, which is
cheaper, covers every open role instead of the few a search surfaces, and gives each posting's own date. A page with links to more than one board is
left alone: which one is the company's own is not for code to guess."""

from __future__ import annotations

import html
import re
import sqlite3
from collections.abc import Callable
from datetime import date, timedelta

from bd.accounts import list_accounts
from bd.ats import AtsClient, AtsError, board_from_url
from bd.config import Config
from bd.htmltext import links, parse_html
from bd.postings import learn_board
from bd.sources import FetchError, Fetcher
from bd.urls import hostname

STEP = "ats_find"
CAREER_PATHS = ("/careers", "/jobs")
CAREER_LINK = re.compile(r"^\s*(careers|jobs|join us|work (with|at) us|open (positions|roles)|search jobs|view (all )?(jobs|openings))\b", re.I)
URL = re.compile(r"(?:https?:)?//[^\s\"'<>)\\]+")
MAX_PAGES = 4


def boards_in(body: str) -> set[tuple[str, str]]:
    """Every supported job board a page links to or embeds."""
    out: set[tuple[str, str]] = set()
    for raw in URL.findall(body):
        u = html.unescape(raw.rstrip(".,;"))
        b = board_from_url("https:" + u if u.startswith("//") else u)
        if b:
            out.add(b)
    return out


def find_board(fetcher: Fetcher, domain: str) -> tuple[set[tuple[str, str]], str]:
    """(boards found on the company's own pages, a note). Reads the home page, the careers pages it links to, then the usual careers paths."""
    found: set[tuple[str, str]] = set()
    tried: set[str] = set()
    home = f"https://{domain}/"
    queue = [home]
    reached = 0
    while queue and len(tried) < MAX_PAGES:
        url = queue.pop(0)
        if url in tried:
            continue
        tried.add(url)
        try:
            got = fetcher.get(url, retries=0)
        except FetchError as e:
            if not tried - {url}:
                return found, f"home page not read ({e.reason})"
            continue
        if "html" not in got.content_type:
            continue
        reached += 1
        found |= boards_in(got.body)
        if found:
            break
        if url == home:                                                            # from the home page: follow its own careers links, else the usual paths
            own = [h for h, text, _a in links(parse_html(got.body).root, got.final_url) if CAREER_LINK.match(text) and hostname(h) == hostname(got.final_url)]
            queue += own[:2] + [f"https://{domain}{p}" for p in CAREER_PATHS]
    return found, "" if reached else "no page could be read"


def due_accounts(conn: sqlite3.Connection, cfg: Config, today: date, only: str | None = None) -> list:
    weeks = cfg.hiring.ats_find_every_weeks
    if not weeks:
        return []
    since = (today - timedelta(days=weeks * 7)).isoformat()
    looked = {r[0] for r in conn.execute("SELECT DISTINCT account_id FROM runs WHERE step = ? AND run_date >= ? AND errors IS NULL", (STEP, since))}
    failed = {r[0] for r in conn.execute("SELECT DISTINCT account_id FROM runs WHERE step = ? AND run_date >= ? AND errors IS NOT NULL", (STEP, since))}
    out = [a for a in list_accounts(conn, status=None) if a.domain and not a.ats_board and a.status != "excluded" and a.id not in looked
           and (not only or only.lower() in a.name.lower())]
    out.sort(key=lambda a: a.id in failed)                       # a company whose pages could not be read goes last, so it cannot keep the others from their turn
    return out


def run_find(conn: sqlite3.Connection, cfg: Config, fetcher: Fetcher, ats: AtsClient, today: date, limit: int | None = None,
             only: str | None = None, log: Callable[[str], None] = lambda s: None) -> tuple[int, int]:
    """Returns (companies looked at, boards saved). A company whose pages could not be read is not marked as looked at, so it is tried again."""
    todo = due_accounts(conn, cfg, today, only)[: cfg.hiring.ats_find_per_run if limit is None else limit]
    saved = 0
    for a in todo:
        try:
            boards, note = find_board(fetcher, a.domain)
        except Exception as e:      # noqa: BLE001  one company's surprise must not stop the search for the rest
            boards, note = set(), f"unexpected {type(e).__name__}: {str(e)[:120]}"
        err = note or None
        if len(boards) == 1:
            provider, token = next(iter(boards))
            try:
                feed = ats.fetch(provider, token)
            except AtsError as e:
                err = f"board {provider}:{token} found on the site but its feed did not answer ({e})"
                log(f"  {a.name}: {err}")
            else:
                with conn:
                    if learn_board(conn, a.id, (provider, token), actor="ats-find"):
                        saved += 1
                log(f"  {a.name}: job board found on its own site: {provider}:{token} ({len(feed)} dated postings in its feed)")
        elif len(boards) > 1:
            log(f"  {a.name}: links to more than one job board ({', '.join(f'{p}:{t}' for p, t in sorted(boards))}); left alone")
        elif not err:
            log(f"  {a.name}: no supported job board linked from its careers pages")
        else:
            log(f"  {a.name}: {err}")
        with conn:
            conn.execute("INSERT INTO runs(run_date, step, account_id, accounts_scanned, signals_found, tokens, est_cost, errors) VALUES (?,?,?,?,?,?,?,?)",
                         (today.isoformat(), STEP, a.id, 1, 0, 0, 0.0, err))
    return len(todo), saved
