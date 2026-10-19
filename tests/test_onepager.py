from datetime import timedelta

import pytest

from bd.score_store import score_accounts
from bd.timeline import FAMILY_COLORS, legend, timeline_svg
from tests.conftest import AS_OF, add_account, add_signal, make_db
from tests.test_update import app_env  # noqa: F401  (fixture)


def s(age, family="leadership", type_="leadership_change", conf="High"):
    return {"event_date": (AS_OF - timedelta(days=age)).isoformat(), "family": family, "type": type_, "confidence": conf}


def test_one_dot_per_signal_in_the_window_and_nothing_to_draw_otherwise():
    svg = timeline_svg([s(3), s(40, "hiring", "hiring_cluster"), s(400)], AS_OF)
    assert svg.count("<circle") == 2 and "Signals over the last 180 days" in svg
    assert timeline_svg([], AS_OF) == "" and timeline_svg([s(400)], AS_OF) == ""
    assert timeline_svg([{"event_date": "garbage"}, {"family": "x"}], AS_OF) == ""


def test_colors_follow_the_signal_group_and_medium_is_paler():
    svg = timeline_svg([s(3), s(10, "hiring", "hiring_cluster", "Medium")], AS_OF)
    assert FAMILY_COLORS["leadership"] in svg and FAMILY_COLORS["hiring"] in svg and svg.count("fill-opacity") == 1
    assert [l for l, _ in legend([s(3), s(4, "hiring", "x")])] == ["Hiring", "Leadership"]


def test_nearby_dots_stack_instead_of_overlapping_and_text_is_escaped():
    svg = timeline_svg([s(5), s(5, type_="leadership_transformation_ai_data"), s(5, type_="<script>alert(1)</script>")], AS_OF)
    ys = sorted({part.split('cy="')[1].split('"')[0] for part in svg.split("<circle")[1:]})
    assert len(ys) == 3 and "<script>" not in svg and "&lt;script&gt;" in svg


def test_months_are_labelled():
    svg = timeline_svg([s(3)], AS_OF)
    for label in ("May", "Jun", "Jul", "Aug", "Sep", "Oct"):
        assert f">{label}<" in svg


def test_the_profile_page_and_the_old_onepager_address(app_env):  # noqa: F811
    from datetime import date
    e, today = app_env, date.today()
    a = add_account(e.conn, "Acme", industry="Health care", hq_city="Chicago", employees=12000)
    add_signal(e.conn, e.cfg, a, "leadership_change", 12, as_of=today, summary="New CIO named <b>today</b>.", role="Chief Information Officer")
    add_signal(e.conn, e.cfg, a, "hiring_cluster", 8, as_of=today)
    score_accounts(e.conn, e.cfg, today)
    e.post(f"/accounts/{a}/stage", {"stage": "pursuing", "note": "Intro via board"})
    r = e.get(f"/accounts/{a}/onepager")
    assert r.code == 303 and r.h["Location"] == f"/companies/{a}"                      # the printable page is now the profile
    body = e.get(f"/companies/{a}").body
    assert "Acme" in body and "Health care" in body and "12,000 employees" in body and "Why this score" in body
    assert "Signals over the last 180 days" in body and body.count("<circle") == 2 and "Chief Information Officer" in body
    assert "New CIO named &lt;b&gt;today&lt;/b&gt;." in body and "Intro via board" in body and "Pursuing" in body
    assert "Research only" in body and f"/accounts/{a}" in body and "Manage this company" in body
    assert e.get("/companies/999").code == 404


def test_the_profile_works_for_an_unscored_company(app_env):  # noqa: F811
    e = app_env
    a = add_account(e.conn, "Quietco")
    page = e.get(f"/companies/{a}")
    assert page.code == 200 and "not scored" in page.body and "No signals in the last 180 days" in page.body and "Why this score" not in page.body
