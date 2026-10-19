import pytest

from bd.accounts import list_accounts, seed_csv
from bd.curate import (add_exclusion, apply_selected, create_account, gate_one, load_proposals, remove_exclusion,
                       update_account)
from tests.conftest import make_db

HDR = "name,domain,hq_city,chicagoland,revenue,employees,ownership,sponsor,industry,tier,cik\n"


@pytest.fixture
def conn(tmp_path):
    c = make_db(tmp_path)
    c.execute("INSERT INTO accounts(id, name) VALUES (1, 'Acme Corp')")
    c.execute("INSERT INTO accounts(id, name, domain) VALUES (2, 'Beta Inc', 'beta.com')")
    c.commit()
    yield c
    c.close()


def audit(conn):
    return [tuple(r) for r in conn.execute("SELECT actor, action, account_id, field, old, new FROM audit_log ORDER BY id")]


def test_editing_facts_updates_the_account_audits_and_reruns_the_gate(cfg, conn):
    r = update_account(conn, cfg, 1, {"chicagoland": "yes", "revenue": "500,000,000", "ownership": "public", "domain": "https://www.acme.com"})
    assert r.ok and {c for c, _, _ in r.changed} == {"chicagoland", "revenue", "ownership", "domain"}
    assert r.fit.verdict == "pass"
    a = conn.execute("SELECT * FROM accounts WHERE id=1").fetchone()
    assert (a["status"], a["domain"], a["revenue"]) == ("active", "acme.com", 5e8)
    log = audit(conn)
    assert ("ui", "update", 1, "revenue", None, "500000000.0") in log and ("ui", "status", 1, "status", "active", "active") not in log


def test_status_change_is_audited_when_facts_make_an_account_fail(cfg, conn):
    update_account(conn, cfg, 1, {"chicagoland": "yes", "revenue": "500000000", "ownership": "public"})
    r = update_account(conn, cfg, 1, {"chicagoland": "no", "revenue": "500000000", "ownership": "public"})
    assert r.fit.verdict == "fail" and conn.execute("SELECT status FROM accounts WHERE id=1").fetchone()[0] == "excluded"
    assert ("ui", "status", 1, "status", "active", "excluded") in audit(conn)


def test_a_blank_form_field_clears_the_fact(cfg, conn):
    update_account(conn, cfg, 1, {"chicagoland": "yes", "revenue": "500000000", "ownership": "public", "industry": "Tools"})
    r = update_account(conn, cfg, 1, {"chicagoland": "", "revenue": "", "ownership": "", "industry": ""})
    a = conn.execute("SELECT * FROM accounts WHERE id=1").fetchone()
    assert (a["chicagoland"], a["revenue"], a["ownership"], a["industry"]) == ("unknown", None, None, None)
    assert r.fit.verdict == "unknown" and r.fit.unverified and a["status"] == "active"      # unverified is still scanned


def test_unchanged_submit_writes_nothing(cfg, conn):
    update_account(conn, cfg, 2, {"domain": "beta.com"})
    n = len(audit(conn))
    r = update_account(conn, cfg, 2, {"domain": "beta.com"})
    assert r.ok and r.changed == [] and len(audit(conn)) == n


def test_invalid_input_is_rejected_whole_and_nothing_is_written(cfg, conn):
    r = update_account(conn, cfg, 1, {"chicagoland": "yes", "revenue": "lots", "cik": "x"})
    assert not r.ok and len(r.errors) == 2
    assert conn.execute("SELECT chicagoland FROM accounts WHERE id=1").fetchone()[0] == "unknown" and audit(conn) == []


def test_domain_must_be_unique(cfg, conn):
    r = update_account(conn, cfg, 1, {"domain": "beta.com"})
    assert not r.ok and r.errors == ["another account already uses that domain"]
    assert conn.execute("SELECT domain FROM accounts WHERE id=1").fetchone()[0] is None


def test_unknown_account_and_unknown_columns_are_ignored_or_refused(cfg, conn):
    assert update_account(conn, cfg, 99, {"tier": "B"}).errors == ["no such account"]
    r = update_account(conn, cfg, 1, {"tier": "B", "status": "active", "id": "5", "name": "Hacked"})
    a = conn.execute("SELECT * FROM accounts WHERE id=1").fetchone()
    assert (a["tier"], a["id"], a["name"]) == ("B", 1, "Acme Corp")          # status, id and name are not editable


def test_create_account(cfg, conn):
    r = create_account(conn, cfg, "  Gamma   Co ", {"domain": "gamma.com", "chicagoland": "yes", "revenue": "900000000", "ownership": "private"})
    assert r.ok and r.fit.verdict == "pass"
    a = conn.execute("SELECT * FROM accounts WHERE id=?", (r.account_id,)).fetchone()
    assert (a["name"], a["status"], a["tier"]) == ("Gamma Co", "active", "A")
    assert create_account(conn, cfg, "gamma co", {}).errors == ["an account with that name already exists"]
    assert create_account(conn, cfg, "", {}).errors == ["name is required"]
    assert create_account(conn, cfg, "Delta", {"domain": "gamma.com"}).errors == ["another account already uses that domain"]
    assert create_account(conn, cfg, "Eps", {"tier": "Q"}).errors


def test_exclusion_demotes_the_account_at_once_and_removal_restores_it(cfg, conn):
    update_account(conn, cfg, 2, {"chicagoland": "yes", "revenue": "500000000", "ownership": "public"})
    assert conn.execute("SELECT status FROM accounts WHERE id=2").fetchone()[0] == "active"
    assert add_exclusion(conn, cfg, "Someone", "beta.com", "client") == []
    assert conn.execute("SELECT status FROM accounts WHERE id=2").fetchone()[0] == "excluded"
    ex_id = conn.execute("SELECT id FROM exclusions").fetchone()[0]
    assert remove_exclusion(conn, cfg, ex_id) is True
    assert conn.execute("SELECT status FROM accounts WHERE id=2").fetchone()[0] == "active"
    assert remove_exclusion(conn, cfg, ex_id) is False
    actions = [a[1] for a in audit(conn)]
    assert "exclusion_add" in actions and "exclusion_remove" in actions


def test_exclusion_validation(cfg, conn):
    assert add_exclusion(conn, cfg, "", "", "client") == ["name is required"]
    assert "reason" in add_exclusion(conn, cfg, "X", "", "because")[0]
    assert add_exclusion(conn, cfg, "X", "not a host!", "client")
    assert add_exclusion(conn, cfg, "X", "", "client") == [] and add_exclusion(conn, cfg, "x", "", "client") == ["that exclusion already exists"]


# ---- enrich proposals -------------------------------------------------------------------------------------------

def proposals_csv(tmp_path, body):
    p = tmp_path / "enrich_proposals.csv"
    p.write_text(HDR.strip() + ",evidence_urls,notes,sec_name\n" + body)
    return p


def test_load_proposals_classifies_each_fact_against_the_database(cfg, conn, tmp_path):
    update_account(conn, cfg, 1, {"revenue": "100", "industry": "Tools"})
    p = proposals_csv(tmp_path, 'Acme Corp,acme.com,,yes,500,,,,Tools,,0000001234,'
                                '"il_county=https://acme.com/loc; revenue_usd=https://www.sec.gov/x; cik=javascript:alert(1)",note here,ACME CORP (ACME)\n'
                                'Ghost Co,ghost.com,,,,,,,,,,,,\n')
    props, errs = load_proposals(conn, p)
    assert errs == []
    acme, ghost = props
    d = {x.column: x for x in acme.diffs}
    assert d["domain"].status == "new" and d["chicagoland"].status == "new" and d["industry"].status == "same"
    assert d["revenue"].status == "conflict" and d["revenue"].current == 100 and d["revenue"].proposed == 500
    assert d["chicagoland"].evidence_url == "https://acme.com/loc" and d["revenue"].evidence_url == "https://www.sec.gov/x"
    assert d["cik"].evidence_url is None                                   # a javascript: URL is never kept as a link
    assert (acme.notes, acme.sec_name) == ("note here", "ACME CORP (ACME)")
    assert ghost.account_id is None and ghost.diffs == []


def test_load_proposals_reports_bad_rows_and_bad_headers(cfg, conn, tmp_path):
    p = proposals_csv(tmp_path, "Acme Corp,,,maybe,,,,,,,,,,\n")
    props, errs = load_proposals(conn, p)
    assert any("chicagoland" in e for e in errs)
    p.write_text("name,mystery\nA,1\n")
    assert load_proposals(conn, p) == ([], ["unknown columns: ['mystery']"])
    p.write_text("domain\nx.com\n")
    assert "name" in load_proposals(conn, p)[1][0]


def test_apply_selected_applies_only_what_was_ticked_and_revalidates(cfg, conn):
    r = apply_selected(conn, cfg, 1, {"chicagoland": "yes", "revenue": "500000000", "ownership": "public", "name": "Hack", "status": "excluded"})
    assert r.ok and {c for c, _, _ in r.changed} == {"chicagoland", "revenue", "ownership"}
    a = conn.execute("SELECT * FROM accounts WHERE id=1").fetchone()
    assert (a["name"], a["status"]) == ("Acme Corp", "active")
    assert all(x[0] == "enrich" for x in audit(conn) if x[1] == "update")
    bad = apply_selected(conn, cfg, 1, {"revenue": "lots"})
    assert not bad.ok
    assert apply_selected(conn, cfg, 1, {}).changed == []


def test_gate_one_for_unknown_account_raises(cfg, conn):
    with pytest.raises(KeyError):
        gate_one(conn, cfg, 404)


# ---- bulk apply of an enrich proposals file ---------------------------------------------------------------------------
def test_apply_all_new_fills_only_unknown_facts_and_audits_evidence(cfg, tmp_path):
    from bd.curate import apply_all_new
    from tests.conftest import add_account, make_db
    conn = make_db(tmp_path)
    a = add_account(conn, "Acme", domain="acme.com", chicagoland="unknown", revenue=None, ownership=None, hq_city="Chicago, IL")
    b = add_account(conn, "Beta", domain="beta.com", revenue=900000000, ownership="public")
    p = tmp_path / "p.csv"
    p.write_text("name,domain,hq_city,chicagoland,revenue,employees,ownership,evidence_urls\n"
                 "Acme,,Evanston,yes,500000000,2500,public,il_county=https://acme.com/loc; revenue_usd=https://acme.com/ir; ownership=https://acme.com/ir\n"
                 "Beta,,,,1,,,revenue_usd=https://beta.com/x\n"
                 "Ghost Co,,,yes,5,,,\n")
    r = apply_all_new(conn, cfg, p)
    assert r == {"accounts": 1, "facts": 4, "conflicts_skipped": 2, "not_in_database": 1, "failed": 0, "file_errors": 0}
    row = conn.execute("SELECT * FROM accounts WHERE id = ?", (a,)).fetchone()
    assert (row["chicagoland"], row["revenue"], row["ownership"], row["employees"]) == ("yes", 5e8, "public", 2500)
    assert row["hq_city"] == "Chicago, IL"                                  # a known fact is never replaced here
    assert conn.execute("SELECT revenue FROM accounts WHERE id = ?", (b,)).fetchone()[0] == 900000000
    ev = {r["field"]: r["new"] for r in conn.execute("SELECT field, new FROM audit_log WHERE action = 'evidence'")}
    assert ev["revenue"] == "https://acme.com/ir" and ev["chicagoland"] == "https://acme.com/loc"
    assert apply_all_new(conn, cfg, p)["facts"] == 0                         # idempotent
