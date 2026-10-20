"""Test doubles for the page reader. Synthetic pages live only in tests; nothing here is ever reachable from the application."""

from __future__ import annotations

import httpx2 as httpx

from bd.sources import Fetcher


class FakeResp:
    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status_code, self.headers, self._body = status, headers, body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_bytes(self):
        for i in range(0, len(self._body), 64):
            yield self._body[i:i + 64]


def page(body: str, status: int = 200, ctype: str = "text/html; charset=utf-8", **headers) -> tuple:
    return (status, {"content-type": ctype, **headers}, body.encode("utf-8"))


class FakeClient:
    """routes: url -> a page tuple, an exception to raise, or a list of those (consumed in order, the last one repeating)."""

    def __init__(self, routes: dict) -> None:
        self.routes, self.calls = dict(routes), []

    def stream(self, method, url, **kw):
        self.calls.append(url)
        r = self.routes.get(url, (404, {"content-type": "text/plain"}, b"not found"))
        if isinstance(r, list):
            r = r.pop(0) if len(r) > 1 else r[0]
        if isinstance(r, Exception):
            raise r
        return FakeResp(*r)

    def close(self):
        pass


class OpenHosts:
    """A verifier stand-in: every host is public unless listed."""

    def __init__(self, bad: dict[str, str] | None = None) -> None:
        self.bad = bad or {}

    def host_problem(self, host: str):
        return self.bad.get(host)


class Clock:
    def __init__(self) -> None:
        self.t, self.sleeps = 1000.0, []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def make_fetcher(cfg, routes, bad_hosts=None, robots=False):
    c = cfg.model_copy(deep=True)
    c.pe_portfolio.fetch.respect_robots = robots
    c.pe_portfolio.fetch.min_seconds_between_requests = 0
    clock = Clock()
    client = FakeClient(routes)
    return Fetcher(c, "SEI Test ops@example.com", client=client, verifier=OpenHosts(bad_hosts), sleep=clock.sleep, monotonic=clock.monotonic), client, clock, c


Timeout = httpx.TimeoutException
