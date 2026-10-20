# SEI Chicago BD Signal Agent (prototype)

Each week this scans public sources for Chicagoland companies showing demand for SEI's services
(AI & Technology, Data & Analytics, Strategy & Operations, Concept to Delivery) and posts a scored
shortlist to Microsoft Teams as an Adaptive Card. **Research only**: no outreach is drafted. Public
sources only: no LinkedIn, no paywalled sites. For the external services and data involved, see
[SECURITY.md](SECURITY.md).

> **Status.** All stages are built and tested offline (fake LLM and HTTP). It has **not yet run against the
> live Anthropic API, SEC EDGAR, live websites or a real Teams webhook**. Run it supervised first
> (see [First live run](#first-live-run)).

## How it works

```
accounts.csv ──bd seed──► database ──► fit gate ──► scan (leadership, hiring, press, events) ──► score ──► shortlist ──► card ──► Teams
 exclusions.csv            out of scope?        │                              │          │            │
                           (only a KNOWN        │ Claude + web search          │ plain    │ cooldown,  │ briefer writes one
                            reason skips it)    │ extracts; CODE verifies      │ code,    │ 90-day     │ sentence; Adaptive
                                                │ every source URL             │ no LLM   │ High gate  │ Card 1.4, links only
```

* **Claude extracts, code decides.** Claude proposes signals; code accepts one only if its type, date window
  and source URL pass (the URL must have been returned by the search tool *and* load when fetched).
  Scoring is deterministic and unit tested.
* **Fewer, better leads.** Only Hot (75+) and Warm (60-74) accounts with a High-confidence signal in the
  last 90 days are posted, at most 10, with a 4-week cooldown unless a new signal family appears or the
  score rises 15+. Low-confidence signals are stored but never scored.
* **Signals first, facts second.** Every company on your list is scanned for leadership changes, hiring, press and
  company events unless it is *known* to be out of scope (an exclusion, outside Chicagoland, or below the size floor).
  A location or size that is not confirmed does not stop a scan: the company is scanned and tagged *unverified* (on Analysis too: the ranked list, the top three and the tracked list carry the tag, with the reasons on hover; it is never scored down for it).
  Nothing is guessed: facts are never inferred, and an unverified company's brief says so.
* **Themes, not prose.** What a company's signals add up to is named by fixed rules in `bd/insights.py` (for example
  *New leadership and a hiring push*: a leadership signal and a hiring signal both found), shown with the rule and the
  stored, sourced signals that satisfied it. No model writes it and it never changes a score.

## Setup

Requires Python 3.12+.

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
# .env.example lists the variables. The code reads the process environment only (it does NOT load .env files),
# so export them in your shell, service unit or secret store.
```

| Variable | Needed for | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | scan, enrich, card | secret |
| `ANTHROPIC_MODEL` | scan, enrich, card | a model that supports `web_search_20260209`, e.g. Sonnet 5.5 or Opus 5.5 |
| `ANTHROPIC_SCAN_MODEL` | the `sweep` and `tech_procurement` scanners (Update, the weekly run, Find) and `bd compare` | the cheaper model the combined sweep scan runs on, e.g. `claude-haiku-5-5` (tested; see **Keeping the spend down**). Unset: the sweep runs on `ANTHROPIC_MODEL`, which still costs one call a company instead of four but not the full saving. It must support the web search tool and have a price in `config.yaml` `llm.pricing` (`bd check` warns if not). Nothing else uses it: hiring, facts research and the card stay on `ANTHROPIC_MODEL`. Which scanners use it is `SCAN_MODEL_SCANNERS` in `bd/scanners/__init__.py` |
| `BD_USER_AGENT` | EDGAR, CIK checks | `"SEI BD Research you@company.com"`; the SEC requires a contact |
| `TEAMS_WEBHOOK_URL` | posting | secret; **if unset, `bd card` is a dry run** |
| `BD_DB_PATH`, `BD_CONFIG` | optional | default `bd.sqlite`, `config.yaml` |

Costs are estimated from prices in `config.yaml` (`pricing`, from Anthropic's pricing page, 2026-10-15;
web search is $10 per 1,000). A model missing from that table logs cost as NULL, never a guess. A per-run
soft cap (`llm.max_cost_per_run_usd`, default $10) stops new calls once reached.

## Scanners

Each scanner is a module in `bd/scanners/` with its prompt in `bd/prompts/<name>.md`. All of them return
signals validated by code; none writes outreach.

| Scanner | Signal types | Runs for | Notes |
|---|---|---|---|
| `leadership` | leadership_change, leadership_transformation_ai_data, open_exec_search | every account | web search |
| `edgar` | leadership_change, leadership_transformation_ai_data | accounts with a CIK | SEC 8-K Item 5.02 from EDGAR; Claude only reads the excerpt |
| `earnings` | cost_program, restructuring_layoffs, ma_divestiture, ai_commitment, ops_failure, strategy_rebrand | accounts with a CIK | SEC 8-K Item 2.02 earnings releases (the exhibit 99 press release) from EDGAR; EDGAR supplies the date and URL, Claude only reads the release text and reports events it states. Routine results are not signals; earnings-call transcripts are not used (paywalled) |
| `events` | restructuring_layoffs, ma_divestiture, strategy_rebrand, ai_commitment, activist, ops_failure, cost_program | every account | includes Illinois WARN notices |
| `press` | leadership_change, leadership_transformation_ai_data, open_exec_search, ma_divestiture, strategy_rebrand, ai_commitment, platform_migration, vendor_deal, cost_program, restructuring_layoffs, ops_failure | accounts with a domain | the company's own announcements: the search tool is restricted to the company's domain, wire services and SEC pages, so every candidate is first-party. Overlaps `events` on purpose; the same event found by both counts once when scoring |
| `pe` | pe_platform_or_sponsor_exec, pe_addon | accounts whose ownership is `pe-backed` | The holding-duration signal `pe_hold_2_4` is no longer requested and carries no score weight: how long a sponsor has held a company is not evidence of a need. Rows stored earlier stay in the database and score 0 |
| `hiring` | hiring_cluster, hiring_leadership, hiring_interim, hiring_single | accounts without a known job board | Claude lists postings; **code** verifies each, counts the cluster (3+ related postings within 60 days) and writes the summaries |
| `ats` | hiring_cluster, hiring_leadership, hiring_interim, hiring_single | accounts whose Greenhouse / Lever / Ashby board is known | **free and daily**: reads the company's own job board JSON (every open posting, no model). The board id is learned from posting URLs the `hiring` scanner verified, never guessed. Titles are classified by the keyword lists in `config.yaml` (`hiring.theme_keywords`): data/AI, transformation/strategy/operations, product/engineering build-out. `hiring_leadership` = a VP-or-above posting in one of those areas. Postings are stored and shown in the Hiring section at the bottom of Analysis |
| `warn` | restructuring_layoffs | every account | Illinois WARN (mass layoff / plant closing) notices: the search tool is restricted to the state's own domains (`sources.warn_domains`), and the page must be on one of them. Most companies have none, which is a normal result |
| `financials` | financial_pressure | accounts with a CIK | **free**: no model. Reads the company's latest 10-K figures from the SEC's structured data (data.sec.gov company facts) and flags revenue down, operating margin down, or an operating loss versus the prior year (thresholds in `config.yaml` `financials`). Dated by the day the 10-K was filed; needs a 10-K filed inside the 180-day window. Banks and insurers usually lack comparable tags and get no signal |
| `sec_events` | restructuring_layoffs, ma_divestiture | accounts with a CIK | **free**: no model. Reads the company's SEC filing list for 8-Ks that report Item 2.05 (costs of exit or disposal activities) or Item 2.01 (completed acquisition or disposition); which items count, and what each becomes, is `config.yaml` `sec_events`. The filing is fetched and must carry the item's own heading; the summary is written by code from the item's title; dated by the report date on the filing. Each filing is read once |
| `tech_procurement` | platform_migration, vendor_deal, rfp_match, risk_factor | every account | an RFP must have a future due date; a risk factor must be on sec.gov |
| `sweep` | leadership, press, events and technology types (not hiring, RFPs or risk factors) | every account | **One** model call per company in place of four (`leadership`, `press`, `events`, `tech_procurement`), with a small search budget (`scan.sweep_searches`, 6) and the same acceptance rules. Runs on `ANTHROPIC_SCAN_MODEL` when set. The core scan of Update, the weekly run and Find (with `hiring`); not part of `all`, which is the older separate-scanner list |

**Free scans.** Three scanners make no model call: `ats` (job boards), `financials` and `sec_events` (SEC data). They go first in any scan and the spending limit never holds them back, and `Update my list` runs them over every company on the scan list before its limited, paid scan, so a first look is never empty. The paid signal scan comes before the work that fills in facts or finds new companies, so facts never use up the spending limit that signals need. It is tiered to save money: every company gets the core scan (the combined `sweep` for leadership, press, company events and technology, plus `hiring`), and the deeper technology scanner (RFP matches, filing risk factors, extra searches) runs for companies that already show a signal **and for every scanned company on a cycle**: one with no successful deep scan in `scan.deep_every_weeks` (4) weeks is due one, so nobody is looked at only shallowly for long (about $0.12 a company per cycle; a scan that ended in a search or fetch error does not count as a look). The Admin Home's *Signal coverage* card measures it from what actually ran: companies given a deep look this cycle, companies due one, runs in the last 8 weeks, new signals it stored that nothing had stored before, runs that ended in an error, and the estimated cost. WARN notices are off by default.

**Keeping the spend down.** Four things, in order of effect. (1) *The combined sweep on a cheaper model.* One call per company covers what four scanners did. A side-by-side test (`bd compare`) on four companies with `claude-haiku-5-5` cost **$0.33** against **$3.88** recorded for the four separate scanners last time, and found 13 signals against 8 stored, with two real misses (both AI-partnership press releases) and one company where it found nothing; the other nine were not independently checked. Four companies and one run is a guide, not a measurement: watch the weekly signal counts on Signal coverage, and revert by setting `CORE_SCANNERS` back in `bd/jobs.py`. (2) *Tiered scan*: the deep technology scan runs where a signal shows and otherwise once every 4 weeks per company (`scan.deep_every_weeks`; 0 = only where a signal shows). (3) *Quiet companies wait longer*: a company already scanned that shows no signal in the last 60 days is checked every 4 weeks instead of its tier's cadence (`scan.quiet_days`, `scan.quiet_every_weeks`; 0 turns it off); `bd scan` says how many are waiting. A company with a signal, or one never scanned, is never held back, and `--force` ignores it. (4) *Per-model pricing*: each scanner's calls are priced by the model they ran on, including the higher rate some models charge above a long prompt, so the cost figures and the spending limit follow what you are actually using. `bd compare` (Admin, Operate, *Test the cheaper scan*, capped at $3, stores no signal) can be re-run any time; its cost is logged on Runs and cost. **A failed search is not a quiet company:** if the search tool itself errors (rate limit, unavailable) and a scan, discovery segment or RFP group finds nothing, the call is reported as an error ("search tool errors: ...") and the company is not marked scanned, so it is tried again instead of waiting four weeks as a quiet one. Running out of the allowed searches is not an error. The first live Update on this scan (10 companies) cost **$1.98**, about $0.20 a company: the sweep $0.99, the technology extra $0.63 (9 companies, 3 signals) and hiring $0.36; the web searches themselves are about $0.76 of the $1.34 core scan, so fewer searches are the lever left in the sweep, and the technology extra now runs on the cheaper model too. **That is untested:** before the next Update, run `bd compare --scanner tech_procurement` (Operate, *Test the cheaper scan*, *Technology scan*), which compares it with the results the same scanner stored on the main model; if it misses real RFP matches or vendor deals, take `tech_procurement` out of `SCAN_MODEL_SCANNERS`. Prompt caching was looked at and left out: it does not pay for single-shot searches.

**Open public RFPs** (`bd rfps`, the Open RFPs page) are a separate, market-level feed rather than a scanner: one search per agency group in `config.yaml` `rfps.groups` (City of Chicago, Cook County, State of Illinois, schools and universities, transit and water agencies, federal), each restricted to that group's own domains. A solicitation is kept only if its page is on those domains, was in the search results, resolves, and states a due date that has not passed. They are not tied to a company and are not scored. Part of the weekly run, and of `Update my list` only when its RFP setting is turned on (off by default); roughly six searches per run.

**Data health** (`/health`, in Admin) and **Market view** (`/market`, in Admin under Reports) are read-only pages built from what past runs stored. Data health shows which scanners produced anything (and which failed or never ran), cost per scanned company, which facts are still unverified, and how fresh the data is. Market view shows companies by band at every scoring run (a stacked-bar trend: Hot, Warm, Watch, one bar per run as that run scored them, so it also moves when companies become ready), an **industry heatmap** (signals in the last 90 days by industry and signal type; click a number to see those companies), the industry numbers with a 30-day trend, signal groups by month, sponsor activity, and the large companies with no signal in 180 days.

**Ask your data** (`/ask`, under More, hosted app only) answers a plain-English question about your own database. It runs as a job (paid-call tick, a few cents): a model writes one read-only query from the table layout, code checks and runs it on a read-only connection limited to an allow-list of tables, and a second call phrases the answer from the returned rows. The answer, the rows and the SQL are stored and shown together. Your free-text notes are not available to it. `bd ask --id N` is the command behind the button.

**Facts for SEC filers come from the SEC, not the model.** For a company with a CIK, `bd enrich` (and discover's research) reads the SEC's company-facts and submissions files in code: revenue is the latest fiscal year from its 10-K, ownership is `public` when it has a listed ticker, and HQ city is the business address. The evidence link on each fact is the SEC file read. Private companies and nonprofits still rely on the model's sourced figures. `bd sec-fill` does this for every company on file before the model is asked (free), and `bd enrich` also finds a missing CIK from the SEC's list of listed companies (exact name match) and then reads the rest from the SEC, so a public company's size no longer waits for a web search. **Update my list** has a setting for whether to search agency sites for RFPs; it is **off by default** (the first two live runs cost about $0.90 each and found nothing: the model's search budget ran out before it reached a listing page). With it switched on, the two most recent live Updates cost about $0.27 and $0.34 for this step, and the second found its first open RFP (a transit-agency study solicitation); the groups where the search tool hit its usage limit are reported as such, not as empty.

**Pattern bonus (optional, off by default).** `scoring.sequences` in `config.yaml` lets order count: for example a new executive followed within 120 days by a hiring push earns a bonus (each pattern once, capped together at `sequence_bonus_max`, shown on the opportunity as a "Pattern" reason). It is empty by default because enabling it changes scores, including the spec's reference accounts; the config file holds four starting patterns to uncomment.

## PE portfolio intelligence

`/firms` is the directory of the PE firms already in the product; each firm page shows what its **own portfolio page** lists, split into current, pending, unclear and historical holdings, with the reading status and coverage ("complete" only when every page was read and nothing suggested more; otherwise "partial" with the reason, "unknown", or "not read yet"). Nothing is shown for a firm that has not been read, and nothing is invented to fill a gap.

* **Ownership ledger** (`bd/pe_ownership.py`). One relationship per (firm, company), so a company held by two sponsors has two relationships and one company record. Status, role (platform, add-on, co-investor), stake (majority/minority only when a source says so), announcement date, closing date, exit date and "as of" are **derived from evidence rows by fixed rules**, not typed in. An announcement is never a closing, an old announcement never proves current ownership, a company that disappears from a page is *unclear* not exited, and holding duration is never calculated from an announcement date (the old `pe_hold_2_4` weight is 0 and the scanner no longer requests it).
* **Evidence** (`pe_evidence`). Each row keeps URL, publisher, title, publication date, event date, retrieval time, excerpt, source type, content hash and a verification state: `page_verified` (code fetched the page and found the excerpt in it) or `snippet_only` (never changes a status). Retrieval time is never shown as a publication date.
* **Geography.** The firm's headquarters, a portfolio company's headquarters and other documented locations are stored separately. A holding is *inside* or *outside* the configured scope (`fit_gate` state and counties, plus `pe_portfolio.scope_cities`) only when a retrieved page shows where the company is; otherwise it is *not verified*, and is never assumed local because its owner is. Holdings shown to be outside are hidden by default and can be shown.
* **Priority** (`bd/opportunity_model.py`, `opportunity_model` in `config.yaml`). Specific need 30, timing 25, investment evidence 20, potential for external help 15, evidence quality 10, each scored 0-100 from written anchors and summed. A dimension no evidence speaks to is *unknown*: it adds nothing, is not negative, and the others are not scaled up. Repeated coverage of one event counts once. There is no input for company size, news volume, job count, portfolio size or PE ownership. Confidence is reported separately (facts established versus the consulting hypothesis). Categories: **Prospect now**, **Validate first**, **Nurture**, **Monitor**, **Insufficient evidence**, with every rule listed on `/help` and on each brief. It is not a probability of purchase or win, and it is separate from the Hot/Warm/Watch score above (both are shown; neither feeds the other). **An event at someone else's place does not make a company "Prospect now":** if every specific, current event names a place listed in `opportunity_model.outside_places` (India, England, Canada and so on; whole words, case-sensitive; edit the list in `config.yaml`), the company is at most *Validate first* until someone confirms the events involve it, because a subsidiary's brief can otherwise rest on a parent's plant abroad. The score is unchanged, each such fact shows which place it names, the unknowns say how many events do, and no question to explore is built on them. This is a place-name check, not entity resolution: a parent's event in the U.S. is not caught.
* **Briefs.** A company page has the ownership table, the priority breakdown, confirmed facts (dated, linked), counterevidence, the potential consulting need labelled as a hypothesis with its general category and the *function* that would own it (a named person only when the evidence names one; buying authority is never assumed), unknowns, the next research step and a question to explore built from the confirmed fact. Cross-portfolio patterns appear only when at least two holdings each have their own sourced evidence in one category.

**Firm details.** A firm that came from an account's sponsor has no website or portfolio page. Open it under `/firms` and fill *Firm details* (domain, portfolio page URL, headquarters as entered); a blank means not known. With a domain, *Read PE portfolio pages* looks for a Portfolio link on the firm's home page; with a URL it reads that page. Changing the URL drops the old page only if it was never read. A name that reads like two firms (for example "A and B") is flagged, never split by guessing.

**Automatic.** *Update my list* and the Monday run both read the PE portfolio pages first (free, polite, never able to stop the run), so portfolios refresh without anyone clicking. A sponsor name an account stores as one string but that is really several firms is split only where `pe_portfolio.sponsor_splits` in `config.yaml` says so (the Charlesbank / Partners Group name is listed); a company with two sponsors then has two relationships and one record.

Refresh: Admin, Operate has *Read PE portfolio pages* (free), *Research transactions* and *Research portfolio companies for the target list* (paid, capped, with confirmation), or run `bd pe-collect`, `bd pe-deals`, `bd pe-promote` (below). The first reading of a firm says no earlier reading exists; later readings list what was added and what is no longer listed, from stored snapshots.

## Commands

| Command | What it does |
|---|---|
| `bd seed accounts.csv [--exclusions exclusions.csv] [--overwrite]` | Load accounts and exclusions, then run the fit gate. **The database is the master copy**: for an account that already exists, a CSV value only fills a fact that is still unknown; differences are reported and the database value is kept unless you pass `--overwrite`. Blank cells stay *unknown*, never guessed. Also loads a reviewed `bd enrich` file |
| `bd export [--accounts accounts.csv] [--exclusions exclusions.csv]` | Write the database's accounts and exclusions back to CSV (for git or backup). Overwrites those files, so review with `git diff` |
| `bd ui` | Open the local review-and-curate UI (see **Local UI** below) |
| `bd discover [--segment TEXT] [--limit N] [--dry-run]` | **Market research.** Finds Chicagoland companies by segment (`discovery.segments` in config), researches each one's facts with the same verified-evidence rules as `enrich`, **auto-adds** those that pass the fit gate (tier B, marked *auto-added*), queues uncertain ones on the Discovery page and records fit-gate failures so they are never researched twice. Auto-added accounts are scanned only up to `discovery.scan_unreviewed_per_run` per run until you mark them reviewed |
| `bd pe-sync` / `bd pe-portfolio [--firm T] [--limit N]` / `bd pe-signals [--firm T] [--limit N]` | **Private equity.** `pe-sync` (free) derives PE firms and portfolio links from accounts that have a sponsor. `pe-portfolio` reads each firm's own site and finds its other Chicagoland companies, which go through the same verify / fit-gate / auto-add path as `discover`; a company is linked to the firm only when the firm's own site lists it. `pe-signals` looks for firm-level news (new fund, exit, operating-partner hire) with the same source rules as company signals. A firm's signals add a small, **capped** lift (`scoring.sponsor`) to portfolio companies that already have a signal of their own; the lift is shown separately and can never make a company postable by itself |
| `bd pe-collect [--firm T] [--force] [--locate N] [--soft]` | **PE portfolio collection. Free, no model.** Reads each active PE firm's own portfolio page (registered in `pe_sources`; a "Portfolio" link on the firm's home page is followed when none is registered) with a polite, bounded reader (robots.txt, per-host rate limit, retries, size cap, challenge pages recognised and never bypassed), follows pagination and the site's public WordPress API where it exists, and records every listed company as an ownership relationship with its evidence (URL, excerpt, content hash, retrieval time). Status comes from the evidence by fixed rules: **current**, **pending**, **historical** or **unclear**; a company missing from a later reading becomes *unclear*, never an exit. Idempotent (pages read within `pe_portfolio.fetch.cache_hours` are not read again unless `--force`), one failing source never stops the others, and a failed reading keeps the last good evidence and marks it stale. `--locate` reads a company's own site for a headquarters address (free). Before reading, a firm with no website gets one **confirmed by code** (candidate hosts built from its name, accepted only when the page they return names the firm), and a firm with a website but no portfolio page gets one from a Portfolio link on its home page or a usual path (`/portfolio`, `/companies`, `/investments`...) that returns a listing of companies. Anything not confirmed stays unknown. `--soft` never fails the caller (used inside Update and the Monday run). Needs `BD_USER_AGENT`. Also Admin, Operate, *Read PE portfolio pages (free)* |
| `bd pe-deals [--firm T] [--limit N] [--dry-run]` | **Paid (web search).** Researches each firm's transactions (acquisitions, add-ons, exits). The model only *reports* a transaction with a verbatim excerpt; **code** fetches the cited page, requires the excerpt on it, reads completed versus announced from its wording, takes dates and any majority/minority stake only when the page states them, and drops anything it cannot verify. Search snippets are never treated as page contents |
| `bd pe-promote --limit N [--dry-run]` | **Paid.** Sends portfolio companies that are current, pending or unclear and not shown to be outside the configured geography through the **existing** fact research and fit gate. Only those that pass become accounts (no second target list), and only then are they scanned. Companies matching an existing account by domain, alias or name are linked, not duplicated |
| `bd ats-find [--limit N] [--only TEXT] [--soft]` | **Free, no model.** Reads each company's own careers page (home page, the careers pages it links to, `/careers`, `/jobs`; at most 4 polite page reads) for a link or embed to its Greenhouse, Lever or Ashby job board. The board id comes from the company's own site, never from its name, and is saved only when the provider's public feed answers; a page linking to more than one board is left alone. A company with a board gets the free, complete `ats` feed (every open role, each with its own date) instead of a model search for postings, which is cheaper and does not depend on what a search happens to surface. A company looked at is not read again for `hiring.ats_find_every_weeks` (8); one whose pages could not be read is retried. Runs inside Update (before the free scan, so a board found now is read now) and the Monday run; `--soft` never fails the caller. Needs `BD_USER_AGENT` |
| `bd sec-fill [--only TEXT [--exact]] [--limit N]` | **Free, no model.** Fills what the SEC itself can tell us: for a company with a CIK, its revenue (latest 10-K), public ownership and headquarters city from the SEC's company files; for a company with no CIK that is not recorded as private or PE-backed, a CIK found by **exact name match** (legal suffixes, punctuation and case ignored; a name that fits two filers is skipped) in the SEC's list of listed companies. Applied through the same writer as every edit (fit gate re-run, actor `sec` on the Change log, with the SEC page each fact came from and the SEC's registered name for a CIK match); never overwrites a stored value. Reads at most `sec_fill.max_accounts_per_run` SEC records a run, oldest-tried first. Skips with a note when `BD_USER_AGENT` is not set. First step of `Update my list` |
| `bd enrich` | **Proposes** missing facts (domain, Chicagoland, size, ownership, CIK) with an evidence URL each, into `enrich_proposals.csv`. Never writes to the database. Review, then `bd seed enrich_proposals.csv` |
| `bd compare [--scanner sweep\|tech_procurement] [--count N] [--only TEXT]` | **Test, stores no signal.** Runs the `sweep` (against the separate scanners it replaces) or the `tech_procurement` scanner (against its own earlier results) on `ANTHROPIC_SCAN_MODEL`, for the N companies with the most stored signals of the kinds it covers, or one named company, and prints stored vs found per company, the cost of each side, and a one-line verdict. Stops before the spending limit. Admin: Operate, *Test the cheaper scan* ($3 cap) |
| `bd scan [--tier A\|B\|all] [--only TEXT [--exact]] [--dry-run] [--force] [--scanners leadership,events,press,earnings,pe,hiring,tech_procurement,edgar,warn,financials,sec_events,sweep] [--extras events,tech_procurement]` | Fit-gate, then scan accounts that are due (A weekly, B every other week) concurrently, logging tokens and cost per account. `--extras` names scanners that run afterwards for the scanned companies that already show a signal and for any scanned company not deeply looked at within `scan.deep_every_weeks`. `--dry-run` shows what would run with no API calls. Update my list and the weekly run use `--scanners sweep,hiring --extras tech_procurement`; WARN notices are left to Advanced tools |
| `bd score` | Deterministic scores for fit-gated accounts |
| `bd shortlist` | Read-only preview of who would be posted, and why others were not |
| `bd card [--dry-run] [--no-llm]` | Brief the shortlist, write `card.json`, post to Teams **only if `TEAMS_WEBHOOK_URL` is set** |
| `bd evaluate` | Run the scanners over `tests/gold.csv` and report found / missed / wrong date / fabricated or unreachable URLs / false positives |

Run dates are America/Chicago dates; pass `--as-of YYYY-MM-DD` to override.

## Local UI

```bash
pip install -e '.[ui]'     # adds Jinja2, the only extra dependency
bd ui                      # prints a one-time link; open it (a browser is opened for you unless --no-open)
```

A review-and-curate screen for the database. It **cannot** scan, score, call a model or post to Teams; those stay
on the command line.

The app has two areas on one login. **Analysis** is what you read, and it is one page (where the app opens after login). Its old addresses (`/week`, `/opportunities?view=…`, `/leadership`, `/hiring`) all lead to it. **Admin**, a link at the far right, is a separate area with its own header and a left-hand list: updating the list,
the company lists and review queues (including Look up, Existing clients and the watchlist), the extra **Reports** (Market view, Private equity firms, Open RFPs, Ask your data, Monday card; they open in the analysis layout) and the system pages (data health, run history, change log, settings).

| Page | What you can do |
|---|---|
| **Signal coverage** (Admin Home) | How many companies on the scan list have been scanned, how many have a signal in the last 90 days, how many have not been scanned yet and how many presses of Update my list that takes at the saved scan size, and what was found by kind (leadership, hiring, press, events, filings). Companies with an unconfirmed location or size are counted but never block a scan |
| **Look up a company** | Type any name: where it stands, its score and newest signal. If it is not on the list, add it and check the SEC for free (a public company gets size, ownership, headquarters and CIK in seconds), or run the capped (`$3`) research-and-scan job for that one company. A name that fits two companies is never guessed |
| **Existing clients** | Mark SEI clients (on a company's page, or paste a list: names are matched apart from Inc/Corp/punctuation/case, nothing is created). A client is still scanned and scored; this page shows news at clients apart from new business |
| **Analysis** (`/opportunities`, the home page) | The one page to read, top to bottom. A header band with a search box and three number tiles (companies that are Hot or Warm, new signals found this week, signals in the last 4 weeks against the 4 before). **The top three**: the three highest-scoring *new-business* companies (an existing client is expansion, so it is not shown here but is tagged in the list), whatever the dates of their signals. Each card shows its theme, its score with a trend line drawn from the stored scores, the rank it holds in the list below, the date of its newest signal and the signals behind it with their source links. **Is it heating up?**: a full-width chart of the signals found each week for the last 12 weeks by kind (leadership, hiring, press, company events and technology) with one sentence built only from those counts and the numbers one click away (a phone gets the last 6 weeks, drawn larger). **Ranked**: every company at Watch or above, numbered 1 to n by score, with its band, theme, newest signal date, change since the last run and a *client* tag; tabs for All, Hot, Warm, Watch and *Everything scanned* (which adds companies below Watch), and the Market view heatmap links here filtered by industry and signal type (when nobody is at Watch yet, the closest companies are listed instead). Then companies you track with what is new, everything found this week (folded away) and, at the bottom, **Hiring**: open job postings at companies on the list by area, ranked by senior roles, and the newest postings. The page says how fresh the scores are and flags any company whose stored score no longer matches its signals. Every score shown is the one the Score step stored; nothing on the page is computed differently from the data it came from |
| **Every signal** (`/signals`, linked from Analysis) | The whole feed: every stored signal of the last 7, 30 or 90 days, newest first, filtered by kind (leadership changes, hiring, press and media, company events and technology, filings and financials). A signal reported in a press release or news article is *press and media*; one from a filing, a notice or the company's own site is a *company event*. Rumours (Low confidence) are never shown, and one event found at several sources shows once, at the most trustworthy one |
| **Find a company** (`/find`) | The search box on Analysis. Type any name: companies on the list show their score and newest signal (or when they were last scanned and that nothing was found) with an **Open brief** link. A company never scanned, or a name not on the list, can be **scanned for signals**: job boards are read first (free), then the model searches for leadership changes, hiring and press, capped at **$1** of model spend and started only with the confirmation tick; the page then opens its brief. Anyone signed in can run it, one job at a time. Below Watch scores read "Below Watch" |
| **How this works** | A one-page plain guide: the jobs (see who ranks first, browse every signal, prepare for one company, track a company, show the market), a short glossary (sign, kinds of signs, theme, score, bands with the real thresholds, client, unverified), how fresh the data is, and what the tool does not do |
| **Company brief** | Open any company: a short read. *What this adds up to* (the main theme with its rule, best-fit service line and the signals behind it), a score line with its trend, and *What happened* (every dated signal with its source link). One folded *More detail* holds the last 180 days as a timeline, *Background* (company facts, PE owner, open postings, and a note when location or size is unconfirmed), *How the score is built* (points per signal type plus the stacking bonus, summing to the score) and the other themes that also match. A company not yet scanned says so and offers the capped scan. **Plain-text version** (`/companies/<id>/text`) is the same brief to paste into an email; the page also prints cleanly (Save as PDF). **Track this company**, a stage (New, Reviewing, Pursuing, Passed, Won, Lost) and notes are set on the page; tracked companies appear on Analysis with what is new. "Manage (Admin)" opens the page where facts are edited |
| **Monday card** | The part of the ranking the weekly run would post (Hot or Warm, a High-confidence signal in the last 90 days, not in cooldown, capped): score, why-now, every source link, and a "how the score was calculated" table |
| **Accounts** | List and search; see which companies are scanned, unverified (scanned, with a fact still unconfirmed) or out of scope and why; edit facts (blank = unknown) and see the fit-gate verdict at once; add an account |
| **Enrich review** | Go through `bd enrich`'s proposals fact by fact, each with its evidence link. New facts are ticked, ones that would replace a known value are not. **Apply** writes only the ticked facts, using the values in the file (not the page) |
| **Exclusions** | Add or remove clients and do-not-pursue companies; every account's status is re-checked immediately |
| **Runs & cost** | Tokens and estimated cost per step and per account; unpriced calls shown as unpriced, never as $0 |
| **Changes** | The audit log of every edit made in the UI, by `bd seed`, or by applying proposals |

**Look and feel.** The whole app is styled by one design system, `bd/ui/templates/design.css`, defined once and included by every page. It holds the tokens (colours, type, spacing, radii), the layout and the components: a coloured header band carrying each analysis page's title (on Analysis also the search box and three number tiles), white cards, solid band badges, ranked entries with a red or amber stripe by band, rounded tables and signal grids, and a darker header for the Admin area. It follows the device's light or dark setting, adapts to a phone, and prints cleanly. Pages carry no CSS of their own: `tests/test_design_system.py` fails if a template has a style block or a style attribute, uses a class the design system does not define, or if the stylesheet uses a hex colour outside its token blocks. To change the look everywhere, edit a token or a component in `design.css`; the rules are also recorded in `CLAUDE.md`. No scripts are used.

Because the **database is the master copy**, the UI edits `bd.sqlite` directly and `bd seed` no longer
overwrites it. Use `bd export` to write the accounts and exclusions back to CSV when you want them in git.

It listens on `127.0.0.1` only, behind a random per-run token, with CSRF and Host checks and a no-scripts
Content-Security-Policy; see SECURITY.md. Stop it with Ctrl-C.

## First live run

1. **Facts (optional).** `bd seed accounts.csv` holds your 58 account names with every fact blank. They are all
   scanned anyway and shown as *unverified*; only a known exclusion, a known non-Chicagoland location or a known
   below-floor size takes one out. Filling in `chicagoland` (yes/no), `revenue` or `employees`, `ownership`,
   `domain` and `cik` for public companies helps: a domain lets the press scanner run, a CIK the filing scanners.
   Do it in `bd ui`, in the CSV before seeding, or with `bd enrich --limit 3` first (to see real per-account
   cost) and then reviewing the proposals in the UI's **Enrich review** page.
2. **Measure accuracy.** Copy `tests/gold.template.csv` to `tests/gold.csv` and fill in expected signals you
   have verified yourself (write `none` for accounts with no signal), then `bd evaluate`.
3. **Dry-run the pipeline.** `bd scan --limit 3`, `bd score`, `bd shortlist`, `bd card --dry-run`, and
   open `card.json`. Check the per-account cost lines before widening.
4. **Post.** Set `TEAMS_WEBHOOK_URL` and run `bd card`.

## Daily light refresh

Every day except Monday at 05:00 America/Chicago the hosted app runs a *light refresh*: `bd scan --scanners ats,edgar,earnings --force --light`, then `bd score`. Job boards are read for free; only SEC filings not read before cost a model call; it never builds a card or posts, and `--light` leaves the weekly cadence alone. Turn it off on the Settings page. Monday's full run does everything.

## Hosted app (use it from a browser, no terminal)

`bd serve` is the internet-facing version of the UI: password sign-in, an **Operate** page with buttons for enrich,
scan, score and dry runs (spend-confirmed, one at a time, never posting), and the Monday 05:00 America/Chicago run
built in, all on one database. It refuses to start without `BD_UI_PASSWORD` (16+ characters). Everything needed to
deploy it (Dockerfile, `railway.json`) is in the repo; the steps and variables are in [docs/DEPLOY.md](docs/DEPLOY.md).
`bd ui` remains the local, loopback-only variant.

## Weekly schedule: Monday 05:00 America/Chicago

(If you use the hosted app, the schedule is already built in and you can skip this section.)


`scripts/weekly.sh` runs `seed → scan → pe-signals → score → card`, stops at the first failing step (`bd scan` exits 3
if every call failed, so nothing is posted on a broken run), and honours `BD_DRY_RUN=1`. Two ways to
schedule it:

### Option A: GitHub Actions (`.github/workflows/weekly.yml`)

* GitHub cron is UTC-only, so the workflow schedules both 10:00 and 11:00 UTC on Mondays and
  `scripts/chicago_gate.sh` lets exactly one through (05:00 CDT or 05:00 CST). GitHub may start scheduled
  runs late; one that starts after 05:59 Chicago time is skipped. Re-run it with **Run workflow**.
* Set repository **secrets** `ANTHROPIC_API_KEY` and `TEAMS_WEBHOOK_URL`, and **variables**
  `ANTHROPIC_MODEL` and `BD_USER_AGENT`.
* Runners are ephemeral, so the SQLite database is kept on a single-commit branch `bd-state`
  (`scripts/state.sh`). **The repository must be private.** The first run creates the database from
  `accounts.csv`; if the state branch cannot be reached the run aborts rather than start empty.
* **Caveat if you also use `bd ui`:** the UI edits *your* copy of the database, but the Monday run works on the
  copy on `bd-state`, and its final save replaces that branch. An edit made in the UI never reaches the run
  unless you push your copy (`scripts/state.sh save`), and a push made while the run is in progress is lost.
  Workable with discipline, not safe by default. If you will use the UI regularly, prefer Option B.
* "Run workflow" is a dry run by default (it never posts to Teams).

### Option B: cron on an SEI-managed host

See `deploy/crontab.example` (`CRON_TZ=America/Chicago`, `0 5 * * 1`). The database lives on the host's
disk, so no state branch is needed, and `bd ui` run on the same host (with the same `BD_DB_PATH`) edits the very
database the Monday run uses. This is the clean setup for the database-as-master model.

## Configuration

**Admin values.** Admin, *Settings* holds the numbers you can change without a deploy: the Update numbers (companies researched, segments searched, companies scanned, spending limit, RFPs), the new **Scan cycles** (how often the deep technology and procurement scan repeats for every company, how often a company's own job board is looked for, and how many careers pages are read per run), and the discovery settings. A value saved there overrides `config.yaml` for every run, and every change is on the Change log. A release can raise a saved number **once**, and says so: it is audited under the actor `release`, shown on Admin Home and on Settings for 30 days, applied only when the saved number is lower than the new floor, and never touched again (so if you set it lower afterwards it stays). The first one raised *Companies to research per update* to 25 (the unconfirmed-company backlog). Settings also shows how many updates one full pass over the list takes at your scan number, and whether that is enough for the deep cycle.

Everything tunable is in `config.yaml`: base points and families, recency bands, confidence and service
multipliers, bands and post gate, counties, size floors, exclusion industries, scan window and cadence,
concurrency and budget, blocked domains, pricing. Edit it, not the code. The **database** is the master copy of accounts and exclusions;
`accounts.csv` and `exclusions.csv` are seed files and backups (`bd export`).

## Testing

```bash
pytest -q          # offline: no network, no API key
```

The tests reproduce the three scoring reference accounts (A = 91, B = 55, C = 7) and cover the scanners,
URL verifier, fit gate, runner, briefer, card, EDGAR client, enrich, and the schedule scripts. The PE work has its own tests
(`tests/test_pe_*.py`, `tests/test_opportunity_model.py`): ownership status rules, entity resolution and duplicates, the page reader, listing parsing,
collection idempotency and failure handling, transaction verification, promotion, priority scoring and the PE screens. They use synthetic pages
through a fake HTTP client (`tests/pe_fakes.py`); **no test or screen in the normal app uses synthetic data**.

## Design rules and where they are enforced

| Rule | Enforced in |
|---|---|
| Claude extracts, code scores; scoring unit tested | `bd/scoring.py`, `tests/test_scoring.py` |
| Every `source_url` was retrieved and resolves | `bd/scanners/base.py`, `bd/verify.py` |
| `event_date` from the source; dedupe on account + type + date + domain | `bd/hashing.py`, `bd/scanners/base.py` |
| Fewer, higher-confidence leads; Low is stored, never scored | `bd/scoring.py`, `bd/shortlist.py` |
| Settings in `config.yaml`; secrets and model name from env only | `bd/config.py` |
| Tokens and estimated cost per account per run | `bd/runner.py`, `bd/cost.py`, table `runs` |

## Known limits

* A URL that loads shows the page exists, not that it supports the claim; `bd evaluate` against a
  human-verified gold file is how that is measured.
* The same event reported on several domains counts once (same account, type and date); an event dated a
  day apart by two sources is counted twice.
* Every scanner is a separate web-search call per account, so a full run costs several times a single scanner
  (see the table above). Measure on a few accounts before running all of them, and tune `--scanners`.
* `hiring` only counts postings that state a date and sit on the company's careers or applicant-tracking
  pages; job boards (Indeed, Glassdoor, ZipRecruiter) and LinkedIn are blocked, so postings on those
  alone are never seen.
* `rfp_match` needs a stated, future due date; `risk_factor` must link to the filing on sec.gov.
* PE portfolio pages that render their list with scripts and have no public API cannot be read by a reader that runs no JavaScript: they are reported as *partial* or *unknown* coverage, never as complete. Pages behind a login, paywall or CAPTCHA are not read.
* A firm's page lists what the firm chooses to show. "Complete" means the whole listing was read, not that it names every holding; a holding the page omits is not known to the product.
* A company's location is taken only from a page that shows it. A listing that gives no location leaves geography *not verified* (the free locate step reads the company's own site; `pe_portfolio.research_unresolved` can pay for fact research).
* Transaction research (`bd pe-deals`) and promotion (`bd pe-promote`) need `ANTHROPIC_API_KEY`; `bd pe-collect` does not.
* A web search that comes back with `invalid_tool_input` on every search of a call (nothing returned) is repeated once with the basic search tool (`llm.web_search_fallback_tool_type`; `null` turns it off). If that fails too it is still reported as a search failure, never as "found nothing". The cause of the rejections has not been established; the repeat is a remedy, not a diagnosis.
* A CIK the SEC has no structured data for (HTTP 404, for example a subsidiary that reports inside its parent's filing) is recorded as `no_company_facts`, not as an error every week; other SEC failures are still errors.
* The counter-signal types the priority model understands (a canceled program, a spending restriction) are not yet produced by any scanner, so counterevidence appears only once one is added or stored.
* See [SECURITY.md](SECURITY.md) for the full list of gaps.

## Layout

```
bd/            package: cli, config, db, scoring, scanners/ (one module per family), prompts/, verify, edgar, runner, enrich, briefer, card
               PE portfolio: pe_ownership (ledger), sources + pe_listing + pe_collect (bounded reader), pe_deals, pe_promote, opportunity_model (priority), pe_view (screens' data)
config.yaml    all tunables        accounts.csv / exclusions.csv   seed files and backups of the database
bd/ui/         the local review-and-curate UI (templates in bd/ui/templates)
scripts/       weekly.sh, chicago_gate.sh, state.sh      deploy/   crontab.example
.github/workflows/weekly.yml                              tests/    offline test suite
```
