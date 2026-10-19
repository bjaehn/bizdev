"""Typer CLI."""

from __future__ import annotations

import os
from datetime import date, datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Optional

import typer

from bd.config import Config, Settings, load_config

app = typer.Typer(help="SEI Chicago BD Signal Agent", no_args_is_help=True)


def _fail(msg: str) -> None:
    typer.echo(msg, err=True)
    raise typer.Exit(code=1)


def _today() -> date:
    """Run dates are America/Chicago dates, whatever timezone the host or CI runner uses."""
    return datetime.now(ZoneInfo("America/Chicago")).date()


def _parse_date(s: Optional[str]) -> date:
    try:
        return date.fromisoformat(s) if s else _today()
    except ValueError:
        _fail(f"--as-of must be YYYY-MM-DD, got {s!r}")
        raise


SEC_SCANNERS = ("edgar", "earnings", "financials", "sec_events")       # need BD_USER_AGENT (the SEC requires a declared contact)


def _resolve_scanner_names(opt: str, settings: Settings) -> list[str]:
    """`all` = every implemented scanner, minus edgar when no BD_USER_AGENT is configured (the SEC
    requires a declared contact). Naming edgar explicitly without one is an error."""
    from bd.scanners import SCANNERS

    implemented = [n for n, c in SCANNERS.items() if c.implemented]
    if opt == "all":
        if not settings.user_agent_configured:
            skipped = [n for n in SEC_SCANNERS if n in implemented]
            for n in skipped:
                implemented.remove(n)
            if skipped:
                typer.echo(f"{', '.join(skipped)} scanner(s) skipped: set BD_USER_AGENT (company name and contact email), "
                           "as the SEC requires.", err=True)
        return implemented
    names = [n.strip() for n in opt.split(",") if n.strip()]
    bad = [n for n in names if n not in SCANNERS or not SCANNERS[n].implemented]
    if bad:
        _fail(f"unknown or unimplemented scanner(s): {bad}")
    if any(n in names for n in SEC_SCANNERS) and not settings.user_agent_configured:
        _fail("The edgar, earnings, financials and sec_events scanners need BD_USER_AGENT set to your company name and contact email (SEC fair access).")
    return names


def _build_scanners(names: list[str], cfg: Config, settings: Settings):
    """Real scanners with their dependencies. Needs ANTHROPIC_API_KEY and ANTHROPIC_MODEL."""
    from bd.ats import AtsClient
    from bd.edgar import EdgarClient
    from bd.llm import AnthropicWebSearchClient
    from bd.scanners import SCANNERS
    from bd.verify import UrlVerifier

    try:
        key, model = settings.require_llm()
    except RuntimeError as e:
        _fail(str(e))
        raise
    web = AnthropicWebSearchClient(cfg, model=model, api_key=key)
    text = AnthropicWebSearchClient(cfg, model=model, api_key=key, web_search=False)
    verifier = UrlVerifier(cfg, settings.user_agent)
    edgar = EdgarClient(cfg, settings.user_agent) if any(n in names for n in SEC_SCANNERS) else None
    ats = AtsClient(cfg, settings.user_agent) if "ats" in names else None
    return [SCANNERS[n].create(cfg, web, text, verifier, edgar, ats=ats) for n in names]


@app.command()
def seed(
    csv_path: str,
    exclusions: Optional[str] = typer.Option(None, help="Exclusions CSV: name,domain,reason"),
    overwrite: bool = typer.Option(False, "--overwrite", help="Let CSV values replace different values already in the database"),
) -> None:
    """Load accounts (and optionally exclusions) from CSV. The DATABASE is the master copy: for an account that
    already exists a CSV value only fills a fact that is still unknown, and differences are reported (use
    --overwrite to let the CSV win). Blank cells stay unknown; nothing is guessed."""
    from bd.accounts import seed_csv
    from bd.db import connect, init_db
    from bd.fit_gate import apply_fit_gate, seed_exclusions

    settings = Settings.from_env()
    conn = connect(settings.db_path)
    init_db(conn)
    if not Path(csv_path).exists():
        _fail(f"{csv_path} not found")
    res = seed_csv(conn, csv_path, overwrite=overwrite)
    if res.errors:
        _fail("Seed aborted, nothing written:\n" + "\n".join(res.errors))
    typer.echo(f"Seeded {settings.db_path}: {res.inserted} inserted, {res.updated} existing matched, "
               f"{res.filled} unknown fact(s) filled" + (f", {res.overwritten} overwritten" if res.overwritten else "") + ".")
    if res.conflicts:
        typer.echo(f"{len(res.conflicts)} CSV value(s) differ from the database; the database values were kept "
                   "(re-run with --overwrite to apply the CSV):", err=True)
        for c in res.conflicts[:10]:
            typer.echo(f"  {c}", err=True)
        if len(res.conflicts) > 10:
            typer.echo(f"  ... and {len(res.conflicts) - 10} more", err=True)
    if exclusions:
        if not Path(exclusions).exists():
            _fail(f"{exclusions} not found")
        n, errs = seed_exclusions(conn, exclusions)
        if errs:
            _fail("Exclusions aborted, nothing written:\n" + "\n".join(errs))
        typer.echo(f"Exclusions: {n} added.")
    _print_gate(apply_fit_gate(conn, load_config()))


def _print_gate(g) -> None:
    typer.echo(f"Fit gate: {len(g.passed) + len(g.unverified)} to scan ({len(g.unverified)} with location or size still unverified), "
               f"{len(g.excluded)} known out of scope.")
    for acc, res in g.unverified[:10]:
        typer.echo(f"  unverified: {acc.name}: {'; '.join(res.reasons)}")
    if len(g.unverified) > 10:
        typer.echo(f"  ... and {len(g.unverified) - 10} more unverified (scanned anyway; fill in chicagoland / revenue / employees / ownership when known)")
    for acc, res in g.excluded:
        typer.echo(f"  excluded: {acc.name}: {'; '.join(res.reasons)}")


@app.command()
def scan(
    tier: str = typer.Option("all", help="A, B or all"),
    only: Optional[str] = typer.Option(None, help="Only accounts whose name contains this text"),
    exact: bool = typer.Option(False, "--exact", help="With --only: the whole name must match (not just contain the text)"),
    limit: Optional[int] = typer.Option(None, help="Scan at most N accounts"),
    as_of: Optional[str] = typer.Option(None, help="Run date YYYY-MM-DD (default today)"),
    scanners: str = typer.Option("all", help="Comma list of scanner names, or all implemented"),
    workers: Optional[int] = typer.Option(None, help="Concurrent calls (default llm.concurrency)"),
    force: bool = typer.Option(False, help="Ignore tier cadence"),
    dry_run: bool = typer.Option(False, help="Apply the fit gate and show what would be scanned; no API calls"),
    light: bool = typer.Option(False, "--light", help="Daily refresh: do not mark accounts as scanned, so the weekly cadence is unaffected"),
    allow_empty: bool = typer.Option(False, "--allow-empty", help="Exit 0 (not an error) when no account is ready to scan"),
) -> None:
    """Fit-gate, then scan due accounts that passed (concurrently), logging tokens and cost."""
    from bd.accounts import list_accounts
    from bd.db import connect, init_db
    from bd.fit_gate import apply_fit_gate
    from bd.runner import is_due, run_scans
    from bd.scanners import SCANNERS

    if tier not in ("A", "B", "all"):
        _fail("--tier must be A, B or all")
    run_date = _parse_date(as_of)
    cfg, settings = load_config(), Settings.from_env()
    names = _resolve_scanner_names(scanners, settings)
    conn = connect(settings.db_path)
    init_db(conn)
    from bd.settings_store import apply_overrides
    cfg = apply_overrides(cfg, conn)
    _print_gate(apply_fit_gate(conn, cfg))
    accounts = list_accounts(conn, tier=tier)
    from bd.discover import select_scan_accounts
    accounts, held_back = select_scan_accounts(accounts, cfg)
    if held_back:
        typer.echo(f"{held_back} auto-added account(s) not yet reviewed are held back this run "
                   f"(limit {cfg.discovery.scan_unreviewed_per_run}); mark them reviewed in the app to scan them all.")
    if only:
        accounts = [a for a in accounts if (a.name.lower() == only.lower() if exact else only.lower() in a.name.lower())]
    if not accounts:
        if allow_empty:
            typer.echo(f"{only!r} is not on the scan list (it is not on your list, or it is known to be out of scope)."
                       if only else "No companies to scan: the list is empty or every company is known to be out of scope.")
            return
        _fail("No accounts to scan. Add companies with `bd seed accounts.csv`.")
    due = sorted((a for a in accounts if is_due(a, run_date, cfg, force)), key=lambda a: (a.last_scanned or "", a.id))
    if limit:
        due = due[:limit]                           # --limit counts accounts that are due; never-scanned ones go first
    probes = [SCANNERS[n].create(cfg, None, None, None, None) for n in names]
    n_calls = sum(p.applies_to(a) for a in due for p in probes)   # e.g. edgar only applies to accounts with a CIK
    typer.echo(f"{len(due)} of {len(accounts)} accounts due (tier {tier}) x {names} = {n_calls} calls")
    if dry_run:
        for a in due:
            typer.echo(f"  would scan: {a.name} (tier {a.tier})")
        return
    if not due:
        return
    objs = _build_scanners(names, cfg, settings)
    summary = run_scans(conn, cfg, objs, due, run_date, model=settings.anthropic_model, workers=workers,
                        force=force, touch_last_scanned=not light, log=typer.echo)
    typer.echo(f"\nPer account (tokens, est. cost):")
    for r in summary.accounts.values():
        cost = "unpriced" if r.est_cost is None else f"${r.est_cost:.4f}"
        typer.echo(f"  {r.account.name}: {r.tokens:,} tokens, {r.web_searches} searches, {cost}, "
                   f"{r.new_signals} new signals" + (f", {len(r.errors)} errors" if r.errors else ""))
    note = f" ({summary.unpriced_calls} calls unpriced: model not in config pricing)" if summary.unpriced_calls else ""
    typer.echo(f"Total: {summary.tokens:,} tokens, ${summary.est_cost:.4f} estimated{note}, "
               f"{summary.new_signals} new signals, {summary.errors} call errors.")
    if summary.budget_hit:
        typer.echo(f"BUDGET CAP HIT (${cfg.llm.max_cost_per_run_usd}): {summary.skipped_budget} calls not started. "
                   "Raise llm.max_cost_per_run_usd in config.yaml and re-run to finish.", err=True)
    if summary.calls and summary.errors == summary.calls:
        typer.echo("Every scan call failed (bad API key or model, no network?). Exiting with an error so a scheduled "
                   "run stops before scoring and posting.", err=True)
        raise typer.Exit(code=3)


@app.command()
def discover(
    segment: Optional[str] = typer.Option(None, help="Research this one segment instead of the configured list"),
    limit: Optional[int] = typer.Option(None, help="Research at most N segments"),
    as_of: Optional[str] = typer.Option(None, help="Run date YYYY-MM-DD (default today)"),
    workers: Optional[int] = typer.Option(None, help="Concurrent calls (default llm.concurrency)"),
    dry_run: bool = typer.Option(False, help="List what would be researched; no API calls"),
) -> None:
    """Market research: find Chicagoland companies by segment, research their facts, auto-add those that clear the
    fit gate (marked unreviewed, scanned only up to a per-run limit), and queue the rest for a person."""
    from bd.db import connect, init_db
    from bd.discover import Discoverer, run_discover
    from bd.edgar import EdgarClient
    from bd.enrich import Enricher
    from bd.llm import AnthropicWebSearchClient
    from bd.verify import UrlVerifier

    run_date = _parse_date(as_of)
    cfg, settings = load_config(), Settings.from_env()
    conn = connect(settings.db_path)
    init_db(conn)
    from bd.settings_store import apply_overrides
    cfg = apply_overrides(cfg, conn)          # limits and segments edited in the app
    segs = [segment] if segment else cfg.discovery.segments[: limit or None]
    typer.echo(f"{len(segs)} segment(s); up to {cfg.discovery.per_segment} companies each; research at most "
               f"{cfg.discovery.max_candidates_per_run} new companies, auto-add at most {cfg.discovery.max_auto_add_per_run}.")
    if dry_run:
        for s_ in segs:
            typer.echo(f"  would research: {s_}")
        return
    try:
        key, model = settings.require_llm()
    except RuntimeError as e:
        _fail(str(e))
        raise
    web = AnthropicWebSearchClient(cfg, model=model, api_key=key)
    verifier = UrlVerifier(cfg, settings.user_agent)
    edgar = EdgarClient(cfg, settings.user_agent) if settings.user_agent_configured else None
    summary = run_discover(conn, cfg, Discoverer(cfg, web, verifier), Enricher(cfg, web, verifier, edgar), run_date,
                           model=model, segments=segs, workers=workers, log=typer.echo)
    typer.echo(f"\nFound {summary.found} ({summary.skipped_known} already known, {summary.skipped_source} with an "
               f"unverifiable source); researched {summary.researched}: {len(summary.added)} auto-added, "
               f"{len(summary.queued)} queued for review, {len(summary.rejected)} rejected by the fit gate.")
    note = f" ({summary.unpriced_calls} calls unpriced)" if summary.unpriced_calls else ""
    typer.echo(f"Total: {summary.tokens:,} tokens, ${summary.est_cost:.4f} estimated{note}, {summary.errors} call errors.")
    if summary.added:
        typer.echo("Auto-added (unreviewed, tier B): " + ", ".join(summary.added))
    if summary.budget_hit:
        typer.echo(f"BUDGET CAP HIT (${cfg.llm.max_cost_per_run_usd}); some candidates were not researched.", err=True)
    if summary.segments and summary.errors >= summary.segments and not summary.researched:
        _fail("Every discovery call failed (bad API key or model, no network?).")


def _pe_deps(settings, cfg):
    from bd.edgar import EdgarClient
    from bd.enrich import Enricher
    from bd.llm import AnthropicWebSearchClient
    from bd.verify import UrlVerifier

    try:
        key, model = settings.require_llm()
    except RuntimeError as e:
        _fail(str(e))
        raise
    web = AnthropicWebSearchClient(cfg, model=model, api_key=key)
    verifier = UrlVerifier(cfg, settings.user_agent)
    edgar = EdgarClient(cfg, settings.user_agent) if settings.user_agent_configured else None
    return web, verifier, Enricher(cfg, web, verifier, edgar), model


@app.command(name="pe-sync")
def pe_sync() -> None:
    """Derive PE firms and portfolio links from accounts that have a sponsor (no API calls)."""
    from bd.db import connect, init_db
    from bd.pe_store import sync_from_accounts

    settings = Settings.from_env()
    conn = connect(settings.db_path)
    init_db(conn)
    r = sync_from_accounts(conn)
    n = conn.execute("SELECT COUNT(*) FROM pe_firms").fetchone()[0]
    typer.echo(f"{n} PE firm(s) known: {r.firms_added} added, {r.links_added} portfolio link(s) added, {r.links_exited} marked exited.")


@app.command(name="pe-portfolio")
def pe_portfolio(
    firm: Optional[str] = typer.Option(None, help="Only firms whose name contains this text"),
    limit: Optional[int] = typer.Option(None, help="At most N firms (least recently researched first)"),
    as_of: Optional[str] = typer.Option(None, help="Run date YYYY-MM-DD (default today)"),
    workers: Optional[int] = typer.Option(None, help="Concurrent calls (default llm.concurrency)"),
    dry_run: bool = typer.Option(False, help="List the firms that would be researched; no API calls"),
) -> None:
    """Find each PE firm's Chicagoland portfolio companies from its own site; research and auto-add the new ones."""
    from bd.db import connect, init_db
    from bd.pe_firms import PortfolioFinder, run_portfolio, select_firms
    from bd.pe_store import sync_from_accounts
    from bd.settings_store import apply_overrides

    run_date = _parse_date(as_of)
    cfg, settings = load_config(), Settings.from_env()
    conn = connect(settings.db_path)
    init_db(conn)
    cfg = apply_overrides(cfg, conn)
    sync_from_accounts(conn)
    firms = select_firms(conn, firm, limit or cfg.pe_firms.max_firms_per_run, "last_portfolio")
    typer.echo(f"{len(firms)} PE firm(s) to research.")
    if dry_run or not firms:
        for f in firms:
            typer.echo(f"  would research: {f['name']}")
        return
    web, verifier, enricher, model = _pe_deps(settings, cfg)
    s = run_portfolio(conn, cfg, PortfolioFinder(cfg, web, verifier), enricher, firms, run_date, model, workers, log=typer.echo)
    typer.echo(f"\nFound {s.found} portfolio companies ({s.skipped_known} already known, {s.skipped_source} with an unverifiable "
               f"source); researched {s.researched}: {len(s.added)} auto-added, {len(s.queued)} queued, {len(s.rejected)} rejected.")
    typer.echo(f"Total: {s.tokens:,} tokens, ${s.est_cost:.4f} estimated, {s.errors} call errors.")
    sync_from_accounts(conn)
    if s.segments and s.errors >= s.segments and not s.found:
        _fail("Every portfolio call failed (bad API key or model, no network?).")


@app.command(name="pe-signals")
def pe_signals(
    firm: Optional[str] = typer.Option(None, help="Only firms whose name contains this text"),
    limit: Optional[int] = typer.Option(None, help="At most N firms (least recently scanned first)"),
    as_of: Optional[str] = typer.Option(None, help="Run date YYYY-MM-DD (default today)"),
    workers: Optional[int] = typer.Option(None, help="Concurrent calls (default llm.concurrency)"),
    dry_run: bool = typer.Option(False, help="List the firms that would be scanned; no API calls"),
) -> None:
    """Look for firm-level signals (new fund, exit, operating-partner hire) that lift its portfolio companies' scores."""
    from bd.db import connect, init_db
    from bd.pe_firms import FirmScanner, run_firm_scans, select_firms
    from bd.pe_store import sync_from_accounts
    from bd.settings_store import apply_overrides

    run_date = _parse_date(as_of)
    cfg, settings = load_config(), Settings.from_env()
    conn = connect(settings.db_path)
    init_db(conn)
    cfg = apply_overrides(cfg, conn)
    sync_from_accounts(conn)
    firms = select_firms(conn, firm, limit or cfg.pe_firms.max_firms_per_run)
    typer.echo(f"{len(firms)} PE firm(s) to scan.")
    if dry_run or not firms:
        for f in firms:
            typer.echo(f"  would scan: {f['name']}")
        return
    web, verifier, _enricher, model = _pe_deps(settings, cfg)
    s = run_firm_scans(conn, cfg, FirmScanner(cfg, web, verifier), firms, run_date, model, workers, log=typer.echo)
    typer.echo(f"Total: {s.tokens:,} tokens, ${s.est_cost:.4f} estimated, {s.new_signals} new firm signals, {s.errors} call errors.")
    if s.firms and s.errors == s.firms:
        typer.echo("Every firm call failed (bad API key or model, no network?).", err=True)
        raise typer.Exit(code=3)


@app.command()
def ask(
    id: int = typer.Option(..., "--id", help="Id of a stored question (the Ask page creates it)"),
    as_of: Optional[str] = typer.Option(None, help="Today's date YYYY-MM-DD (default today)"),
) -> None:
    """Answer one plain-English question about your own data: the model writes a read-only query, code runs it, the model phrases the
    answer from the rows. Two small text calls; no web access."""
    from bd.ask import run_ask
    from bd.db import connect, init_db
    from bd.llm import AnthropicWebSearchClient

    run_date = _parse_date(as_of)
    cfg, settings = load_config(), Settings.from_env()
    conn = connect(settings.db_path)
    init_db(conn)
    try:
        key, model = settings.require_llm()
    except RuntimeError as e:
        _fail(str(e))
        raise
    llm = AnthropicWebSearchClient(cfg, model=model, api_key=key, web_search=False)
    o = run_ask(conn, settings.db_path, cfg, llm, model, id, run_date, max_cost=cfg.llm.max_cost_per_run_usd)
    if o is None:
        _fail(f"No pending question with id {id}.")
        raise typer.Exit(code=2)
    cost = f"${o.cost:.4f}" if o.cost is not None else "unpriced"
    if o.status != "done":
        typer.echo(f"Could not answer: {o.error} ({cost})", err=True)
        raise typer.Exit(code=3)
    typer.echo(f"Answered ({len(o.rows or [])} row(s), {cost}).")


@app.command()
def rfps(
    limit: Optional[int] = typer.Option(None, help="At most N agency groups"),
    as_of: Optional[str] = typer.Option(None, help="Run date YYYY-MM-DD (default today)"),
    allow_empty: bool = typer.Option(False, "--allow-empty", help="Exit 0 even when every call failed"),
    dry_run: bool = typer.Option(False, help="List the agency groups that would be searched; no API calls"),
) -> None:
    """Find open public RFPs from Chicago-area agencies (not tied to a company; shown on the Open RFPs page)."""
    from bd.db import connect, init_db
    from bd.rfps import RfpFinder, run_rfps

    run_date = _parse_date(as_of)
    cfg, settings = load_config(), Settings.from_env()
    conn = connect(settings.db_path)
    init_db(conn)
    groups = cfg.rfps.groups[:limit] if limit else cfg.rfps.groups
    typer.echo(f"{len(groups)} agency group(s) to search.")
    if dry_run:
        for g in groups:
            typer.echo(f"  would search: {g.name} ({', '.join(g.domains)})")
        return
    web, verifier, _enricher, model = _pe_deps(settings, cfg)
    s = run_rfps(conn, cfg, RfpFinder(cfg, web, verifier), run_date, model, groups, log=typer.echo)
    typer.echo(f"Total: {s.tokens:,} tokens, ${s.est_cost:.4f} estimated, {s.new} new RFPs ({s.known} already known), {s.errors} call errors.")
    if s.budget_hit:
        typer.echo("Spending limit reached: some agency groups were not searched.", err=True)
    if s.groups and s.errors == s.groups and not allow_empty:
        typer.echo("Every agency call failed (bad API key or model, no network?).", err=True)
        raise typer.Exit(code=3)


@app.command()
def ui(
    port: int = typer.Option(8765, help="Port on 127.0.0.1 (0 = pick a free one)"),
    no_open: bool = typer.Option(False, "--no-open", help="Do not open a browser; just print the link"),
) -> None:
    """Open the local review-and-curate UI (leads, accounts, enrich review, exclusions, runs).

    It listens on 127.0.0.1 only, and cannot scan, score, call a model or post to Teams."""
    try:
        from bd.ui.server import serve
    except ImportError as e:
        _fail(f"The UI needs Jinja2 ({e.name}). Install it with: pip install -e '.[ui]'")
        raise
    from bd.db import connect, init_db

    settings = Settings.from_env()
    init_db(connect(settings.db_path))
    try:
        serve(load_config(), settings.db_path, port=port, open_browser=not no_open)
    except OSError as e:
        _fail(f"Could not start the UI on 127.0.0.1:{port}: {e}")


@app.command()
def serve(
    port: Optional[int] = typer.Option(None, help="Port (default: $PORT, else 8080)"),
    host: str = typer.Option("0.0.0.0", help="Interface to listen on"),
) -> None:
    """Run the hosted app: password login, review UI, Operate page, and the Monday 05:00 Chicago run.

    Needs BD_UI_PASSWORD (16+ characters); refuses to start without it. Data lives in $BD_DATA_DIR (default /data)."""
    import os

    try:
        from bd.ui.auth import AuthConfigError
        from bd.ui.server import build_hosted
    except ImportError as e:
        _fail(f"The hosted app needs Jinja2 ({e.name}). Install it with: pip install '.[ui]'")
        raise
    from bd.db import connect, init_db

    from bd.ui.auth import Hosted

    try:
        Hosted(os.environ.get("BD_UI_PASSWORD", ""))    # fail closed before touching any data
    except AuthConfigError as e:
        _fail(str(e))
        raise
    data_dir = Path(os.environ.get("BD_DATA_DIR", "/data"))
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        _fail(f"Cannot use BD_DATA_DIR {data_dir}: {e}")
    db_path = str(data_dir / "bd.sqlite")
    os.environ["BD_DB_PATH"] = db_path                 # child jobs and Settings.from_env() use the same file
    conn = connect(db_path)
    init_db(conn)
    _first_boot_seed(conn)
    conn.close()
    the_port = port if port is not None else int(os.environ.get("PORT", "8080"))
    try:
        httpd, _app, scheduler = build_hosted(load_config(), db_path, str(data_dir),
                                              os.environ.get("BD_UI_PASSWORD", ""), the_port, host)
    except AuthConfigError as e:
        _fail(str(e))
        raise
    except OSError as e:
        _fail(f"Could not listen on {host}:{the_port}: {e}")
        raise
    scheduler.start()
    typer.echo(f"BD Signals listening on {host}:{httpd.server_port}; data in {data_dir}; "
               "weekly run Mondays 05:00 America/Chicago.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        typer.echo("Stopped.")
    finally:
        scheduler.stop()
        httpd.server_close()


def _first_boot_seed(conn) -> None:
    """On a brand-new volume, load the reviewed account/exclusion lists shipped with the image. After that the
    database is the master copy and is never reseeded automatically."""
    import os

    from bd.accounts import list_accounts, seed_csv
    from bd.fit_gate import apply_fit_gate, seed_exclusions

    if list_accounts(conn, status=None):
        return
    base = Path(os.environ.get("BD_CONFIG", "config.yaml")).resolve().parent
    if (base / "accounts.csv").exists():
        res = seed_csv(conn, base / "accounts.csv")
        typer.echo(f"First boot: seeded {res.inserted} account(s) from accounts.csv." if not res.errors
                   else "First boot: accounts.csv had errors; nothing seeded: " + "; ".join(res.errors[:3]), err=bool(res.errors))
    if (base / "exclusions.csv").exists():
        n, errs = seed_exclusions(conn, base / "exclusions.csv")
        if errs:
            typer.echo("First boot: exclusions.csv had errors; nothing added: " + "; ".join(errs[:3]), err=True)
    apply_fit_gate(conn, load_config())


@app.command()
def export(
    accounts: str = typer.Option("accounts.csv", help="Where to write accounts"),
    exclusions: str = typer.Option("exclusions.csv", help="Where to write exclusions"),
) -> None:
    """Write the database's accounts and exclusions to CSV (for git or backup). Overwrites those files."""
    from bd.accounts import export_accounts_csv, export_exclusions_csv
    from bd.db import connect, init_db

    settings = Settings.from_env()
    conn = connect(settings.db_path)
    init_db(conn)
    n = export_accounts_csv(conn, accounts)
    m = export_exclusions_csv(conn, exclusions)
    typer.echo(f"Wrote {n} account(s) to {accounts} and {m} exclusion(s) to {exclusions}. Review with `git diff`.")


@app.command()
def enrich(
    out: str = typer.Option("enrich_proposals.csv", help="Where to write the review CSV"),
    all_accounts: bool = typer.Option(False, "--all", help="Also re-check accounts that already pass the fit gate"),
    only: Optional[str] = typer.Option(None, help="Only accounts whose name contains this text"),
    exact: bool = typer.Option(False, "--exact", help="With --only: the whole name must match (not just contain the text)"),
    limit: Optional[int] = typer.Option(None, help="At most N accounts"),
    as_of: Optional[str] = typer.Option(None, help="Run date YYYY-MM-DD (default today)"),
    workers: Optional[int] = typer.Option(None, help="Concurrent calls (default llm.concurrency)"),
    allow_empty: bool = typer.Option(False, "--allow-empty", help="Exit 0 (not an error) when no account needs facts"),
) -> None:
    """PROPOSE missing account facts (domain, Chicagoland, size, ownership, CIK) with evidence URLs.

    Writes a CSV for you to review and edit, then load with `bd seed`. Never writes to the database."""
    from bd.db import connect, init_db
    from bd.edgar import EdgarClient
    from bd.enrich import Enricher, run_enrich, select_accounts, write_proposals
    from bd.llm import AnthropicWebSearchClient
    from bd.verify import UrlVerifier

    run_date = _parse_date(as_of)
    cfg, settings = load_config(), Settings.from_env()
    conn = connect(settings.db_path)
    init_db(conn)
    accounts = select_accounts(conn, all_accounts, only, limit, exact, cfg)
    if not accounts:
        if allow_empty:
            typer.echo("No companies have unverified facts right now.")
            return
        _fail("No accounts to enrich (companies with unverified facts by default; use --all, or run `bd seed accounts.csv` first).")
    try:
        key, model = settings.require_llm()
    except RuntimeError as e:
        _fail(str(e))
        raise
    edgar = EdgarClient(cfg, settings.user_agent) if settings.user_agent_configured else None
    if edgar is None:
        typer.echo("Note: BD_USER_AGENT is not set, so proposed CIKs cannot be confirmed with EDGAR and will be left out.",
                   err=True)
    enricher = Enricher(cfg, AnthropicWebSearchClient(cfg, model=model, api_key=key),
                        UrlVerifier(cfg, settings.user_agent), edgar)
    from bd.sec_fill import cik_cleared
    enricher.no_lookup = cik_cleared(conn)
    typer.echo(f"Enriching {len(accounts)} account(s) (proposals only; nothing is written to the database)")
    results, spent, hit = run_enrich(conn, cfg, enricher, accounts, run_date, settings.anthropic_model,
                                     workers=workers, log=typer.echo)
    n = write_proposals(results, out)
    typer.echo(f"\nWrote {out}: {len(results)} row(s), {n} with proposals. Estimated cost ${spent:.4f}.")
    typer.echo("Review it (each fact lists its evidence URL; `notes` says what was rejected and why), "
               f"then load it with: bd seed {out}")
    if hit:
        typer.echo(f"BUDGET CAP HIT (${cfg.llm.max_cost_per_run_usd}): some accounts were not enriched. "
                   "Raise llm.max_cost_per_run_usd and re-run.", err=True)


@app.command(name="sec-fill")
def sec_fill(
    only: Optional[str] = typer.Option(None, help="Only accounts whose name contains this text"),
    exact: bool = typer.Option(False, "--exact", help="With --only: the whole name must match (not just contain the text)"),
    limit: Optional[int] = typer.Option(None, help="Read at most N SEC records (default sec_fill.max_accounts_per_run)"),
    as_of: Optional[str] = typer.Option(None, help="Run date YYYY-MM-DD (default today)"),
) -> None:
    """Fill size, public ownership, headquarters and a missing CIK from the SEC's own data. No model, no cost.

    A company with a CIK gets its revenue, ownership and city from the SEC's company files; a listed company with no CIK is matched
    to the SEC's list by exact name. Never overwrites a stored value; every fact is logged with the SEC page it came from."""
    from bd.db import connect, init_db
    from bd.edgar import EdgarClient
    from bd.sec_fill import run_sec_fill

    run_date = _parse_date(as_of)
    cfg, settings = load_config(), Settings.from_env()
    if not settings.user_agent_configured:
        typer.echo("Skipped: BD_USER_AGENT is not set (the SEC requires a declared contact, e.g. YourCo BD Research (you@yourco.com)).")
        return
    conn = connect(settings.db_path)
    init_db(conn)
    s = run_sec_fill(conn, cfg, EdgarClient(cfg, settings.user_agent), run_date, limit=limit, only=only, exact=exact, log=typer.echo)
    if not s.candidates:
        typer.echo("Nothing for the SEC to fill: every company already has its size, ownership and headquarters, or is not a filer.")
        return
    facts = sum(len(f.facts) for f in s.filled)
    typer.echo(f"\nSEC fill: read {s.read} SEC record(s); filled {facts} fact(s) for {len(s.filled)} company(ies), "
               f"{s.passed_gate} now ready for scanning; {len(s.unmatched)} with no unique SEC match"
               + (f"; {s.not_reached} left for the next run" if s.not_reached else "")
               + (f"; {s.errors} read problem(s)" if s.errors else "") + ". No cost.")
    if s.unmatched:
        shown = ", ".join(f"{n} [{why}]" for n, why in s.unmatched[:12])
        typer.echo(f"No unique SEC match (left to the web search or to you): {shown}" + (f" and {len(s.unmatched) - 12} more" if len(s.unmatched) > 12 else ""))


@app.command(name="apply-proposals")
def apply_proposals(file: str = typer.Argument("enrich_proposals.csv", help="A proposals CSV written by `bd enrich`")) -> None:
    """Apply every NEW fact in a proposals file (facts the database does not have yet). Never overwrites a stored value and
    never touches companies that are not accounts; each fact is logged with its source URL. A missing file is not an error."""
    from bd.curate import apply_all_new
    from bd.db import connect, init_db

    cfg, settings = load_config(), Settings.from_env()
    conn = connect(settings.db_path)
    init_db(conn)
    if not Path(file).is_file():
        typer.echo(f"{file} not found: nothing to apply.")
        return
    r = apply_all_new(conn, cfg, file)
    typer.echo(f"Applied {r['facts']} new fact(s) to {r['accounts']} account(s). Left alone: {r['conflicts_skipped']} that differ from "
               f"what is stored, {r['not_in_database']} for companies not in the database"
               + (f"; {r['failed']} failed validation" if r["failed"] else "") + ".")


@app.command()
def check() -> None:
    """Test the model connection with one tiny call (about $0.0001) and say in plain words what to fix."""
    from bd.llm import AnthropicWebSearchClient, LLMError
    from bd.setup_check import explain_error, static_problems

    cfg, settings = load_config(), Settings.from_env()
    problems = static_problems(cfg, os.environ)
    for p_ in problems:
        typer.echo(f"- {p_}")
    if not (settings.anthropic_api_key and settings.anthropic_model):
        _fail("Not ready: set ANTHROPIC_API_KEY and ANTHROPIC_MODEL in the hosting settings.")
    client = AnthropicWebSearchClient(cfg, model=settings.anthropic_model, api_key=settings.anthropic_api_key, web_search=False)
    try:
        client.run("Reply with the single word OK.", [{"role": "user", "content": "ping"}])
    except LLMError as e:
        _fail("The model connection failed: " + explain_error(str(e)))
    from bd.verify import UrlVerifier
    verifier = UrlVerifier(cfg, settings.user_agent)
    try:
        fetched = verifier.verify("https://www.sec.gov/")
    finally:
        verifier.close()
    if not fetched.ok:
        _fail(f"The model works, but fetching web pages fails ({fetched.reason}). Every source URL would be rejected, so no "
              "facts could be added. If the reason mentions the header, re-save BD_USER_AGENT as one plain line, e.g. "
              "YourCo BD Research (you@yourco.com).")
    typer.echo("Connection OK: the key, the model, the workspace settings and web fetching work.")


@app.command()
def score(
    as_of: Optional[str] = typer.Option(None, help="Score date YYYY-MM-DD (default today)"),
    allow_empty: bool = typer.Option(False, "--allow-empty", help="Exit 0 (not an error) when no account is ready to score"),
) -> None:
    """Score every fit-gated (active) account from its stored signals. Deterministic; no API calls."""
    from bd.db import connect, init_db
    from bd.score_store import score_accounts

    run_date = _parse_date(as_of)
    cfg, settings = load_config(), Settings.from_env()
    conn = connect(settings.db_path)
    init_db(conn)
    from bd.pe_store import sync_from_accounts
    sync_from_accounts(conn)                      # so a sponsor's activity reaches its portfolio companies
    scored = score_accounts(conn, cfg, run_date)
    if not scored:
        if allow_empty:
            typer.echo("No companies are ready to score yet.")
            return
        _fail("No active accounts to score. Seed accounts and fill in their fit-gate facts first.")
    counts: dict[str, int] = {}
    for sa in scored:
        counts[sa.result.band] = counts.get(sa.result.band, 0) + 1
    typer.echo(f"Scored {len(scored)} accounts as of {run_date}: "
               + ", ".join(f"{b} {counts.get(b, 0)}" for b in ("Hot", "Warm", "Watch", "Stored")))
    for sa in sorted(scored, key=lambda s: (-s.result.score, s.account.id))[:15]:
        if sa.result.score:
            typer.echo(f"  {sa.result.score:>3} {sa.result.band:<6} {sa.account.name}: {sa.result.rationale}")


def _shortlist_or_fail(conn, cfg, run_date):
    from bd.shortlist import ShortlistError, build_shortlist

    try:
        return build_shortlist(conn, cfg, run_date)
    except ShortlistError as e:
        _fail(str(e))
        raise


@app.command()
def shortlist(
    as_of: Optional[str] = typer.Option(None, help="Run date YYYY-MM-DD (default today)"),
) -> None:
    """Preview who would be posted (read-only: nothing is marked posted, no API calls)."""
    from bd.db import connect, init_db

    run_date = _parse_date(as_of)
    cfg, settings = load_config(), Settings.from_env()
    conn = connect(settings.db_path)
    init_db(conn)
    sl = _shortlist_or_fail(conn, cfg, run_date)
    typer.echo(f"Shortlist for {run_date}: {len(sl.leads)} lead(s)"
               + (f" (+{len(sl.overflow)} over the cap of {cfg.post_gate.max_leads_per_post})" if sl.overflow else ""))
    for lead in sl.leads:
        typer.echo(f"  {lead.result.score:>3} {lead.result.band:<5} {lead.account.name}: {lead.result.rationale}")
    tracked = [s for s in sl.skipped if s.band in ("Watch", "Hot", "Warm")]
    if tracked:
        typer.echo("Tracked, not posted:")
        for s in sorted(tracked, key=lambda s: -s.score):
            typer.echo(f"  {s.score:>3} {s.band:<5} {s.account.name}: {s.reason}")


@app.command()
def card(
    as_of: Optional[str] = typer.Option(None, help="Run date YYYY-MM-DD (default today)"),
    out: str = typer.Option("card.json", help="Where to write the Adaptive Card"),
    dry_run: bool = typer.Option(False, help="Never post, even if TEAMS_WEBHOOK_URL is set"),
    no_llm: bool = typer.Option(False, help="Skip the briefer: use plain source-summary fallbacks"),
    rebrief: bool = typer.Option(False, help="Regenerate briefs instead of using the cached ones"),
) -> None:
    """Brief the shortlist, write card.json, and post to Teams ONLY if TEAMS_WEBHOOK_URL is set
    (default is a dry run). Leads are marked posted, starting their cooldown, only after a successful post."""
    import json

    from bd.briefer import Briefer, get_briefs
    from bd.card import CardTooLarge, build_card, post_card, validate_card
    from bd.db import connect, init_db
    from bd.shortlist import mark_posted

    run_date = _parse_date(as_of)
    cfg, settings = load_config(), Settings.from_env()
    conn = connect(settings.db_path)
    init_db(conn)
    sl = _shortlist_or_fail(conn, cfg, run_date)

    llm = None
    if sl.leads and not no_llm:
        from bd.llm import AnthropicWebSearchClient
        try:
            key, model = settings.require_llm()
        except RuntimeError as e:
            _fail(f"{e}\n(or pass --no-llm to use plain source-summary lines)")
            raise
        llm = AnthropicWebSearchClient(cfg, model=model, api_key=key, web_search=False)
    briefs = get_briefs(conn, cfg, sl.leads, run_date, Briefer(cfg, llm), settings.anthropic_model, rebrief=rebrief) if sl.leads else {}

    try:
        card_json = build_card(sl.leads, briefs, run_date, cfg, overflow=len(sl.overflow))
    except CardTooLarge as e:
        _fail(str(e))
        raise
    allowed = {s.url for lead in sl.leads for s in lead.signals}
    problems = validate_card(card_json, cfg, allowed_urls=allowed)
    if problems:
        _fail("Card failed validation, not written:\n" + "\n".join(problems))
    Path(out).write_text(json.dumps(card_json, indent=2, ensure_ascii=False))
    n_fb = sum(b.source == "fallback" for b in briefs.values())
    typer.echo(f"Wrote {out}: {len(sl.leads)} lead(s), {len(json.dumps(card_json).encode())} bytes"
               + (f", {n_fb} plain fallback why-now line(s)" if n_fb else ""))
    for lead in sl.leads:
        b = briefs[lead.account.id]
        typer.echo(f"  {lead.result.score:>3} {lead.result.band:<5} {lead.account.name} [{b.best_service_line or '-'}] "
                   f"({b.source}): {b.why_now}")

    if dry_run:
        typer.echo("DRY RUN (--dry-run): not posted; nothing marked as posted.")
    elif not settings.teams_webhook_url:
        typer.echo("DRY RUN: TEAMS_WEBHOOK_URL is not set, so nothing was posted and nothing is marked as posted.")
    else:
        res = post_card(settings.teams_webhook_url, card_json)
        if not res.ok:
            _fail(f"Teams post failed ({res.detail}). Nothing marked as posted.")
        mark_posted(conn, sl.leads, run_date, cfg)
        typer.echo(f"Posted to Teams ({res.detail}); {len(sl.leads)} lead(s) marked posted, "
                   f"{cfg.post_gate.cooldown_weeks}-week cooldown started.")


@app.command()
def evaluate(
    gold: str = typer.Option("tests/gold.csv", help="Verified gold file"),
    as_of: Optional[str] = typer.Option(None, help="Evaluate as of YYYY-MM-DD (default today)"),
    tolerance_days: int = typer.Option(3, help="Allowed |event_date - expected_date| in days"),
    limit: Optional[int] = typer.Option(None, help="Evaluate only the first N accounts"),
    out: str = typer.Option("eval_report.json", help="Where to write the JSON report"),
    scanners: str = typer.Option("all", help="Comma list of scanner names, or all implemented"),
) -> None:
    """Run scanners over the gold file and report found/missed/wrong date/bad URLs."""
    from bd.evaluate import GoldError, combined_scan, evaluate as run_eval, load_gold

    try:
        rows = load_gold(gold)
    except GoldError as e:
        _fail(str(e))
        raise
    run_date = _parse_date(as_of)
    cfg, settings = load_config(), Settings.from_env()
    names = _resolve_scanner_names(scanners, settings)
    scan_fn = combined_scan(_build_scanners(names, cfg, settings))
    stale = [g for g in rows if g.expected_date and (run_date - g.expected_date).days > cfg.scan.window_days]
    if stale:
        typer.echo(f"WARNING: {len(stale)} gold rows are older than the {cfg.scan.window_days}-day window as of "
                   f"{run_date}; they cannot be found. Pass --as-of closer to their dates.", err=True)
    report = run_eval(rows, scan_fn, run_date, tolerance_days=tolerance_days, limit=limit)
    Path(out).write_text(report.to_json())
    typer.echo(report.render())
    typer.echo(f"\nFull report: {out}")


if __name__ == "__main__":
    app()
