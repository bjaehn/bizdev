"""Sweep scanner: leadership, press, company events and technology in ONE model call per company.

The four scanners it replaces each search the web for the same company on their own, so a company costs six calls and dozens of
searches. The sweep asks one question with a small search budget (`scan.sweep_searches`), uses the same acceptance rules as every
scanner (type, date window, a URL the search tool really returned and that loads, source type reconciled with the real domain), and
can run on a cheaper model (ANTHROPIC_SCAN_MODEL). Hiring stays its own scanner: it reads job postings and code builds its signals.
RFP matches and filing risk factors need extra checks and stay with the technology scanner."""

from __future__ import annotations

from datetime import date, timedelta

from bd.accounts import Account
from bd.scanners.base import Scanner


class SweepScanner(Scanner):
    name = "sweep"
    allowed_types = frozenset({
        "leadership_change", "leadership_transformation_ai_data", "open_exec_search",
        "restructuring_layoffs", "ma_divestiture", "strategy_rebrand", "ai_commitment", "activist", "ops_failure", "cost_program",
        "platform_migration", "vendor_deal"})

    def user_prompt(self, account: Account, as_of: date) -> str:
        start = as_of - timedelta(days=self.cfg.scan.window_days)
        return (
            f"Company: {account.name}\n"
            f"Website domain: {account.domain or 'unknown'}\n"
            f"Headquarters city: {account.hq_city or 'unknown'}\n"
            f"Today's date: {as_of.isoformat()}\n"
            f"Window: events dated {start.isoformat()} through {as_of.isoformat()}\n"
            f"Search budget: at most {self.cfg.scan.sweep_searches} web searches.\n\n"
            "Search for signals for this company per your instructions. Return the JSON object only."
        )

    def _call_kwargs(self, account: Account) -> dict:
        kw = dict(self.llm_kwargs(account))
        kw["max_uses"] = self.cfg.scan.sweep_searches
        return kw
