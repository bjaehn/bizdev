from typer.testing import CliRunner

from bd.cli import app

runner = CliRunner()


def test_all_commands_registered_and_stubbed():
    for cmd in (["seed", "x.csv"], ["scan"], ["score"], ["shortlist"], ["card"], ["evaluate"]):
        r = runner.invoke(app, cmd)
        assert r.exit_code == 2, cmd
