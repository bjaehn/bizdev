import json
import threading
from datetime import date, timedelta

import pytest

from bd.accounts import list_accounts
from bd.llm import LLMResult, Usage
from bd.models import ScoringSignal
from bd.pe_firms import FirmScanner, PortfolioFinder, run_firm_scans, run_portfolio, select_firms
from bd.pe_store import (add_firm, ensure_firm, find_firm, firm_signals_for_account, link_company, norm_name,
                         set_firm_status, store_firm_signals, sync_from_accounts)
from bd.score_store import score_one
from bd.scoring import score_account, sponsor_lift
from bd.urls import normalize_url
from tests.conftest import AS_OF, FakeVerifier, add_account, add_signal, make_db
from tests.test_discover import co, county_facts

FIRM_PAGE = "https://examplecapital.com/portfolio"
NEWS = "https://examplecapital.com/news/fund-iv"


@pytest.fixture
def cfg(cfg):
    return cfg.model_copy(deep=True)


@pytest.fixture
def conn(tmp_path):
    return make_db(tmp_path)


class Router:
    """Thread-safe fake: firm scans, portfolio calls and enrich calls get their own canned replies."""

    def __init__(self, firm_signals=None, portfolio=None, facts=None, urls=()):
        self.fs, self.pf, self.facts = firm_signals or [], portfolio or {}, facts or {}
        self.urls = {normalize_url(u) for u in urls}
        self.lock, self.calls = threading.Lock(), []

    def run(self, system, messages, **kw):
        user = messages[-1]["content"]
        with self.lock:
            self.calls.append(user)
        if user.startswith("Firm:") and "Portfolio page" in user:
            body = self.pf
        elif user.startswith("Firm:"):
            body = {"signals": self.fs}
        else:
            body = {"facts": self.facts[user.split("Company: ")[1].splitlines()[0]]}
        text = json.dumps(body)
        return LLMResult(text=text, retrieved_urls=set(self.urls), usage=Usage(input_tokens=100, output_tokens=50, web_searches=1),
                         conversation=list(messages) + [{"role": "assistant", "content": text}])


def fsig(type_="firm_new_fund", age=10, url=NEWS, st="company_release", conf="High"):
    return {"type": type_, "event_date": (AS_OF - timedelta(days=age)).isoformat(), "summary": "Example Capital closed Fund IV.",
            "source_url": url, "source_type": st, "confidence": conf}


# ---- names, firms, sync ---------------------------------------------------------------------------------------------
def test_firm_names_compare_without_case_punctuation_or_legal_suffixes():
    assert norm_name("Example Capital, L.P.") == norm_name("EXAMPLE CAPITAL LP") == norm_name("The Example Capital")
    assert norm_name("A&B Partners") == norm_name("A & B Partners")


def test_sync_derives_firms_and_links_from_accounts_and_marks_exits(conn):
    a = add_account(conn, "Pecoco", ownership="pe-backed", sponsor="Example Capital LP")
    b = add_account(conn, "Otherco", ownership="pe-backed", sponsor="example capital")
    add_account(conn, "Publicco", ownership="public", sponsor="Ignored Capital")
    r = sync_from_accounts(conn)
    assert (r.firms_added, r.links_added, r.links_exited) == (1, 2, 0)
    assert conn.execute("SELECT COUNT(*) FROM pe_firms").fetchone()[0] == 1
    assert sync_from_accounts(conn).links_added == 0                       # idempotent
    conn.execute("UPDATE accounts SET sponsor = 'Another Fund' WHERE id = ?", (b,))
    r = sync_from_accounts(conn)
    assert r.firms_added == 1 and r.links_exited == 1
    st = {x["company_name"]: x["status"] for x in conn.execute("SELECT * FROM portfolio_links")}
    assert st["Otherco"] in ("exited", "current") and st["Pecoco"] == "current"
    fid = find_firm(conn, "Example Capital")["id"]
    assert conn.execute("SELECT status FROM portfolio_links WHERE firm_id = ? AND account_id = ?", (fid, b)).fetchone()["status"] == "exited"
    assert a


def test_add_and_exclude_firm_validate_input(conn):
    assert add_firm(conn, "Example Capital", "examplecapital.com", "Chicago", "https://examplecapital.com/portfolio") == []
    assert add_firm(conn, "example capital") == ["that firm already exists"]
    assert add_firm(conn, "", "not a host!") != [] and add_firm(conn, "X", portfolio_url="javascript:alert(1)") != []
    fid = find_firm(conn, "Example Capital")["id"]
    assert set_firm_status(conn, fid, "excluded") and not set_firm_status(conn, fid, "bogus") and not set_firm_status(conn, 999, "active")
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE field='pe_firm_status'").fetchone()[0] == 1


# ---- scoring lift -----------------------------------------------------------------------------------------------------
def ss(type_, age, conf="High"):
    return ScoringSignal(type=type_, event_date=AS_OF - timedelta(days=age), confidence=conf, ref=1)


def test_lift_counts_strongest_fully_decays_the_rest_and_is_capped(cfg):
    one, rows = sponsor_lift([(ss("firm_new_fund", 5), "F")], AS_OF, cfg)
    assert one == 8 and rows[0]["firm"] == "F"
    cfg.scoring.sponsor.lift_max = 100                                       # look at the uncapped arithmetic first
    two, _ = sponsor_lift([(ss("firm_new_fund", 5), "F"), (ss("firm_operating_partner", 5), "F")], AS_OF, cfg)
    assert two == 8 + 8 * cfg.scoring.within_family_decay
    cfg.scoring.sponsor.lift_max = 10
    capped, _ = sponsor_lift([(ss("firm_new_fund", 5), "F"), (ss("firm_operating_partner", 5), "F"),
                              (ss("firm_exit", 5), "G"), (ss("firm_new_fund", 6), "G")], AS_OF, cfg)
    assert capped == cfg.scoring.sponsor.lift_max
    assert sponsor_lift([(ss("firm_new_fund", 5, "Low"), "F"), (ss("firm_new_fund", 400), "F")], AS_OF, cfg)[0] == 0


def test_lift_never_creates_a_lead_and_is_shown_separately(cfg):
    own = [ScoringSignal("leadership_change", AS_OF - timedelta(days=5), "High", ("Strategy & Operations",), 1)]
    base = score_account(own, AS_OF, cfg)
    lifted = score_account(own, AS_OF, cfg, [(ss("firm_new_fund", 5), "Example Capital")])
    assert lifted.score > base.score and lifted.sponsor_lift == 8
    assert "sponsor activity" in lifted.rationale and lifted.components["sponsor"]["signals"][0]["firm"] == "Example Capital"
    alone = score_account([], AS_OF, cfg, [(ss("firm_new_fund", 5), "Example Capital")])
    assert alone.score == 0 and alone.sponsor_lift == 0                    # no own signal, no lift
    assert score_account(own, AS_OF, cfg).components["sponsor"]["lift"] == 0


def test_lift_reaches_only_current_links_of_active_firms(cfg, conn):
    a = add_account(conn, "Pecoco", ownership="pe-backed", sponsor="Example Capital")
    add_signal(conn, cfg, a, "leadership_change", 5)
    sync_from_accounts(conn)
    fid = find_firm(conn, "Example Capital")["id"]
    from bd.pe_firms import AcceptedFirmSignal
    from bd.models import FirmSignalExtract
    ex = FirmSignalExtract(**fsig())
    with conn:
        store_firm_signals(conn, fid, [AcceptedFirmSignal(ex, "h1")], AS_OF)
    acct = list_accounts(conn)[0]
    assert score_one(conn, cfg, acct, AS_OF).result.sponsor_lift == 8
    set_firm_status(conn, fid, "excluded")
    assert score_one(conn, cfg, acct, AS_OF).result.sponsor_lift == 0
    set_firm_status(conn, fid, "active")
    conn.execute("UPDATE portfolio_links SET status = 'exited'")
    assert firm_signals_for_account(conn, a) == []


# ---- firm scanner ------------------------------------------------------------------------------------------------------
def firm(conn, name="Example Capital", domain="examplecapital.com"):
    with conn:
        fid, _ = ensure_firm(conn, name, "manual", "t", domain)
    return conn.execute("SELECT * FROM pe_firms WHERE id = ?", (fid,)).fetchone()


def scan(cfg, conn, llm, bad=None):
    f = firm(conn)
    return FirmScanner(cfg, llm, FakeVerifier(bad)).scan(f, AS_OF)


def test_firm_signal_acceptance_rules(cfg, conn):
    ok = scan(cfg, conn, Router(firm_signals=[fsig()], urls=[NEWS]))
    assert len(ok.signals) == 1 and ok.signals[0].extract.source_type == "company_release"
    for bad_sig, urls, bad in [
        (fsig(url="https://www.linkedin.com/posts/x"), ["https://www.linkedin.com/posts/x"], None),
        (fsig(age=400), [NEWS], None),
        (fsig(), [], None),                                           # URL never returned by the search
        (fsig(), [NEWS], {NEWS: "404"})]:
        assert scan(cfg, conn, Router(firm_signals=[bad_sig], urls=urls), bad).signals == []


def test_firm_signal_source_type_and_confidence_are_reconciled(cfg, conn):
    other = "https://news.example/example-capital"
    r = scan(cfg, conn, Router(firm_signals=[fsig(url=other, st="company_release", conf="High")], urls=[other]))
    s = r.signals[0].extract
    assert s.source_type == "secondary_report" and s.confidence == "Medium"      # claimed own release on someone else's domain


def test_run_firm_scans_stores_logs_cost_and_stamps_last_scanned(cfg, conn):
    firm(conn)
    llm = Router(firm_signals=[fsig()], urls=[NEWS])
    s = run_firm_scans(conn, cfg, FirmScanner(cfg, llm, FakeVerifier()), select_firms(conn, None, None), AS_OF, None, workers=1)
    assert s.new_signals == 1 and s.firms == 1
    assert conn.execute("SELECT last_scanned FROM pe_firms").fetchone()[0] == AS_OF.isoformat()
    r = conn.execute("SELECT step, account_id, tokens FROM runs").fetchone()
    assert (r["step"], r["account_id"], r["tokens"]) == ("pe_firm", None, 150)
    s2 = run_firm_scans(conn, cfg, FirmScanner(cfg, llm, FakeVerifier()), select_firms(conn, None, None), AS_OF, None, workers=1)
    assert s2.new_signals == 0                                                    # same event is not stored twice


def test_unscanned_firms_come_first(cfg, conn):
    firm(conn, "A Capital", "a.com"), firm(conn, "B Capital", "b.com")
    conn.execute("UPDATE pe_firms SET last_scanned = '2026-01-01' WHERE name = 'A Capital'")
    assert [f["name"] for f in select_firms(conn, None, None)] == ["B Capital", "A Capital"]
    assert [f["name"] for f in select_firms(conn, "a cap", None)] == ["A Capital"]


# ---- portfolio research -----------------------------------------------------------------------------------------------
def pcompany(name, domain, exited=False, url=FIRM_PAGE, st="company_site"):
    return {**co(name, domain, url), "source_type": st, "exited": exited}


def portfolio(cfg, conn, companies, facts, extra_urls=(), bad=None):
    f = firm(conn, domain=None)
    llm = Router(portfolio={"firm_domain": "examplecapital.com", "portfolio": companies}, facts=facts,
                 urls=[FIRM_PAGE, *extra_urls])
    v = FakeVerifier(bad)
    from bd.enrich import Enricher
    s = run_portfolio(conn, cfg, PortfolioFinder(cfg, llm, v), Enricher(cfg, llm, v, None), [f], AS_OF, None, workers=1)
    return s, llm


def test_new_portfolio_company_is_researched_added_and_linked(cfg, conn):
    s, _ = portfolio(cfg, conn, [pcompany("Newco", "newco.com")], {"Newco": county_facts("Newco", "newco.com")},
                     ["https://newco.com", "https://newco.com/about"])
    assert s.added == ["Newco"]
    a = list_accounts(conn, status=None)[0]
    link = conn.execute("SELECT * FROM portfolio_links").fetchone()
    assert link["account_id"] == a.id and link["status"] == "current" and link["origin"] == "portfolio" and link["source_url"] == FIRM_PAGE
    assert conn.execute("SELECT domain FROM pe_firms").fetchone()[0] == "examplecapital.com"     # firm domain learned and verified
    steps = [r["step"] for r in conn.execute("SELECT step FROM runs ORDER BY id")]
    assert steps == ["pe_portfolio", "discover_enrich"]


def test_existing_account_is_only_linked_without_research_cost(cfg, conn):
    a = add_account(conn, "Existing Co", domain="existing.com")
    s, llm = portfolio(cfg, conn, [pcompany("existing co", "other.com")], {})
    assert s.researched == 0 and len(llm.calls) == 1
    assert conn.execute("SELECT account_id FROM portfolio_links").fetchone()[0] == a


def test_listing_on_a_third_party_site_is_researched_but_not_linked(cfg, conn):
    third = "https://news.example/example-capital-portfolio"
    s, _ = portfolio(cfg, conn, [pcompany("Newco", "newco.com", url=third, st="secondary_report")],
                     {"Newco": county_facts("Newco", "newco.com")}, [third, "https://newco.com", "https://newco.com/about"])
    assert s.added == ["Newco"] and conn.execute("SELECT COUNT(*) FROM portfolio_links").fetchone()[0] == 0


def test_portfolio_source_must_be_retrieved_allowed_and_reachable(cfg, conn):
    cases = [pcompany("A Co", "a.com", url="https://never.example/x"), pcompany("B Co", "b.com", url="https://www.linkedin.com/c/b"),
             pcompany("C Co", "c.com", url="https://dead.example/c")]
    s, _ = portfolio(cfg, conn, cases, {}, ["https://www.linkedin.com/c/b", "https://dead.example/c"], bad={"https://dead.example/c": "404"})
    assert s.skipped_source == 3 and s.researched == 0


def test_exited_company_gets_an_exited_link_and_never_lifts(cfg, conn):
    s, _ = portfolio(cfg, conn, [pcompany("Soldco", "soldco.com", exited=True)], {"Soldco": county_facts("Soldco", "soldco.com")},
                     ["https://soldco.com", "https://soldco.com/about"])
    assert conn.execute("SELECT status FROM portfolio_links").fetchone()[0] == "exited"


def test_queued_company_is_linked_when_a_person_accepts_it(cfg, conn):
    f = county_facts("Mystery Co", "mystery.com", revenue=None, employees=None)
    f["evidence"].pop("revenue_usd"), f["evidence"].pop("employees")
    portfolio(cfg, conn, [pcompany("Mystery Co", "mystery.com")], {"Mystery Co": f}, ["https://mystery.com", "https://mystery.com/about"])
    link = conn.execute("SELECT * FROM portfolio_links").fetchone()
    assert link["account_id"] is None
    from bd.curate import accept_candidate
    cid = conn.execute("SELECT id FROM candidates").fetchone()[0]
    res = accept_candidate(conn, cfg, cid)
    assert res.ok and conn.execute("SELECT account_id FROM portfolio_links").fetchone()[0] == res.account_id


# ---- app ---------------------------------------------------------------------------------------------------------------
def test_firms_pages_and_sponsor_lift_are_visible_in_the_app(cfg, tmp_path):
    from tests.test_hosted import PW, call
    from bd.jobs import JobRunner
    from bd.ui.app import App
    from bd.ui.auth import Hosted
    c = make_db(tmp_path)
    a = add_account(c, "Pecoco", ownership="pe-backed", sponsor="Example Capital")
    sync_from_accounts(c)
    fid = find_firm(c, "Example Capital")["id"]
    app = App(cfg, str(tmp_path / "t.sqlite"), root=tmp_path, hosted=Hosted(PW), jobs=JobRunner(str(tmp_path / "t.sqlite"), tmp_path, ["true"]))
    app.fail_delay = 0
    cookie = call(app, "POST", "/login", {"password": PW}).h["Set-Cookie"].split(";")[0].split("=", 1)[1]
    csrf = app._csrf
    assert "Example Capital" in call(app, "GET", "/firms", cookie=cookie).body
    page = call(app, "GET", f"/firms/{fid}", cookie=cookie).body
    assert "Pecoco" in page and "No firm signals stored yet" in page
    assert "Private equity owner" in call(app, "GET", f"/accounts/{a}", cookie=cookie).body
    assert call(app, "POST", "/firms/add", {"csrf": csrf, "name": "New Fund Partners"}, cookie=cookie).code == 303
    bad = call(app, "POST", "/firms/add", {"csrf": csrf, "name": "X", "domain": "not a host!"}, cookie=cookie)
    assert bad.code == 200 and "Not saved" in bad.body
    assert call(app, "POST", f"/firms/{fid}/status", {"csrf": csrf, "status": "excluded"}, cookie=cookie).code == 303
    assert "excluded" in call(app, "GET", f"/firms/{fid}", cookie=cookie).body
    assert call(app, "POST", "/firms/add", {"name": "NoCsrf"}, cookie=cookie).code == 403
    assert call(app, "GET", "/firms/999", cookie=cookie).code == 404
    ops = call(app, "GET", "/operate", cookie=cookie).body
    assert "Portfolio research" in ops and "Firm signals" in ops


def test_pe_jobs_validate_and_pe_sync_needs_no_confirmation():
    from bd.jobs import JobError, sequence_for, validate
    assert validate("pe_sync", {}) == ["pe-sync"]
    assert validate("pe_portfolio", {"limit": "5"}) == ["pe-portfolio", "--limit", "5"]
    assert validate("pe_signals", {"limit": "50"}) == ["pe-signals", "--limit", "50"]
    for kind in ("pe_portfolio", "pe_signals"):
        for bad in ({}, {"limit": "0"}, {"limit": "51"}):
            with pytest.raises(JobError):
                validate(kind, bad)
    assert sequence_for("pe_signals", ["pe-signals", "--limit", "5"], "2026-01-05") == [["pe-signals", "--limit", "5", "--as-of", "2026-01-05"]]
    assert [s[0] for s in sequence_for("weekly", [], "2026-01-05")] == ["pe-sync", "pe-import", "pe-collect", "ats-find", "scan", "pe-signals", "rfps", "score", "card"]
    assert sequence_for("weekly", [], "2026-01-05")[1] == ["pe-import", "--soft", "--as-of", "2026-01-05"]
    assert sequence_for("weekly", [], "2026-01-05")[2] == ["pe-collect", "--soft", "--as-of", "2026-01-05"]


# ---- a sponsor string that is really several firms: explicit, never guessed -----------------------------------------------------------------------
SPLIT = {"Charlesbank Capital Partners and Partners Group": ["Charlesbank Capital Partners", "Partners Group"]}
COMBINED = "Charlesbank Capital Partners and Partners Group"


def test_a_listed_combined_sponsor_becomes_each_firm_with_its_own_relationship_and_the_combined_firm_goes_away(conn):
    a = add_account(conn, "Sharedco", ownership="pe-backed", sponsor=COMBINED)
    add_account(conn, "Soloco", ownership="pe-backed", sponsor="Hellman & Friedman")
    sync_from_accounts(conn, splits={})                                         # before the mapping existed: one firm called by the whole string
    assert {r["name"] for r in conn.execute("SELECT name FROM pe_firms")} == {COMBINED, "Hellman & Friedman"}
    sync_from_accounts(conn, splits=SPLIT)
    assert {r["name"] for r in conn.execute("SELECT name FROM pe_firms")} == {"Charlesbank Capital Partners", "Partners Group", "Hellman & Friedman"}   # an unlisted name is never split
    own = conn.execute("SELECT f.name, o.status, o.basis FROM pe_ownership o JOIN pe_firms f ON f.id = o.firm_id JOIN pe_companies c ON c.id = o.company_id WHERE c.name = 'Sharedco' ORDER BY f.name").fetchall()
    assert [tuple(r) for r in own] == [("Charlesbank Capital Partners", "unclear", "legacy_unverified"), ("Partners Group", "unclear", "legacy_unverified")]
    assert conn.execute("SELECT COUNT(*) FROM pe_companies WHERE name = 'Sharedco'").fetchone()[0] == 1            # one company, two sponsors
    assert conn.execute("SELECT COUNT(*) FROM portfolio_links l JOIN pe_firms f ON f.id = l.firm_id WHERE f.name LIKE '%and Partners Group'").fetchone()[0] == 0
    n = conn.execute("SELECT COUNT(*) FROM pe_ownership").fetchone()[0]
    sync_from_accounts(conn, splits=SPLIT)
    assert conn.execute("SELECT COUNT(*) FROM pe_ownership").fetchone()[0] == n and conn.execute("SELECT COUNT(*) FROM pe_firms").fetchone()[0] == 3     # idempotent
    assert a


def test_a_combined_firm_with_evidence_read_from_a_page_is_excluded_not_deleted(conn):
    from bd.pe_ownership import add_evidence, ensure_ownership, resolve_company
    add_account(conn, "Sharedco", ownership="pe-backed", sponsor=COMBINED)
    sync_from_accounts(conn, splits={})
    fid = find_firm(conn, COMBINED)["id"]
    with conn:
        m = resolve_company(conn, "Readco", "readco.example", today=date(2026, 10, 20))
        oid, _ = ensure_ownership(conn, fid, m.id, date(2026, 10, 20), "unspecified", "portfolio_page")
        add_evidence(conn, "ownership", oid, "listed_current", "https://x.example/p", retrieved_at="2026-10-20T00:00:00+00:00", source_type="firm_site",
                     verification="page_verified", content_hash="h", excerpt="Readco")
    sync_from_accounts(conn, splits=SPLIT)
    assert find_firm(conn, COMBINED)["status"] == "excluded"
    assert conn.execute("SELECT COUNT(*) FROM pe_ownership WHERE firm_id = ?", (fid,)).fetchone()[0] >= 1       # what was read is kept


def test_the_repository_config_lists_the_split_it_was_given_and_loads(cfg):
    assert cfg.pe_portfolio.sponsor_splits == SPLIT
    from bd.pe_store import split_sponsor
    assert split_sponsor("charlesbank capital partners  AND Partners Group", SPLIT) == ["Charlesbank Capital Partners", "Partners Group"]
    assert split_sponsor("Hellman & Friedman", SPLIT) == ["Hellman & Friedman"]
