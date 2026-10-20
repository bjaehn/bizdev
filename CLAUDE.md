# SEI Chicago BD Signal Agent: project memory

Each week this finds Chicagoland companies showing demand for SEI-type management consulting and ranks them. **The product is signals**:
leadership changes, hiring, press and media, company events and technology. Company facts (SEC figures, size, ownership) are background and never
gate a scan. Claude extracts signals from public sources; **code scores** them and **code names the theme** they add up to, deterministically. A server-rendered web app (stdlib WSGI + Jinja2, SQLite) lets people read the
ranking and lets an admin run updates. Deployed on Railway from `main`.

## Commands
- Tests: `.venv/bin/python -m pytest -q` (must stay green; about 1,200 tests, about 70 seconds).
- UI locally: `bd ui` (needs `pip install -e '.[ui]'`). Screenshots: Playwright Chromium at `/opt/pw-browsers/chromium-1194/chrome-linux/chrome`.
- Everything the app can run is a whitelisted `python -m bd.cli` subprocess (see `bd/jobs.py`).

## Hard rules (do not break)
- **Do not invent data.** Every sentence on a page is built from stored, sourced signals or counts. Every source URL must have been retrieved and resolve.
- **Scoring is deterministic code** (`bd/scoring.py`, weights in `config.yaml`); a model never sets a score.
- **Signals first, facts second.** Everything not *known* out of scope is scanned (`bd/fit_gate.py`: only an exclusion, a known non-Chicagoland location or a known
  below-floor size skips a company; unknown facts mean "unverified", never "held"). The paid signal scan comes before facts research and discovery in Update,
  so facts never use up the spending limit signals need. Themes and the feed are code rules over stored signals (`bd/insights.py`, `bd/feed.py`); no model-written prose.
- **Research only**: no outreach copy, no "what to say". No LinkedIn or paywalled sites. The UI cannot post to Teams.
- **No JavaScript.** The CSP forbids scripts. Secrets and the model name come from environment variables only.
- Analysis (what people read) and Admin (everything that maintains data) are separate areas on one login; keep them apart.
- **PE ownership is a ledger of evidence, never a typed field** (`bd/pe_ownership.py`): one relationship per (firm, company); status (current/pending/historical/unclear),
  closing/exit dates and stake are *derived* from evidence rows by fixed rules. An announcement is not a closing, an old announcement is not current ownership,
  disappearing from a page is *unclear* not an exit, a stake is stated only when a source states it, and holding duration is never calculated or scored
  (`pe_hold_2_4` weight is 0). Evidence is `page_verified` (code fetched the page and found the excerpt) or `snippet_only` (never changes a status). A company's
  location comes only from a retrieved page; a firm's headquarters is never a portfolio company's. A portfolio is "complete" only when coverage says so.
- **Priority is not a score of the firm and not a probability** (`bd/opportunity_model.py`, `opportunity_model` in `config.yaml`): anchored dimensions 30/25/20/15/10,
  unknown is not zero and is never rescaled, one event counts once, no input for size, news volume, job count, portfolio size or PE ownership, a model never sets it.
  Facts, hypotheses, unknowns and next steps stay separate on every brief. The `its own area` gate (`opportunity_model.outside_places`) keeps events at places outside the configured area from making a company Prospect now by themselves; the question to explore is built only from the event the first hypothesis rests on. It lives beside the older Hot/Warm/Watch score; neither feeds the other.
- A release may change a saved Admin number only through `RELEASE_CHANGES` in `bd/settings_store.py`: once, only upward to a floor, audited as actor `release`, and shown on Admin Home and Settings. New tunables belong in `CYCLE_FIELDS` (Settings page) so they are not left in `config.yaml` unseen.
- `ats-find --soft` (a company's own job board, found on its own careers page, never guessed from its name) runs in Update and the Monday run like `pe-collect --soft` and must never fail them.
- `pe-collect --soft` is a step of Update and the Monday run: it must never fail those jobs (a failing step stops the job). A search that fails with `invalid_tool_input` on every search is repeated once with the basic search tool; `sponsor_splits` in config is the only way a sponsor name is split.
- **No synthetic data outside tests.** PE pages show real collected rows or an honest empty/failed/partial state. Tests use `tests/pe_fakes.py`.

## Design system (one stylesheet, defined once, applied globally)
**All styling lives in `bd/ui/templates/design.css`**, included once by `base.html`. Its header comment is the component catalogue; read it
before touching any template. The rules are enforced by `tests/test_design_system.py`:

1. **No per-page CSS.** No style blocks and no `style=""` attributes in any template. The only allowed inline style is passing a data
   value to a component as a custom property (`style="--v:.4"`, `style="--w:60%"`, `style="--c:{{ color }}"`).
2. **Use components, do not invent classes.** Every class in a template must be defined in `design.css`. Need a new look or a new
   element? Add or extend a component **in `design.css`** so every page gets it; never solve it in one template.
3. **Tokens only.** Colours, font sizes, spacing (`--s1..--s6`), radii and shadows come from the tokens (section 1). Hex colours
   appear only in the two token blocks (light and dark). Dark mode, phone and print are handled in the stylesheet, not per page.
4. **Rhythm, not margins.** Vertical spacing comes from the stack rule (`main`, `.stack`, `.card`, `.prose`, `.entry-main`), so do not
   add margins to elements. Wrap a group in `.stack` if it needs the rhythm.
5. **Repeated markup is a macro** in `bd/ui/templates/_ui.html` (for example `company_entry`), not copy-pasted HTML.
6. Charts are SVG built in Python (`bd/charts.py`, `bd/timeline.py`); they use the same tokens (`var(--hotbg)` and so on), never their own palette
   (the timeline's categorical signal-type colours are the one deliberate exception).

Look: a coloured header band (`.hero`) carries each analysis page's title; white `.card`s; solid band `.badge`s; `.tiles` for big numbers;
`.entry` for ranked list items; `table.matrix` + `.cell` for company-by-signal grids. Admin has a darker header and a sidebar in the same system.

To change the look of the whole app: edit the tokens or one component in `design.css`. To add a page: build it from existing components.

## Where things are
- `bd/ui/app.py` routes and handlers; `bd/ui/templates/` pages; `design.css` the design system; `_ui.html` / `_cards.html` shared markup.
- `bd/week.py` the one Analysis page's data (top three new-business companies by score, the numbered Watch-and-above list, evidence, tracked); `bd/trend.py` weekly signals by kind, the takeaway sentence and score history; `bd/compare.py` the stores-nothing test of the combined sweep scan against what the separate scanners stored; `bd/insights.py` opportunity themes (rules over signal types, with evidence); `bd/feed.py` the signal feed (`/signals`); `bd/signal_coverage.py` how much
  of the list has been scanned (Admin Home); the viewer lookup is the capped (`$1`) `find` signal-scan job in `bd/jobs.py` + `/find` in `app.py`; `bd/profile.py` the company brief and its plain-text copy;
  `bd/market.py` the Market view data; `bd/charts.py` SVG charts.
  **PE portfolio**: `bd/pe_ownership.py` the ledger and status rules; `bd/sources.py` (bounded, polite reader: robots, per-host rate limit, retries, size cap, challenge-page detection),
  `bd/pe_listing.py` (portfolio page parser: sections, pagination, WordPress API fallback, coverage claims), `bd/pe_collect.py` (`bd pe-collect`, free, idempotent, failure-isolated, snapshots and diffs),
  `bd/pe_deals.py` (`bd pe-deals`: the model reports a deal with a quote, **code** verifies the page), `bd/pe_promote.py` (`bd pe-promote`: through the existing fit gate, no second target list),
  `bd/pe_sites.py` (a firm's website and portfolio page found and confirmed by code, never guessed), `bd/opportunity_model.py` (priority and action categories), `bd/pe_view.py` (data for `/firms`, `/firms/<id>`, `/firms/runs/<id>`, `/ownership/<id>`, `/portfolio/<id>` and the company brief). The UI process
  must not import the network or model modules (`tests/test_ui.py`).
- **Analysis is one page**, `/opportunities` (top three, "Is it heating up?" chart, numbered ranked list, tracked, Hiring at the bottom); the nav is Analysis and Admin. `/week`, `/leadership` and `/hiring` redirect to it; the Signals feed (`/signals`) is reached from it. The matrix, priority chart and card views were removed on purpose, so do not bring them back without asking. Reports live under Admin. `ADMIN_GROUPS` in `app.py` lists the Admin pages.
- A web search that errors (rate limit, unavailable) must never read as "found nothing": `bd/llm.py` records the codes (`search_failure`) and scanners, discovery and RFPs turn "nothing found and the tool failed" into an error, so the company is not marked scanned or taken for quiet.
- Cost levers (the user finds the weekly scan too expensive): the core scan is the combined `sweep` (on `ANTHROPIC_SCAN_MODEL`, Haiku, set in Railway) plus `hiring`; `tech_procurement` is the extra that runs where a signal shows and for any scanned company with no successful deep scan in `scan.deep_every_weeks` (4) weeks (`bd.runner.deep_due`; an errored run is not a look)
  (`CORE_SCANNERS` / `EXTRA_SCANNERS` in `bd/jobs.py`). Quiet companies wait `scan.quiet_every_weeks` (`bd/runner.py`); per-model pricing is in `bd/cost.py`. The switch rests on a four-company `bd compare` run (sweep $0.33 against $3.88; 13 found against 8 stored,
  two real misses): watch signal counts after the first weekly run. First live Update (10 companies): about $0.20 a company (sweep $0.99, technology extra $0.63, hiring $0.36 in total; web searches are most of the sweep's cost). The technology extra now also runs on the cheaper model (`SCAN_MODEL_SCANNERS` in `bd/scanners/__init__.py`), **untested against its Sonnet results until the user runs `bd compare --scanner tech_procurement`**; revert by removing it from that set. `scripts/weekly.sh` still runs the older `all` list.

## Working agreements
- Open pull requests as drafts and subscribe to their activity; the user merges. Never merge unless explicitly told to.
- Confirm with a question before a big change of direction; show rendered options (screenshots) for visual choices.
- Report outcomes plainly, including mistakes and what was not verified (for example "seen on demo data, not live data").
- New behaviour comes with tests; update the README when behaviour or structure changes.
