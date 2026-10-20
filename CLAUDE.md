# SEI Chicago BD Signal Agent: project memory

Each week this finds Chicagoland companies showing demand for SEI-type management consulting and ranks them. **The product is signals**:
leadership changes, hiring, press and media, company events and technology. Company facts (SEC figures, size, ownership) are background and never
gate a scan. Claude extracts signals from public sources; **code scores** them and **code names the theme** they add up to, deterministically. A server-rendered web app (stdlib WSGI + Jinja2, SQLite) lets people read the
ranking and lets an admin run updates. Deployed on Railway from `main`.

## Commands
- Tests: `.venv/bin/python -m pytest -q` (must stay green; about 1,020 tests, about 50 seconds).
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
- **Analysis is one page**, `/opportunities` (top three, "Is it heating up?" chart, numbered ranked list, tracked, Hiring at the bottom); the nav is Analysis and Admin. `/week`, `/leadership` and `/hiring` redirect to it; the Signals feed (`/signals`) is reached from it. The matrix, priority chart and card views were removed on purpose, so do not bring them back without asking. Reports live under Admin. `ADMIN_GROUPS` in `app.py` lists the Admin pages.
- A web search that errors (rate limit, unavailable) must never read as "found nothing": `bd/llm.py` records the codes (`search_failure`) and scanners, discovery and RFPs turn "nothing found and the tool failed" into an error, so the company is not marked scanned or taken for quiet.
- Cost levers (the user finds the weekly scan too expensive): the core scan is the combined `sweep` (on `ANTHROPIC_SCAN_MODEL`, Haiku, set in Railway) plus `hiring`; `tech_procurement` is the extra that runs only where a signal shows
  (`CORE_SCANNERS` / `EXTRA_SCANNERS` in `bd/jobs.py`). Quiet companies wait `scan.quiet_every_weeks` (`bd/runner.py`); per-model pricing is in `bd/cost.py`. The switch rests on a four-company `bd compare` run (sweep $0.33 against $3.88; 13 found against 8 stored,
  two real misses): watch signal counts after the first weekly run. First live Update (10 companies): about $0.20 a company (sweep $0.99, technology extra $0.63, hiring $0.36 in total; web searches are most of the sweep's cost). The technology extra now also runs on the cheaper model (`SCAN_MODEL_SCANNERS` in `bd/scanners/__init__.py`), **untested against its Sonnet results until the user runs `bd compare --scanner tech_procurement`**; revert by removing it from that set. `scripts/weekly.sh` still runs the older `all` list.

## Working agreements
- Open pull requests as drafts and subscribe to their activity; the user merges. Never merge unless explicitly told to.
- Confirm with a question before a big change of direction; show rendered options (screenshots) for visual choices.
- Report outcomes plainly, including mistakes and what was not verified (for example "seen on demo data, not live data").
- New behaviour comes with tests; update the README when behaviour or structure changes.
