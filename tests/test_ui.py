import io
import re
import subprocess
import sys
from datetime import timedelta
from urllib.parse import urlencode

import pytest

from bd.accounts import list_accounts
from bd.briefer import Brief
from bd.curate import update_account
from bd.score_store import score_accounts
from bd.ui.app import App
from tests.conftest import AS_OF, ROOT, add_account, add_signal, make_db

TOKEN = "test-token-123"
HOST = "127.0.0.1:8765"


class Client:
    """Minimal WSGI test client that behaves like a browser that has (or has not) signed in."""

    def __init__(self, app, signed_in=True):
        self.app, self.cookie = app, (f"bd_session={TOKEN}" if signed_in else "")

    def call(self, method, path, data=None, headers=None, host=HOST, raw_body=None):
        path, _, qs = path.partition("?")
        body = raw_body if raw_body is not None else (urlencode(data, doseq=True).encode() if data else b"")
        env = {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": qs, "HTTP_HOST": host,
               "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body)}
        if self.cookie:
            env["HTTP_COOKIE"] = self.cookie
        for k, v in (headers or {}).items():
            env["HTTP_" + k.upper().replace("-", "_")] = v
        out = {}
        chunks = self.app(env, lambda status, hdrs: out.update(status=status, headers=hdrs))
        return Resp(out["status"], dict(out["headers"]), b"".join(chunks).decode())

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, data=None, csrf=True, **kw):
        data = dict(data or {})
        if csrf:
            data["csrf"] = self.app._csrf
        return self.call("POST", path, data, **kw)


class Resp:
    def __init__(self, status, headers, body):
        self.status, self.headers, self.body = status, headers, body
        self.code = int(status.split()[0])

    @property
    def location(self):
        return self.headers.get("Location", "")


@pytest.fixture
def world(cfg, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "t.sqlite"
    conn = make_db(tmp_path)
    hot = add_account(conn, "Hotco", industry="Industrial", hq_city="Chicago", ownership="pe-backed", sponsor="Test Capital")
    for t, age in (("leadership_change", 21), ("hiring_cluster", 10), ("pe_addon", 45)):
        add_signal(conn, cfg, hot, t, age, url=f"https://hotco.com/{t}", summary=f"Hotco {t.replace('_', ' ')} summary.")
    add_signal(conn, cfg, hot, "restructuring_layoffs", 5, conf="Low", url="https://gossip.example/hotco")
    held = add_account(conn, "Heldco", chicagoland="unknown", revenue=None, ownership=None)       # unverified: still scanned
    score_accounts(conn, cfg, AS_OF)
    conn.commit()
    app = App(cfg, str(db), TOKEN, root=tmp_path)
    app.port = 8765
    return type("W", (), {"app": app, "c": Client(app), "conn": conn, "hot": hot, "held": held, "tmp": tmp_path, "cfg": cfg})


# ======================== security ===============================================================================

def test_unauthenticated_requests_are_refused(world):
    anon = Client(world.app, signed_in=False)
    for path in ("/", "/accounts", "/runs", "/enrich", "/exclusions", "/audit", "/accounts/1", "/accounts/new"):
        r = anon.get(path)
        assert r.code == 403 and "Hotco" not in r.body, path
    assert Client(world.app, signed_in=False).post("/accounts/new", {"name": "X"}).code == 403
    wrong = Client(world.app)
    wrong.cookie = "bd_session=nope"
    assert wrong.get("/").code == 403


def test_auth_link_sets_a_strict_httponly_cookie_and_wrong_token_does_not(world):
    anon = Client(world.app, signed_in=False)
    r = anon.get(f"/auth?t={TOKEN}")
    assert r.code == 303 and r.location == "/"
    cookie = r.headers["Set-Cookie"]
    assert cookie.startswith(f"bd_session={TOKEN}") and "HttpOnly" in cookie and "SameSite=Strict" in cookie
    for t in ("", "wrong", TOKEN[:-1], TOKEN + "x"):
        r = anon.get(f"/auth?t={t}")
        assert r.code == 403 and "Set-Cookie" not in r.headers


@pytest.mark.parametrize("host", ["evil.example", "127.0.0.1", "127.0.0.1:9999", "localhost:1", "evil.example:8765", ""])
def test_wrong_host_header_is_refused_even_with_a_valid_cookie(world, host):
    assert world.c.get("/", host=host).code == 403          # DNS-rebinding defence


def test_localhost_name_is_accepted(world):
    assert world.c.get("/", host="localhost:8765").code == 200


def test_posts_need_the_csrf_token_and_a_same_origin_request(world):
    ok = {"name": "Newco"}
    assert world.c.post("/accounts/new", ok, csrf=False).code == 403
    r = world.c.call("POST", "/accounts/new", {**ok, "csrf": "wrong"})
    assert r.code == 403
    assert world.c.post("/accounts/new", ok, headers={"Origin": "http://evil.example"}).code == 403
    assert world.c.post("/accounts/new", ok, headers={"Origin": "null"}).code == 403
    assert world.c.post("/accounts/new", ok, headers={"Sec-Fetch-Site": "cross-site"}).code == 403
    assert world.c.post("/accounts/new", ok, headers={"Sec-Fetch-Site": "same-site"}).code == 403
    assert world.conn.execute("SELECT COUNT(*) FROM accounts WHERE name='Newco'").fetchone()[0] == 0
    assert world.c.post("/accounts/new", ok, headers={"Origin": f"http://{HOST}", "Sec-Fetch-Site": "same-origin"}).code == 303
    assert world.conn.execute("SELECT COUNT(*) FROM accounts WHERE name='Newco'").fetchone()[0] == 1


def test_a_get_can_never_change_anything(world):
    before = world.conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    for p in ("/", "/accounts", "/accounts/1", "/enrich", "/exclusions", "/runs", "/audit",
              "/accounts/1?domain=evil.com&revenue=1", "/exclusions/add?name=X&reason=client", "/exclusions/1/remove"):
        world.c.get(p)
    assert world.conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == before


def test_security_headers_on_every_response(world):
    for r in (world.c.get("/monday"), world.c.get("/nope"), Client(world.app, signed_in=False).get("/")):
        h = r.headers
        assert "script-src 'none'" in h["Content-Security-Policy"] and "default-src 'none'" in h["Content-Security-Policy"]
        assert "frame-ancestors 'none'" in h["Content-Security-Policy"] and "form-action 'self'" in h["Content-Security-Policy"]
        assert h["X-Content-Type-Options"] == "nosniff" and h["Referrer-Policy"] == "same-origin"
        assert h["Cache-Control"] == "no-store" and h["X-Frame-Options"] == "DENY"


def test_pages_contain_no_scripts_and_no_external_resources(world):
    for p in ("/", "/accounts", "/accounts/1", "/enrich", "/exclusions", "/runs", "/audit", "/accounts/new"):
        body = world.c.get(p).body
        assert "<script" not in body.lower() and "javascript:" not in body.lower(), p
        assert not re.search(r'(src|href)="https?://', body.replace('target="_blank"', "")) or 'rel="noopener noreferrer"' in body
        assert "<link" not in body.lower() and "@import" not in body


def test_oversized_and_odd_requests(world):
    big = world.c.call("POST", "/accounts/new", raw_body=b"x=" + b"a" * 70_000)
    assert big.code == 400 and "too large" in big.body
    assert world.c.call("PUT", "/accounts/1").code == 405
    assert world.c.call("DELETE", "/accounts/1").code == 405
    assert world.c.get("/nope").code == 404 and world.c.get("/accounts/99999").code == 404
    assert world.c.get("/accounts/abc").code == 404 and world.c.get("/accounts/1/extra").code == 404


def test_internal_errors_do_not_leak_details(world, monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("secret sql /home/user/path")
    monkeypatch.setattr(App, "runs", boom)
    r = world.c.get("/runs")
    assert r.code == 500 and "secret" not in r.body and "/home/user" not in r.body and "Traceback" not in r.body


def test_html_is_escaped(world):
    evil = "<script>alert(1)</script>"
    world.conn.execute("UPDATE accounts SET name = ?, industry = ? WHERE id = ?", (evil, '"><img src=x onerror=alert(1)>', world.hot))
    world.conn.commit()
    for p in ("/", "/accounts", f"/accounts/{world.hot}"):
        body = world.c.get(p).body
        assert evil not in body and "&lt;script&gt;" in body and "<img" not in body, p


def test_only_http_urls_become_links(world):
    world.conn.execute("UPDATE signals SET source_url = 'javascript:alert(1)' WHERE account_id=? AND type='leadership_change'", (world.hot,))
    world.conn.commit()
    body = world.c.get(f"/accounts/{world.hot}").body
    assert "javascript:" not in body
    body = world.c.get("/monday").body
    assert "javascript:" not in body


def test_external_links_open_safely(world):
    body = world.c.get("/monday").body
    links = re.findall(r"<a [^>]*href=\"https://hotco\.com[^>]*>", body)
    assert links and all('target="_blank"' in l and 'rel="noopener noreferrer"' in l for l in links)


def test_the_ui_has_no_way_to_scan_score_call_a_model_or_post(world):
    for path in ("/scan", "/score", "/card", "/post", "/run", "/shortlist", "/enrich/run", "/api/scan"):
        assert world.c.get(path).code == 404 and world.c.post(path).code == 404, path
    code = ("import sys, bd.ui.app, bd.ui.server\n"
            "bad=[m for m in ('anthropic','bd.llm','bd.card','bd.runner','bd.scanners','bd.enrich','bd.briefer','bd.verify','bd.edgar') if m in sys.modules]\n"
            "assert not bad, bad\nprint('clean')")
    r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, cwd=ROOT)
    assert r.stdout.strip() == "clean", r.stderr


def test_server_binds_to_loopback_only(cfg, tmp_path):
    from bd.ui.server import HOST, build
    httpd, url = build(cfg, str(tmp_path / "x.sqlite"), port=0)
    try:
        assert HOST == "127.0.0.1" and httpd.server_address[0] == "127.0.0.1"
        assert url.startswith("http://127.0.0.1:") and "/auth?t=" in url
        token = url.split("t=")[1]
        assert len(token) >= 32
    finally:
        httpd.server_close()


def test_each_run_gets_a_different_token(cfg, tmp_path):
    from bd.ui.server import build
    a, ua = build(cfg, str(tmp_path / "a.sqlite"), port=0)
    b, ub = build(cfg, str(tmp_path / "b.sqlite"), port=0)
    try:
        assert ua.split("t=")[1] != ub.split("t=")[1]
    finally:
        a.server_close(); b.server_close()


# ======================== leads =======================================================================================

def test_leads_page_shows_the_shortlist_sources_and_the_arithmetic(world):
    r = world.c.get("/monday")
    assert r.code == 200
    b = r.body
    assert "Hotco" in b and "Hot · 91" in b and "Industrial" in b and "sponsor: Test Capital" in b
    assert "Leadership change" in b and "https://hotco.com/leadership_change" in b
    assert "How the 91 was calculated" in b and "stacking bonus 20" in b
    assert "plain source summary (no brief generated yet)" in b
    assert "Heldco" not in b                                           # held accounts are never scored or listed
    assert "gossip.example" not in b                                   # the Low-confidence signal never scored, so it is not a source


def test_leads_page_uses_a_stored_brief_when_one_exists(world):
    world.conn.execute("INSERT INTO briefs(account_id, run_date, why_now, best_service_line, model) VALUES (?,?,?,?,?)",
                       (world.hot, AS_OF.isoformat(), "Hotco named a new CIO and is hiring.", "Data & Analytics", "m"))
    world.conn.commit()
    b = world.c.get("/monday").body
    assert "Hotco named a new CIO and is hiring." in b and "written brief" in b and "Data &amp; Analytics" in b


def test_leads_empty_states(world, cfg, tmp_path):
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    conn = make_db(fresh)
    app = App(cfg, str(fresh / "t.sqlite"), TOKEN, root=fresh)
    app.port = 8765
    assert "No scores yet" in Client(app).get("/monday").body
    add_account(conn, "Quiet")
    score_accounts(conn, cfg, AS_OF)
    assert "No account met the posting gate" in Client(app).get("/monday").body


def test_leads_flags_stale_scores_instead_of_showing_wrong_numbers(world, cfg):
    add_signal(world.conn, cfg, world.hot, "ai_commitment", 1)
    b = world.c.get("/monday").body
    assert "no longer matches its signals" in b and "Hot · 91" not in b


def test_leads_date_picker_ignores_garbage(world):
    assert world.c.get("/monday?date=2020-01-01").code == 200 and "Hotco" in world.c.get("/monday?date=<script>").body
    assert f'<option value="{AS_OF.isoformat()}" selected>' in world.c.get("/monday").body


def test_posted_leads_are_marked(world):
    from bd.shortlist import mark_posted, build_shortlist
    sl = build_shortlist(world.conn, world.cfg, AS_OF)
    mark_posted(world.conn, sl.leads, AS_OF, world.cfg)
    assert "No account met the posting gate" in world.c.get("/monday").body        # now in cooldown, so it is not listed again


# ======================== accounts ====================================================================================

def test_accounts_list_filters_and_explains_holds(world):
    b = world.c.get("/accounts").body
    assert "Hotco" in b and "Heldco" in b and "1 unverified" in b
    assert "Chicagoland status unverified" in b
    only_held = world.c.get("/accounts?status=unverified").body
    assert "Heldco" in only_held and ">Hotco<" not in only_held
    assert ">Hotco<" in world.c.get("/accounts?q=hot").body and "Heldco" not in world.c.get("/accounts?q=hot").body
    assert "No accounts match" in world.c.get("/accounts?q=zzz").body


def test_account_page_shows_facts_signals_scores_and_history(world):
    update_account(world.conn, world.cfg, world.hot, {"hq_city": "Chicago", "chicagoland": "yes", "revenue": "600000000", "ownership": "pe-backed", "tier": "A"})
    b = world.c.get(f"/accounts/{world.hot}").body
    assert "Scanned for signals" in b and 'value="600000000"' in b and "Edit facts" in b
    assert "stored, never scored" in b and "gossip.example" in b          # the Low-confidence signal is visible but marked
    assert "Hot" in b and "stacking bonus 20" in b                        # score history with the calculation
    assert "revenue" in b and "600000000.0" in b                          # audit trail
    assert 'name="csrf"' in b


def test_editing_an_account_saves_audits_and_shows_the_new_fit_verdict(world):
    r = world.c.post(f"/accounts/{world.held}", {"chicagoland": "yes", "revenue": "800,000,000", "ownership": "private", "tier": "B", "domain": "https://www.heldco.com"})
    assert r.code == 303 and r.location == f"/accounts/{world.held}"
    page = world.c.get(r.location).body
    assert "Saved (4 changes)" in page and "Ready to scan" in page and "Not scanned" not in page
    a = world.conn.execute("SELECT * FROM accounts WHERE id=?", (world.held,)).fetchone()
    assert (a["status"], a["domain"], a["revenue"], a["tier"]) == ("active", "heldco.com", 8e8, "B")
    assert "Saved" not in world.c.get(r.location).body                    # the flash shows once


def test_invalid_edit_is_refused_keeps_the_input_and_changes_nothing(world):
    before = world.conn.execute("SELECT * FROM accounts WHERE id=?", (world.held,)).fetchone()
    r = world.c.post(f"/accounts/{world.held}", {"revenue": "lots", "cik": "xyz", "domain": "typed.com"})
    assert r.code == 400 and "Not saved" in r.body and "revenue must be a plain non-negative number" in r.body
    assert 'value="typed.com"' in r.body and 'value="lots"' in r.body        # the form keeps what was typed
    assert tuple(world.conn.execute("SELECT * FROM accounts WHERE id=?", (world.held,)).fetchone()) == tuple(before)


def test_duplicate_domain_is_reported_not_crashed(world):
    world.conn.execute("UPDATE accounts SET domain='hotco.com' WHERE id=?", (world.hot,))
    world.conn.commit()
    r = world.c.post(f"/accounts/{world.held}", {"domain": "hotco.com"})
    assert r.code == 400 and "another account already uses that domain" in r.body


def test_clearing_a_field_makes_it_unknown_and_unverified_but_still_scanned(world):
    world.c.post(f"/accounts/{world.hot}", {"chicagoland": "yes", "revenue": "500000000", "ownership": "pe-backed", "tier": "A"})
    assert world.conn.execute("SELECT status FROM accounts WHERE id=?", (world.hot,)).fetchone()[0] == "active"
    world.c.post(f"/accounts/{world.hot}", {"chicagoland": "unknown", "revenue": "", "ownership": "", "tier": "A"})
    a = world.conn.execute("SELECT * FROM accounts WHERE id=?", (world.hot,)).fetchone()
    assert (a["status"], a["revenue"], a["chicagoland"]) == ("active", None, "unknown")


def test_a_forged_form_cannot_change_name_status_or_id(world):
    world.c.post(f"/accounts/{world.hot}", {"tier": "B", "name": "Hacked", "status": "excluded", "id": "999", "last_scanned": "2020-01-01"})
    a = world.conn.execute("SELECT * FROM accounts WHERE id=?", (world.hot,)).fetchone()
    assert (a["name"], a["id"], a["last_scanned"]) == ("Hotco", world.hot, None) and a["status"] != "excluded" and a["tier"] == "B"


def test_add_account(world):
    assert "Add account" in world.c.get("/accounts/new").body
    r = world.c.post("/accounts/new", {"name": "  Brand   New ", "domain": "brandnew.com", "chicagoland": "yes", "revenue": "900000000", "ownership": "private"})
    assert r.code == 303
    page = world.c.get(r.location).body
    assert "Added Brand   New" in page or "Added" in page
    assert "Fit gate: pass" in page
    dup = world.c.post("/accounts/new", {"name": "brand new"})
    assert dup.code == 400 and "already exists" in dup.body
    bad = world.c.post("/accounts/new", {"name": "Zed", "tier": "Q"})
    assert bad.code == 400 and 'value="Zed"' in bad.body


# ======================== exclusions =================================================================================

def test_exclusions_add_remove_and_their_effect_on_status(world):
    world.c.post(f"/accounts/{world.hot}", {"chicagoland": "yes", "revenue": "500000000", "ownership": "pe-backed", "domain": "hotco.com", "tier": "A"})
    r = world.c.post("/exclusions/add", {"name": "Hotco", "domain": "hotco.com", "reason": "client"})
    assert r.code == 303 and "re-run" in world.c.get("/exclusions").body
    assert world.conn.execute("SELECT status FROM accounts WHERE id=?", (world.hot,)).fetchone()[0] == "excluded"
    ex = world.conn.execute("SELECT id FROM exclusions").fetchone()[0]
    assert "Remove" in world.c.get("/exclusions").body
    assert world.c.post(f"/exclusions/{ex}/remove").code == 303
    assert world.conn.execute("SELECT status FROM accounts WHERE id=?", (world.hot,)).fetchone()[0] == "active"


def test_exclusion_validation_and_missing_ids(world):
    r = world.c.post("/exclusions/add", {"name": "", "reason": "because", "domain": "not a host!"})
    assert r.code == 400 and "name is required" in r.body and "reason must be one of" in r.body and "valid host" in r.body
    assert world.c.post("/exclusions/9999/remove").code == 303
    assert world.c.post("/exclusions/abc/remove").code == 404


# ======================== enrich review ===============================================================================

PROPOSALS = (
    "name,domain,hq_city,chicagoland,revenue,employees,ownership,sponsor,industry,tier,cik,evidence_urls,notes,sec_name\n"
    'Heldco,heldco.com,,yes,800000000,,private,,Tools,,,"il_county=https://heldco.com/locations; revenue_usd=https://www.sec.gov/x",FY2025,\n'
    'Hotco,hotco.com,,,999,,,,,,,,,\n'
    "Ghost,ghost.com,,,,,,,,,,,,\n")


@pytest.fixture
def prop(world):
    p = world.tmp / "enrich_proposals.csv"
    p.write_text(PROPOSALS)
    world.conn.execute("UPDATE accounts SET revenue = 500000000 WHERE id=?", (world.hot,))
    world.conn.commit()
    return p


def test_enrich_review_lists_each_fact_with_evidence_and_conflicts_unticked(world, prop):
    b = world.c.get("/enrich").body
    assert "Heldco" in b and 'href="https://heldco.com/locations"' in b and 'href="https://www.sec.gov/x"' in b
    assert re.search(r'value="chicagoland"[^>]*checked', b) and re.search(r'value="revenue"[^>]*checked', b)
    hot_section = b[b.index("Hotco"):b.index("Ghost")]
    assert 'value="revenue"' in hot_section and "checked" not in hot_section.split('value="revenue"')[1].split(">")[0]
    assert "differs from a known value" in b and "not in the database" in b and "FY2025" in b


def test_applying_takes_values_from_the_file_not_the_form_and_only_ticked_columns(world, prop):
    forged = {"file": "enrich_proposals.csv", "account_id": str(world.held), "apply": ["chicagoland", "revenue"],
              "revenue": "1", "val_revenue": "1", "chicagoland": "no"}
    r = world.c.post("/enrich/apply", forged)
    assert r.code == 303
    a = world.conn.execute("SELECT * FROM accounts WHERE id=?", (world.held,)).fetchone()
    assert (a["chicagoland"], a["revenue"], a["industry"], a["ownership"]) == ("yes", 8e8, None, None)  # file values; unticked untouched
    assert world.conn.execute("SELECT COUNT(*) FROM audit_log WHERE actor='enrich' AND action='update'").fetchone()[0] == 2
    assert world.conn.execute("SELECT new FROM audit_log WHERE actor='enrich' AND action='status'").fetchone() is None   # scanned before and after: no status change
    assert "Applied 2 fact" in world.c.get(r.location).body


def test_a_conflict_is_applied_only_when_explicitly_ticked(world, prop):
    world.c.post("/enrich/apply", {"file": "enrich_proposals.csv", "account_id": str(world.hot), "apply": []})
    assert world.conn.execute("SELECT revenue FROM accounts WHERE id=?", (world.hot,)).fetchone()[0] == 5e8
    world.c.post("/enrich/apply", {"file": "enrich_proposals.csv", "account_id": str(world.hot), "apply": ["revenue"]})
    assert world.conn.execute("SELECT revenue FROM accounts WHERE id=?", (world.hot,)).fetchone()[0] == 999


def test_enrich_apply_ignores_columns_that_are_not_in_the_proposal(world, prop):
    world.c.post("/enrich/apply", {"file": "enrich_proposals.csv", "account_id": str(world.held), "apply": ["status", "name", "tier", "ownership"]})
    a = world.conn.execute("SELECT * FROM accounts WHERE id=?", (world.held,)).fetchone()
    assert (a["name"], a["status"], a["tier"]) == ("Heldco", "active", "A")
    assert a["ownership"] == "private"                                     # the one real, proposed column among them


@pytest.mark.parametrize("raw", ["../outside.csv", "/etc/passwd", "enrich_proposals.txt", "sub/../../x.csv", "nope.csv", "", "~/x.csv", "C:\\x.csv"])
def test_enrich_file_paths_are_confined_to_the_working_directory(world, prop, raw):
    outside = world.tmp.parent / "outside.csv"
    outside.write_text(PROPOSALS)
    r = world.c.get("/enrich?" + urlencode({"file": raw}))
    assert r.code == 200 and ("No readable CSV" in r.body or raw == "")
    r = world.c.post("/enrich/apply", {"file": raw, "account_id": str(world.held), "apply": ["chicagoland"]})
    assert world.conn.execute("SELECT chicagoland FROM accounts WHERE id=?", (world.held,)).fetchone()[0] == "unknown" or raw == ""


def test_a_symlink_out_of_the_directory_is_refused(world, tmp_path):
    outside = world.tmp.parent / "secret.csv"
    outside.write_text(PROPOSALS)
    (world.tmp / "link.csv").symlink_to(outside)
    assert "No readable CSV" in world.c.get("/enrich?file=link.csv").body


def test_a_bad_proposals_file_is_reported(world):
    (world.tmp / "enrich_proposals.csv").write_text("name,mystery\nA,1\n")
    assert "unknown columns" in world.c.get("/enrich").body


# ======================== runs and changes =============================================================================

def test_runs_page_totals_by_step_and_marks_unpriced_calls(world):
    world.conn.executemany("INSERT INTO runs(run_date,step,account_id,accounts_scanned,signals_found,tokens,est_cost,errors) VALUES (?,?,?,?,?,?,?,?)", [
        ("2026-10-15", "scan:leadership", world.hot, 1, 2, 1500, 0.027, None),
        ("2026-10-15", "scan:leadership", world.held, 1, 0, 1000, None, "rejected: url_not_retrieved=1"),
        ("2026-10-15", "scan:total", None, 2, 2, 2500, 0.027, None)])
    world.conn.commit()
    b = world.c.get("/runs").body
    totals = b.split("Latest 300")[0]
    assert "scan:leadership" in totals and "2,500" in totals and "$0.03" in totals and "1 unpriced" in totals
    assert "(whole run)" in b and "url_not_retrieved" in b and "unpriced" in b.split("Latest 300")[1]


def test_changes_page_lists_edits(world):
    world.c.post(f"/accounts/{world.held}", {"hq_city": "Evanston", "tier": "A"})
    b = world.c.get("/audit").body
    assert "Evanston" in b and "hq_city" in b and "Heldco" in b and "ui" in b


def test_a_step_with_only_unpriced_calls_never_shows_a_fake_zero(world):
    world.conn.execute("INSERT INTO runs(run_date,step,account_id,accounts_scanned,signals_found,tokens,est_cost,errors) VALUES (?,?,?,?,?,?,?,?)",
                       ("2026-10-15", "scan:events", world.hot, 1, 0, 900, None, None))
    world.conn.commit()
    totals = world.c.get("/runs").body.split("Latest 300")[0]
    row = totals[totals.index("scan:events"):]
    assert "unpriced" in row.split("</tr>")[0] and "$0.00" not in row.split("</tr>")[0]
