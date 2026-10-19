# Workbook readout: Chicago PE firms and portfolio companies

Source: `Chicago_PE_Firms_and_Portfolio_Companies.xlsx` (researched October 19, 2026). The original file was not modified (SHA-256 `e10f3c92…075af`, same before and after).
How it was read: every populated row of every sheet through openpyxl, formulas and cached values both, with the URL taken from each `HYPERLINK` formula. Nothing in the analysis rests on the first rows or on cached values alone.

This is a company and ownership research map for SEI's Chicago business development. It says nothing about any company's budget, intent or need for consulting.

## What the workbook is

A dated, selective map. It is not a complete or maintained private-equity database. Scope notes in rows 2-6 of each sheet say so, and the data below agrees.

| Sheet | Expected | Found | Header row | Notes |
|---|---|---|---|---|
| Chicago Companies | 36 | **36** records (rows 9-44) | 8 | 14 columns, 36 unique Company IDs |
| PE Firms | 27 | **27** records (rows 9-35) | 8 | 10 columns used; 27 unique Firm IDs |
| Portfolio Companies | 208 | **208** records (rows 9-216) | 8 | 11 columns used; 208 unique Relationship IDs |

No discrepancy with the expected counts. No hidden sheets, comments or cell hyperlinks outside formulas. 315 `HYPERLINK` formulas: 53 on Chicago Companies, 54 on PE Firms, 208 on Portfolio Companies. The 27 `COUNTIF` formulas on PE Firms reproduce a direct count of Portfolio Companies rows for every firm (sum 208). All 315 URLs are in `source_links.csv` (105 distinct).

## How the sheets join

- `Firm ID` joins PE Firms to Portfolio Companies. All 208 relationship rows resolve to a firm; every firm has at least one row. **No orphan IDs, no mismatch between a firm's name and the sponsor named on its relationship rows.**
- `Chicago Company ID` joins matched Portfolio Companies rows to Chicago Companies. **15 of 208 rows are linked**; every link resolves. The other 193 are blank, which means geography was not classified, not that the company is outside Chicago. Portfolio Companies has no location or industry column.
- **21 of the 36 Chicago Companies records have no Portfolio Companies row.** Their sponsors are not among the 27 local firms, which the workbook intends: it answers whether Chicago-area companies have non-Chicago sponsors.
- `Relationship ID` identifies a sponsor-company pairing, not a company.

## Actual counts

**Relationships and companies**
- 208 relationship rows, **207 distinct company names**: Eptura appears twice (R031 under Thoma Bravo, R062 under Waud Capital). The workbook as a whole names **228 distinct companies** (207, plus the 21 Chicago records with no relationship row). These are name matches; parent/subsidiary overlaps (for example Archer Foodservice Partners and its operating units, R093) are not resolved.
- 15 Chicago Companies records have more than one sponsor in the sponsor field.

**Chicago Companies (36)**
- 16 have at least one of the 27 local firms in the sponsor field: 15 with a matched relationship row plus C036 CCC, where GTCR is only a *proposed* buyer.
- **20 list only sponsors that are not in PE Firms** (Blackstone, Hellman & Friedman, KKR, Silver Lake, Berkshire, Platinum, Parthenon, Carlyle, Advent and others).
- Evidence confidence: High 25, Moderate 11. Evidence date present on 18, blank on 18. Research date 2026-10-19 on all.
- Special cases the workbook names: Walgreens (completed), Medline and NIQ (public companies), CCC (announced, not completed), Dickson (sold to a Blackstone portfolio company), Weber (combined), West Monroe (2021 percentage), JSSI (Genstar investment, GTCR retained relationship).

**PE Firms (27)**
- 15 "Headquartered locally"; 7 "Chicago office; multi-office firm"; 5 other single-office or platform descriptions (also Nashville, also Palm Beach, multi-office merchant bank, diversified investment manager, Chicago-based firm). Cities: Chicago 25, Rosemont 1, Lake Forest 1. Not all are buyout funds (for example BDT & MSD is a merchant bank, Sterling Partners a diversified manager, PPC long-duration capital).
- Researched relationship rows per firm: Wind Point 25, Waud 20, GTCR 17, Flexpoint Ford 14, Frontenac 14, then 12 or fewer; **12 firms have 1-3 rows**. These count selected rows only. They are not portfolio sizes.

**Portfolio Companies (208), by what the evidence rests on**
- 94 carry an explicit "current / active" label from a sponsor page, team biography or overview. That is a page label, not an independent check.
- 51 are plain sponsor-portfolio listings.
- 49 are listings with a caveat the sheet states: filtered portfolio (20), current and historical mixed (12), "no exit marker shown" (17).
- 14 cite a specific transaction or a company page.
- **Evidence date is blank on all 208.** Research date is 2026-10-19 on all. Stake: 206 "Stake not specified", one "50% at 2021 close" (West Monroe), one "Majority" (Culligan). Despite its name, the "Ownership confirmed" column is a stake qualifier, not a confirmation.

## Missing data and how to read blanks

- A blank **Evidence date** means the date was not captured. It does not mean a deal closed on the research date.
- A blank **Chicago Company ID** is "unclassified", not "not Chicago".
- Cached link text on all 315 hyperlink cells reads `HYPERLINK is not implemented. linkLocation=…` instead of the friendly name. That looks like an artifact of how the file was generated. A reader that recalculates may show the friendly name; one that shows cached values will not. The URLs in the formulas are intact.
- Confidence describes how well a cited fact is supported, not whether ownership is still current. Examples: Chamberlain's Moderate rests on a 2021 release; Culligan's High rests on an August 2026 company page.

## Findings that change how rows may be used

42 issues are in `data_quality_review.csv` (8 High, 20 Medium, 14 Low), each with the stable ID, evidence and a suggested fix. The ones that matter most:

1. **Do not count CCC (C036) as PE-owned.** Web check on 2026-10-19: the agreement was announced Oct 15, 2026, expected to close Q1 2027 pending regulatory and stockholder approval.
2. **Applied Systems (C011)** is dated 2017 in the workbook and its "Sept 2026 sale exploration" statement has no source. Reuters-based reports dated Sept 23-24, 2026 support the statement (anonymous sources; no signed deal).
3. **Weber (C019 / R191):** the 2025 combination release names BDT & MSD as Weber's *financial advisor*, not as an owner. Ownership of the combined company is not established anywhere in the workbook or in what I found.
4. **Furlani Foods (R189)** is labeled "Completed acquisition in 2026" on an announcement alone.
5. **49 Portfolio rows (ParkerGale, Periscope, Pfingsten, Waud) are unconfirmed as current holdings**; treat them as leads.
6. **Peers, not prospects:** West Monroe (C020, business/technology consulting) and CXponent (C028, technology advisory) are consultancies, so they are peers of SEI in the workbook's own industry labels.

## How to use each tab

**For PE-firm relationship research**
- *PE Firms* shows who is local, how (headquarters, branch, platform), focus areas, three selected examples and the firm's own directory page. Use the focus column to see overlap with SEI's themes; use "Local presence" to avoid assuming a Chicago office makes Chicago investment decisions.
- *Portfolio Companies*, filtered by Firm ID, lists researched relationships. Read the Evidence status first: a plain listing is a lead, a transaction-specific row is a fact with a date in its text.
- The count column is not a measure of a firm's activity.

**For direct company research**
- *Chicago Companies* is the only sheet with Chicago-area locations. Read Ownership role and Evidence status before anything else: majority, minority, co-sponsor, indirect, former, proposed and public are all present and not interchangeable.
- *Portfolio Companies* cannot be used for local targeting until geography is added for the 193 unlinked rows.
- Use "Verification needed" as a prompt, not a result: it is generic text.

## Limitations of this readout

- Everything above is a statement about the workbook except where marked as a web check. Web checks were run on 2026-10-19 using search; direct page fetches failed for some hosts in this environment (`mayrivercapital.com`, `culligan.com` did not resolve), so several checks rest on search summaries of third-party reports and on the sources those results returned. Each is labeled in the CSV and in the shortlist.
- Absence of a 2026 ownership change in search results does not establish that ownership is unchanged.
- No outreach, scheduling or monitoring was set up. No company or sponsor was contacted.
