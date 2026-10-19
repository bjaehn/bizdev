"""No dead links: every internal link on the main pages must lead somewhere that exists."""
import html
import re
from datetime import date

import pytest

from bd.pipeline import set_client
from bd.score_store import score_accounts
from tests.conftest import add_account, add_signal
from tests.test_update import app_env  # noqa: F401  (fixture: hosted app)

HREF = re.compile(r'href="(/[^"#]*)"')


def links(body):
    return {html.unescape(h) for h in HREF.findall(body) if not h.startswith("//")}


@pytest.fixture
def seeded(app_env):  # noqa: F811
    e = app_env
    today = date.today()
    ids = []
    for name in ("Acme Corp", "Beta Industries"):
        a = add_account(e.conn, name, industry="Healthcare")
        for t in ("leadership_change", "hiring_cluster", "restructuring_layoffs"):
            add_signal(e.conn, e.cfg, a, t, 4, as_of=today)
        ids.append(a)
    set_client(e.conn, ids[0], True)
    add_account(e.conn, "Waitco", chicagoland="unknown", revenue=None, ownership=None)
    score_accounts(e.conn, e.cfg, today)
    return e, ids


def test_every_link_on_the_main_pages_resolves(seeded):
    e, ids = seeded
    start = ["/", "/week", "/signals", "/signals?group=hiring&days=7", "/help", "/find?q=acme", "/find?q=Zeta+Holdings", "/opportunities", "/opportunities?view=list", "/opportunities?view=chart", "/reports", "/admin", "/lookup?q=acme", "/clients", "/watchlist", "/market", "/hiring",
             "/firms", "/monday", "/accounts", f"/accounts/{ids[0]}", f"/companies/{ids[0]}", "/health", "/settings"]
    seen, bad = set(), []
    for path in start:
        page = e.get(path)
        assert page.code == 200, path
        for href in sorted(links(page.body)):
            if href in seen or href in start:
                continue
            seen.add(href)
            r = e.get(href)
            if r.code not in (200, 303):
                bad.append((path, href, r.code))
    assert not bad, bad
    assert len(seen) > 15          # the crawl really followed links
