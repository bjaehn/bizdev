"""Finding a company's own job board on its careers page: the board id comes from the company's own site, and is saved only when the feed answers."""

from datetime import date, timedelta

import pytest
from typer.testing import CliRunner

from bd.ats import AtsError, RawPosting, board_from_url
from bd.ats_find import boards_in, due_accounts, find_board, run_find
from bd.cli import app
from tests.conftest import add_account, make_db
from tests.pe_fakes import make_fetcher, page
from tests.test_pe_cli import env  # noqa: F401  (fixture)

TODAY = date(2026, 10, 20)


@pytest.fixture
def cfg(cfg):
    return cfg.model_copy(deep=True)


class FakeAts:
    def __init__(self, feeds=None, error=None):
        self.feeds, self.error, self.calls = feeds or {}, error, []

    def fetch(self, provider, token):
        self.calls.append((provider, token))
        if self.error:
            raise AtsError(self.error)
        return self.feeds.get((provider, token), [RawPosting("Data Engineer", f"https://jobs.lever.co/{token}/1", TODAY)])


def site(body="", title="Acme"):
    return page(f"<html><head><title>{title}</title></head><body>{body}</body></html>")


def test_board_urls_in_every_form_a_company_page_uses():
    assert board_from_url("https://boards.greenhouse.io/acme/jobs/123") == ("greenhouse", "acme")
    assert board_from_url("https://boards.greenhouse.io/embed/job_board?for=acme") == ("greenhouse", "acme")
    assert board_from_url("https://boards.greenhouse.io/embed/job_board/js?for=acme&b=x") == ("greenhouse", "acme")
    assert board_from_url("https://boards-api.greenhouse.io/v1/boards/acme/jobs") == ("greenhouse", "acme")
    assert board_from_url("https://jobs.lever.co/acme") == ("lever", "acme") and board_from_url("https://jobs.ashbyhq.com/acme/abc") == ("ashby", "acme")
    assert board_from_url("https://boards.greenhouse.io/embed/job_board") is None and board_from_url("https://example.com/acme") is None
    assert board_from_url("https://boards.greenhouse.io/embed/job_board?for=../../etc") is None            # a token is letters, digits, - and _ only


def test_a_page_is_searched_for_links_embeds_and_escaped_urls():
    body = ('<a href="https://jobs.lever.co/acme">Jobs</a> <iframe src="//boards.greenhouse.io/embed/job_board?for=other"></iframe>'
            "<script>var u='https://jobs.ashbyhq.com/third';</script> <a href=\"https://boards.greenhouse.io/escaped?x=1&amp;y=2\">x</a>")
    assert boards_in(body) == {("lever", "acme"), ("greenhouse", "other"), ("ashby", "third"), ("greenhouse", "escaped")}
    assert boards_in("<p>Join the team at https://example.com/jobs</p>") == set()


def test_a_board_linked_from_the_home_page_is_found_without_reading_more(cfg):
    f, client, *_ = make_fetcher(cfg, {"https://acme.example/": site('<a href="https://jobs.lever.co/acme">Careers</a>')})
    boards, note = find_board(f, "acme.example")
    assert boards == {("lever", "acme")} and note == "" and client.calls == ["https://acme.example/"]


def test_the_careers_page_the_home_page_links_to_is_followed_and_the_usual_paths_are_tried(cfg):
    f, client, *_ = make_fetcher(cfg, {"https://acme.example/": site('<a href="/work-here">Careers</a>'),
                                       "https://acme.example/work-here": site('<a href="https://boards.greenhouse.io/acme">Open roles</a>')})
    assert find_board(f, "acme.example")[0] == {("greenhouse", "acme")}
    f, client, *_ = make_fetcher(cfg, {"https://acme.example/": site("<p>Hello</p>"), "https://acme.example/careers": site('<iframe src="https://jobs.ashbyhq.com/acme"></iframe>')})
    assert find_board(f, "acme.example")[0] == {("ashby", "acme")}
    f, client, *_ = make_fetcher(cfg, {"https://acme.example/": site("<p>Hello</p>")})
    boards, note = find_board(f, "acme.example")
    assert boards == set() and note == "" and len(client.calls) <= 4                                            # bounded


def test_an_unreadable_home_page_says_so(cfg):
    f, client, *_ = make_fetcher(cfg, {})
    boards, note = find_board(f, "acme.example")
    assert boards == set() and "home page not read" in note


def test_a_board_is_saved_only_from_the_companys_own_site_and_only_when_its_feed_answers(tmp_path, cfg):
    conn = make_db(tmp_path)
    ok = add_account(conn, "Okco", domain="okco.example")
    dead = add_account(conn, "Deadco", domain="deadco.example")
    none = add_account(conn, "Noneco", domain="noneco.example")
    many = add_account(conn, "Manyco", domain="manyco.example")
    nodomain = add_account(conn, "Nodomainco")
    conn.execute("UPDATE accounts SET domain = NULL WHERE id = ?", (nodomain,))
    has = add_account(conn, "Hasco", domain="hasco.example")
    conn.execute("UPDATE accounts SET ats_provider = 'lever', ats_board = 'hasco' WHERE id = ?", (has,))
    gone = add_account(conn, "Goneco", domain="goneco.example", status="excluded")
    conn.commit()
    routes = {"https://okco.example/": site('<a href="https://jobs.lever.co/okco">Careers</a>'),
              "https://deadco.example/": site('<a href="https://jobs.lever.co/deadco">Careers</a>'),
              "https://noneco.example/": site("<p>No jobs here</p>"),
              "https://manyco.example/": site('<a href="https://jobs.lever.co/a">Careers</a><a href="https://jobs.ashbyhq.com/b">More</a>'),
              "https://goneco.example/": site('<a href="https://jobs.lever.co/goneco">Careers</a>')}
    f, client, *_ = make_fetcher(cfg, routes)

    class Ats(FakeAts):
        def fetch(self, provider, token):
            if token == "deadco":
                raise AtsError("HTTP 404")
            return super().fetch(provider, token)
    ats, logs = Ats(), []
    looked, saved = run_find(conn, cfg, f, ats, TODAY, log=logs.append)
    assert (looked, saved) == (4, 1)                                                                            # no domain, already has a board and excluded companies are not looked at
    board = lambda i: tuple(conn.execute("SELECT ats_provider, ats_board FROM accounts WHERE id = ?", (i,)).fetchone())      # noqa: E731
    assert board(ok) == ("lever", "okco") and board(dead) == (None, None) and board(none) == (None, None) and board(many) == (None, None)
    assert board(has) == ("lever", "hasco") and board(nodomain) == (None, None) and board(gone) == (None, None)
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE field = 'ats_board' AND actor = 'ats-find'").fetchone()[0] == 1
    assert any("links to more than one job board" in line for line in logs) and any("feed did not answer" in line for line in logs)
    assert "https://goneco.example/" not in client.calls and "https://nodomainco.example/" not in client.calls


def test_a_company_looked_at_is_not_read_again_for_weeks_but_one_that_could_not_be_read_is_retried(tmp_path, cfg):
    conn = make_db(tmp_path)
    a = add_account(conn, "Noneco", domain="noneco.example")
    b = add_account(conn, "Downco", domain="downco.example")
    conn.commit()
    f, client, *_ = make_fetcher(cfg, {"https://noneco.example/": site("<p>No jobs</p>")})
    assert run_find(conn, cfg, f, FakeAts(), TODAY)[0] == 2
    assert [x.id for x in due_accounts(conn, cfg, TODAY + timedelta(days=14))] == [b]                         # Noneco was looked at; Downco could not be read
    assert [x.id for x in due_accounts(conn, cfg, TODAY + timedelta(days=60))] == [a, b]                      # after hiring.ats_find_every_weeks it is looked at again
    off = cfg.model_copy(deep=True)
    off.hiring.ats_find_every_weeks = 0
    assert due_accounts(conn, off, TODAY) == []


def test_the_per_run_limit_bounds_the_reading(tmp_path, cfg):
    conn = make_db(tmp_path)
    for i in range(5):
        add_account(conn, f"Co{i}", domain=f"co{i}.example")
    conn.commit()
    f, client, *_ = make_fetcher(cfg, {})
    assert run_find(conn, cfg, f, FakeAts(), TODAY, limit=2)[0] == 2


def test_soft_mode_never_fails_the_caller(monkeypatch):
    monkeypatch.delenv("BD_USER_AGENT")
    r = CliRunner().invoke(app, ["ats-find", "--soft"])
    assert r.exit_code == 0 and "were not looked for" in r.output
    assert CliRunner().invoke(app, ["ats-find"]).exit_code == 1
