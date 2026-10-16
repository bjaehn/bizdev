"""Press scanner: a company's own announcements and press releases.

The model's web search is restricted (by the search tool itself) to the company's own domain, the configured wire
services and SEC pages, so every candidate comes from a first-party or wire source. The usual acceptance rules still
apply on top (date from the source and inside the window, URL actually returned and resolving, source type reconciled
with the real domain, confidence only lowered). It overlaps the events scanner on purpose: the same event found by
both is stored twice (different sources) and counted once when scoring."""

from __future__ import annotations

from bd.accounts import Account
from bd.scanners.base import Scanner


class PressScanner(Scanner):
    name = "press"
    allowed_types = frozenset({
        "leadership_change", "leadership_transformation_ai_data", "open_exec_search", "ma_divestiture", "strategy_rebrand",
        "ai_commitment", "platform_migration", "vendor_deal", "cost_program", "restructuring_layoffs", "ops_failure"})

    def applies_to(self, account: Account) -> bool:
        return bool(account.domain)            # nothing to restrict the search to without the company's own site

    def llm_kwargs(self, account: Account) -> dict:
        s = self.cfg.sources
        return {"allowed_domains": [account.domain, *s.wire_services, *s.sec_domains]}
