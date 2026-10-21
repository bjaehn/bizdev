import pytest

from bd.accounts import Account, seed_csv
from bd.db import connect, init_db
from bd.fit_gate import Exclusion, apply_fit_gate, check_fit, load_exclusions, seed_exclusions


def acct(**kw):
    base = dict(id=1, name="Acme", domain="acme.com", chicagoland="yes", revenue=500e6, employees=None,
                ownership="public", industry="Industrial")
    base.update(kw)
    return Account(**base)


@pytest.mark.parametrize("kw,verdict", [
    ({}, "pass"),
    ({"revenue": 250e6}, "pass"),                              # floor is inclusive
    ({"revenue": 249e6}, "unknown"),                           # employees unknown: could still qualify
    ({"revenue": 249e6, "employees": 999}, "fail"),
    ({"revenue": 249e6, "employees": 1000}, "pass"),           # either measure passes
    ({"revenue": None, "employees": 1000}, "pass"),
    ({"revenue": None, "employees": None}, "unknown"),
    ({"revenue": None, "employees": 999}, "unknown"),
    ({"chicagoland": "no"}, "fail"),
    ({"chicagoland": "unknown"}, "unknown"),
    ({"chicagoland": "no", "revenue": None}, "fail"),          # a definite fail beats unknowns
    ({"chicagoland": "unknown", "revenue": 1e6, "employees": 5}, "fail"),
    ({"industry": "Cannabis retail"}, "fail"),
    ({"industry": "CANNABIS"}, "fail"),
])
def test_default_floor_and_location(cfg, kw, verdict):
    assert check_fit(acct(**kw), [], cfg).verdict == verdict


@pytest.mark.parametrize("kw,verdict", [
    ({"ownership": "pe-backed", "revenue": 75e6}, "pass"),
    ({"ownership": "pe-backed", "revenue": 74e6}, "fail"),
    ({"ownership": "pe-backed", "revenue": 74e6, "employees": 5000}, "fail"),   # PE floor is revenue only
    ({"ownership": "pe-backed", "revenue": None, "employees": 5000}, "unknown"),
    ({"ownership": "private", "revenue": 100e6}, "unknown"),                    # below $250M, employees unknown
    ({"ownership": "private", "revenue": 100e6, "employees": 10}, "fail"),      # PE floor does not apply
    ({"ownership": None, "revenue": 100e6}, "unknown"),                         # might be PE-backed
    ({"ownership": None, "revenue": 70e6, "employees": 10}, "fail"),            # below both floors
    ({"ownership": None, "revenue": 300e6}, "pass"),
])
def test_pe_floor(cfg, kw, verdict):
    assert check_fit(acct(**kw), [], cfg).verdict == verdict


def test_exclusions_by_name_and_domain(cfg):
    assert check_fit(acct(), [Exclusion("ACME", None, "client")], cfg).reasons == ("excluded: client",)
    assert check_fit(acct(name="Other"), [Exclusion("x", "acme.com", "conflict")], cfg).verdict == "fail"
    assert check_fit(acct(domain="www.careers.acme.com", name="Other"),
                     [Exclusion("x", "acme.com", "do_not_pursue")], cfg).verdict == "fail"
    assert check_fit(acct(name="Other", domain="notacme.com"), [Exclusion("x", "acme.com", "client")], cfg).verdict == "pass"


def test_unknown_reasons_say_what_is_missing(cfg):
    res = check_fit(Account(id=1, name="Bare"), [], cfg)
    assert res.verdict == "unknown" and res.unverified and res.status == "active"      # unverified is still scanned
    assert any("Chicagoland" in r for r in res.reasons) and any("size" in r for r in res.reasons)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.sqlite")
    init_db(c)
    yield c
    c.close()


def write(tmp_path, body, name="a.csv"):
    p = tmp_path / name
    p.write_text("name,domain,hq_city,chicagoland,revenue,employees,ownership,sponsor,industry,tier\n" + body)
    return p


def test_apply_gate_sets_status_and_is_recomputed_when_facts_arrive(cfg, conn, tmp_path):
    seed_csv(conn, write(tmp_path, "A,a.com,,yes,500000000,,public,,,A\nB,b.com,,,,,,,,A\nC,c.com,,no,5e8,,public,,,A\n"))
    g = apply_fit_gate(conn, cfg)
    assert [a.name for a in g.passed] == ["A"] and [a.name for a, _ in g.unverified] == ["B"]
    assert [a.name for a, _ in g.excluded] == ["C"] and g.changed == 1      # only C changes: B is unverified but still scanned
    st = {r["name"]: r["status"] for r in conn.execute("SELECT name, status FROM accounts")}
    assert st == {"A": "active", "B": "active", "C": "excluded"}
    # a human fills in B's facts -> next gate run marks it verified
    seed_csv(conn, write(tmp_path, "B,,,yes,900000000,,private,,,\n", "b.csv"))
    g = apply_fit_gate(conn, cfg)
    assert sorted(a.name for a in g.passed) == ["A", "B"] and g.changed == 0
    assert apply_fit_gate(conn, cfg).changed == 0   # idempotent
    # a legacy database that still holds a company on hold: the gate releases it
    conn.execute("UPDATE accounts SET status='hold_review' WHERE name='B'")
    conn.commit()
    assert apply_fit_gate(conn, cfg).changed == 1 and conn.execute("SELECT status FROM accounts WHERE name='B'").fetchone()[0] == "active"


def test_adding_an_exclusion_demotes_an_active_account(cfg, conn, tmp_path):
    seed_csv(conn, write(tmp_path, "A,a.com,,yes,500000000,,public,,,A\n"))
    assert apply_fit_gate(conn, cfg).passed
    ex = tmp_path / "ex.csv"
    ex.write_text("name,domain,reason\nSomeone,a.com,client\n")
    assert seed_exclusions(conn, ex) == (1, [])
    assert seed_exclusions(conn, ex) == (0, [])           # idempotent
    assert len(load_exclusions(conn)) == 1
    g = apply_fit_gate(conn, cfg)
    assert not g.passed and g.excluded[0][1].reasons == ("excluded: client",)


def test_seed_exclusions_validation_is_all_or_nothing(conn, tmp_path):
    ex = tmp_path / "ex.csv"
    ex.write_text("name,domain,reason\nGood,,client\nBad,,nope\n,x.com,conflict\n")
    n, errs = seed_exclusions(conn, ex)
    assert n == 0 and len(errs) == 2
    assert conn.execute("SELECT COUNT(*) FROM exclusions").fetchone()[0] == 0
    ex.write_text("name,reason\nA,client\n")
    assert "header" in seed_exclusions(conn, ex)[1][0]


# ---- competitors are not prospects ------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name,domain,out", [
    ("RSM US LLP", None, True),                      # the name, ignoring case, punctuation and legal suffixes
    ("rsm us", None, True),
    ("West Monroe Partners", "westmonroe.com", True),
    ("ZS", None, True),
    ("Deloitte Consulting LLP", "www2.deloitte.com", True),      # a subdomain of the firm's own site
    ("Anything Inc", "huronconsultinggroup.com", True),          # the domain alone is enough
    ("Bain & Company", None, True),
    ("Bain Capital", "baincapital.com", False),                  # a private equity firm: not the consultancy of the same first word
    ("ZS Pharma", None, False),                                  # the whole name must match, not its first word
    ("Kirkland & Ellis", "kirkland.com", False),                 # law firms are prospects, not on the list
    ("Hub Group", "hubgroup.com", False),
])
def test_a_competitor_is_out_of_scope_as_a_conflict_and_nothing_near_it_is(cfg, name, domain, out):
    r = check_fit(acct(name=name, domain=domain), [], cfg)
    assert (r.verdict == "fail") is out
    if out:
        assert r.reasons == ("excluded: conflict (a competitor of the firm; fit_gate.competitors)",) and r.status == "excluded"


def test_the_gate_excludes_a_competitor_already_on_the_list_and_filling_in_facts_cannot_bring_it_back(cfg, tmp_path):
    conn = connect(tmp_path / "g.sqlite")
    init_db(conn)
    conn.execute("INSERT INTO accounts(name, domain, status, chicagoland, revenue, ownership) VALUES ('RSM US LLP','rsmus.com','active','yes',5e9,'private')")
    conn.execute("INSERT INTO accounts(name, domain, status, chicagoland, revenue, ownership) VALUES ('Acme','acme.com','active','yes',5e8,'public')")
    conn.commit()
    s = apply_fit_gate(conn, cfg)
    assert [a.name for a, _r in s.excluded] == ["RSM US LLP"] and [a.name for a in s.passed] == ["Acme"] and s.changed == 1
    assert conn.execute("SELECT status FROM accounts WHERE name = 'RSM US LLP'").fetchone()[0] == "excluded"


def test_the_competitor_list_can_be_emptied_in_config(cfg):
    off = cfg.model_copy(deep=True)
    off.fit_gate.competitors.names, off.fit_gate.competitors.domains = [], []
    assert check_fit(acct(name="RSM US LLP", domain="rsmus.com"), [], off).verdict == "pass"
