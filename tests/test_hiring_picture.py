"""Hiring depth: programs named in job titles (read from the whole board feed), stored readings of the board, and the picture a company page shows."""

from datetime import date, timedelta
from html import unescape

import pytest

from bd.accounts import list_accounts
from bd.ats import AtsError, programs_in
from bd.hiring_picture import hiring_picture
from bd.labels import fmt_date
from bd.postings import BoardReading, store_board_reading
from bd.runner import run_scans
from bd.scanners import AtsScanner
from tests.conftest import AS_OF, add_account
from tests.test_ats_refresh import FakeAts, acct, conn, d, rp  # noqa: F401  (fixture and helpers)
from tests.test_update import app_env  # noqa: F401  (fixture)


@pytest.fixture
def cfg(cfg):
    return cfg.model_copy(deep=True)


def test_a_title_names_a_program_by_whole_words_in_config_order(cfg):
    assert programs_in("SAP S/4HANA Data Migration Analyst", cfg) == ["SAP"]
    assert programs_in("Workday HCM and Salesforce Integration Lead", cfg) == ["Workday", "Salesforce"]
    assert programs_in("Senior ERP Business Analyst", cfg) == ["ERP"]
    assert programs_in("Sapphire Security Guard", cfg) == [] and programs_in("Pharmacist Assistant", cfg) == []       # "sap" inside another word is not SAP


def test_programs_are_read_from_the_whole_board_not_only_the_titles_that_match_a_theme(cfg):
    feed = [rp("SAP Finance Systems Lead", 30, 1), rp("SAP Basis Administrator", 20, 2), rp("SAP S/4HANA Data Migration Analyst", 10, 3),
            rp("Workday HCM Lead", 5, 4), rp("Accountant", 3, 5), rp("Data Engineer", 200, 6)]
    out = AtsScanner(cfg, FakeAts(feed)).scan(acct(), AS_OF)
    assert out.board.total == 6 and out.board.relevant == 1                    # only "Data Engineer" matches a theme, but all six open roles are counted
    assert sorted(out.board.programs) == ["SAP", "Workday"] and len(out.board.programs["SAP"]) == 3 and len(out.board.programs["Workday"]) == 1
    (sap,) = [s for s in out.signals if "SAP" in s.extract.summary]
    e = sap.extract
    assert e.type == "hiring_cluster" and sap.family == "hiring" and e.confidence == "High" and e.source_type == "company_site" and e.service_lines == ["AI & Technology"]
    assert e.event_date == d(10) and e.source_url.endswith("/jobs/3")           # dated by the posting that completed the cluster, so the date holds from week to week
    assert e.summary == "3 job postings name SAP in their titles within 60 days, including SAP S/4HANA Data Migration Analyst." and len(e.summary.split()) <= 25
    assert not [s for s in out.signals if "Workday" in s.extract.summary]       # one posting is not a cluster


def test_two_postings_are_not_a_program_and_old_or_foreign_postings_do_not_count(cfg):
    feed = [rp("SAP Lead", 5, 1), rp("SAP Analyst", 6, 2), rp("SAP Architect", 7, 3, host="evil.example"), rp("SAP Director", 300, 4)]
    out = AtsScanner(cfg, FakeAts(feed)).scan(acct(), AS_OF)
    assert out.board.total == 4 and [p[0] for p in out.board.programs["SAP"]] == ["SAP Lead", "SAP Analyst"]
    assert not [s for s in out.signals if "SAP" in s.extract.summary]


def test_the_biggest_programs_make_signals_up_to_the_limit(cfg):
    feed = ([rp(f"SAP Analyst {i}", 5 + i, i) for i in range(5)] + [rp(f"Workday Analyst {i}", 5 + i, 10 + i) for i in range(4)]
            + [rp(f"Salesforce Admin {i}", 5 + i, 20 + i) for i in range(3)] + [rp(f"ServiceNow Dev {i}", 5 + i, 30 + i) for i in range(3)])
    out = AtsScanner(cfg, FakeAts(feed)).scan(acct(), AS_OF)
    named = sorted(next(n for n in ("SAP", "Workday", "Salesforce", "ServiceNow") if n in s.extract.summary) for s in out.signals if "name " in s.extract.summary)
    assert named == ["SAP", "Salesforce", "Workday"] and cfg.hiring.program_signals_max == 3           # ServiceNow ties Salesforce, and a name decides the tie
    assert len({s.hash for s in out.signals}) == len(out.signals)


def test_programs_can_be_switched_off_by_emptying_the_list(cfg):
    cfg.hiring.programs = {}
    out = AtsScanner(cfg, FakeAts([rp(f"SAP Analyst {i}", 5 + i, i) for i in range(4)])).scan(acct(), AS_OF)
    assert out.board.programs == {} and not [s for s in out.signals if "SAP" in s.extract.summary]


def test_config_rejects_program_service_lines_that_are_not_service_lines(cfg):
    from bd.config import Config
    raw = cfg.model_dump(mode="json")
    raw["hiring"]["program_service_lines"] = ["Nope"]
    with pytest.raises(Exception, match="program_service_lines"):
        Config.model_validate(raw)


# ---- readings are stored, one per day, and only from a board that answered -------------------------------------------------------------------
def test_the_runner_keeps_one_reading_a_day_and_a_failed_fetch_keeps_none(cfg, conn):
    add_account(conn, "Acme", ats_provider="greenhouse", ats_board="acme", domain="acme.com")
    run_scans(conn, cfg, [AtsScanner(cfg, FakeAts([rp("Data Engineer", 5, 1), rp("Accountant", 4, 2)]))], list_accounts(conn), AS_OF, None, workers=1)
    run_scans(conn, cfg, [AtsScanner(cfg, FakeAts([rp("Data Engineer", 5, 1), rp("Accountant", 4, 2), rp("Nurse", 3, 3)]))], list_accounts(conn), AS_OF, None, workers=1, force=True)
    rows = conn.execute("SELECT seen_date, total, relevant FROM board_readings").fetchall()
    assert [tuple(r) for r in rows] == [(AS_OF.isoformat(), 3, 1)]              # the second read of the same day replaced the first
    run_scans(conn, cfg, [AtsScanner(cfg, FakeAts(error=AtsError("HTTP 503")))], list_accounts(conn), AS_OF + timedelta(days=1), None, workers=1)
    assert conn.execute("SELECT COUNT(*) FROM board_readings").fetchone()[0] == 1
    with conn:
        store_board_reading(conn, 1, AS_OF + timedelta(days=500), BoardReading(1, 0))
    assert [r[0] for r in conn.execute("SELECT seen_date FROM board_readings")] == [(AS_OF + timedelta(days=500)).isoformat()]      # a year-old reading is dropped


# ---- the picture ----------------------------------------------------------------------------------------------------------------------------------
def test_no_board_and_a_board_not_read_yet_each_say_so_plainly(cfg, conn):
    a = add_account(conn, "Boardless", domain="b.com")
    p = hiring_picture(conn, cfg, a, AS_OF)
    assert not p.has_board and "No job board of this company's own has been found" in p.note and p.total is None
    conn.execute("UPDATE accounts SET ats_provider = 'lever', ats_board = 'b' WHERE id = ?", (a,))
    p = hiring_picture(conn, cfg, a, AS_OF)
    assert p.has_board and p.total is None and "known but has not been read yet" in p.note


def test_the_picture_states_counts_the_change_since_four_weeks_ago_themes_and_programs(cfg, conn):
    a = add_account(conn, "Acme", ats_provider="greenhouse", ats_board="acme", domain="acme.com")
    with conn:
        store_board_reading(conn, a, AS_OF - timedelta(days=28), BoardReading(34, 4))
        store_board_reading(conn, a, AS_OF - timedelta(days=9), BoardReading(40, 5))                  # too recent to be "four weeks ago": not used
    feed = ([rp("VP, Business Transformation", 3, 1), rp("Senior Data Engineer", 12, 2), rp("Director of Data Platform", 20, 3), rp("Machine Learning Engineer", 40, 4)]
            + [rp(f"SAP Analyst {i}", 8 + i, 10 + i) for i in range(3)] + [rp(f"Warehouse Associate {i}", 2, 20 + i) for i in range(40)])
    run_scans(conn, cfg, [AtsScanner(cfg, FakeAts(feed))], list_accounts(conn), AS_OF, None, workers=1)
    p = hiring_picture(conn, cfg, a, AS_OF)
    assert (p.total, p.relevant, p.recent) == (47, 4, 3) and p.change == 13 and p.change_text == f"Up 13 from 34 on {fmt_date(AS_OF - timedelta(days=28))}."
    assert p.read_date == AS_OF and p.provider == "greenhouse"
    themes = {label: (n, senior) for label, n, senior in p.themes}
    assert themes == {"transformation and change": (1, 1), "data and analytics": (2, 1), "AI and technology": (1, 0)}
    assert [(g.name, g.n) for g in p.programs] == [("SAP", 3)] and p.programs[0].examples[0]["title"].startswith("SAP Analyst")


def test_without_a_reading_three_to_six_weeks_older_the_picture_makes_no_claim_about_change(cfg, conn):
    a = add_account(conn, "Acme", ats_provider="greenhouse", ats_board="acme", domain="acme.com")
    with conn:
        store_board_reading(conn, a, AS_OF, BoardReading(10, 2))
    p = hiring_picture(conn, cfg, a, AS_OF)
    assert p.change is None and "shows from a later reading" in p.change_text
    with conn:
        store_board_reading(conn, a, AS_OF - timedelta(days=30), BoardReading(10, 2))
    assert hiring_picture(conn, cfg, a, AS_OF).change_text == f"Unchanged since {fmt_date(AS_OF - timedelta(days=30))} (10)."
    with conn:
        store_board_reading(conn, a, AS_OF - timedelta(days=30), BoardReading(25, 2))
    assert hiring_picture(conn, cfg, a, AS_OF).change_text.startswith("Down 15 from 25 on ")


# ---- the page ---------------------------------------------------------------------------------------------------------------------------------------
def test_the_company_page_shows_the_hiring_picture_or_says_there_is_no_board(app_env):
    e = app_env
    today = date.today()
    a = add_account(e.conn, "Acme Co", ats_provider="greenhouse", ats_board="acme", domain="acme.com")
    b = add_account(e.conn, "Boardless Co", domain="boardless.com")
    sap = [("SAP Lead", "https://boards.greenhouse.io/acme/jobs/1", today.isoformat()), ("SAP Analyst", "https://boards.greenhouse.io/acme/jobs/2", today.isoformat()),
           ("SAP <b>x</b>", "https://boards.greenhouse.io/acme/jobs/3", today.isoformat())]
    with e.conn:
        store_board_reading(e.conn, a, today, BoardReading(31, 3, {"SAP": sap}))
    page = unescape(e.get(f"/companies/{a}").body)
    assert "Hiring picture" in page and "31" in page and "open roles on the company's own Greenhouse board" in page
    assert "Programs named in job titles" in page and "SAP Lead" in page and "3 postings dated in the last 60 days" in page and "<b>x</b>" not in e.get(f"/companies/{a}").body      # titles are escaped
    none = unescape(e.get(f"/companies/{b}").body)
    assert "Hiring picture" in none and "No job board of this company's own has been found" in none
