"""Technology and procurement scanner.

  platform_migration  announced ERP / cloud / data-platform migration or modernization
  vendor_deal         a named technology vendor deal (AI, cloud, data, ERP) announced publicly
  rfp_match           an OPEN public RFP or solicitation matching SEI's services
  risk_factor         risk-factor language in an SEC filing about AI, technology, data or transformation

Code adds two hard rules on top of the shared checks: an RFP counts only if it states a due date that has
not passed, and a risk_factor must come from sec.gov."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel

from bd.accounts import Account
from bd.models import SignalExtract
from bd.scanners.base import Scanner
from bd.urls import host_in, hostname


class TechExtract(SignalExtract):
    due_date: date | None = None   # RFPs only; not stored (the summary carries what matters)


class TechResponse(BaseModel):
    signals: list[TechExtract]


class TechProcurementScanner(Scanner):
    name = "tech_procurement"
    allowed_types = frozenset({"platform_migration", "vendor_deal", "rfp_match", "risk_factor"})
    response_model = TechResponse

    def extra_problem(self, c: SignalExtract, account: Account, as_of: date) -> tuple[str, str] | None:
        if c.type == "rfp_match":
            due = getattr(c, "due_date", None)
            if due is None:
                return "rfp_no_due_date", "an RFP needs a stated due date to be actionable"
            if due < as_of:
                return "rfp_closed", f"due {due.isoformat()}"
        if c.type == "risk_factor" and not host_in(hostname(c.source_url), self.cfg.sources.sec_domains):
            return "risk_factor_needs_sec_filing", hostname(c.source_url)
        return None
