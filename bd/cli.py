"""Typer CLI. Commands not yet built exit 2 with a message naming their stage."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Optional

import typer

from bd.config import Config, Settings, load_config

app = typer.Typer(help="SEI Chicago BD Signal Agent", no_args_is_help=True)


def _not_yet(stage: int, name: str) -> None:
    typer.echo(f"`bd {name}` is not implemented yet (Stage {stage}).", err=True)
    raise typer.Exit(code=2)


def _fail(msg: str) -> None:
    typer.echo(msg, err=True)
    raise typer.Exit(code=1)


def _parse_date(s: Optional[str]) -> date:
    try:
        return date.fromisoformat(s) if s else date.today()
    except ValueError:
        _fail(f"--as-of must be YYYY-MM-DD, got {s!r}")
        raise


def _scanner_deps(cfg: Config, settings: Settings):
    """Real LLM client and URL verifier. Needs ANTHROPIC_API_KEY and ANTHROPIC_MODEL."""
    from bd.llm import AnthropicWebSearchClient
    from bd.verify import UrlVerifier

    try:
        key, model = settings.require_llm()
    except RuntimeError as e:
        _fail(str(e))
        raise
    return AnthropicWebSearchClient(cfg, model=model, api_key=key), UrlVerifier(cfg, settings.user_agent)


@app.command()
def seed(csv_path: str) -> None:
    """Load accounts from a CSV (blank cells stay unknown; nothing is guessed)."""
    from bd.accounts import seed_csv
    from bd.db import connect, init_db

    settings = Settings.from_env()
    conn = connect(settings.db_path)
    init_db(conn)
    if not Path(csv_path).exists():
        _fail(f"{csv_path} not found")
    res = seed_csv(conn, csv_path)
    if res.errors:
        _fail("Seed aborted, nothing written:\n" + "\n".join(res.errors))
    typer.echo(f"Seeded {settings.db_path}: {res.inserted} inserted, {res.updated} updated.")


@app.command()
def scan(
    tier: str = typer.Option("all", help="A, B or all"),
    limit: Optional[int] = typer.Option(None, help="Scan at most N accounts"),
    as_of: Optional[str] = typer.Option(None, help="Run date YYYY-MM-DD (default today)"),
) -> None:
    """Scan active accounts with every implemented scanner (leadership for now; sequential)."""
    from bd.accounts import list_accounts
    from bd.db import connect, init_db
    from bd.scanners import SCANNERS, store_signals

    if tier not in ("A", "B", "all"):
        _fail("--tier must be A, B or all")
    run_date = _parse_date(as_of)
    cfg, settings = load_config(), Settings.from_env()
    llm, verifier = _scanner_deps(cfg, settings)
    conn = connect(settings.db_path)
    init_db(conn)
    accounts = list_accounts(conn, tier=tier, limit=limit)
    scanners = [c(cfg, llm, verifier) for c in SCANNERS.values() if c.implemented]
    if not accounts:
        _fail("No active accounts. Run `bd seed accounts.csv` first.")
    typer.echo(f"Scanning {len(accounts)} accounts with {[s.name for s in scanners]} as of {run_date}")
    for acc in accounts:
        for sc in scanners:
            out = sc.scan(acc, run_date)
            ins, known = store_signals(conn, acc.id, out.signals, run_date)
            errors = "; ".join(x for x in (out.error, out.rejection_summary() and "rejected: " + out.rejection_summary()) if x)
            with conn:
                conn.execute(
                    "INSERT INTO runs(run_date,step,account_id,accounts_scanned,signals_found,tokens,est_cost,errors) "
                    "VALUES (?,?,?,?,?,?,NULL,?)",
                    (run_date.isoformat(), f"scan:{sc.name}", acc.id, 1, ins, out.usage.total_tokens, errors or None))
                if not out.error:
                    conn.execute("UPDATE accounts SET last_scanned = ? WHERE id = ?", (run_date.isoformat(), acc.id))
            typer.echo(f"  {acc.name}: {ins} new, {known} known, {len(out.rejections)} rejected, "
                       f"{out.usage.total_tokens:,} tokens" + (f"  ERROR {out.error}" if out.error else ""))


@app.command()
def score() -> None:
    """Score accounts from stored signals."""
    _not_yet(4, "score")


@app.command()
def shortlist() -> None:
    """Build the weekly shortlist."""
    _not_yet(4, "shortlist")


@app.command()
def card() -> None:
    """Write card.json (and post to Teams only if TEAMS_WEBHOOK_URL is set)."""
    _not_yet(4, "card")


@app.command()
def evaluate(
    gold: str = typer.Option("tests/gold.csv", help="Verified gold file"),
    as_of: Optional[str] = typer.Option(None, help="Evaluate as of YYYY-MM-DD (default today)"),
    tolerance_days: int = typer.Option(0, help="Allowed |event_date - expected_date| in days"),
    limit: Optional[int] = typer.Option(None, help="Evaluate only the first N accounts"),
    out: str = typer.Option("eval_report.json", help="Where to write the JSON report"),
) -> None:
    """Run the leadership scanner over the gold file and report found/missed/wrong date/bad URLs."""
    from bd.evaluate import GoldError, evaluate as run_eval, load_gold
    from bd.scanners import LeadershipScanner

    try:
        rows = load_gold(gold)
    except GoldError as e:
        _fail(str(e))
        raise
    run_date = _parse_date(as_of)
    cfg, settings = load_config(), Settings.from_env()
    llm, verifier = _scanner_deps(cfg, settings)
    scanner = LeadershipScanner(cfg, llm, verifier)
    stale = [g for g in rows if g.expected_date and (run_date - g.expected_date).days > cfg.scan.window_days]
    if stale:
        typer.echo(f"WARNING: {len(stale)} gold rows are older than the {cfg.scan.window_days}-day window as of "
                   f"{run_date}; they cannot be found. Pass --as-of closer to their dates.", err=True)
    report = run_eval(rows, scanner.scan, run_date, tolerance_days=tolerance_days, limit=limit)
    Path(out).write_text(report.to_json())
    typer.echo(report.render())
    typer.echo(f"\nFull report: {out}")


if __name__ == "__main__":
    app()
