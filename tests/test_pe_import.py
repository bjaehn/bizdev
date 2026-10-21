"""The researched Chicago PE seed loads as leads: nothing is current because a workbook says so, a rerun adds nothing, the database wins."""

import csv
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bd.cli import app
from bd.pe_import import SEED_DIR, SEED_FILES, SeedError, import_seed, load_seed
from bd.pe_ownership import add_evidence, evidence_for, recompute
from bd.pe_store import ensure_firm
from tests.conftest import add_account, make_db
from tests.test_update import app_env  # noqa: F401  (fixture)

TODAY = date(2026, 10, 21)
FIRM_COLS = ["seed_ref", "name", "scope", "domain", "local_city", "presence", "focus", "portfolio_url", "presence_source_url", "note"]
CO_COLS = ["seed_ref", "name", "aliases", "kind", "location", "industry", "ownership_role", "evidence_status", "confidence", "evidence_date", "research_date", "note"]
REL_COLS = ["seed_ref", "firm", "firm_note", "company", "company_seed_ref", "aliases", "claim", "workbook_status", "role", "source_url", "evidence_date",
            "research_date", "text", "note"]


@pytest.fixture
def cfg(cfg):
    return cfg.model_copy(deep=True)


@pytest.fixture
def conn(tmp_path):
    return make_db(tmp_path)


def firm(ref, name, scope="chicago", **kw):
    row = dict.fromkeys(FIRM_COLS, "") | {"seed_ref": ref, "name": name, "scope": scope}
    return row | kw


def company(ref, name, **kw):
    return dict.fromkeys(CO_COLS, "") | {"seed_ref": ref, "name": name, "kind": "company", "research_date": "2026-10-19"} | kw


def rel(ref, firm_name, company_name, claim="listed", url="https://src.example/p", **kw):
    return dict.fromkeys(REL_COLS, "") | {"seed_ref": ref, "firm": firm_name, "company": company_name, "claim": claim, "source_url": url,
                                          "research_date": "2026-10-19", "workbook_status": "Listed in sponsor portfolio"} | kw


def seed(tmp_path, firms, companies, rels):
    d = tmp_path / "seed"
    d.mkdir(exist_ok=True)
    for name, cols, rows in (("pe_firms.csv", FIRM_COLS, firms), ("pe_companies.csv", CO_COLS, companies), ("pe_relationships.csv", REL_COLS, rels)):
        with open(d / name, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
    return d


def small(tmp_path):
    return seed(
        tmp_path,
        [firm("F01", "Acme Capital", domain="acmecap.com", local_city="Chicago", presence="Headquartered locally", focus="Industrials",
              portfolio_url="https://www.acmecap.com/portfolio/"),
         firm("F02", "Beta Partners", domain="betapartners.com"), firm("I01", "Big Fund", scope="investor")],
        [company("C001", "Widget Works", location="Deerfield", industry="Industrial", evidence_date="2026-05-01"),
         company("C002", "West Monroe", kind="peer", aliases="West Monroe Partners", location="Chicago")],
        [rel("R001", "Acme Capital", "Widget Works", "listed_current", company_seed_ref="C001", workbook_status="Active per sponsor"),
         rel("R002", "Beta Partners", "Widget Works", "announced", url="https://news.example/deal", company_seed_ref="C001",
             workbook_status="Completed acquisition in 2026", evidence_date="2026-05-01", text="Beta completed its investment."),
         rel("R003", "Big Fund", "Widget Works", "listed_historical", company_seed_ref="C001"),
         rel("R004", "Acme Capital", "Plain Co", "listed", aliases="PC"),               # no Chicago-company row: created from the relationship itself
         rel("R005", "Beta Partners", "West Monroe", "listed", company_seed_ref="C002")])


def own(conn, firm_name, company_name):
    return conn.execute("SELECT o.* FROM pe_ownership o JOIN pe_firms f ON f.id = o.firm_id JOIN pe_companies c ON c.id = o.company_id "
                        "WHERE f.name = ? AND c.name = ?", (firm_name, company_name)).fetchone()


# ---- validation ------------------------------------------------------------------------------------------------------------------------------
def test_a_bad_seed_loads_nothing_and_says_every_problem(tmp_path, conn, cfg):
    d = seed(tmp_path, [firm("F01", "Acme Capital"), firm("F01b", "acme capital"), firm("F02", "Odd", scope="mystery")],
             [company("C001", "Widget")],
             [rel("R1", "Nobody Capital", "Widget", "completed"), rel("R2", "Acme Capital", "Widget", url="ftp://x"), rel("R3", "Acme Capital", "Widget", company_seed_ref="C9")])
    with pytest.raises(SeedError) as e:
        import_seed(conn, cfg, d, TODAY)
    msg = str(e.value)
    for expect in ("listed twice", "scope must be chicago or investor", "not in pe_firms.csv", "a completion or an exit needs a page code has read",
                   "source_url must start with http", "company_seed_ref 'C9'"):
        assert expect in msg, expect
    assert conn.execute("SELECT COUNT(*) FROM pe_firms").fetchone()[0] == 0                  # all or nothing


def test_missing_columns_and_files_are_reported(tmp_path):
    d = tmp_path / "x"
    d.mkdir()
    (d / "pe_firms.csv").write_text("name\nAcme\n")
    with pytest.raises(SeedError) as e:
        load_seed(d)
    assert "missing column" in str(e.value) and "pe_companies.csv" in str(e.value)


# ---- what a load makes ----------------------------------------------------------------------------------------------------------------------
def test_a_load_makes_leads_never_current_ownership_and_never_a_place(tmp_path, conn, cfg):
    res = import_seed(conn, cfg, small(tmp_path), TODAY)
    assert (res.firms, res.firms_added, res.companies, res.companies_added) == (3, 3, 2, 3) and res.relationships == 5 and res.relationships_added == 5 and not res.unresolved
    f = conn.execute("SELECT * FROM pe_firms WHERE name = 'Acme Capital'").fetchone()
    assert (f["scope"], f["presence"], f["local_city"], f["focus"], f["domain"], f["portfolio_url"], f["seed_ref"], f["origin"]) == (
        "chicago", "Headquartered locally", "Chicago", "Industrials", "acmecap.com", "https://www.acmecap.com/portfolio/", "F01", "manual")
    assert f["hq_city"] is None                                                               # the workbook's city is where the Chicago office is, not the headquarters
    assert conn.execute("SELECT scope FROM pe_firms WHERE name = 'Big Fund'").fetchone()[0] == "investor"
    w = conn.execute("SELECT * FROM pe_companies WHERE name = 'Widget Works'").fetchone()
    assert (w["industry"], w["seed_ref"]) == ("Industrial", "C001")
    for firm_name, claim in (("Acme Capital", "listed_current"), ("Beta Partners", "announced"), ("Big Fund", "listed_historical")):
        o = own(conn, firm_name, "Widget Works")
        assert (o["status"], o["basis"], o["confidence"], o["origin"]) == ("unclear", "workbook_lead", "Low", "manual")
        rows = evidence_for(conn, "ownership", o["id"])
        assert [(r["claim"], r["verification"], r["source_type"]) for r in rows] == [(claim, "snippet_only", "workbook")]
    ann = evidence_for(conn, "ownership", own(conn, "Beta Partners", "Widget Works")["id"])[0]
    assert "Completed acquisition in 2026" in ann["excerpt"] and "Beta completed its investment" in ann["excerpt"] and ann["event_date"] == "2026-05-01"
    assert conn.execute("SELECT COUNT(*) FROM pe_evidence WHERE claim IN ('completed', 'exited')").fetchone()[0] == 0
    loc = conn.execute("SELECT * FROM pe_evidence WHERE subject_type = 'company' AND claim = 'location'").fetchall()
    assert {(r["value"], r["verification"], r["source_type"]) for r in loc} == {("Deerfield", "snippet_only", "workbook"), ("Chicago", "snippet_only", "workbook")}
    assert conn.execute("SELECT COUNT(*) FROM pe_locations").fetchone()[0] == 0              # a place comes only from a retrieved page
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE actor = 'pe-import'").fetchone()[0] >= 1


def test_re_deriving_a_lead_keeps_it_unclear_and_a_read_page_is_the_only_thing_that_changes_it(tmp_path, conn, cfg):
    import_seed(conn, cfg, small(tmp_path), TODAY)
    o = own(conn, "Acme Capital", "Widget Works")
    with conn:
        recompute(conn, o["id"], TODAY, cfg)
    again = own(conn, "Acme Capital", "Widget Works")
    assert (again["status"], again["basis"]) == ("unclear", "workbook_lead")                  # an announcement and a "current" label from a workbook change nothing
    ann = own(conn, "Beta Partners", "Widget Works")
    with conn:
        recompute(conn, ann["id"], TODAY, cfg)
    assert own(conn, "Beta Partners", "Widget Works")["status"] == "unclear"
    with conn:                                                                                # now code has read the firm's own page
        add_evidence(conn, "ownership", o["id"], "listed_current", "https://www.acmecap.com/portfolio/", retrieved_at="2026-10-21T08:00:00+00:00",
                     source_type="firm_site", verification="page_verified", content_hash="abc", excerpt="Widget Works")
        recompute(conn, o["id"], TODAY, cfg)
    up = own(conn, "Acme Capital", "Widget Works")
    assert up["status"] == "current" and up["basis"] == "firm_listing"
    assert {r["verification"] for r in evidence_for(conn, "ownership", o["id"])} == {"snippet_only", "page_verified"}      # the workbook row stays, marked unchecked


def test_a_rerun_adds_nothing_and_an_old_database_is_never_overwritten(tmp_path, conn, cfg):
    d = small(tmp_path)
    with conn:
        fid, _ = ensure_firm(conn, "Acme Capital", "derived", "sync", None, "Boston", None)    # known already, from an account's sponsor, with its own city
    first = import_seed(conn, cfg, d, TODAY)
    assert first.firms_added == 2                                                              # Acme Capital existed
    f = conn.execute("SELECT * FROM pe_firms WHERE id = ?", (fid,)).fetchone()
    assert (f["domain"], f["portfolio_url"], f["scope"], f["hq_city"], f["origin"]) == ("acmecap.com", "https://www.acmecap.com/portfolio/", "chicago", "Boston", "derived")
    snapshot = [tuple(r) for r in conn.execute("SELECT * FROM pe_evidence ORDER BY id")]
    second = import_seed(conn, cfg, d, TODAY)
    assert (second.firms_added, second.companies_added, second.relationships_added, second.evidence_added) == (0, 0, 0, 0)
    assert [tuple(r) for r in conn.execute("SELECT * FROM pe_evidence ORDER BY id")] == snapshot
    # a relationship the app already read from the firm's own page keeps its status; the workbook only adds evidence
    o = own(conn, "Acme Capital", "Widget Works")
    with conn:
        add_evidence(conn, "ownership", o["id"], "listed_current", "https://www.acmecap.com/portfolio/", retrieved_at="2026-10-21T08:00:00+00:00",
                     source_type="firm_site", verification="page_verified", content_hash="h", excerpt="Widget Works")
        recompute(conn, o["id"], TODAY, cfg, verified=True)
        conn.execute("DELETE FROM pe_evidence WHERE source_type = 'workbook'")
    import_seed(conn, cfg, d, TODAY)
    assert own(conn, "Acme Capital", "Widget Works")["status"] == "current"


def test_a_company_already_an_account_is_linked_by_any_of_its_names(tmp_path, conn, cfg):
    add_account(conn, "Plain Company")
    conn.commit()
    d = seed(tmp_path, [firm("F01", "Acme Capital")], [company("C001", "Plain Co", aliases="Plain Company")], [rel("R1", "Acme Capital", "Plain Co", company_seed_ref="C001")])
    res = import_seed(conn, cfg, d, TODAY)
    assert res.accounts_linked == 1
    assert conn.execute("SELECT a.name FROM pe_companies c JOIN accounts a ON a.id = c.account_id WHERE c.name = 'Plain Co'").fetchone()[0] == "Plain Company"


def test_peers_are_named_and_one_missing_from_the_competitor_list_is_warned(tmp_path, conn, cfg):
    d = small(tmp_path)
    res = import_seed(conn, cfg, d, TODAY)
    assert res.peers == ["West Monroe"] and res.peers_unlisted == []
    off = cfg.model_copy(deep=True)
    off.fit_gate.competitors.names = []
    assert import_seed(conn, off, d, TODAY).peers_unlisted == ["West Monroe"]


def test_an_ambiguous_company_is_left_out_not_guessed(tmp_path, conn, cfg):
    with conn:
        for n in ("Dup One", "Dup Two"):
            conn.execute("INSERT INTO pe_companies(name, name_key, created) VALUES (?, 'dup', '2026-01-01')", (n,))
    d = seed(tmp_path, [firm("F01", "Acme Capital")], [], [rel("R1", "Acme Capital", "Dup")])
    res = import_seed(conn, cfg, d, TODAY)
    assert res.unresolved == ["Dup"] and res.relationships_added == 0


# ---- investor-only firms are not guessed at -------------------------------------------------------------------------------------------------------
def test_an_investor_only_firm_has_its_website_found_only_when_someone_gives_it(tmp_path, conn, cfg):
    from bd.pe_sites import locate_firm_sites
    from tests.pe_fakes import make_fetcher
    import_seed(conn, cfg, small(tmp_path), TODAY)
    with conn:
        ensure_firm(conn, "Derived Fund", "derived", "sync")                                   # no scope: still looked for, as before
    f, client, *_ = make_fetcher(cfg, {})
    locate_firm_sites(conn, f)
    asked = " ".join(client.calls)
    assert "derivedfund" in asked and "bigfund" not in asked


# ---- the real seed files ---------------------------------------------------------------------------------------------------------------------------
def test_the_real_seed_files_load_cleanly_and_make_only_leads(tmp_path, conn, cfg):
    firms, companies, rels = load_seed(SEED_DIR)
    assert len([f for f in firms if f["scope"] == "chicago"]) == 27 and len(companies) == 36 and {c["seed_ref"] for c in companies if c["kind"] == "peer"} == {"C020", "C028"}
    assert {r["claim"] for r in rels} <= {"listed", "listed_current", "listed_historical", "announced"}
    res = import_seed(conn, cfg, SEED_DIR, TODAY)
    assert res.unresolved == [] and res.peers_unlisted == [] and sorted(res.peers) == ["CXponent", "West Monroe"]
    statuses = {r[0] for r in conn.execute("SELECT status FROM pe_ownership")}
    assert statuses == {"unclear"} and conn.execute("SELECT COUNT(*) FROM pe_ownership WHERE basis != 'workbook_lead'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM pe_evidence WHERE verification != 'snippet_only' OR source_type != 'workbook'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM pe_locations").fetchone()[0] == 0
    g = conn.execute("SELECT * FROM pe_firms WHERE name = 'GTCR'").fetchone()
    assert (g["scope"], g["domain"], g["portfolio_url"]) == ("chicago", "gtcr.com", "https://www.gtcr.com/investments/")
    ccc = own(conn, "GTCR", "CCC Intelligent Solutions")
    assert ccc["status"] == "unclear" and {r["claim"] for r in evidence_for(conn, "ownership", ccc["id"])} == {"announced"}   # an announced take-private is not ownership
    assert conn.execute("SELECT COUNT(*) FROM pe_firms WHERE scope = 'investor'").fetchone()[0] >= 25
    again = import_seed(conn, cfg, SEED_DIR, TODAY)
    assert (again.firms_added, again.companies_added, again.relationships_added, again.evidence_added) == (0, 0, 0, 0)


# ---- the command ---------------------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("BD_DB_PATH", str(tmp_path / "cli.sqlite"))
    return tmp_path


def test_the_command_loads_reports_and_never_fails_soft(env, tmp_path):
    runner = CliRunner()
    r = runner.invoke(app, ["pe-import", "--as-of", "2026-10-21"])
    assert r.exit_code == 0 and r.output.startswith("PE seed: ") and "each a lead until a page confirms it" in r.output and "peer(s) of the firm, never prospects" in r.output
    again = runner.invoke(app, ["pe-import", "--as-of", "2026-10-21"])
    assert "0 new" in again.output and "0 evidence row(s) added" in again.output
    empty = tmp_path / "none"
    empty.mkdir()
    soft = runner.invoke(app, ["pe-import", "--dir", str(empty), "--soft"])
    assert soft.exit_code == 0 and "were not found" in soft.output and "Nothing was loaded" in soft.output
    assert runner.invoke(app, ["pe-import", "--dir", str(empty)]).exit_code != 0
    (empty / "pe_firms.csv").write_text("name\nx\n")
    for name in SEED_FILES[1:]:
        (empty / name).write_text("name\nx\n")
    bad = runner.invoke(app, ["pe-import", "--dir", str(empty), "--soft"])
    assert bad.exit_code == 0 and "was not loaded" in bad.output and "Nothing was changed" in bad.output
    assert runner.invoke(app, ["pe-import", "--dir", str(empty)]).exit_code != 0


def test_the_update_and_the_monday_run_load_the_seed_right_after_the_sync_and_never_stop_on_it():
    from bd.jobs import sequence_for
    for kind, args in (("update", ["update", "--research", "5", "--segments", "3", "--scan", "5", "--budget", "8", "--rfps", "0"]), ("weekly", ["weekly"])):
        steps = sequence_for(kind, args, "2026-10-21")
        names = [s[0] for s in steps]
        assert names.index("pe-import") == names.index("pe-sync") + 1 and "--soft" in steps[names.index("pe-import")]
        assert names.index("pe-import") < names.index("pe-collect")
    assert Path(SEED_DIR / "Dockerfile").exists() and "pe_firms.csv" in (SEED_DIR / "Dockerfile").read_text()           # the container carries the seed files


# ---- the pages -----------------------------------------------------------------------------------------------------------------------------------
def test_the_firm_pages_show_where_a_chicago_firm_sits_and_say_an_investor_is_there_for_a_company(app_env, tmp_path):  # noqa: F811
    e = app_env
    import_seed(e.conn, e.cfg, small(tmp_path), TODAY)
    e.conn.commit()
    ids = {r["name"]: r["id"] for r in e.conn.execute("SELECT id, name FROM pe_firms")}
    listing = e.get("/firms").body
    assert "Acme Capital" in listing and "Chicago" in listing and "Headquartered locally" in listing and "office in Chicago" in listing
    assert "investor in a company on the Chicago list" in listing
    page = e.get(f"/firms/{ids['Acme Capital']}").body
    assert "Headquartered locally" in page and "office in Chicago" in page and "Industrials" in page and "not necessarily the firm's headquarters" in page
    other = e.get(f"/firms/{ids['Big Fund']}").body
    assert "Headquartered locally" not in other
