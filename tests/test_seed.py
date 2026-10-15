import pytest

from bd.accounts import SEED_COLUMNS, list_accounts, normalize_domain, seed_csv
from bd.db import connect, init_db
from tests.conftest import ROOT


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.sqlite")
    init_db(c)
    yield c
    c.close()


def write(tmp_path, body, header=",".join(SEED_COLUMNS)):
    p = tmp_path / "a.csv"
    p.write_text(header + "\n" + body)
    return p


def test_seed_inserts_blank_as_unknown_never_guessed(conn, tmp_path):
    res = seed_csv(conn, write(tmp_path, "Acme,,,,,,,,,\nBeta,https://www.Beta.com/x,Chicago,yes,1,000,2500,public,,Fin,B\n".replace("1,000", "1000")))
    assert (res.inserted, res.updated, res.errors) == (2, 0, [])
    a, b = list_accounts(conn)
    assert a.domain is None and a.chicagoland == "unknown" and a.revenue is None and a.tier == "A"
    assert b.domain == "beta.com" and b.chicagoland == "yes" and b.employees == 2500 and b.tier == "B"


def test_reseed_updates_without_blanking_and_is_case_insensitive(conn, tmp_path):
    seed_csv(conn, write(tmp_path, "Acme,acme.com,Chicago,,,,,,,\n"))
    res = seed_csv(conn, write(tmp_path, "ACME,,,yes,,,,,,\n"))
    assert (res.inserted, res.updated) == (0, 1)
    (a,) = list_accounts(conn)
    assert a.domain == "acme.com" and a.hq_city == "Chicago" and a.chicagoland == "yes"


@pytest.mark.parametrize("row,msg", [
    (",acme.com,,,,,,,,", "name is required"),
    ("A,,,maybe,,,,,,", "chicagoland"),
    ("A,,,,,,nonprofit,,,", "ownership"),
    ("A,,,,,,,,,C", "tier"),
    ("A,,,,lots,,,,,", "non-negative number"),
])
def test_bad_rows_abort_everything(conn, tmp_path, row, msg):
    res = seed_csv(conn, write(tmp_path, "Good Co,,,,,,,,,\n" + row + "\n"))
    assert any(msg in e for e in res.errors)
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0


def test_bad_header(conn, tmp_path):
    assert seed_csv(conn, write(tmp_path, "x\n", header="company")).errors
    assert "unknown columns" in seed_csv(conn, write(tmp_path, "A,1\n", header="name,ticker")).errors[0]


def test_list_filters(conn, tmp_path):
    seed_csv(conn, write(tmp_path, "A,,,,,,,,,A\nB,,,,,,,,,B\nC,,,,,,,,,B\n"))
    conn.execute("UPDATE accounts SET status='hold_review' WHERE name='C'")
    assert [a.name for a in list_accounts(conn, tier="B")] == ["B"]
    assert [a.name for a in list_accounts(conn)] == ["A", "B"]
    assert [a.name for a in list_accounts(conn, limit=1)] == ["A"]
    assert [a.name for a in list_accounts(conn, status="hold_review")] == ["C"]


def test_normalize_domain():
    assert normalize_domain("https://www.Acme.com/x") == "acme.com"
    assert normalize_domain("acme.com") == "acme.com"


def test_repo_accounts_csv_is_valid_names_only(conn):
    res = seed_csv(conn, ROOT / "accounts.csv")
    assert res.errors == [] and res.inserted == 58
    rows = conn.execute("SELECT domain, hq_city, revenue, employees, ownership, chicagoland FROM accounts").fetchall()
    assert all(r["domain"] is None and r["revenue"] is None and r["chicagoland"] == "unknown" for r in rows)
