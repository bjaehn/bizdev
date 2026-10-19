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
  A location or size that is not confirmed does not stop a scan: the company is scanned and tagged *unverified*.
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
| `ANTHROPIC_SCAN_MODEL` | the `sweep` scanner and `bd compare` (optional) | a cheaper model for the combined sweep scan, e.g. Haiku 5.5. Unset: the sweep uses `ANTHROPIC_MODEL`. It must support the web search tool and have a price in `config.yaml` `llm.pricing` (`bd check` warns if not). Nothing else uses it |
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
| `pe` | pe_platform_or_sponsor_exec, pe_addon, pe_hold_2_4 | accounts whose ownership is `pe-backed` | `pe_hold_2_4` is a status signal: valid only while the sponsor's original investment is 2-4 years old, and it stops scoring when the hold passes 4 years |
| `hiring` | hiring_cluster, hiring_leadership, hiring_interim, hiring_single | accounts without a known job board | Claude lists postings; **code** verifies each, counts the cluster (3+ related postings within 60 days) and writes the summaries |
| `ats` | hiring_cluster, hiring_leadership, hiring_interim, hiring_single | accounts whose Greenhouse / Lever / Ashby board is known | **free and daily**: reads the company's own job board JSON (every open posting, no model). The board id is learned from posting URLs the `hiring` scanner verified, never guessed. Titles are classified by the keyword lists in `config.yaml` (`hiring.theme_keywords`): data/AI, transformation/strategy/operations, product/engineering build-out. `hiring_leadership` = a VP-or-above posting in one of those areas. Postings are stored and shown on the Hiring page |
| `warn` | restructuring_layoffs | every account | Illinois WARN (mass layoff / plant closing) notices: the search tool is restricted to the state's own domains (`sources.warn_domains`), and the page must be on one of them. Most companies have none, which is a normal result |
| `financials` | financial_pressure | accounts with a CIK | **free**: no model. Reads the company's latest 10-K figures from the SEC's structured data (data.sec.gov company facts) and flags revenue down, operating margin down, or an operating loss versus the prior year (thresholds in `config.yaml` `financials`). Dated by the day the 10-K was filed; needs a 10-K filed inside the 180-day window. Banks and insurers usually lack comparable tags and get no signal |
| `sec_events` | restructuring_layoffs, ma_divestiture | accounts with a CIK | **free**: no model. Reads the company's SEC filing list for 8-Ks that report Item 2.05 (costs of exit or disposal activities) or Item 2.01 (completed acquisition or disposition); which items count, and what each becomes, is `config.yaml` `sec_events`. The filing is fetched and must carry the item's own heading; the summary is written by code from the item's title; dated by the report date on the filing. Each filing is read once |
| `tech_procurement` | platform_migration, vendor_deal, rfp_match, risk_factor | every account | an RFP must have a future due date; a risk factor must be on sec.gov |
| `sweep` | leadership, press, events and technology types (not hiring, RFPs or risk factors) | every account | **One** model call per company in place of four (`leadership`, `press`, `events`, `tech_procurement`), with a small search budget (`scan.sweep_searches`, 6) and the same acceptance rules. It can run on a cheaper model (`ANTHROPIC_SCAN_MODEL`). **Not part of `all` and not in any job yet:** use `bd compare` first and decide on its result |

**Free scans.** Three scanners make no model call: `ats` (job boards), `financials` and `sec_events` (SEC data). They go first in any scan and the spending limit never holds them back, and `Update my list` runs them over every company on the scan list before its limited, paid scan, so a first look is never empty. The paid signal scan comes before the work that fills in facts or finds new companies, so facts never use up the spending limit that signals need. It is tiered to save money: every company gets the core scan (leadership, press, hiring), and the extras (company events, technology) run only for companies that already show a signal. On the first live run events, technology and WARN notices were about half the spend and found one signal in four companies, so WARN is off by default.

**Keeping the spend down.** Four things, in order of effect. (1) *Tiered scan* as above. (2) *Quiet companies wait longer*: a company already scanned that shows no signal in the last 60 days is checked every 4 weeks instead of its tier's cadence (`scan.quiet_days`, `scan.quiet_every_weeks`; 0 turns it off); `bd scan` says how many are waiting. A company with a signal, or one never scanned, is never held back, and `--force` ignores it. (3) *The combined sweep and a cheaper model*, both built but **off until tested**: `bd compare` (Admin, Operate, *Test the cheaper scan*; capped at $3, nothing is saved) runs the sweep on the companies with the most stored signals and prints, for each, what the separate scanners had stored against what the sweep found now (the same, not stored before, missed), its cost and what the four separate calls cost last time. Switch only if the sweep finds at least as many signals for less; the costs in this README are estimates until you have run it. (4) *Per-model pricing*: each scanner's calls are priced by the model they ran on, including the higher rate some models charge above a long prompt, so the cost figures and the spending limit follow what you are actually using. Prompt caching was looked at and left out: it does not pay for single-shot searches.

**Open public RFPs** (`bd rfps`, the Open RFPs page) are a separate, market-level feed rather than a scanner: one search per agency group in `config.yaml` `rfps.groups` (City of Chicago, Cook County, State of Illinois, schools and universities, transit and water agencies, federal), each restricted to that group's own domains. A solicitation is kept only if its page is on those domains, was in the search results, resolves, and states a due date that has not passed. They are not tied to a company and are not scored. Part of the weekly run, and of `Update my list` only when its RFP setting is turned on (off by default); roughly six searches per run.

**Data health** (`/health`, in Admin) and **Market view** (`/market`, in Admin under Reports) are read-only pages built from what past runs stored. Data health shows which scanners produced anything (and which failed or never ran), cost per scanned company, which facts are still unverified, and how fresh the data is. Market view shows companies by band at every scoring run (a stacked-bar trend: Hot, Warm, Watch, one bar per run as that run scored them, so it also moves when companies become ready), an **industry heatmap** (signals in the last 90 days by industry and signal type; click a number to see those companies), the industry numbers with a 30-day trend, signal groups by month, sponsor activity, and the large companies with no signal in 180 days.

**Ask your data** (`/ask`, under More, hosted app only) answers a plain-English question about your own database. It runs as a job (paid-call tick, a few cents): a model writes one read-only query from the table layout, code checks and runs it on a read-only connection limited to an allow-list of tables, and a second call phrases the answer from the returned rows. The answer, the rows and the SQL are stored and shown together. Your free-text notes are not available to it. `bd ask --id N` is the command behind the button.

**Facts for SEC filers come from the SEC, not the model.** For a company with a CIK, `bd enrich` (and discover's research) reads the SEC's company-facts and submissions files in code: revenue is the latest fiscal year from its 10-K, ownership is `public` when it has a listed ticker, and HQ city is the business address. The evidence link on each fact is the SEC file read. Private companies and nonprofits still rely on the model's sourced figures. `bd sec-fill` does this for every company on file before the model is asked (free), and `bd enrich` also finds a missing CIK from the SEC's list of listed companies (exact name match) and then reads the rest from the SEC, so a public company's size no longer waits for a web search. **Update my list** has a setting for whether to search agency sites for RFPs; it is **off by default** (two live runs cost about $0.90 each and found nothing: the model's search budget ran out before it reached a listing page).

**Pattern bonus (optional, off by default).** `scoring.sequences` in `config.yaml` lets order count: for example a new executive followed within 120 days by a hiring push earns a bonus (each pattern once, capped together at `sequence_bonus_max`, shown on the opportunity as a "Pattern" reason). It is empty by default because enabling it changes scores, including the spec's reference accounts; the config file holds four starting patterns to uncomment.

## Commands

| Command | What it does |
|---|---|
| `bd seed accounts.csv [--exclusions exclusions.csv] [--overwrite]` | Load accounts and exclusions, then run the fit gate. **The database is the master copy**: for an account that already exists, a CSV value only fills a fact that is still unknown; differences are reported and the database value is kept unless you pass `--overwrite`. Blank cells stay *unknown*, never guessed. Also loads a reviewed `bd enrich` file |
| `bd export [--accounts accounts.csv] [--exclusions exclusions.csv]` | Write the database's accounts and exclusions back to CSV (for git or backup). Overwrites those files, so review with `git diff` |
| `bd ui` | Open the local review-and-curate UI (see **Local UI** below) |
| `bd discover [--segment TEXT] [--limit N] [--dry-run]` | **Market research.** Finds Chicagoland companies by segment (`discovery.segments` in config), researches each one's facts with the same verified-evidence rules as `enrich`, **auto-adds** those that pass the fit gate (tier B, marked *auto-added*), queues uncertain ones on the Discovery page and records fit-gate failures so they are never researched twice. Auto-added accounts are scanned only up to `discovery.scan_unreviewed_per_run` per run until you mark them reviewed |
| `bd pe-sync` / `bd pe-portfolio [--firm T] [--limit N]` / `bd pe-signals [--firm T] [--limit N]` | **Private equity.** `pe-sync` (free) derives PE firms and portfolio links from accounts that have a sponsor. `pe-portfolio` reads each firm's own site and finds its other Chicagoland companies, which go through the same verify / fit-gate / auto-add path as `discover`; a company is linked to the firm only when the firm's own site lists it. `pe-signals` looks for firm-level news (new fund, exit, operating-partner hire) with the same source rules as company signals. A firm's signals add a small, **capped** lift (`scoring.sponsor`) to portfolio companies that already have a signal of their own; the lift is shown separately and can never make a company postable by itself |
| `bd sec-fill [--only TEXT [--exact]] [--limit N]` | **Free, no model.** Fills what the SEC itself can tell us: for a company with a CIK, its revenue (latest 10-K), public ownership and headquarters city from the SEC's company files; for a company with no CIK that is not recorded as private or PE-backed, a CIK found by **exact name match** (legal suffixes, punctuation and case ignored; a name that fits two filers is skipped) in the SEC's list of listed companies. Applied through the same writer as every edit (fit gate re-run, actor `sec` on the Change log, with the SEC page each fact came from and the SEC's registered name for a CIK match); never overwrites a stored value. Reads at most `sec_fill.max_accounts_per_run` SEC records a run, oldest-tried first. Skips with a note when `BD_USER_AGENT` is not set. First step of `Update my list` |
| `bd enrich` | **Proposes** missing facts (domain, Chicagoland, size, ownership, CIK) with an evidence URL each, into `enrich_proposals.csv`. Never writes to the database. Review, then `bd seed enrich_proposals.csv` |
| `bd compare [--count N] [--only TEXT]` | **Test, stores nothing.** Runs the `sweep` scanner (on `ANTHROPIC_SCAN_MODEL` when set) on the N companies with the most stored signals of the kinds it covers, or on one named company, and prints stored vs found per company, the cost of each side, and a one-line verdict. Stops before the spending limit. Admin: Operate, *Test the cheaper scan* ($3 cap) |
| `bd scan [--tier A\|B\|all] [--only TEXT [--exact]] [--dry-run] [--force] [--scanners leadership,events,press,earnings,pe,hiring,tech_procurement,edgar,warn,financials,sec_events,sweep] [--extras events,tech_procurement]` | Fit-gate, then scan accounts that are due (A weekly, B every other week) concurrently, logging tokens and cost per account. `--extras` names scanners that run afterwards only for the scanned companies that already show a signal. `--dry-run` shows what would run with no API calls. Update my list and the weekly run use `--scanners leadership,press,hiring --extras events,tech_procurement`; WARN notices are left to Advanced tools |
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

The app has two areas on one login. **Analysis** is what you read, and it has two screens: **This week** (where the app opens after login) and **Companies**. **Admin**, a link at the far right, is a separate area with its own header and a left-hand list: updating the list,
the company lists and review queues (including Look up, Existing clients and the watchlist), the extra **Reports** (Market view, Hiring, Private equity firms, Open RFPs, Ask your data, Monday card; they open in the analysis layout) and the system pages (data health, run history, change log, settings).

| Page | What you can do |
|---|---|
| **Signal coverage** (Admin Home) | How many companies on the scan list have been scanned, how many have a signal in the last 90 days, how many have not been scanned yet and how many presses of Update my list that takes at the saved scan size, and what was found by kind (leadership, hiring, press, events, filings). Companies with an unconfirmed location or size are counted but never block a scan |
| **Look up a company** | Type any name: where it stands, its score and newest signal. If it is not on the list, add it and check the SEC for free (a public company gets size, ownership, headquarters and CIK in seconds), or run the capped (`$3`) research-and-scan job for that one company. A name that fits two companies is never guessed |
| **Existing clients** | Mark SEI clients (on a company's page, or paste a list: names are matched apart from Inc/Corp/punctuation/case, nothing is created). A client is still scanned and scored; this page shows news at clients apart from new business |
| **This week** (home) | The weekly answer. Three number tiles (companies worth a call, new signals found this week, signals in the last 4 weeks against the 4 before). *Worth a call*: up to three new-business companies that are Hot or Warm **and** have a signal dated in the last 14 days, strongest first (if fewer qualify, the strongest Watch companies fill the list and it says so). Each card shows the theme, the score with a trend line drawn from the stored scores, and the newest signals behind it with their source links. *Is it heating up?*: a 12-week chart of signals found by kind (leadership, hiring, press, company events and technology) and one sentence built only from those counts. *Next in line* as score bars (or *Closest to Watch* when nobody is ranked yet). Then existing clients with news, companies you track and, folded away, everything found this week. A search box finds any company by name; the page says how fresh the scores are |
| **Every signal** (`/signals`, linked from the folded list on This week) | The whole feed: every stored signal of the last 7, 30 or 90 days, newest first, filtered by kind (leadership changes, hiring, press and media, company events and technology, filings and financials). A signal reported in a press release or news article is *press and media*; one from a filing, a notice or the company's own site is a *company event*. Rumours (Low confidence) are never shown, and one event found at several sources shows once, at the most trustworthy one |
| **Find a company** (`/find`) | The search box on This week. Type any name: companies on the list show their score and newest signal (or when they were last scanned and that nothing was found) with an **Open brief** link. A company never scanned, or a name not on the list, can be **scanned for signals**: job boards are read first (free), then the model searches for leadership changes, hiring and press, capped at **$1** of model spend and started only with the confirmation tick; the page then opens its brief. Anyone signed in can run it, one job at a time. Below Watch scores read "Below Watch" |
| **How this works** | A one-page plain guide: the jobs (see what changed, browse every signal, prepare for one company, show the market, track a company), a short glossary (sign, kinds of signs, theme, score, bands with the real thresholds, client, unverified), how fresh the data is, and what the tool does not do |
| **Companies** | One ranked list: a score bar and theme per company, search by name, and tabs for All, Hot, Warm, Watch and Everything scanned (including companies below Watch). The fuller views stay for anyone who types the address: `?view=matrix` (a **signal matrix**: one row per company, one column per signal type, a cell shaded by the points that type added, a dot for news in the last 30 days, **Types** counting how many kinds agree), `?view=chart` (the **priority chart**: score up, revenue across on a log scale, dot size = signal types; a company with no revenue on file is listed under the chart, not drawn at a guessed size) and `?view=list` (cards). Those views have the full filters (band, industry, signal group, service line, PE-backed, new, status) and sorting |
| **Company brief** | Open any company: a short read. *What this adds up to* (the main theme with its rule, best-fit service line and the signals behind it), a score line with its trend, and *What happened* (every dated signal with its source link). One folded *More detail* holds the last 180 days as a timeline, *Background* (company facts, PE owner, open postings, and a note when location or size is unconfirmed), *How the score is built* (points per signal type plus the stacking bonus, summing to the score) and the other themes that also match. A company not yet scanned says so and offers the capped scan. **Plain-text version** (`/companies/<id>/text`) is the same brief to paste into an email; the page also prints cleanly (Save as PDF). **Track this company**, a stage (New, Reviewing, Pursuing, Passed, Won, Lost) and notes are set on the page; tracked companies appear on This week with what is new. "Manage (Admin)" opens the page where facts are edited |
| **Monday card** | The part of the ranking the weekly run would post (Hot or Warm, a High-confidence signal in the last 90 days, not in cooldown, capped): score, why-now, every source link, and a "how the score was calculated" table |
| **Accounts** | List and search; see which companies are scanned, unverified (scanned, with a fact still unconfirmed) or out of scope and why; edit facts (blank = unknown) and see the fit-gate verdict at once; add an account |
| **Enrich review** | Go through `bd enrich`'s proposals fact by fact, each with its evidence link. New facts are ticked, ones that would replace a known value are not. **Apply** writes only the ticked facts, using the values in the file (not the page) |
| **Exclusions** | Add or remove clients and do-not-pursue companies; every account's status is re-checked immediately |
| **Runs & cost** | Tokens and estimated cost per step and per account; unpriced calls shown as unpriced, never as $0 |
| **Changes** | The audit log of every edit made in the UI, by `bd seed`, or by applying proposals |

**Look and feel.** The whole app is styled by one design system, `bd/ui/templates/design.css`, defined once and included by every page. It holds the tokens (colours, type, spacing, radii), the layout and the components: a coloured header band carrying each analysis page's title (on This week also the search box and four number tiles), white cards, solid band badges, ranked entries with a red or amber stripe by band, rounded tables and signal grids, and a darker header for the Admin area. It follows the device's light or dark setting, adapts to a phone, and prints cleanly. Pages carry no CSS of their own: `tests/test_design_system.py` fails if a template has a style block or a style attribute, uses a class the design system does not define, or if the stylesheet uses a hex colour outside its token blocks. To change the look everywhere, edit a token or a component in `design.css`; the rules are also recorded in `CLAUDE.md`. No scripts are used.

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

Everything tunable is in `config.yaml`: base points and families, recency bands, confidence and service
multipliers, bands and post gate, counties, size floors, exclusion industries, scan window and cadence,
concurrency and budget, blocked domains, pricing. Edit it, not the code. The **database** is the master copy of accounts and exclusions;
`accounts.csv` and `exclusions.csv` are seed files and backups (`bd export`).

## Testing

```bash
pytest -q          # offline: no network, no API key
```

The tests reproduce the three scoring reference accounts (A = 91, B = 55, C = 7) and cover the scanners,
URL verifier, fit gate, runner, briefer, card, EDGAR client, enrich, and the schedule scripts.

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
* See [SECURITY.md](SECURITY.md) for the full list of gaps.

## Layout

```
bd/            package: cli, config, db, scoring, scanners/ (one module per family), prompts/, verify, edgar, runner, enrich, briefer, card
config.yaml    all tunables        accounts.csv / exclusions.csv   seed files and backups of the database
bd/ui/         the local review-and-curate UI (templates in bd/ui/templates)
scripts/       weekly.sh, chicago_gate.sh, state.sh      deploy/   crontab.example
.github/workflows/weekly.yml                              tests/    offline test suite
```
