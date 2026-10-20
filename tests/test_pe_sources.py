from datetime import datetime, timedelta, timezone

import pytest

from bd.sources import FetchError, recently_read, store_snapshot, visible_text, latest_snapshot
from tests.conftest import make_db
from tests.pe_fakes import Timeout, make_fetcher, page

URL = "https://firm.example/portfolio"


@pytest.fixture
def cfg(cfg):
    return cfg.model_copy(deep=True)


def test_a_page_is_read_hashed_and_decoded(cfg):
    f, client, _c, _cfg = make_fetcher(cfg, {URL: page("<html><body><p>Café Corp</p></body></html>")})
    got = f.get(URL)
    assert got.status == 200 and "Café Corp" in got.body and len(got.content_hash) == 64 and not got.truncated and got.content_type == "text/html"
    assert f.get(URL).content_hash == got.content_hash                                     # the same content hashes the same


def test_a_response_larger_than_the_limit_is_cut_and_marked(cfg):
    f, _client, _c, c = make_fetcher(cfg, {URL: page("<p>" + "x" * 5000 + "</p>")})
    c.pe_portfolio.fetch.max_bytes = 1000
    got = f.get(URL)
    assert got.truncated and len(got.body.encode()) == 1000


def test_redirects_are_followed_and_each_hop_is_checked(cfg):
    routes = {URL: (301, {"location": "https://firm.example/companies"}, b""), "https://firm.example/companies": page("<p>ok</p>")}
    f, client, _c, _cfg = make_fetcher(cfg, routes)
    assert f.get(URL).final_url == "https://firm.example/companies" and client.calls == [URL, "https://firm.example/companies"]
    f2, _cl, _c2, _cfg2 = make_fetcher(cfg, {URL: (302, {"location": "https://www.linkedin.com/company/x"}, b"")})
    with pytest.raises(FetchError) as e:
        f2.get(URL)
    assert e.value.reason == "blocked_domain"
    f3, _cl3, _c3, _cfg3 = make_fetcher(cfg, {URL: (302, {"location": URL}, b"")})
    with pytest.raises(FetchError) as e:
        f3.get(URL)
    assert e.value.reason == "too_many_redirects"


def test_private_addresses_and_bad_urls_are_refused(cfg):
    f, client, _c, _cfg = make_fetcher(cfg, {"http://10.0.0.5/x": page("secret")}, bad_hosts={"10.0.0.5": "private_address"})
    with pytest.raises(FetchError) as e:
        f.get("http://10.0.0.5/x")
    assert e.value.reason == "private_address" and client.calls == []
    with pytest.raises(FetchError) as e2:
        f.get("ftp://firm.example/x")
    assert e2.value.reason == "bad_url"


def test_transient_failures_are_retried_with_backoff_and_client_errors_are_not(cfg):
    f, client, clock, _cfg = make_fetcher(cfg, {URL: [page("busy", 503), page("busy", 429), page("<p>fine</p>")]})
    assert "fine" in f.get(URL).body and clock.sleeps == [2.0, 4.0]
    f2, client2, clock2, _cfg2 = make_fetcher(cfg, {URL: page("no", 404)})
    with pytest.raises(FetchError) as e:
        f2.get(URL)
    assert e.value.reason == "http_404" and client2.calls == [URL] and clock2.sleeps == []
    f3, client3, _c3, _cfg3 = make_fetcher(cfg, {URL: page("busy", 503)})
    with pytest.raises(FetchError) as e3:
        f3.get(URL)
    assert e3.value.reason == "http_503" and len(client3.calls) == 3                         # 1 try + 2 retries
    f4, client4, _c4, _cfg4 = make_fetcher(cfg, {URL: Timeout("slow")})
    with pytest.raises(FetchError) as e4:
        f4.get(URL)
    assert e4.value.reason == "timeout" and len(client4.calls) == 3


def test_requests_to_one_host_are_spaced_out(cfg):
    f, _client, clock, c = make_fetcher(cfg, {URL: page("<p>a</p>"), "https://firm.example/b": page("<p>b</p>"), "https://other.example/": page("<p>c</p>")})
    c.pe_portfolio.fetch.min_seconds_between_requests = 2
    f.get(URL)
    f.get("https://firm.example/b")
    assert clock.sleeps == [2.0]
    f.get("https://other.example/")
    assert clock.sleeps == [2.0]                                                              # a different host is not delayed


def test_robots_txt_is_respected_and_a_server_that_cannot_answer_is_not_a_licence(cfg):
    robots = ("User-agent: *\nDisallow: /private/\n").encode()
    routes = {"https://firm.example/robots.txt": (200, {"content-type": "text/plain"}, robots), URL: page("<p>ok</p>"),
              "https://firm.example/private/x": page("<p>no</p>")}
    f, client, _c, _cfg = make_fetcher(cfg, routes, robots=True)
    assert "ok" in f.get(URL).body
    with pytest.raises(FetchError) as e:
        f.get("https://firm.example/private/x")
    assert e.value.reason == "robots_disallowed" and "https://firm.example/private/x" not in client.calls
    f2, _cl2, _c2, _cfg2 = make_fetcher(cfg, {URL: page("<p>ok</p>")}, robots=True)           # robots.txt is a 404: there are no rules
    assert "ok" in f2.get(URL).body
    f3, cl3, _c3, _cfg3 = make_fetcher(cfg, {"https://firm.example/robots.txt": page("err", 500), URL: page("<p>ok</p>")}, robots=True)
    with pytest.raises(FetchError) as e3:
        f3.get(URL)
    assert e3.value.reason == "robots_unavailable" and URL not in cl3.calls


def test_a_bot_check_page_is_a_failure_not_an_empty_portfolio(cfg):
    f, _c, _clk, _cfg = make_fetcher(cfg, {URL: page("<html><body><h1>Please verify you are human</h1><p>Complete the CAPTCHA</p></body></html>")})
    with pytest.raises(FetchError) as e:
        f.get(URL)
    assert e.value.reason == "challenge_page"
    long = "<html><body>" + "<p>Our portfolio companies include many firms. captcha is mentioned once.</p>" * 120 + "</body></html>"
    f2, _c2, _clk2, _cfg2 = make_fetcher(cfg, {URL: page(long)})
    assert f2.get(URL).status == 200                                                          # a real page that mentions it in passing is fine


def test_content_that_is_not_a_web_page_is_refused(cfg):
    f, _c, _clk, _cfg = make_fetcher(cfg, {URL: (200, {"content-type": "application/pdf"}, b"%PDF-1.4")})
    with pytest.raises(FetchError) as e:
        f.get(URL)
    assert e.value.reason.startswith("unsupported_content_type")


def test_snapshots_store_the_text_once_move_forward_when_seen_again_and_keep_the_newest(cfg, tmp_path):
    conn = make_db(tmp_path)
    f, _client, _c, c = make_fetcher(cfg, {URL: page("<html><head><title>Portfolio</title></head><body><p>Acme</p></body></html>")})
    got = f.get(URL)
    with conn:
        a = store_snapshot(conn, c, None, got)
    got2 = type(got)(**{**got.__dict__, "fetched_at": "2099-01-01T00:00:00+00:00"})
    with conn:
        b = store_snapshot(conn, c, None, got2)
    assert a == b and conn.execute("SELECT COUNT(*) FROM pe_snapshots").fetchone()[0] == 1
    row = latest_snapshot(conn, URL)
    assert row["text"] == "Acme" and row["title"] == "Portfolio" and row["fetched_at"] == "2099-01-01T00:00:00+00:00"
    for i in range(7):                                                                        # a changing page: only the newest few are kept
        changed = type(got)(**{**got.__dict__, "content_hash": f"h{i}", "fetched_at": f"2099-02-0{i + 1}T00:00:00+00:00", "body": f"<p>v{i}</p>"})
        with conn:
            store_snapshot(conn, c, None, changed)
    assert conn.execute("SELECT COUNT(*) FROM pe_snapshots WHERE url = ?", (URL,)).fetchone()[0] == c.pe_portfolio.snapshot_keep


def test_a_recent_read_is_not_repeated_until_the_cache_window_passes(cfg, tmp_path):
    conn = make_db(tmp_path)
    f, _client, _c, c = make_fetcher(cfg, {URL: page("<p>x</p>")})
    got = f.get(URL)
    with conn:
        store_snapshot(conn, c, None, got)
    read_at = datetime.fromisoformat(got.fetched_at)
    assert recently_read(conn, URL, c, read_at + timedelta(hours=1)) is not None
    assert recently_read(conn, URL, c, read_at + timedelta(hours=c.pe_portfolio.fetch.cache_hours + 1)) is None
    assert recently_read(conn, "https://firm.example/never", c, datetime.now(timezone.utc)) is None


def test_json_is_stored_decoded_so_an_excerpt_is_on_the_page(cfg):
    f, _c, _clk, c = make_fetcher(cfg, {URL: page('{"title": {"rendered": "Caf\\u00e9 \\"Corp\\""}}', ctype="application/json")})
    text, truncated = visible_text(f.get(URL), c.pe_portfolio.text_cap_bytes)
    assert "Café" in text and not truncated
