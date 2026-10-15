from datetime import date, timedelta
from pathlib import Path

import pytest

from bd.config import load_config
from bd.models import ScoringSignal

AS_OF = date(2026, 10, 15)
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def cfg():
    return load_config(ROOT / "config.yaml")


@pytest.fixture
def as_of():
    return AS_OF


def sig(type_, age, conf="High", lines=("Strategy & Operations",), as_of=AS_OF):
    return ScoringSignal(type=type_, event_date=as_of - timedelta(days=age), confidence=conf,
                         service_lines=tuple(lines))


# ---- Stage 2 helpers -------------------------------------------------------------------
from types import SimpleNamespace  # noqa: E402

from bd.accounts import Account  # noqa: E402
from bd.llm import LLMResult, Usage  # noqa: E402
from bd.verify import VerifyResult  # noqa: E402


class FakeLLM:
    """Returns canned replies in order; records every call."""

    def __init__(self, replies, urls=(), usage=None):
        self.replies = list(replies)
        from bd.urls import normalize_url
        self.urls = {normalize_url(u) for u in urls}  # real client stores normalized URLs
        self.usage = usage or Usage(input_tokens=100, output_tokens=50, web_searches=1)
        self.calls = []

    def run(self, system, messages):
        self.calls.append((system, list(messages)))
        text = self.replies.pop(0)
        u = Usage()
        u.add(self.usage)
        return LLMResult(text=text, retrieved_urls=set(self.urls), usage=u,
                         conversation=list(messages) + [{"role": "assistant", "content": text}])


class FakeVerifier:
    def __init__(self, bad=None):
        self.bad = bad or {}  # url -> reason
        self.checked = []

    def verify(self, url):
        self.checked.append(url)
        if url in self.bad:
            return VerifyResult(False, self.bad[url], final_url=url)
        return VerifyResult(True, "ok", status=200, final_url=url)


def account(name="Acme Corp", domain="acme.com", id=1):
    return Account(id=id, name=name, domain=domain)


def block(**kw):
    """A fake SDK content block with model_dump()."""
    return SimpleNamespace(model_dump=lambda: dict(kw), **kw)
