# Refresh plan for the Chicago PE workbook

Prioritized by what could make a row wrong or misleading if someone used it today. **Done** means finished in this pass on 2026-10-19; **Proposed** means not started. Nothing below has been scheduled, automated or sent to anyone.

## Done in this pass

| Item | Result |
|---|---|
| Read every populated row of the three sheets (36 / 27 / 208), formulas and cached values | Counts match the workbook's own notes; no hidden sheets or comments |
| Extracted all 315 `HYPERLINK` targets | `source_links.csv`, 105 distinct URLs |
| Join checks (Firm ID, Chicago Company ID, Relationship ID), duplicate names, sponsor naming, `COUNTIF` agreement | No orphan IDs; 15 of 208 relationships linked; 21 of 36 Chicago records without a relationship row; Eptura duplicated across two sponsors |
| Data-quality review | `data_quality_review.csv`: 42 issues (8 High, 20 Medium, 14 Low) |
| Web checks by search, for the shortlist and the cases the workbook flags | Dickson, Walgreens, Weber, Applied Systems, CCC, Sphera, Culligan, HUB, AHEAD, Payroc, Cision, Chamberlain, Ensono, Medline (sellers in the March 2026 secondary only); firms GTCR, BDT & MSD, CORE, Flexpoint Ford, Wind Point |

**Not done:** Opening the 105 source URLs to confirm each resolves and says what the row says. Two direct fetches failed with DNS errors in this environment (`mayrivercapital.com`, `culligan.com`), which says nothing about the sources. NIQ's current Advent stake, Medline's current holder percentages, and the 49 caveated portfolio rows were not checked. A search that finds no 2026 news is not evidence that ownership is unchanged.

## Proposed

### Priority 0: resolve before anyone uses the workbook to pick targets
1. **Pending and announced transactions** (small, one person, about a day)
   - CCC (C036): record agreement date 2026-10-15 and expected close Q1 2027; re-check after the stockholder vote and again on close. Keep out of completed-ownership counts.
   - Sphera (C006): find a closing statement for the Neuberger Berman Capital Solutions investment, or relabel "announced, closing not verified".
   - Applied Systems (C011): attach the Sept 23-24, 2026 reports and relabel "sale process reported, not agreed"; get the latest disclosure of Hellman & Friedman's stake.
   - Furlani Foods (R189), Luna Innovations (R207), Colorescience (R195): find closing sources or relabel "announced".
   - Weber (C019/R191): find the combined company's owners; until then label the relationship "former sponsor, combined-company ownership unverified".
2. **Labels that mislead** (small)
   - Rename "Ownership confirmed" on Portfolio Companies to "Stake disclosed" and add "Current ownership verified (date)".
   - Re-save the file so hyperlink cells show their names; confirm the URLs are unchanged.
   - Split the sponsor field into clean name, role (majority, minority, co-sponsor, indirect, former, proposed) and note.
   - Add a "Relationship to SEI" tag (prospect / peer / partner) so West Monroe and CXponent, and any other consultancy found, are not exported as targets.

### Priority 1: refresh older ownership evidence (medium)
Order by age and by how much the row says:
- C011 (2017), then the 2021 group C002, C007, C009, C020 (West Monroe's 50% in particular), C012 (2022; settle April vs June 2021), C004 and C015 (2023).
- Public-company exceptions: take Medline (C034) and NIQ (C035) holder percentages from the latest proxy or 13D/13G filings; do not use news summaries.
- Culligan (C018): read the cited facts page, record each named owner and role, check Mubadala's role.
- HUB (C010): record the non-PE co-investors from the May 2025 release; AHEAD (C008): record the Berkshire and Centerbridge split from a current source; Cision (C016): confirm the creditor position before treating Platinum as controlling.
- Walgreens (C001): add the Pessina reinvestment qualifier and settle the Aug 27 vs 28 date.
- For each refreshed row record: source, source date, date read, and whether the source is a primary document (filing, company release, sponsor release) or a report.

### Priority 2: complete the directory (large, can be split by firm)
1. **Portfolio Companies geography and industry.** Add HQ city/state and industry for the 193 unlinked rows, then link any Chicago-area ones to Chicago Companies. Begin with the firms that carry the most rows (Wind Point 25, Waud 20, GTCR 17, Flexpoint 14, Frontenac 14).
2. **Evidence dates.** Capture a date for each of the 208 rows (blank today for all).
3. **Caveated holdings.** Confirm current status for 49 rows: Pfingsten R177-R188, ParkerGale R160-R168, Periscope R169-R176, Waud R057-R076. Mark each current, realized or unknown. Resolve Eptura (R031 / R062).
4. **Company-specific sources** for the rows that share a generic sponsor page (C021-C028; R204-R206 and homepage-only rows R195, R207).
5. **Former and indirect sponsors.** Record May River's sale of Dickson and Genstar's role at JSSI as relationships with dates, so the sponsor view and company view agree.

### Priority 3: add missing Chicago-area firms and companies (large, needs a stated rule)
- **Rule first.** The workbook says "verified Chicago-area presence" for firms. Define it: a firm-controlled page showing a Chicago-area address, plus whether the office makes investment decisions. Record both.
- **Sources to consult** (proposed, none yet consulted): trade directories and membership lists of Chicago private-equity associations, state and federal registration data for Illinois-based advisers, and sponsor "offices" pages. Add only what a firm-controlled page supports.
- **Non-local sponsors in Chicago Companies** (for example Parthenon, Genstar, Sycamore, Hellman & Friedman, KKR, Silver Lake, Berkshire, Centerbridge, Carlyle, Advent, Platinum, Blackstone): check whether any has a Chicago-area office. The workbook does not establish this either way.
- **Companies.** Add Chicago-area companies from the firms' own portfolio pages once geography is classified (Priority 2.1), and from deal announcements naming a Chicago-area headquarters. Keep completed, announced and pending transactions as separate statuses.
- **Gaps to size first:** how many of the 193 unclassified relationships turn out to be Chicago-area. That tells you whether the 36-company list is close to complete or a small slice.

### Priority 4: keep it current (proposed practice, not set up)
- Define "current ownership verified": a dated primary source no older than 12 months, or a filing, for any row used for outreach planning. Pending transactions would need a check at each filing or announcement.
- Keep an open-question column per row instead of the two generic phrases.
- Archive or note the retrieval date of each source page, since sponsor pages change.
- Note: no monitoring or schedule has been created, as instructed.

## Next three research actions
1. Resolve the pending and announced rows (Priority 0.1): CCC, Sphera, Applied Systems, Furlani, Luna, Weber.
2. Refresh the 2021-or-older ownership statements and the two public-company stakes (Priority 1) from filings and primary releases.
3. Classify location and industry for the 193 unlinked portfolio rows, firm by firm, starting with Wind Point, Waud and GTCR (Priority 2.1).
