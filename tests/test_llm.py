import pytest

from bd.llm import (AnthropicWebSearchClient, InvalidJSON, LLMError, LLMRefusal, Usage, extract_json,
                    generate_validated, retrieved_urls_from)
from bd.models import ScanResponse
from bd.urls import normalize_url
from tests.conftest import FakeLLM, block
from types import SimpleNamespace

GOOD = ('{"signals":[{"type":"leadership_change","event_date":"2026-09-20","summary":"New CIO named.",'
        '"source_url":"https://acme.com/news/cio","source_type":"company_release","confidence":"High",'
        '"service_lines":["AI & Technology"],"role_or_person":"Pat Lee, CIO"}]}')


def test_extract_json_variants():
    assert extract_json('{"signals": []}') == {"signals": []}
    assert extract_json('Here you go:\n```json\n{"signals": []}\n```') == {"signals": []}
    assert extract_json('{"note": 1} then {"signals": [1]} trailing')["signals"] == [1]
    with pytest.raises(ValueError):
        extract_json("no json here")
    with pytest.raises(ValueError):
        extract_json('{"other": []}')


def test_generate_validated_ok_first_time():
    llm = FakeLLM([GOOD])
    resp, res = generate_validated(llm, "sys", "usr", ScanResponse)
    assert len(resp.signals) == 1 and len(llm.calls) == 1
    assert res.usage.total_tokens == 150


def test_one_retry_on_invalid_json_then_success_and_usage_merged():
    llm = FakeLLM(["not json at all", GOOD], urls={"acme.com/news/cio"})
    resp, res = generate_validated(llm, "sys", "usr", ScanResponse, retries=1)
    assert len(resp.signals) == 1 and len(llm.calls) == 2
    retry_msgs = llm.calls[1][1]
    assert retry_msgs[-1]["role"] == "user" and "could not be parsed" in retry_msgs[-1]["content"]
    assert retry_msgs[-2] == {"role": "assistant", "content": "not json at all"}
    assert res.usage.total_tokens == 300 and res.usage.web_searches == 2


def test_schema_invalid_triggers_retry_then_fails_after_one():
    bad = GOOD.replace("leadership_change", "made_up_type")
    llm = FakeLLM([bad, bad])
    with pytest.raises(InvalidJSON):
        generate_validated(llm, "sys", "usr", ScanResponse, retries=1)
    assert len(llm.calls) == 2  # exactly one retry


def test_summary_over_25_words_is_invalid():
    bad = GOOD.replace("New CIO named.", " ".join(["word"] * 26))
    with pytest.raises(InvalidJSON):
        generate_validated(FakeLLM([bad, bad]), "s", "u", ScanResponse)


def test_retrieved_urls_only_from_tool_results_and_citations():
    content = [
        block(type="server_tool_use", name="web_search", input={"query": "https://invented.example/q"}),
        block(type="web_search_tool_result", content=[
            {"type": "web_search_result", "url": "https://www.acme.com/news/cio/", "title": "t"},
            {"type": "web_search_result", "url": "https://sec.gov/a", "title": "t"}]),
        block(type="web_search_tool_result", content={"type": "web_search_tool_result_error", "error_code": "unavailable"}),
        block(type="text", text="x", citations=[{"type": "web_search_result_location", "url": "https://prnewswire.com/p"}]),
        block(type="text", text="https://model-wrote-this.example/page", citations=None),
    ]
    urls = retrieved_urls_from(content)
    assert urls == {normalize_url(u) for u in ("https://acme.com/news/cio", "https://sec.gov/a", "https://prnewswire.com/p")}


class FakeMessages:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def create(self, **kw):
        self.calls.append(kw)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def resp(content, stop="end_turn", out=10, searches=1):
    usage = SimpleNamespace(input_tokens=100, output_tokens=out, cache_read_input_tokens=0,
                            cache_creation_input_tokens=0, server_tool_use=SimpleNamespace(web_search_requests=searches))
    return SimpleNamespace(content=content, stop_reason=stop, usage=usage, stop_details=None)


def client(cfg, responses):
    msgs = FakeMessages(responses)
    return AnthropicWebSearchClient(cfg, model="m-1", client=SimpleNamespace(messages=msgs)), msgs


def test_request_shape_and_text(cfg):
    c, msgs = client(cfg, [resp([block(type="text", text=GOOD, citations=None)])])
    r = c.run("SYS", [{"role": "user", "content": "go"}])
    kw = msgs.calls[0]
    assert kw["model"] == "m-1" and kw["system"] == "SYS" and kw["max_tokens"] == cfg.llm.max_tokens
    tool = kw["tools"][0]
    assert tool["type"] == "web_search_20260209" and tool["name"] == "web_search"
    assert tool["max_uses"] == cfg.llm.web_search_max_uses
    assert "linkedin.com" in tool["blocked_domains"]
    assert "allowed_domains" not in tool and len(kw["tools"]) == 1  # no separate code_execution tool
    assert "thinking" not in kw and "tool_choice" not in kw and "temperature" not in kw
    assert r.text == GOOD and r.usage.web_searches == 1


def test_pause_turn_resumes_without_extra_user_message(cfg):
    paused = [block(type="server_tool_use", name="web_search", input={}), ]
    final = [block(type="text", text="done", citations=None)]
    c, msgs = client(cfg, [resp(paused, stop="pause_turn", out=5, searches=2), resp(final, out=7, searches=1)])
    r = c.run("S", [{"role": "user", "content": "go"}])
    second = msgs.calls[1]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant"]
    assert second[1]["content"] is paused
    assert r.text == "done" and r.usage.output_tokens == 12 and r.usage.web_searches == 3


def test_too_many_continuations(cfg):
    n = cfg.llm.max_continuations + 1
    c, _ = client(cfg, [resp([], stop="pause_turn") for _ in range(n)])
    with pytest.raises(LLMError, match="pause_turn"):
        c.run("S", [{"role": "user", "content": "go"}])


def test_refusal_and_api_errors_raise_llm_errors(cfg):
    import anthropic, httpx2
    c, _ = client(cfg, [resp([], stop="refusal")])
    with pytest.raises(LLMRefusal):
        c.run("S", [{"role": "user", "content": "go"}])
    err = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com"))
    c, _ = client(cfg, [err])
    with pytest.raises(LLMError, match="APIConnectionError"):
        c.run("S", [{"role": "user", "content": "go"}])


def test_workspace_header_is_sent_only_when_configured(cfg, monkeypatch):
    import anthropic as sdk
    seen = {}

    class Spy:
        def __init__(self, **kw):
            seen.update(kw)

    monkeypatch.setattr(sdk, "Anthropic", Spy)
    monkeypatch.delenv("ANTHROPIC_WORKSPACE_ID", raising=False)
    AnthropicWebSearchClient(cfg, model="m", api_key="k")
    assert seen["default_headers"] is None
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", " wrkspc_123 ")
    AnthropicWebSearchClient(cfg, model="m", api_key="k")
    assert seen["default_headers"] == {"anthropic-workspace-id": "wrkspc_123"}
    AnthropicWebSearchClient(cfg, model="m", api_key="k", workspace_id="wrkspc_explicit")
    assert seen["default_headers"] == {"anthropic-workspace-id": "wrkspc_explicit"}


def test_a_request_over_the_models_long_prompt_threshold_is_counted_in_the_long_buckets(cfg):
    big = SimpleNamespace(input_tokens=150_000, output_tokens=40, cache_read_input_tokens=0, cache_creation_input_tokens=0,
                          server_tool_use=SimpleNamespace(web_search_requests=3))
    small = resp([block(type="text", text=GOOD, citations=None)], out=7)
    first = SimpleNamespace(content=[block(type="server_tool_use", name="web_search", input={})], stop_reason="pause_turn", usage=big, stop_details=None)
    msgs = FakeMessages([first, small])
    c = AnthropicWebSearchClient(cfg, model="claude-haiku-5-5", client=SimpleNamespace(messages=msgs))
    r = c.run("S", [{"role": "user", "content": "go"}])
    assert (r.usage.long_input_tokens, r.usage.long_output_tokens, r.usage.input_tokens, r.usage.output_tokens) == (150_000, 40, 100, 7)
    from bd.cost import estimate_cost
    assert estimate_cost(r.usage, "claude-haiku-5-5", cfg) == pytest.approx(150_000 * 0.50e-6 + 40 * 2.50e-6 + 100 * 0.10e-6 + 7 * 0.50e-6 + 4 * 0.01, abs=1e-5)
    # the same big request on a model with one rate stays in the normal buckets
    msgs = FakeMessages([first, small])
    c = AnthropicWebSearchClient(cfg, model="claude-sonnet-5-5", client=SimpleNamespace(messages=msgs))
    assert c.run("S", [{"role": "user", "content": "go"}]).usage.long_input_tokens == 0
