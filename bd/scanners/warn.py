"""Illinois WARN scanner: mass-layoff and plant-closing notices filed with the State of Illinois.

A WARN notice is a legal filing, so it is the most reliable early sign of a restructuring. The search is restricted to the
state's own domains (config.yaml `sources.warn_domains`), so every result is an official page; the shared checks still require
the URL to have been in the results, to resolve, and the date to be inside the window. Emits only restructuring_layoffs, and
source_type is warn_notice / High only when the URL really is on a state domain."""

from __future__ import annotations

from datetime import date

from bd.accounts import Account
from bd.models import SignalExtract
from bd.scanners.base import Scanner
from bd.urls import host_in, hostname


class WarnScanner(Scanner):
    name = "warn"
    allowed_types = frozenset({"restructuring_layoffs"})

    def llm_kwargs(self, account: Account) -> dict:
        return {"allowed_domains": list(self.cfg.sources.warn_domains)}

    def extra_problem(self, c: SignalExtract, account: Account, as_of: date) -> tuple[str, str] | None:
        if not host_in(hostname(c.source_url), self.cfg.sources.warn_domains):
            return "warn_needs_state_page", hostname(c.source_url)
        return None
