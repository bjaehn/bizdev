"""Human-readable labels shared by the Teams card and the local UI (no network or LLM code here)."""

from __future__ import annotations

from datetime import date

from bd.models import SIGNAL_TYPES

TYPE_LABELS = {
    "leadership_change": "Leadership change",
    "leadership_transformation_ai_data": "New transformation/AI/data leader",
    "open_exec_search": "Open executive search",
    "pe_platform_or_sponsor_exec": "PE platform or sponsor executive",
    "pe_addon": "PE add-on acquisition",
    "pe_hold_2_4": "PE hold 2-4 years",
    "restructuring_layoffs": "Restructuring / layoffs",
    "ma_divestiture": "M&A / divestiture",
    "activist": "Activist pressure",
    "ops_failure": "Operational / tech failure",
    "cost_program": "Cost program / guidance cut",
    "ai_commitment": "AI commitment",
    "strategy_rebrand": "Strategy / rebrand",
    "hiring_cluster": "Hiring cluster",
    "hiring_leadership": "Senior leadership hire",
    "hiring_interim": "Interim hiring",
    "hiring_single": "Single senior hire",
    "platform_migration": "Platform migration",
    "vendor_deal": "Vendor deal",
    "rfp_match": "RFP match",
    "risk_factor": "Risk factor disclosure",
    "financial_pressure": "Financial pressure",
    "financial_distress": "Financial distress / restatement",
}
assert set(TYPE_LABELS) == set(SIGNAL_TYPES)

THEME_LABEL = {"ai_technology": "AI and technology", "data_analytics": "data and analytics",
               "strategy_operations": "strategy and operations", "transformation_change": "transformation and change",
               "product_engineering": "product and engineering"}
FAMILY_LABELS = {"leadership": "Leadership", "hiring": "Hiring", "events": "Company events", "pe": "Private equity",
                 "technology": "Technology", "procurement": "Procurement", "filings": "Filings", "financials": "Financials"}
FIRM_TYPE_LABELS = {"firm_new_fund": "new fund", "firm_exit": "exit", "firm_operating_partner": "operating-partner hire"}


def fmt_date(d: date) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def money(v: object) -> str:
    if v is None or v == "":
        return "unknown"
    v = float(v)  # type: ignore[arg-type]
    if v >= 1e9:
        return f"${v / 1e9:.1f}B"
    if v >= 1e6:
        return f"${v / 1e6:.0f}M"
    return f"${v:,.0f}"
