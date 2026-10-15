"""Typer CLI. Commands are stubs until the stage that implements them."""

from __future__ import annotations

import typer

app = typer.Typer(help="SEI Chicago BD Signal Agent", no_args_is_help=True)


def _not_yet(stage: int, name: str) -> None:
    typer.echo(f"`bd {name}` is not implemented yet (Stage {stage}).", err=True)
    raise typer.Exit(code=2)


@app.command()
def seed(csv_path: str) -> None:
    """Load accounts from a CSV."""
    _not_yet(3, "seed")


@app.command()
def scan(tier: str = typer.Option("all", help="A, B or all")) -> None:
    """Scan accounts for signals."""
    _not_yet(2, "scan")


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
def evaluate() -> None:
    """Run tests/gold.csv through the scanners and report accuracy."""
    _not_yet(2, "evaluate")


if __name__ == "__main__":
    app()
