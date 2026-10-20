"""Claude calls with the web search tool, plus JSON parsing/validation with one retry.

Nothing here decides what is a valid signal - that is code in bd/scanners/base.py. This module
only gets text back, records which URLs the search tool actually returned (so a URL the model
invented can be detected), and counts tokens.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from bd.config import Config
from bd.llm_usage import Usage  # noqa: F401  (re-exported: bd.llm.Usage is the public name)
from bd.urls import normalize_url

M = TypeVar("M", bound=BaseModel)


class LLMError(Exception):
    """`usage` carries tokens already spent when the call failed, so cost is logged even on failure."""

    def __init__(self, msg: str = "", usage: "Usage | None" = None) -> None:
        super().__init__(msg)
        self.usage = usage if usage is not None else Usage()


class LLMRefusal(LLMError):
    pass


class InvalidJSON(LLMError):
    pass


@dataclass
class LLMResult:
    text: str
    retrieved_urls: set[str] = field(default_factory=set)  # normalized (bd.urls.normalize_url)
    usage: Usage = field(default_factory=Usage)
    conversation: list[dict[str, Any]] = field(default_factory=list)  # full messages incl. final reply
    search_errors: list[str] = field(default_factory=list)   # error codes of web searches that failed (rate limit, tool unavailable...)
    fallback_used: bool = False                               # the call was repeated once with the basic search tool (see Llm.web_search_fallback_tool_type)


class LLM(Protocol):
    def run(self, system: str, messages: list[dict[str, Any]], **kwargs: Any) -> LLMResult: ...


def _dump(block: Any) -> dict[str, Any]:
    if isinstance(block, dict):
        return block
    return block.model_dump()


def _collect_urls(obj: Any, out: set[str]) -> None:
    if isinstance(obj, dict):
        u = obj.get("url")
        if isinstance(u, str) and u.startswith(("http://", "https://")):
            out.add(normalize_url(u))
        for v in obj.values():
            _collect_urls(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect_urls(v, out)


def retrieved_urls_from(content: list[Any]) -> set[str]:
    """URLs the server-side search/fetch tools actually returned, plus URLs cited in the text.
    Error results carry no URLs, so they add nothing."""
    urls: set[str] = set()
    for b in content:
        d = _dump(b)
        t = d.get("type", "")
        if t in ("web_search_tool_result", "web_fetch_tool_result"):
            _collect_urls(d.get("content"), urls)
        elif t == "text":
            _collect_urls(d.get("citations") or [], urls)
    return urls


BENIGN_SEARCH_ERRORS = frozenset({"max_uses_exceeded"})      # the model asked for more searches than it was allowed: it has searched, so not a failure


def search_errors_from(content: list[Any]) -> list[str]:
    """Error codes of the web searches in a reply that failed. A failed search returns an error object instead of results, so it adds no URLs."""
    codes: list[str] = []
    for b in content:
        d = _dump(b)
        c = d.get("content")
        if d.get("type") == "web_search_tool_result" and isinstance(c, dict) and str(c.get("type", "")).endswith("_error"):
            codes.append(str(c.get("error_code") or "unknown"))
    return codes


def search_failure(result: "LLMResult") -> str | None:
    """A sentence when the search tool itself failed during the call, else None. Without it an empty answer from a rate-limited or unavailable
    search tool looks exactly like 'nothing found'. Callers use it only when nothing usable came back."""
    bad = [c for c in result.search_errors if c not in BENIGN_SEARCH_ERRORS]
    if not bad:
        return None
    counts = Counter(bad)
    return "search tool errors: " + ", ".join(f"{c} x{n}" for c, n in sorted(counts.items())) + " (the search did not run, so an empty answer is not evidence)"


def _usage_from(u: Any) -> Usage:
    g = lambda o, n: (getattr(o, n, None) or 0) if o is not None else 0  # noqa: E731
    return Usage(
        input_tokens=g(u, "input_tokens"),
        output_tokens=g(u, "output_tokens"),
        cache_read_tokens=g(u, "cache_read_input_tokens"),
        cache_creation_tokens=g(u, "cache_creation_input_tokens"),
        web_searches=g(getattr(u, "server_tool_use", None), "web_search_requests"),
    )


class AnthropicWebSearchClient:
    """One Messages API call (resuming pause_turn) with the web search tool enabled.

    The model id is supplied by the caller (env var ANTHROPIC_MODEL); no thinking or effort
    parameters are sent so the same code works across models.
    """

    def __init__(self, cfg: Config, model: str, client: Any | None = None, api_key: str | None = None,
                 web_search: bool = True, workspace_id: str | None = None) -> None:
        self._cfg = cfg
        self._model = model
        self._web_search = web_search  # False = plain text call (used by the briefer)
        self._fallback_failed = False   # the repeat with the basic search tool failed too: stop repeating for the rest of this run (it would only double the spend)
        # An API key that is not scoped to one workspace needs the workspace named on every request. Environment only
        # (ANTHROPIC_WORKSPACE_ID); a key that is already workspace-scoped needs nothing.
        ws = workspace_id if workspace_id is not None else os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip()
        headers = {"anthropic-workspace-id": ws} if ws else None
        self._client = client or anthropic.Anthropic(api_key=api_key, default_headers=headers)

    def _tools(self, allowed_domains: list[str] | None = None, max_uses: int | None = None, tool_type: str | None = None) -> list[dict[str, Any]]:
        tool: dict[str, Any] = {
            "type": tool_type or self._cfg.llm.web_search_tool_type,
            "name": "web_search",
            "max_uses": max_uses or self._cfg.llm.web_search_max_uses,
        }
        if allowed_domains:      # the API accepts either an allow list or a block list, not both
            tool["allowed_domains"] = list(allowed_domains)
        else:
            tool["blocked_domains"] = list(self._cfg.sources.blocked_domains)
        return [tool]

    def run(self, system: str, messages: list[dict[str, Any]], allowed_domains: list[str] | None = None,
            max_uses: int | None = None, tool_type: str | None = None) -> LLMResult:
        convo = list(messages)
        usage, urls, search_errors = Usage(), set(), []
        resp = None
        for attempt in range(self._cfg.llm.max_continuations + 1):
            try:
                extra: dict[str, Any] = {"tools": self._tools(allowed_domains, max_uses, tool_type)} if self._web_search else {}
                resp = self._client.messages.create(
                    model=self._model, max_tokens=self._cfg.llm.max_tokens,
                    system=system, messages=convo, **extra,
                )
            except anthropic.APIError as e:
                raise LLMError(f"{type(e).__name__}: {e}", usage) from e
            one = _usage_from(resp.usage)
            price = self._cfg.pricing.models.get(self._model)
            long_over = price.long_over_tokens if price else None
            usage.add(one.as_long() if long_over and one.prompt_tokens > long_over else one)
            urls |= retrieved_urls_from(resp.content)
            search_errors += search_errors_from(resp.content)
            if resp.stop_reason == "refusal":
                cat = getattr(getattr(resp, "stop_details", None), "category", None)
                raise LLMRefusal(f"model refused (category={cat})", usage)
            if resp.stop_reason != "pause_turn":
                break
            # Resume: re-send the paused assistant turn; the server continues by itself.
            convo = convo + [{"role": "assistant", "content": resp.content}]
        else:
            raise LLMError("too many pause_turn continuations", usage)
        bad = {c for c in search_errors if c not in BENIGN_SEARCH_ERRORS}
        fallback = self._cfg.llm.web_search_fallback_tool_type
        if (self._web_search and not urls and bad == {"invalid_tool_input"} and fallback and not self._fallback_failed
                and (tool_type or self._cfg.llm.web_search_tool_type) != fallback):
            # Every search in this call was rejected before it ran and nothing came back. The tokens are spent either way, so the call is repeated once
            # with the basic search tool rather than leaving the company, segment or agency unchecked until next week.
            again = self.run(system, messages, allowed_domains, max_uses, tool_type=fallback)
            again.usage.add(usage)
            again.fallback_used = True
            if not again.retrieved_urls and {c for c in again.search_errors if c not in BENIGN_SEARCH_ERRORS} == {"invalid_tool_input"}:
                self._fallback_failed = True
            return again
        text = "".join(_dump(b).get("text", "") for b in resp.content if _dump(b).get("type") == "text")
        convo = convo + [{"role": "assistant", "content": text}]
        return LLMResult(text=text, retrieved_urls=urls, usage=usage, conversation=convo, search_errors=search_errors)


def extract_json(text: str, key: str = "signals") -> Any:
    """Parse the last JSON object in `text` that has `key`, tolerating prose or code fences
    around it."""
    dec = json.JSONDecoder()
    found = None
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = dec.raw_decode(text[i:])
        except ValueError:
            continue
        if isinstance(obj, dict) and key in obj:
            found = obj
    if found is None:
        raise ValueError(f"no JSON object with a {key!r} key found")
    return found


def generate_validated(llm: LLM, system: str, user: str, model_cls: type[M], retries: int = 1,
                       key: str = "signals", context: dict[str, Any] | None = None,
                       llm_kwargs: dict[str, Any] | None = None) -> tuple[M, LLMResult]:
    """Ask, parse, validate; on invalid JSON re-ask `retries` times with the error. Returns the
    model plus a merged LLMResult (usage summed, retrieved URLs unioned). Raises InvalidJSON."""
    kw = llm_kwargs or {}
    result = llm.run(system, [{"role": "user", "content": user}], **kw)
    merged = LLMResult(result.text, set(result.retrieved_urls), Usage(), result.conversation, list(result.search_errors))
    merged.usage.add(result.usage)
    last_err = ""
    for attempt in range(retries + 1):
        try:
            return model_cls.model_validate(extract_json(result.text, key), context=context), merged
        except (ValueError, ValidationError) as e:
            last_err = str(e)[:600]
            if attempt == retries:
                break
            fix = (f"Your previous reply could not be parsed or validated: {last_err}\n"
                   "Reply again with ONLY the corrected JSON object, no other text. "
                   "Do not add any signal you did not already find.")
            try:
                result = llm.run(system, result.conversation + [{"role": "user", "content": fix}], **kw)
            except LLMError as e:
                merged.usage.add(e.usage)
                e.usage = merged.usage
                raise
            merged.text, merged.conversation = result.text, result.conversation
            merged.retrieved_urls |= result.retrieved_urls
            merged.search_errors += result.search_errors
            merged.usage.add(result.usage)
    raise InvalidJSON(last_err, merged.usage)
