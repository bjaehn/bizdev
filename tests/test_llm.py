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


# ---- a failed search is not "found nothing" -------------------------------------------------------------------------------------------
def _search_error(code):
    return block(type="web_search_tool_result", content={"type": "web_search_tool_result_error", "error_code": code})


def test_failed_searches_are_recorded_by_code_and_good_ones_are_not():
    from bd.llm import search_errors_from
    content = [block(type="web_search_tool_result", content=[{"type": "web_search_result", "url": "https://sec.gov/a", "title": "t"}]),
               _search_error("too_many_requests"), _search_error("unavailable"), block(type="text", text="x", citations=None)]
    assert search_errors_from(content) == ["too_many_requests", "unavailable"]
    assert search_errors_from([block(type="web_search_tool_result", content={"type": "web_search_tool_result_error"})]) == ["unknown"]


def test_the_client_returns_the_error_codes_across_continuations(cfg):
    c, _ = client(cfg, [resp([_search_error("too_many_requests")], stop="pause_turn"),
                        resp([_search_error("too_many_requests"), block(type="text", text=GOOD, citations=None)])])
    r = c.run("S", [{"role": "user", "content": "go"}])
    assert r.search_errors == ["too_many_requests", "too_many_requests"]


def test_a_search_failure_is_reported_unless_the_only_error_is_running_out_of_allowed_searches():
    from bd.llm import LLMResult, search_failure
    assert search_failure(LLMResult("x")) is None
    assert search_failure(LLMResult("x", search_errors=["max_uses_exceeded"])) is None             # the model searched as much as it was allowed
    msg = search_failure(LLMResult("x", search_errors=["too_many_requests", "unavailable", "too_many_requests", "max_uses_exceeded"]))
    assert msg.startswith("search tool errors: too_many_requests x2, unavailable x1") and "not evidence" in msg and "max_uses" not in msg


def test_generate_validated_keeps_the_error_codes_from_the_first_call_and_the_retry():
    class Twice(FakeLLM):
        def run(self, system, messages, **kw):
            r = super().run(system, messages, **kw)
            r.search_errors = ["unavailable"]
            return r
    llm = Twice(["not json", GOOD])
    _, res = generate_validated(llm, "s", "u", ScanResponse, retries=1)
    assert res.search_errors == ["unavailable", "unavailable"]


# ---- every search rejected as invalid_tool_input: one repeat with the basic search tool ------------------------------------------------------------
def test_a_call_whose_searches_were_all_rejected_is_repeated_once_with_the_basic_tool(cfg):
    c, msgs = client(cfg, [resp([_search_error("invalid_tool_input"), _search_error("invalid_tool_input"), block(type="text", text="nothing", citations=None)], out=7, searches=2),
                           resp([block(type="web_search_tool_result", content=[{"type": "web_search_result", "url": "https://acme.com/news/cio", "title": "t"}]),
                                 block(type="text", text=GOOD, citations=None)], out=11)])
    r = c.run("S", [{"role": "user", "content": "go"}])
    assert [m["tools"][0]["type"] for m in msgs.calls] == [cfg.llm.web_search_tool_type, "web_search_20250305"]
    assert r.fallback_used and r.search_errors == [] and normalize_url("https://acme.com/news/cio") in r.retrieved_urls
    assert r.usage.output_tokens == 7 + 11 and r.usage.web_searches == 2 + 1               # the first attempt's spend is still counted


def test_no_repeat_when_something_came_back_when_the_error_is_another_kind_or_when_it_is_switched_off(cfg):
    partial = [block(type="web_search_tool_result", content=[{"type": "web_search_result", "url": "https://acme.com/a", "title": "t"}]),
               _search_error("invalid_tool_input"), block(type="text", text=GOOD, citations=None)]
    c, msgs = client(cfg, [resp(partial)])
    r = c.run("S", [{"role": "user", "content": "go"}])
    assert len(msgs.calls) == 1 and not r.fallback_used and r.search_errors == ["invalid_tool_input"]
    c, msgs = client(cfg, [resp([_search_error("too_many_requests"), block(type="text", text="x", citations=None)])])
    assert not c.run("S", [{"role": "user", "content": "go"}]).fallback_used and len(msgs.calls) == 1
    off = cfg.model_copy(deep=True)
    off.llm.web_search_fallback_tool_type = None
    c, msgs = client(off, [resp([_search_error("invalid_tool_input"), block(type="text", text="x", citations=None)])])
    assert c.run("S", [{"role": "user", "content": "go"}]).search_errors == ["invalid_tool_input"] and len(msgs.calls) == 1


def test_a_second_rejection_is_still_reported_as_a_failure_not_as_found_nothing(cfg):
    from bd.llm import search_failure
    c, msgs = client(cfg, [resp([_search_error("invalid_tool_input"), block(type="text", text="x", citations=None)]),
                           resp([_search_error("invalid_tool_input"), block(type="text", text="x", citations=None)])])
    r = c.run("S", [{"role": "user", "content": "go"}])
    assert len(msgs.calls) == 2 and r.fallback_used and "invalid_tool_input" in search_failure(r)      # one repeat only, and the failure still shows


def test_after_one_repeat_fails_too_no_more_repeats_are_made_in_that_run(cfg):
    bad = lambda: resp([_search_error("invalid_tool_input"), block(type="text", text="x", citations=None)])      # noqa: E731
    c, msgs = client(cfg, [bad(), bad(), bad()])
    c.run("S", [{"role": "user", "content": "go"}])                       # primary rejected, the repeat rejected too: two calls
    c.run("S", [{"role": "user", "content": "go"}])                       # the next call is not repeated: one call, so the spend is not doubled again
    assert [m["tools"][0]["type"] for m in msgs.calls] == [cfg.llm.web_search_tool_type, "web_search_20250305", cfg.llm.web_search_tool_type]
