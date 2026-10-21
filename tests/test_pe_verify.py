"""Checking the workbook's transaction leads against the pages they cite: a lead is confirmed only when one sentence on the fetched page names both the company and
the firm and the ordinary deal rules accept it; otherwise it stays a lead. Pages here are synthetic and live only in these tests."""

from datetime import date

import pytest
from typer.testing import CliRunner

from bd.cli import app
from bd.pe_ownership import add_alias, add_evidence, ensure_ownership, recompute, resolve_company
from bd.pe_store import ensure_firm
from bd.pe_verify import LEAD_RETRY_DAYS, find_sentence, run_verify_leads, waiting
from tests.conftest import make_db
from tests.pe_fakes import make_fetcher, page

TODAY = date(2026, 10, 21)
URL = "https://news.example/alpha-acme"


@pytest.fixture
def cfg(cfg):
    return cfg.model_copy(deep=True)


@pytest.fixture
def conn(tmp_path):
    return make_db(tmp_path)


def lead(conn, cfg, firm, company, url=URL, claim="announced", event="2026-10-01", scope="chicago", domain=None):
    """A workbook relationship: unclear, with the workbook's own snippet as its only evidence."""
    with conn:
        fid = ensure_firm(conn, firm, "manual", "t", domain or f"{firm.split()[0].lower()}.example")[0]
        conn.execute("UPDATE pe_firms SET scope = ? WHERE id = ?", (scope, fid))
        cid = resolve_company(conn, company, None, today=TODAY).id
        oid, _ = ensure_ownership(conn, fid, cid, TODAY, "platform", "manual")
        add_evidence(conn, "ownership", oid, claim, url, retrieved_at="2026-10-10T00:00:00+00:00", source_type="workbook", verification="snippet_only",
                     event=event, excerpt="Workbook row")
        recompute(conn, oid, TODAY, cfg, True)
    return fid, cid, oid


def html(*paragraphs):
    return page("<html><head><title>News</title></head><body>" + "".join(f"<p>{p}</p>" for p in paragraphs) + "</body></html>")


def status(conn, oid):
    return conn.execute("SELECT status FROM pe_ownership WHERE id = ?", (oid,)).fetchone()[0]


def verified(conn, oid):
    return [(r["claim"], r["source_type"]) for r in conn.execute("SELECT claim, source_type FROM pe_evidence WHERE subject_type = 'ownership' AND subject_id = ? "
                                                                  "AND verification = 'page_verified' ORDER BY id", (oid,))]


def run(conn, cfg, routes, **kw):
    fetcher, client, _clock, c = make_fetcher(cfg, routes)
    return run_verify_leads(conn, c, fetcher, TODAY, **kw), client


# ---- confirming ------------------------------------------------------------------------------------------------------------------------------------------
def test_a_page_that_names_both_in_one_sentence_makes_an_announced_lead_pending_with_the_page_as_evidence(conn, cfg):
    _, _, oid = lead(conn, cfg, "Alpha Capital", "Acme Foods")
    assert status(conn, oid) == "unclear"
    res, _ = run(conn, cfg, {URL: html("Alpha Capital to acquire Acme Foods, announced on October 1, 2026, in a deal expected to close in the first quarter.")})
    assert res.confirmed == ["Alpha Capital / Acme Foods: pending"] and status(conn, oid) == "pending"
    assert verified(conn, oid)[0][0] == "announced" and "workbook" not in {s for _, s in verified(conn, oid)}              # the page, not the workbook, is the evidence
    assert conn.execute("SELECT lead_checked FROM pe_ownership WHERE id = ?", (oid,)).fetchone()[0] == TODAY.isoformat()


def test_an_announcement_is_never_taken_for_a_closing(conn, cfg):
    _, _, oid = lead(conn, cfg, "Alpha Capital", "Acme Foods")
    run(conn, cfg, {URL: html("Alpha Capital announces the acquisition of Acme Foods, expected to close once regulators approve it.")})
    assert [c for c, _ in verified(conn, oid)] == ["announced"] and status(conn, oid) == "unclear"          # the page gives no date, so it is not even pending
    run(conn, cfg, {URL: html("Alpha Capital announces the acquisition of Acme Foods on October 1, 2026, expected to close once regulators approve it.")}, limit=1)
    assert "completed" not in [c for c, _ in verified(conn, oid)] and status(conn, oid) != "current"


def test_a_page_that_says_the_deal_closed_makes_it_current_when_the_source_is_the_parties_own_release(conn, cfg):
    url = "https://www.prnewswire.com/news-releases/alpha-acme"
    _, _, oid = lead(conn, cfg, "Alpha Capital", "Acme Foods", url=url)
    res, _ = run(conn, cfg, {url: html("Alpha Capital completes the acquisition of Acme Foods, closing the deal on October 3, 2026.")})
    assert res.confirmed and status(conn, oid) == "current" and "completed" in [c for c, _ in verified(conn, oid)]


# ---- refusing --------------------------------------------------------------------------------------------------------------------------------------------
def test_a_page_that_names_only_the_company_leaves_the_lead_a_lead(conn, cfg):
    _, _, oid = lead(conn, cfg, "Alpha Capital", "Acme Foods")
    res, _ = run(conn, cfg, {URL: html("Acme Foods names a new chief financial officer.", "Beta Partners to acquire Other Co.")})
    assert res.confirmed == [] and res.no_sentence == 1 and status(conn, oid) == "unclear" and verified(conn, oid) == []


def test_the_firm_and_the_company_in_different_sentences_is_not_enough(conn, cfg):
    _, _, oid = lead(conn, cfg, "Alpha Capital", "Acme Foods")
    res, _ = run(conn, cfg, {URL: html("Alpha Capital is a Chicago investor with many holdings and a long history.", "Acme Foods makes snacks and has grown quickly in recent years.")})
    assert res.no_sentence == 1 and status(conn, oid) == "unclear"


def test_a_sentence_that_names_both_but_does_not_say_the_firm_did_a_deal_to_the_company_never_confirms_it(conn, cfg):
    _, _, oid = lead(conn, cfg, "Alpha Capital", "Acme Foods")
    for sentence in ("Acme Foods and Alpha Capital both exhibited at the food industry trade show this autumn.",
                     "Acme Foods, a portfolio company of Alpha Capital, acquires Bravo Brands to extend its snack range, announced October 1, 2026.",
                     "Acme Foods, backed by Alpha Capital, announced today that it has agreed to acquire Bravo Brands on October 1, 2026.",
                     "Alpha Capital portfolio company Acme Foods acquires Bravo Brands in a deal announced October 1, 2026.",
                     "Beta Partners sells Acme Foods to Alpha Capital in a deal announced on October 1, 2026.",
                     "Alpha Capital to acquire Acme Foods's largest rival Bravo Brands, announced October 1, 2026."):
        res, _ = run(conn, cfg, {URL: html(sentence)})
        assert res.confirmed == [] and status(conn, oid) == "unclear" and verified(conn, oid) == [], sentence
        with conn:
            conn.execute("UPDATE pe_ownership SET lead_checked = NULL WHERE id = ?", (oid,))


def test_the_company_first_forms_that_do_say_it_confirm_too(conn, cfg):
    forms = ("{c} to be acquired by Alpha Capital in a deal announced on October 1, 2026.",
             "{c} receives a growth investment from Alpha Capital, announced October 1, 2026.",
             "Alpha Capital invests in {c}, announced on October 1, 2026.")
    for n, form in enumerate(forms):
        company, url = f"Acme Foods {n}", f"{URL}/{n}"
        _, _, oid = lead(conn, cfg, "Alpha Capital", company, url=url)
        res, _ = run(conn, cfg, {url: html(form.format(c=company))})
        assert res.confirmed and status(conn, oid) == "pending", form


def test_an_unreachable_page_changes_nothing_and_is_counted(conn, cfg):
    _, _, oid = lead(conn, cfg, "Alpha Capital", "Acme Foods")
    res, _ = run(conn, cfg, {})                                                            # a 404 for every page
    assert res.unreachable == 1 and res.confirmed == [] and status(conn, oid) == "unclear"


def test_a_plain_listing_lead_is_not_this_steps_business(conn, cfg):
    _, _, oid = lead(conn, cfg, "Alpha Capital", "Acme Foods", claim="listed_current")
    assert waiting(conn, TODAY) == ([], 0)
    res, client = run(conn, cfg, {URL: html("Alpha Capital lists Acme Foods.")})
    assert res.leads == 0 and client.calls == [] and status(conn, oid) == "unclear"


def test_an_investor_firm_that_is_not_active_is_left_out(conn, cfg):
    fid, _, oid = lead(conn, cfg, "Alpha Capital", "Acme Foods")
    conn.execute("UPDATE pe_firms SET status = 'excluded' WHERE id = ?", (fid,))
    conn.commit()
    assert waiting(conn, TODAY) == ([], 0)


# ---- politeness and idempotence -------------------------------------------------------------------------------------------------------------------
def test_a_page_that_cites_two_leads_is_fetched_once(conn, cfg):
    _, _, o1 = lead(conn, cfg, "Alpha Capital", "Acme Foods")
    _, _, o2 = lead(conn, cfg, "Alpha Capital", "Bravo Brands")
    res, client = run(conn, cfg, {URL: html("Alpha Capital to acquire Acme Foods, announced October 1, 2026.", "Alpha Capital to acquire Bravo Brands, announced October 1, 2026.")})
    assert len(res.confirmed) == 2 and client.calls.count(URL) == 1 and status(conn, o1) == status(conn, o2) == "pending"


def test_a_lead_is_not_read_again_until_the_retry_window_passes(conn, cfg):
    lead(conn, cfg, "Alpha Capital", "Acme Foods")
    first, c1 = run(conn, cfg, {URL: html("Acme Foods names a new chief financial officer today and says more will follow soon.")})
    assert first.no_sentence == 1
    again, c2 = run(conn, cfg, {URL: html("Alpha Capital to acquire Acme Foods, announced October 1, 2026.")})
    assert again.leads == 0 and again.recent == 1 and c2.calls == []                       # read within the window: left alone
    due, left = waiting(conn, date(2026, 10, 21 + LEAD_RETRY_DAYS + 1) if LEAD_RETRY_DAYS < 10 else date(2026, 11, 30))
    assert len(due) == 1 and left == 0


def test_a_run_can_be_limited_and_chicago_firms_come_first(conn, cfg):
    lead(conn, cfg, "Zeta Fund", "Zed Co", url="https://news.example/z", scope="investor")
    lead(conn, cfg, "Alpha Capital", "Acme Foods")
    res, client = run(conn, cfg, {URL: html("Alpha Capital to acquire Acme Foods, announced October 1, 2026.")}, limit=1)
    assert res.leads == 1 and client.calls == [URL]                                         # the Chicago firm's lead, not the investor's


# ---- finding the sentence ------------------------------------------------------------------------------------------------------------------------------
def test_a_company_is_found_by_any_name_it_is_known_by_and_a_short_name_is_never_matched_inside_a_word(conn, cfg):
    _, cid, _ = lead(conn, cfg, "Alpha Capital", "Acme Foods Inc")
    with conn:
        add_alias(conn, "company", cid, "Acme Brands")
    from bd.pe_verify import _keys
    keys = _keys(conn, cid, "Acme Foods Inc")
    s, written = find_sentence("Alpha Capital agreed to buy Acme Brands in a deal announced today by both companies.", keys, "Alpha Capital")
    assert written == "Acme Brands" and "Acme Brands" in s
    assert find_sentence("Hg said the deal, though large, was unusual for the whole market this year.", [("hg", "Hg")], "Hg Capital") is None
    assert find_sentence("Alpha Capital buys Hg for a reported amount this week in the market.", [("hg", "Hg")], "Alpha Capital") is not None


# ---- the command -----------------------------------------------------------------------------------------------------------------------------------------
def test_the_command_is_soft_without_a_user_agent_and_reports_the_count_with_one(tmp_path, monkeypatch):
    monkeypatch.setenv("BD_DB_PATH", str(tmp_path / "cli.sqlite"))
    monkeypatch.delenv("BD_USER_AGENT", raising=False)
    runner = CliRunner()
    soft = runner.invoke(app, ["pe-verify-leads", "--soft"])
    assert soft.exit_code == 0 and "BD_USER_AGENT" in soft.output
    assert runner.invoke(app, ["pe-verify-leads"]).exit_code != 0
    monkeypatch.setenv("BD_USER_AGENT", "SEC Test ops@example.com")
    ok = runner.invoke(app, ["pe-verify-leads", "--as-of", "2026-10-21"])
    assert ok.exit_code == 0 and "No transaction lead is waiting for a page check" in ok.output


def test_the_update_and_the_monday_run_check_leads_for_free_and_softly_right_after_the_portfolio_pages():
    from bd.jobs import sequence_for
    for kind, args in (("update", ["update", "--research", "5", "--segments", "3", "--scan", "5", "--budget", "8", "--rfps", "0", "--pe-budget", "5"]), ("weekly", ["weekly"])):
        names = [s[0] for s in sequence_for(kind, args, "2026-10-21")]
        steps = sequence_for(kind, args, "2026-10-21")
        assert names.index("pe-verify-leads") == names.index("pe-collect") + 1 and "--soft" in steps[names.index("pe-verify-leads")]
