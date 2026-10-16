"""Plain-English setup checks. No network and no LLM here: pure functions the CLI check and the Home page share."""

from __future__ import annotations

from collections.abc import Mapping

from bd.config import Config


def explain_error(raw: str) -> str:
    """Translate an API error into one sentence saying what to change."""
    t = raw.lower()
    if "not scoped to a workspace" in t or "anthropic-workspace-id" in t:
        return ("your API key is not tied to a workspace. Either create the key inside a workspace in the Anthropic Console, or set "
                "ANTHROPIC_WORKSPACE_ID to that workspace's id.")
    if "connection error" in t or "timed out" in t or "timeout" in t or "name resolution" in t:
        return "the app could not reach Anthropic (a network problem). Try again in a minute; if it keeps failing, check the hosting provider's status."
    if "authentication" in t or "invalid x-api-key" in t or "401" in t:
        return "the API key was rejected. Check ANTHROPIC_API_KEY for typos or an expired key."
    if "permission" in t or "403" in t:
        return "the key is not allowed to use this model or feature. Check the key's workspace and permissions in the Anthropic Console."
    if "not_found" in t or "404" in t or "model:" in t:
        return "the model name was not recognised. Check ANTHROPIC_MODEL (for example claude-sonnet-5-5)."
    if "credit" in t or "billing" in t or "balance" in t:
        return "the account has no credit. Add credit in the Anthropic Console."
    if "rate" in t and "limit" in t or "429" in t:
        return "the key hit a rate limit. Wait a minute and try again."
    return "unexpected error: " + raw[:300]


def static_problems(cfg: Config, env: Mapping[str, str]) -> list[str]:
    """Things wrong with the settings alone (no call needed), as plain sentences. Empty = nothing obviously wrong."""
    out = []
    if not env.get("ANTHROPIC_API_KEY"):
        out.append("ANTHROPIC_API_KEY is not set: nothing that uses the model can run.")
    model = env.get("ANTHROPIC_MODEL", "")
    if not model:
        out.append("ANTHROPIC_MODEL is not set (use claude-sonnet-5-5).")
    elif model not in cfg.pricing.models:
        out.append(f"The model {model!r} has no price in config.yaml, so costs show as unpriced and the spending limit cannot stop a run. "
                   "Use a priced model such as claude-sonnet-5-5.")
    ua = env.get("BD_USER_AGENT", "")
    if not ua.strip():
        out.append("BD_USER_AGENT is not set: the SEC refuses unidentified requests, so company filings cannot be checked. "
                   "Set it to your company name and a contact email.")
    else:
        from bd.config import clean_user_agent
        if clean_user_agent(ua) != ua:
            out.append("BD_USER_AGENT has stray spaces, a line break, quotes or non-English characters; they are removed "
                       "automatically, but re-save it as one plain line such as: YourCo BD Research (you@yourco.com)")
        if "@" not in ua:
            out.append("BD_USER_AGENT should include a contact email (the SEC asks for one), e.g. YourCo BD Research (you@yourco.com)")
    return out
