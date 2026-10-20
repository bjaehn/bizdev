"""PE scanner: signals at private-equity-backed companies.

  pe_platform_or_sponsor_exec  a sponsor acquires the company as a platform, or a sponsor-linked executive
                               is appointed (event inside the 180-day window)
  pe_addon                     the company announces or completes an add-on acquisition (inside the window)
  (pe_hold_2_4 is no longer requested: how long a sponsor has held a company is not evidence of a need. Rows stored
   earlier stay in the database and score 0; see scoring.base_points in config.yaml.)

Runs only for accounts whose ownership is `pe-backed`; every other account costs nothing."""

from __future__ import annotations

from datetime import date

from bd.accounts import Account
from bd.models import SignalExtract
from bd.scanners.base import Scanner


class PeScanner(Scanner):
    name = "pe"
    allowed_types = frozenset({"pe_platform_or_sponsor_exec", "pe_addon"})

    def applies_to(self, account: Account) -> bool:
        return account.ownership == "pe-backed"

    def user_prompt(self, account: Account, as_of: date) -> str:
        base = super().user_prompt(account, as_of)
        known = f"Known private equity sponsor: {account.sponsor}\n" if account.sponsor else "Known private equity sponsor: unknown\n"
        return base.replace("Today's date:", known + "Today's date:", 1)

    def date_problem(self, c: SignalExtract, as_of: date) -> str | None:
        bounds = self.cfg.scan.status_type_age_days.get(c.type)
        if bounds is None:
            return super().date_problem(c, as_of)
        age = (as_of - c.event_date).days
        if bounds[0] <= age <= bounds[1]:
            return None
        return f"{c.event_date.isoformat()} is {age} days ago; a {c.type} hold must be {bounds[0]}-{bounds[1]} days"
