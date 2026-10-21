"""People with their own login: passwords, one-time sign-up links, sessions that end when they should, and who-did-what. Everyone has the same access."""

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from html import unescape

import pytest

from bd import users
from bd.actor import acting_as, current, current_id
from bd.db import connect
from bd.pipeline import add_note, set_stage
from bd.ui.app import App
from bd.ui.auth import Hosted
from bd.ui.server import request_line_for_log
from tests.conftest import add_account
from tests.test_hosted import HOST, PW, call, fake_bd, hosted, login, session  # noqa: F401  (fixtures and helpers)

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
GOOD = "three quiet rivers sing"


@pytest.fixture
def cfg(cfg):
    return cfg.model_copy(deep=True)


@pytest.fixture
def conn(tmp_path):
    from tests.conftest import make_db
    return make_db(tmp_path)


# ---- passwords ---------------------------------------------------------------------------------------------------------------------------------
def test_a_password_is_a_salted_hash_that_checks_in_constant_time_and_never_the_password():
    a, b = users.hash_password(GOOD), users.hash_password(GOOD)
    assert a != b and GOOD not in a and a.startswith("scrypt$") and users.verify_password(GOOD, a) and users.verify_password(GOOD, b)
    assert not users.verify_password(GOOD + "x", a) and not users.verify_password("", a)
    for bad in (None, "", "garbage", "scrypt$1$2", "md5$1$1$1$AA==$AA==", "scrypt$x$8$1$AA==$AA=="):
        assert users.verify_password(GOOD, bad) is False                           # a missing or unreadable hash is a failed check, never an error


def test_what_a_password_and_a_sign_in_name_may_be():
    assert users.password_problem("short") and users.password_problem("aaaaaaaaaaaaaaaa") and users.password_problem("ana@example.com", "ana@example.com")
    assert users.password_problem(GOOD, "ana@example.com", "Ana Cruz") is None
    assert users.login_problem("ana@example.com") is None and users.login_problem("a.b-c_d+e@x.io") is None
    for bad in ("", "ab", "has space", "semi;colon", "x" * 81, "<script>", "owner", "OWNER", "Owner"):
        assert users.login_problem(bad), bad
    assert users.clean_name("  Ana \n  Cruz ") == "Ana Cruz" and len(users.clean_name("x" * 200)) == 80


# ---- adding a person and the one-time link -----------------------------------------------------------------------------------------------------
def test_adding_a_person_keeps_only_a_hash_of_the_link_and_the_log_never_holds_it(conn):
    with conn:
        uid, token, problem = users.add_user(conn, "Ana Cruz", "ana@example.com", "Owner", NOW)
    assert problem is None and uid and len(token) >= 40
    row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    assert row["status"] == "invited" and row["pw_hash"] is None and row["created_by"] == "Owner"
    everything = " ".join(str(v) for r in conn.execute("SELECT * FROM users") for v in tuple(r)) + " ".join(str(tuple(r)) for r in conn.execute("SELECT * FROM audit_log"))
    assert token not in everything and users._token_hash(token) == row["invite_hash"]
    audit = conn.execute("SELECT actor, action, field, new FROM audit_log").fetchone()
    assert tuple(audit) == ("Owner", "create", "user", "Ana Cruz (ana@example.com)")


def test_a_name_and_a_sign_in_name_are_required_and_unique_without_regard_to_case(conn):
    with conn:
        assert users.add_user(conn, "Ana", "ana@example.com", "Owner", NOW)[2] is None
        assert "already used" in users.add_user(conn, "Other Ana", "ANA@Example.com", "Owner", NOW)[2]
        assert users.add_user(conn, "  ", "x@example.com", "Owner", NOW)[2] and users.add_user(conn, "Bo", "owner", "Owner", NOW)[2]
        assert users.add_user(conn, "Bo", "bad name", "Owner", NOW)[2]
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


def test_a_link_works_once_expires_and_a_weak_password_leaves_it_usable(conn):
    with conn:
        uid, token, _ = users.add_user(conn, "Ana Cruz", "ana@example.com", "Owner", NOW)
    assert users.invited_user(conn, token, NOW + timedelta(days=6))["id"] == uid
    assert users.invited_user(conn, token, NOW + timedelta(days=users.INVITE_DAYS, seconds=1)) is None and users.invited_user(conn, token + "x", NOW) is None
    assert users.invited_user(conn, "", NOW) is None
    with conn:
        person, problem = users.accept_invite(conn, token, "short", NOW)
    assert person is None and "at least" in problem and users.invited_user(conn, token, NOW)                    # still usable
    with conn:
        person, problem = users.accept_invite(conn, token, GOOD, NOW)
    assert problem is None and person["status"] == "active" and person["invite_hash"] is None and users.verify_password(GOOD, person["pw_hash"])
    with conn:
        again, problem = users.accept_invite(conn, token, GOOD + "2", NOW)
    assert again is None and "already been used" in problem
    assert [tuple(r) for r in conn.execute("SELECT actor, field FROM audit_log WHERE field = 'user_password'")] == [("Ana Cruz", "user_password")]


def test_signing_in_needs_an_active_person_and_the_right_password(conn):
    with conn:
        uid, token, _ = users.add_user(conn, "Ana Cruz", "ana@example.com", "Owner", NOW)
    assert users.authenticate(conn, "ana@example.com", GOOD) is None                                         # not signed up yet
    with conn:
        users.accept_invite(conn, token, GOOD, NOW)
        assert users.authenticate(conn, "ANA@example.com", GOOD, NOW)["id"] == uid and users.authenticate(conn, " ana@example.com ", GOOD)["id"] == uid
        assert users.authenticate(conn, "ana@example.com", GOOD + "x") is None and users.authenticate(conn, "nobody@example.com", GOOD) is None
    assert conn.execute("SELECT last_login FROM users").fetchone()[0]
    with conn:
        users.remove_user(conn, uid, "Owner")
    assert users.authenticate(conn, "ana@example.com", GOOD) is None


def test_a_new_link_ends_the_old_password_and_sessions_and_removal_ends_everything(conn):
    with conn:
        uid, token, _ = users.add_user(conn, "Ana Cruz", "ana@example.com", "Owner", NOW)
        users.accept_invite(conn, token, GOOD, NOW)
    key = conn.execute("SELECT session_key FROM users").fetchone()[0]
    with conn:
        fresh = users.new_link(conn, uid, "Owner", NOW)
    row = conn.execute("SELECT * FROM users").fetchone()
    assert row["session_key"] != key and row["pw_hash"] is None and row["status"] == "invited" and users.invited_user(conn, fresh, NOW)
    assert users.authenticate(conn, "ana@example.com", GOOD) is None
    with conn:
        assert users.remove_user(conn, uid, "Owner") is True and users.remove_user(conn, uid, "Owner") is False and users.new_link(conn, uid, "Owner", NOW) is None
        assert users.new_link(conn, 999, "Owner", NOW) is None
    row = conn.execute("SELECT * FROM users").fetchone()
    assert row["status"] == "removed" and row["invite_hash"] is None and users.invited_user(conn, fresh, NOW) is None and users.list_users(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1                                    # the row stays, so a name in the log still means someone


# ---- who is acting -----------------------------------------------------------------------------------------------------------------------------
def test_the_actor_is_only_set_while_a_person_is_acting_and_replaces_only_the_generic_ui(conn):
    from bd.actor import resolve
    assert current() is None and current_id() is None and resolve("ui") == "ui"
    with acting_as("Ana Cruz", 3):
        assert current() == "Ana Cruz" and current_id() == 3 and resolve("ui") == "Ana Cruz" and resolve("scan") == "scan" and resolve("ats-find") == "ats-find"
    assert current() is None and current_id() is None


def test_audit_notes_and_stages_carry_the_persons_name_and_scheduled_work_does_not(conn):
    a = add_account(conn, "Acme")
    with acting_as("Ana Cruz", 3):
        assert add_note(conn, a, "called the CFO") and set_stage(conn, a, "reviewing", "worth a look")
    assert add_note(conn, a, "from the command line")
    authors = [r[0] for r in conn.execute("SELECT author FROM lead_notes ORDER BY id")]
    assert authors == ["Ana Cruz", "Ana Cruz", None]
    assert conn.execute("SELECT actor FROM audit_log WHERE action = 'stage'").fetchone()[0] == "Ana Cruz"


# ---- the app: sign-up, sign-in, sessions ---------------------------------------------------------------------------------------------------------
def post(h, path, data, cookie, **kw):
    return call(h.app, "POST", path, {"csrf": h.app._csrf, **data}, cookie=cookie, **kw)


def add_person(h, owner, name="Ana Cruz", login_name="ana@example.com"):
    r = post(h, "/users/add", {"name": name, "login": login_name}, owner)
    assert r.code == 200, r.body[:300]
    link = re.search(r'https?://[^\s<]+/join/([A-Za-z0-9_-]+)', unescape(r.body))
    return r, link.group(1) if link else None


def join(h, token, password=GOOD, again=None, **kw):
    return call(h.app, "POST", f"/join/{token}", {"password": password, "password2": password if again is None else again}, **kw)


def cookie_of(r):
    return r.h["Set-Cookie"].split(";")[0].split("=", 1)[1]


def test_the_owner_adds_a_person_sees_the_link_once_and_the_database_never_has_it(hosted):
    owner = session(hosted)
    r, token = add_person(hosted, owner)
    assert token and "Send Ana Cruz this link" in r.body and f"http://{HOST}/join/{token}" in unescape(r.body) and "waiting for their link" in r.body
    again = call(hosted.app, "GET", "/users", cookie=owner)
    assert token not in again.body and "Ana Cruz" in again.body                                              # shown once, in the page that made it
    db = sqlite3.connect(hosted.db)
    assert token not in " ".join(str(v) for row in db.execute("SELECT * FROM users") for v in row)
    secure = post(hosted, "/users/add", {"name": "Bo", "login": "bo@example.com"}, owner, headers={"X-Forwarded-Proto": "https"})
    assert re.search(r"https://bd\.example\.app/join/", unescape(secure.body))


def test_the_users_table_labels_every_cell_so_it_reads_on_a_phone(hosted):
    owner = session(hosted)
    add_person(hosted, owner)
    body = call(hosted.app, "GET", "/users", cookie=owner).body
    table = body[body.index('<table class="stackable">'):body.index("</table>")]
    row = table[table.index("Ana Cruz") - 40:]
    for label in ("Name", "Sign-in name", "Status", "Last signed in", "Added by"):
        assert f'data-label="{label}"' in row
    css = open("bd/ui/templates/design.css").read()
    assert "table.stackable" in css and "attr(data-label)" in css


def test_a_person_opens_their_link_chooses_a_password_and_is_signed_in_as_themselves(hosted):
    owner = session(hosted)
    _, token = add_person(hosted, owner)
    page = call(hosted.app, "GET", f"/join/{token}")
    assert page.code == 200 and "Welcome, Ana Cruz" in page.body and 'name="password2"' in page.body and "Referrer-Policy" in page.h and page.h["Referrer-Policy"] == "no-referrer"
    assert "Analysis" not in page.body                                                                       # no navigation for someone not signed in
    assert join(hosted, token, again="different words entirely").code == 400
    assert "not the same" in join(hosted, token, again="different words entirely").body
    assert "at least" in join(hosted, token, "short").body
    ok = join(hosted, token)
    assert ok.code == 303 and ok.h["Location"] == "/opportunities" and ok.h["Set-Cookie"].startswith("bd_session=s2.") and "HttpOnly" in ok.h["Set-Cookie"]
    me = call(hosted.app, "GET", "/opportunities", cookie=cookie_of(ok))
    assert me.code == 200 and 'class="who"' in me.body and ">Ana Cruz<" in me.body
    used = call(hosted.app, "GET", f"/join/{token}")
    assert used.code == 404 and "already been used" in used.body and join(hosted, token).code == 404


def test_a_bad_or_foreign_join_request_is_refused(hosted):
    for bad in ("x" * 120, "a b", "../etc", "tok<script>"):
        assert call(hosted.app, "GET", f"/join/{bad}").code == 404
    assert call(hosted.app, "GET", "/join/nosuchtoken").code == 404 and join(hosted, "nosuchtoken").code == 404
    owner = session(hosted)
    _, token = add_person(hosted, owner)
    assert join(hosted, token, headers={"Origin": "https://evil.example"}).code == 403
    assert call(hosted.app, "GET", "/join/").code in (302, 303)                                              # no token is not a join page: it needs a sign-in


def test_signing_in_by_name_and_the_shared_password_both_work_and_say_nothing_about_who_exists(hosted):
    owner = session(hosted)
    _, token = add_person(hosted, owner)
    join(hosted, token)
    ok = call(hosted.app, "POST", "/login", {"login": "ANA@example.com", "password": GOOD})
    assert ok.code == 303 and cookie_of(ok).startswith("s2.")
    wrong, unknown = call(hosted.app, "POST", "/login", {"login": "ana@example.com", "password": "nope nope nope"}), call(hosted.app, "POST", "/login", {"login": "who@example.com", "password": GOOD})
    assert wrong.code == unknown.code == 401 and re.sub(r"\s+", " ", wrong.body) == re.sub(r"\s+", " ", unknown.body) and "sign-in name or password" in wrong.body
    assert call(hosted.app, "POST", "/login", {"login": "", "password": PW}).code == 303 and call(hosted.app, "POST", "/login", {"login": "Owner", "password": PW}).code == 303
    assert cookie_of(call(hosted.app, "POST", "/login", {"login": "owner", "password": PW})).startswith("s1.")
    assert call(hosted.app, "POST", "/login", {"login": "ana@example.com", "password": PW}).code == 401           # the shared password is not Ana's password
    assert 'name="login"' in call(hosted.app, "GET", "/login").body


def test_guessing_one_persons_password_is_slowed_from_anywhere(hosted):
    owner = session(hosted)
    _, token = add_person(hosted, owner)
    join(hosted, token)
    for i in range(5):
        r = call(hosted.app, "POST", "/login", {"login": "ana@example.com", "password": f"wrong guess {i}"}, headers={"X-Forwarded-For": f"10.0.0.{i}"})
        assert r.code == 401
    locked = call(hosted.app, "POST", "/login", {"login": "ana@example.com", "password": GOOD}, headers={"X-Forwarded-For": "10.0.0.99"})
    assert locked.code == 429 and "Too many attempts" in locked.body                                         # a new address, the same name
    assert call(hosted.app, "POST", "/login", {"login": "bo@example.com", "password": "x"}, headers={"X-Forwarded-For": "10.0.0.98"}).code == 401


def test_a_session_ends_when_the_password_changes_the_person_is_removed_or_the_cookie_is_tampered_with(hosted):
    owner = session(hosted)
    _, token = add_person(hosted, owner)
    mine = cookie_of(join(hosted, token))
    uid = int(mine.split(".")[1])
    assert call(hosted.app, "GET", "/opportunities", cookie=mine).code == 200
    parts = mine.split(".")
    for forged in (f"s2.{uid + 1}.{parts[2]}.{parts[3]}", f"s2.{uid}.{parts[2]}.{'0' * 64}", f"s2.{uid}.1.{parts[3]}", f"s2.{uid}.{parts[2]}", "s2.x.1.2", f"s2.{uid}.{int(parts[2]) + 1}.{parts[3]}"):
        assert call(hosted.app, "GET", "/opportunities", cookie=forged).code == 303, forged                  # redirected to sign in
    post(hosted, f"/users/{uid}/link", {}, owner)
    assert call(hosted.app, "GET", "/opportunities", cookie=mine).code == 303                               # a new link ended their session
    _, token2 = add_person(hosted, owner, "Bo Diaz", "bo@example.com")
    bo = cookie_of(join(hosted, token2))
    assert call(hosted.app, "GET", "/opportunities", cookie=bo).code == 200
    post(hosted, f"/users/{int(bo.split('.')[1])}/remove", {}, owner)
    assert call(hosted.app, "GET", "/opportunities", cookie=bo).code == 303
    other = App(hosted.app.cfg, hosted.db, root=hosted.tmp, hosted=Hosted("a completely different shared password"), jobs=hosted.runner)
    fresh = Hosted(PW)
    assert other.identify(owner) is None and other.identify(fresh.make_session()) is None                    # changing BD_UI_PASSWORD signs everyone out, owner and people alike


def test_everyone_has_the_same_access_including_adding_and_removing_people(hosted):
    owner = session(hosted)
    _, token = add_person(hosted, owner)
    ana = cookie_of(join(hosted, token))
    assert call(hosted.app, "GET", "/users", cookie=ana).code == 200 and call(hosted.app, "GET", "/admin", cookie=ana).code == 200
    _, token2 = add_person(hosted, ana, "Bo Diaz", "bo@example.com")
    assert token2 and "Users" in call(hosted.app, "GET", "/admin", cookie=ana).body
    my_id = int(ana.split(".")[1])
    refused = post(hosted, f"/users/{my_id}/remove", {}, ana)
    assert refused.code == 303 and "cannot remove yourself" in call(hosted.app, "GET", "/users", cookie=ana).body        # nobody removes themselves by accident
    assert call(hosted.app, "GET", "/opportunities", cookie=ana).code == 200
    assert call(hosted.app, "GET", "/users").code == 303 and call(hosted.app, "POST", "/users/add", {"name": "x", "login": "xxx"}).code == 403    # signed-out: nothing


def test_users_needs_the_csrf_token_and_a_same_origin_request(hosted):
    owner = session(hosted)
    assert call(hosted.app, "POST", "/users/add", {"name": "Ana", "login": "ana@example.com"}, cookie=owner).code == 403
    assert post(hosted, "/users/add", {"name": "Ana", "login": "ana@example.com"}, owner, headers={"Origin": "https://evil.example"}).code == 403
    empty = post(hosted, "/users/add", {"name": "", "login": "ana@example.com"}, owner)
    assert empty.code == 200 and "Enter the person" in empty.body
    assert sqlite3.connect(hosted.db).execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    assert post(hosted, "/users/999/link", {}, owner).code == 200 and post(hosted, "/users/999/remove", {}, owner).code == 303
    assert call(hosted.app, "POST", "/users/abc/remove", {"csrf": hosted.app._csrf}, cookie=owner).code == 404


# ---- who did what, in the pages --------------------------------------------------------------------------------------------------------------------
def test_what_a_person_does_carries_their_name_the_owners_work_says_owner(hosted):
    owner = session(hosted)
    _, token = add_person(hosted, owner)
    ana = cookie_of(join(hosted, token))
    a = add_account(connect(hosted.db), "Acme Co")
    post(hosted, f"/accounts/{a}/note", {"body": "called the CFO", "back": f"/companies/{a}"}, ana)
    post(hosted, f"/accounts/{a}/stage", {"stage": "reviewing", "note": "", "back": f"/companies/{a}"}, ana)
    post(hosted, "/exclusions/add", {"name": "Rival Inc", "domain": "rival.example", "reason": "conflict"}, owner)
    db = sqlite3.connect(hosted.db)
    assert [r[0] for r in db.execute("SELECT author FROM lead_notes ORDER BY id")] == ["Ana Cruz", "Ana Cruz"]
    actors = {r[0] for r in db.execute("SELECT actor FROM audit_log WHERE field != 'user' AND field != 'user_password'")}
    assert "Ana Cruz" in actors and "Owner" in actors and "ui" not in actors
    page = call(hosted.app, "GET", f"/companies/{a}", cookie=ana).body
    assert "called the CFO" in page and "Ana Cruz" in page
    assert "Ana Cruz" in call(hosted.app, "GET", "/audit", cookie=owner).body


def test_a_job_says_who_started_it_and_a_scheduled_one_says_nobody(hosted):
    owner = session(hosted)
    _, token = add_person(hosted, owner)
    ana = cookie_of(join(hosted, token))
    r = post(hosted, "/operate/run", {"kind": "score"}, ana)
    hosted.runner.wait()
    job = hosted.runner.get(int(r.h["Location"].rsplit("/", 1)[1]))
    assert job.actor == "Ana Cruz" and "started by Ana Cruz" in call(hosted.app, "GET", r.h["Location"], cookie=owner).body
    sid = hosted.runner.submit("score", {}, scheduled=True)
    hosted.runner.wait()
    assert hosted.runner.get(sid).actor is None


def test_one_persons_notice_is_never_shown_to_another(hosted):
    owner = session(hosted)
    _, t1 = add_person(hosted, owner)
    ana = cookie_of(join(hosted, t1))
    _, t2 = add_person(hosted, owner, "Bo Diaz", "bo@example.com")
    bo = cookie_of(join(hosted, t2))
    mine = post(hosted, "/users/add", {"name": "", "login": "x@example.com"}, ana)                            # Ana's notice: "Enter the person's name."
    assert "Enter the person" in mine.body
    assert "Enter the person" not in call(hosted.app, "GET", "/users", cookie=bo).body and "Enter the person" not in call(hosted.app, "GET", "/users", cookie=ana).body   # shown once, to Ana only


def test_pages_escape_what_people_type_and_the_users_page_is_in_the_admin_list(hosted):
    owner = session(hosted)
    _, token = add_person(hosted, owner, "<b>Eve</b>", "eve@example.com")
    raw = call(hosted.app, "GET", "/users", cookie=owner).body
    assert "<b>Eve</b>" not in raw and "&lt;b&gt;Eve&lt;/b&gt;" in raw and 'href="/users"' in raw
    assert "<b>Eve</b>" not in call(hosted.app, "GET", f"/join/{token}").body
    assert 'href="/users"' in call(hosted.app, "GET", "/admin", cookie=owner).body


def test_a_sign_up_link_never_reaches_the_request_log_and_a_local_app_has_no_users_page(hosted, cfg, tmp_path):
    assert request_line_for_log("GET /join/abc123_-XYZ?x=1 HTTP/1.1") == "GET /join/…"
    assert request_line_for_log("POST /join/abc123 HTTP/1.1") == "POST /join/… HTTP/1.1" and request_line_for_log("GET /login?t=secret HTTP/1.1") == "GET /login"
    from tests.conftest import make_db
    make_db(tmp_path / "local").close() if (tmp_path / "local").mkdir() is None else None
    local = App(cfg, str(tmp_path / "local" / "t.sqlite"), "tok", root=tmp_path)
    local.port = 8765
    import io
    env = {"REQUEST_METHOD": "GET", "PATH_INFO": "/users", "QUERY_STRING": "", "HTTP_HOST": "127.0.0.1:8765", "HTTP_COOKIE": "bd_session=tok", "CONTENT_LENGTH": "0", "wsgi.input": io.BytesIO()}
    out = {}
    local(env, lambda s, h: out.update(s=s))
    assert out["s"].startswith("404")
