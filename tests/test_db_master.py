import csv

import pytest
from typer.testing import CliRunner

from bd.accounts import (export_accounts_csv, export_exclusions_csv, list_accounts, parse_fields, seed_csv)
from bd.cli import app
from tests.conftest import ROOT, make_db

runner = CliRunner()
HDR = "name,domain,hq_city,chicagoland,revenue,employees,ownership,sponsor,industry,tier,cik\n"


@pytest.fixture
def conn(tmp_path):
    c = make_db(tmp_path)
    yield c
    c.close()


def write(tmp_path, body, name="a.csv"):
    p = tmp_path / name
    p.write_text(HDR + body)
    return p


# ---- the database is the master copy ----------------------------------------------------------------------

def test_seed_never_overwrites_a_database_value_and_reports_the_conflict(conn, tmp_path):
    seed_csv(conn, write(tmp_path, "Acme,acme.com,Chicago,yes,500000000,,public,,,A,\n"))
    conn.execute("UPDATE accounts SET revenue = 640000000, chicagoland = 'yes'")        # a human edit in the UI
    conn.commit()
    res = seed_csv(conn, write(tmp_path, "Acme,acme.com,Chicago,no,500000000,,private,,,A,\n", "b.csv"))
    a = conn.execute("SELECT * FROM accounts").fetchone()
    assert (a["revenue"], a["chicagoland"], a["ownership"]) == (640000000, "yes", "public")    # edits survive
    assert res.overwritten == 0 and res.filled == 0
    assert sorted(c.split(":")[1].split(" is ")[0].strip() for c in res.conflicts) == ["chicagoland", "ownership", "revenue"]


def test_seed_still_fills_unknown_facts_and_adds_new_accounts(conn, tmp_path):
    seed_csv(conn, write(tmp_path, "Acme,,,,,,,,,,\n"))
    res = seed_csv(conn, write(tmp_path, "Acme,acme.com,Chicago,yes,500000000,,public,,,A,\nNewco,newco.com,,,,,,,,,\n", "b.csv"))
    assert (res.inserted, res.updated, res.filled, res.conflicts) == (1, 1, 5, [])
    a = conn.execute("SELECT * FROM accounts WHERE name='Acme'").fetchone()
    assert (a["domain"], a["chicagoland"], a["ownership"]) == ("acme.com", "yes", "public")


def test_seed_overwrite_flag_lets_the_csv_win(conn, tmp_path):
    seed_csv(conn, write(tmp_path, "Acme,acme.com,,yes,500000000,,public,,,A,\n"))
    res = seed_csv(conn, write(tmp_path, "Acme,acme.com,,no,900000000,,public,,,A,\n", "b.csv"), overwrite=True)
    a = conn.execute("SELECT * FROM accounts").fetchone()
    assert (a["chicagoland"], a["revenue"]) == ("no", 900000000) and res.overwritten == 2 and res.conflicts == []


def test_seed_writes_an_audit_trail(conn, tmp_path):
    seed_csv(conn, write(tmp_path, "Acme,,,,,,,,,,\n"))
    seed_csv(conn, write(tmp_path, "Acme,acme.com,,,,,,,,,\n", "b.csv"))
    rows = conn.execute("SELECT actor, action, field, old, new FROM audit_log ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [("seed", "create", "name", None, "Acme"), ("seed", "update", "domain", None, "acme.com")]


def test_cli_seed_reports_conflicts_and_overwrite_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("BD_DB_PATH", str(tmp_path / "t.sqlite"))
    monkeypatch.setenv("BD_CONFIG", str(ROOT / "config.yaml"))
    assert runner.invoke(app, ["seed", str(write(tmp_path, "Acme,acme.com,,yes,500000000,,public,,,A,\n"))]).exit_code == 0
    p2 = write(tmp_path, "Acme,acme.com,,no,500000000,,public,,,A,\n", "b.csv")
    r = runner.invoke(app, ["seed", str(p2)])
    assert r.exit_code == 0 and "1 CSV value(s) differ from the database" in r.output and "chicagoland" in r.output
    r = runner.invoke(app, ["seed", str(p2), "--overwrite"])
    assert "1 overwritten" in r.output


# ---- parse_fields: CSV vs form semantics -----------------------------------------------------------------

def test_blank_means_no_information_for_csv_but_clear_for_forms():
    row = {"domain": "", "revenue": "", "chicagoland": "", "tier": "", "ownership": "", "cik": ""}
    assert parse_fields(row)[0] == {}
    cleared = parse_fields(row, clear_blank=True)[0]
    assert cleared == {"domain": None, "revenue": None, "chicagoland": "unknown", "ownership": None, "cik": None}
    assert "tier" not in cleared                                    # an account always has a tier


def test_parse_fields_validation_and_normalisation():
    ok, errs = parse_fields({"domain": "https://www.Acme.com/x", "revenue": "$1,500,000", "employees": "1,200",
                             "cik": "1234", "tier": "b", "chicagoland": "YES", "ownership": "PE-Backed"})
    assert errs == [] and ok == {"domain": "acme.com", "revenue": 1500000.0, "employees": 1200, "cik": "0000001234",
                                 "tier": "B", "chicagoland": "yes", "ownership": "pe-backed"}
    _, errs = parse_fields({"revenue": "-5", "employees": "1.5", "cik": "0", "tier": "Z", "chicagoland": "maybe",
                            "ownership": "nonprofit"}, label="x")
    assert len(errs) == 6 and all(e.startswith("x: ") for e in errs)


# ---- export round-trips ------------------------------------------------------------------------------------

def test_export_then_seed_into_a_fresh_database_reproduces_the_accounts(conn, tmp_path):
    seed_csv(conn, write(tmp_path, "Acme,acme.com,Chicago,yes,500000000,2500,public,,Industrial,B,1234\nBare,,,,,,,,,,\n"))
    conn.execute("INSERT INTO exclusions(name, domain, reason) VALUES ('Client Co', 'client.com', 'client')")
    conn.commit()
    out, ex = tmp_path / "accounts.csv", tmp_path / "exclusions.csv"
    assert export_accounts_csv(conn, out) == 2 and export_exclusions_csv(conn, ex) == 1
    rows = list(csv.DictReader(out.open()))
    assert rows[0]["revenue"] == "500000000" and rows[0]["cik"] == "0000001234" and rows[1]["chicagoland"] == ""
    assert list(csv.DictReader(ex.open())) == [{"name": "Client Co", "domain": "client.com", "reason": "client"}]
    fresh = make_db(tmp_path / "x") if (tmp_path / "x").mkdir() is None else None
    res = seed_csv(fresh, out)
    assert res.errors == [] and res.inserted == 2
    a, b = list_accounts(fresh, status=None)
    assert (a.domain, a.revenue, a.employees, a.tier, a.cik, a.chicagoland) == ("acme.com", 5e8, 2500, "B", "0000001234", "yes")
    assert (b.domain, b.chicagoland) == (None, "unknown")


def test_cli_export_writes_both_files(tmp_path, monkeypatch):
    monkeypatch.setenv("BD_DB_PATH", str(tmp_path / "t.sqlite"))
    monkeypatch.setenv("BD_CONFIG", str(ROOT / "config.yaml"))
    runner.invoke(app, ["seed", str(write(tmp_path, "Acme,acme.com,,,,,,,,,\n"))])
    r = runner.invoke(app, ["export", "--accounts", str(tmp_path / "A.csv"), "--exclusions", str(tmp_path / "E.csv")])
    assert r.exit_code == 0 and "Wrote 1 account(s)" in r.output and (tmp_path / "A.csv").exists()


@pytest.mark.parametrize("raw,ok", [
    ("acme.com", True), ("https://www.Acme.com/path", True), ("sub.acme.co.uk", True), ("xn--bcher-kva.example", True),
    ("not a host!", False), ("acme", False), ("192.168.0.1", False), ("-bad.com", False), ("acme..com", False),
    ("a" * 70 + ".com", False), ("javascript:alert(1)", False), ("acme.com:8080", True),
])
def test_domain_validation(raw, ok):
    _, errs = parse_fields({"domain": raw})
    assert (errs == []) is ok, (raw, errs)


def test_exclusion_csv_rejects_a_bad_domain(conn, tmp_path):
    from bd.fit_gate import seed_exclusions
    p = tmp_path / "ex.csv"
    p.write_text("name,domain,reason\nGood,good.com,client\nBad,not a host!,client\n")
    n, errs = seed_exclusions(conn, p)
    assert n == 0 and "not a valid host name" in errs[0]
