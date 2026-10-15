import httpx2 as httpx
import pytest

from bd.urls import host_in, hostname, normalize_url
from bd.verify import UrlVerifier


def test_normalize_url_equivalences():
    a = normalize_url("https://www.Acme.com/news/ceo/?utm_source=x&b=2&a=1#top")
    assert a == normalize_url("http://acme.com/news/ceo?a=1&b=2")
    assert a != normalize_url("https://acme.com/news/cfo?a=1&b=2")
    assert normalize_url("https://acme.com/") == normalize_url("https://acme.com")


def test_host_in_matches_subdomains_not_suffix_tricks():
    assert host_in("careers.acme.com", ["acme.com"])
    assert host_in("www.acme.com", ["acme.com"])
    assert not host_in("notacme.com", ["acme.com"])
    assert not host_in("acme.com.evil.io", ["acme.com"])
    assert hostname("https://WWW.Acme.com:8443/x") == "acme.com"


def make(cfg, handler, resolver=lambda h: ["93.184.216.34"]):
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    return UrlVerifier(cfg, "test-agent", client=client, resolver=resolver)


def test_ok_and_http_errors(cfg):
    def h(req):
        return httpx.Response({"/ok": 200, "/gone": 404, "/deny": 403, "/boom": 500}[req.url.path])
    v = make(cfg, h)
    assert v.verify("https://acme.com/ok").ok
    for path, reason in (("/gone", "http_404"), ("/deny", "http_403"), ("/boom", "http_500")):
        r = v.verify("https://acme.com" + path)
        assert not r.ok and r.reason == reason


def test_redirects_followed_and_capped(cfg):
    def h(req):
        if req.url.path == "/a":
            return httpx.Response(301, headers={"location": "/b"})
        if req.url.path == "/b":
            return httpx.Response(302, headers={"location": "https://news.acme.com/final"})
        if req.url.path == "/final":
            return httpx.Response(200)
        if req.url.path == "/loop":
            return httpx.Response(302, headers={"location": "/loop"})
        return httpx.Response(404)
    v = make(cfg, h)
    r = v.verify("https://acme.com/a")
    assert r.ok and r.final_url == "https://news.acme.com/final"
    r = v.verify("https://acme.com/loop")
    assert not r.ok and r.reason == "too_many_redirects"


def test_redirect_into_blocked_domain_is_rejected(cfg):
    def h(req):
        return httpx.Response(302, headers={"location": "https://www.linkedin.com/in/someone"})
    r = make(cfg, h).verify("https://acme.com/x")
    assert not r.ok and r.reason == "blocked_domain"


def test_blocked_and_bad_urls_never_fetched(cfg):
    calls = []
    v = make(cfg, lambda req: calls.append(req) or httpx.Response(200))
    assert v.verify("https://www.linkedin.com/company/acme").reason == "blocked_domain"
    assert v.verify("https://www.wsj.com/articles/x").reason == "blocked_domain"
    assert v.verify("ftp://acme.com/x").reason == "bad_url"
    assert v.verify("javascript:alert(1)").reason == "bad_url"
    assert calls == []


def test_private_and_loopback_addresses_blocked(cfg):
    calls = []
    v = make(cfg, lambda req: calls.append(req) or httpx.Response(200), resolver=lambda h: ["10.0.0.5"])
    assert v.verify("https://internal.acme.com/x").reason == "private_address"
    v = make(cfg, lambda req: calls.append(req) or httpx.Response(200), resolver=lambda h: ["127.0.0.1"])
    assert v.verify("http://localhost/x").reason == "private_address"
    v = make(cfg, lambda req: calls.append(req) or httpx.Response(200), resolver=lambda h: ["169.254.169.254"])
    assert v.verify("http://meta.example/x").reason == "private_address"
    assert calls == []


def test_dns_failure_without_proxy_is_rejected(cfg, monkeypatch):
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(k, raising=False)
    def bad(host):
        raise OSError("nxdomain")
    r = make(cfg, lambda req: httpx.Response(200), resolver=bad).verify("https://nope.example/x")
    assert not r.ok and r.reason == "dns_failure"


def test_timeout_and_connect_error(cfg):
    def timeout(req):
        raise httpx.ReadTimeout("slow", request=req)
    def conn(req):
        raise httpx.ConnectError("refused", request=req)
    assert make(cfg, timeout).verify("https://acme.com/x").reason == "timeout"
    assert make(cfg, conn).verify("https://acme.com/x").reason.startswith("connect_error")
