"""Typer CLI."""

from __future__ import annotations

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


def _resolve_scanner_names(opt: str, settings: Settings) -> list[str]:
    """`all` = every implemented scanner, minus edgar when no BD_USER_AGENT is configured (the SEC
    requires a declared contact). Naming edgar explicitly without one is an error."""
    from bd.scanners import SCANNERS

    implemented = [n for n, c in SCANNERS.items() if c.implemented]
    if opt == "all":
        if "edgar" in implemented and not settings.user_agent_configured:
            typer.echo("edgar scanner skipped: set BD_USER_AGENT (company name and contact email), as the SEC requires.",
                       err=True)
            implemented.remove("edgar")
        return implemented
    names = [n.strip() for n in opt.split(",") if n.strip()]
    bad = [n for n in names if n not in SCANNERS or not SCANNERS[n].implemented]
    if bad:
        _fail(f"unknown or unimplemented scanner(s): {bad}")
    if "edgar" in names and not settings.user_agent_configured:
        _fail("The edgar scanner needs BD_USER_AGENT set to your company name and contact email (SEC fair access).")
    return names


def _build_scanners(names: list[str], cfg: Config, settings: Settings):
    """Real scanners with their dependencies. Needs ANTHROPIC_API_KEY and ANTHROPIC_MODEL."""
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
    edgar = EdgarClient(cfg, settings.user_agent) if "edgar" in names else None
    return [SCANNERS[n].create(cfg, web, text, verifier, edgar) for n in names]


@app.command()
def seed(
    csv_path: str,
    exclusions: Optional[str] = typer.Option(None, help="Exclusions CSV: name,domain,reason"),
) -> None:
    """Load accounts (and optionally exclusions) from CSV. Blank cells stay unknown; nothing is guessed."""
    from bd.accounts import seed_csv
    from bd.db import connect, init_db
    from bd.fit_gate import apply_fit_gate, seed_exclusions

    settings = Settings.from_env()
    conn = connect(settings.db_path)
    init_db(conn)
    if not Path(csv_path).exists():
        _fail(f"{csv_path} not found")
    res = seed_csv(conn, csv_path)
    if res.errors:
        _fail("Seed aborted, nothing written:\n" + "\n".join(res.errors))
    typer.echo(f"Seeded {settings.db_path}: {res.inserted} inserted, {res.updated} updated.")
    if exclusions:
        if not Path(exclusions).exists():
            _fail(f"{exclusions} not found")
        n, errs = seed_exclusions(conn, exclusions)
        if errs:
            _fail("Exclusions aborted, nothing written:\n" + "\n".join(errs))
        typer.echo(f"Exclusions: {n} added.")
    _print_gate(apply_fit_gate(conn, load_config()))


def _print_gate(g) -> None:
    typer.echo(f"Fit gate: {len(g.passed)} pass, {len(g.held)} held for review, {len(g.excluded)} excluded.")
    for acc, res in g.held[:10]:
        typer.echo(f"  held: {acc.name}: {'; '.join(res.reasons)}")
    if len(g.held) > 10:
        typer.echo(f"  ... and {len(g.held) - 10} more held (fill in chicagoland / revenue / employees / ownership)")
    for acc, res in g.excluded:
        typer.echo(f"  excluded: {acc.name}: {'; '.join(res.reasons)}")


@app.command()
def scan(
    tier: str = typer.Option("all", help="A, B or all"),
    limit: Optional[int] = typer.Option(None, help="Scan at most N accounts"),
    as_of: Optional[str] = typer.Option(None, help="Run date YYYY-MM-DD (default today)"),
    scanners: str = typer.Option("all", help="Comma list of scanner names, or all implemented"),
    workers: Optional[int] = typer.Option(None, help="Concurrent calls (default llm.concurrency)"),
    force: bool = typer.Option(False, help="Ignore tier cadence"),
    dry_run: bool = typer.Option(False, help="Apply the fit gate and show what would be scanned; no API calls"),
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
    _print_gate(apply_fit_gate(conn, cfg))
    accounts = list_accounts(conn, tier=tier, limit=limit)
    if not accounts:
        _fail("No accounts passed the fit gate. Fill in the held accounts' facts or run `bd seed accounts.csv`.")
    due = [a for a in accounts if is_due(a, run_date, cfg, force)]
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
    summary = run_scans(conn, cfg, objs, accounts, run_date, model=settings.anthropic_model, workers=workers,
                        force=force, log=typer.echo)
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
def enrich(
    out: str = typer.Option("enrich_proposals.csv", help="Where to write the review CSV"),
    all_accounts: bool = typer.Option(False, "--all", help="Also re-check accounts that already pass the fit gate"),
    only: Optional[str] = typer.Option(None, help="Only accounts whose name contains this text"),
    limit: Optional[int] = typer.Option(None, help="At most N accounts"),
    as_of: Optional[str] = typer.Option(None, help="Run date YYYY-MM-DD (default today)"),
    workers: Optional[int] = typer.Option(None, help="Concurrent calls (default llm.concurrency)"),
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
    accounts = select_accounts(conn, all_accounts, only, limit)
    if not accounts:
        _fail("No accounts to enrich (held accounts by default; use --all, or run `bd seed accounts.csv` first).")
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


@app.command()
def score(
    as_of: Optional[str] = typer.Option(None, help="Score date YYYY-MM-DD (default today)"),
) -> None:
    """Score every fit-gated (active) account from its stored signals. Deterministic; no API calls."""
    from bd.db import connect, init_db
    from bd.score_store import score_accounts

    run_date = _parse_date(as_of)
    cfg, settings = load_config(), Settings.from_env()
    conn = connect(settings.db_path)
    init_db(conn)
    scored = score_accounts(conn, cfg, run_date)
    if not scored:
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
