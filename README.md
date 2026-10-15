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
accounts.csv ──bd seed──► database ──► fit gate ──► scan (leadership, events, edgar) ──► score ──► shortlist ──► card ──► Teams
 exclusions.csv            pass / HOLD / fail   │                              │          │            │
                           (unknown is held,    │ Claude + web search          │ plain    │ cooldown,  │ briefer writes one
                            never scored)       │ extracts; CODE verifies      │ code,    │ 90-day     │ sentence; Adaptive
                                                │ every source URL             │ no LLM   │ High gate  │ Card 1.4, links only
```

* **Claude extracts, code decides.** Claude proposes signals; code accepts one only if its type, date window
  and source URL pass (the URL must have been returned by the search tool *and* load when fetched).
  Scoring is deterministic and unit tested.
* **Fewer, better leads.** Only Hot (75+) and Warm (60-74) accounts with a High-confidence signal in the
  last 90 days are posted, at most 10, with a 4-week cooldown unless a new signal family appears or the
  score rises 15+. Low-confidence signals are stored but never scored.
* **Nothing is guessed.** Location or size you have not verified means the account is *held for review*,
  not scored.

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
| `events` | restructuring_layoffs, ma_divestiture, strategy_rebrand, ai_commitment, activist, ops_failure, cost_program | every account | includes Illinois WARN notices |
| `pe` | pe_platform_or_sponsor_exec, pe_addon, pe_hold_2_4 | accounts whose ownership is `pe-backed` | `pe_hold_2_4` is a status signal: valid only while the sponsor's original investment is 2-4 years old, and it stops scoring when the hold passes 4 years |
| `hiring` | hiring_cluster, hiring_interim, hiring_single | every account | Claude lists postings; **code** verifies each, counts the cluster (3+ related postings within 60 days) and writes the summaries |
| `tech_procurement` | platform_migration, vendor_deal, rfp_match, risk_factor | every account | an RFP must have a future due date; a risk factor must be on sec.gov |

## Commands

| Command | What it does |
|---|---|
| `bd seed accounts.csv [--exclusions exclusions.csv] [--overwrite]` | Load accounts and exclusions, then run the fit gate. **The database is the master copy**: for an account that already exists, a CSV value only fills a fact that is still unknown; differences are reported and the database value is kept unless you pass `--overwrite`. Blank cells stay *unknown*, never guessed. Also loads a reviewed `bd enrich` file |
| `bd export [--accounts accounts.csv] [--exclusions exclusions.csv]` | Write the database's accounts and exclusions back to CSV (for git or backup). Overwrites those files, so review with `git diff` |
| `bd ui` | Open the local review-and-curate UI (see **Local UI** below) |
| `bd enrich` | **Proposes** missing facts (domain, Chicagoland, size, ownership, CIK) with an evidence URL each, into `enrich_proposals.csv`. Never writes to the database. Review, then `bd seed enrich_proposals.csv` |
| `bd scan [--tier A\|B\|all] [--dry-run] [--force] [--scanners leadership,events,pe,hiring,tech_procurement,edgar]` | Fit-gate, then scan accounts that are due (A weekly, B every other week) concurrently, logging tokens and cost per account. `--dry-run` shows what would run with no API calls |
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

| Page | What you can do |
|---|---|
| **Leads** | See what the weekly run would post: score, the one-line why-now, every source link, and a "how the score was calculated" table. Also who is tracked but not posted, and why |
| **Accounts** | List and search; see why an account is held; edit its facts (blank = unknown) and see the fit-gate verdict at once; add an account |
| **Enrich review** | Go through `bd enrich`'s proposals fact by fact, each with its evidence link. New facts are ticked, ones that would replace a known value are not. **Apply** writes only the ticked facts, using the values in the file (not the page) |
| **Exclusions** | Add or remove clients and do-not-pursue companies; every account's status is re-checked immediately |
| **Runs & cost** | Tokens and estimated cost per step and per account; unpriced calls shown as unpriced, never as $0 |
| **Changes** | The audit log of every edit made in the UI, by `bd seed`, or by applying proposals |

Because the **database is the master copy**, the UI edits `bd.sqlite` directly and `bd seed` no longer
overwrites it. Use `bd export` to write the accounts and exclusions back to CSV when you want them in git.

It listens on `127.0.0.1` only, behind a random per-run token, with CSRF and Host checks and a no-scripts
Content-Security-Policy; see SECURITY.md. Stop it with Ctrl-C.

## First live run

1. **Facts.** `bd seed accounts.csv` holds your 58 account names with every fact blank, so the fit gate
   holds all of them. Fill in `chicagoland` (yes/no), `revenue` or `employees`, `ownership`, `domain`,
   and `cik` for public companies: in `bd ui`, in the CSV before seeding, or with `bd enrich --limit 3`
   first (to see real per-account cost) and then reviewing the proposals in the UI's **Enrich review** page.
2. **Measure accuracy.** Copy `tests/gold.template.csv` to `tests/gold.csv` and fill in expected signals you
   have verified yourself (write `none` for accounts with no signal), then `bd evaluate`.
3. **Dry-run the pipeline.** `bd scan --limit 3`, `bd score`, `bd shortlist`, `bd card --dry-run`, and
   open `card.json`. Check the per-account cost lines before widening.
4. **Post.** Set `TEAMS_WEBHOOK_URL` and run `bd card`.

## Weekly schedule: Monday 05:00 America/Chicago

`scripts/weekly.sh` runs `seed → scan → score → card`, stops at the first failing step (`bd scan` exits 3
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
