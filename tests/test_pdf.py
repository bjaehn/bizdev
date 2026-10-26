"""PDF export of the Analysis page and of a company brief: the page's own print view, drawn by headless Chromium in a child process (bd/pdf.py).

The child is faked here (a small script that plays the part of `python -m bd.pdf`) so these tests need no browser; one test at the end renders for real and is
skipped when no Chromium is installed. Companies and pages are synthetic."""

import glob
import json
import os
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from datetime import date

import pytest

from bd import pdf
from bd.pdf import available as real_available
from bd.score_store import score_accounts
from bd.ui.app import Request
from tests.conftest import add_account, add_signal
from tests.test_hosted import HOST, call
from tests.test_update import app_env  # noqa: F401  (fixture)

TODAY = date.today()


def worker(tmp_path, body, name="worker.py"):
    p = tmp_path / name
    p.write_text(textwrap.dedent(body))
    return [sys.executable, str(p)]


# ---- the file name ---------------------------------------------------------------------------------------------------------------------------------
def test_file_names_are_ascii_slugs_with_the_date():
    assert pdf.filename("analysis", date(2026, 10, 26)) == "sei-bd-analysis-2026-10-26.pdf"
    assert pdf.filename("brief", date(2026, 10, 26), "Lakeshore Software, Inc.") == "sei-bd-brief-lakeshore-software-inc-2026-10-26.pdf"
    odd = pdf.filename("brief", date(2026, 10, 26), 'Ünï"co\r\nde; <b>x</b> ' + "y" * 80)
    assert odd.isascii() and all(c not in odd for c in '"\r\n;<>/\\ ') and odd.endswith("-2026-10-26.pdf") and len(odd) < 90
    assert pdf.filename("brief", date(2026, 10, 26), "日本語") == "sei-bd-brief-2026-10-26.pdf"        # nothing ASCII left: the slug is left out, not left empty


# ---- is the engine installed -----------------------------------------------------------------------------------------------------------------------
def test_available_needs_the_playwright_package_and_a_chromium(tmp_path, monkeypatch):
    pytest.importorskip("playwright")
    monkeypatch.setattr(pdf, "available", real_available)                          # the suite otherwise reads it as not installed
    monkeypatch.delenv(pdf.CHROME_ENV, raising=False)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    assert not pdf.available() and "not installed" in pdf.status_line()
    (tmp_path / "chromium_headless_shell-1234").mkdir()
    assert pdf.available() and "ready" in pdf.status_line()
    other = tmp_path / "other"
    other.mkdir()
    (other / "chromium-1234").mkdir()
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(other))
    assert pdf.available()
    chrome = tmp_path / "chrome"
    monkeypatch.setenv(pdf.CHROME_ENV, str(chrome))                               # an explicit path must exist, whatever else is installed
    assert not pdf.available()
    chrome.write_text("")
    assert pdf.available()
    real = pdf.importlib.util.find_spec
    monkeypatch.setattr(pdf.importlib.util, "find_spec", lambda name, *a: None if name == "playwright" else real(name, *a))
    assert not pdf.available()


# ---- the child process -----------------------------------------------------------------------------------------------------------------------------
OK = """
    import json, os, sys
    job = json.loads(sys.stdin.buffer.read())
    sys.stdout.buffer.write(b"%PDF-fake " + json.dumps({"footer": job["footer"], "html": job["html"], "env": sorted(os.environ), "home": os.environ["HOME"],
                                                        "cwd": os.getcwd()}).encode())
"""


def test_render_hands_the_page_to_the_child_and_returns_its_pdf(tmp_path, monkeypatch):
    monkeypatch.setattr(pdf, "WORKER", worker(tmp_path, OK))
    out = pdf.render("<p>héllo ▲</p>", "BD Signals · Analysis")
    assert out.startswith(b"%PDF")
    got = json.loads(out[len(b"%PDF-fake "):])
    assert got["html"] == "<p>héllo ▲</p>" and got["footer"] == "BD Signals · Analysis"


def test_the_child_gets_no_secrets_and_a_temporary_home_that_is_removed(tmp_path, monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "BD_UI_PASSWORD", "BD_SESSION_SECRET", "DATABASE_URL", "RAILWAY_TOKEN"):
        monkeypatch.setenv(k, "do-not-leak")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/browsers")
    monkeypatch.setattr(pdf, "WORKER", worker(tmp_path, OK))
    got = json.loads(pdf.render("<p>x</p>", "f")[len(b"%PDF-fake "):])
    assert set(got["env"]) <= set(pdf.CHILD_ENV) | {"HOME", "PYTHONPATH", "LC_CTYPE", "PWD", "SHLVL", "_"}
    assert "PLAYWRIGHT_BROWSERS_PATH" in got["env"] and not {"ANTHROPIC_API_KEY", "BD_UI_PASSWORD", "BD_SESSION_SECRET", "DATABASE_URL", "RAILWAY_TOKEN"} & set(got["env"])
    assert got["home"] != os.environ.get("HOME") and got["cwd"] == got["home"]
    assert not os.path.exists(got["home"])


def test_a_failing_child_gives_one_plain_line_and_a_missing_document_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(pdf, "WORKER", worker(tmp_path, "import sys\nsys.stdin.buffer.read()\nsys.stderr.write('Error: first\\nExecutable does not exist\\n')\nsys.exit(2)\n"))
    with pytest.raises(pdf.PdfError, match="The PDF engine failed: Executable does not exist"):
        pdf.render("<p>x</p>", "f")
    monkeypatch.setattr(pdf, "WORKER", worker(tmp_path, "import sys\nsys.stdin.buffer.read()\nsys.stdout.buffer.write(b'not a pdf')\n", "nodoc.py"))
    with pytest.raises(pdf.PdfError, match="returned no document"):
        pdf.render("<p>x</p>", "f")
    monkeypatch.setattr(pdf, "WORKER", [str(tmp_path / "does-not-exist")])
    with pytest.raises(pdf.PdfError, match="could not start"):
        pdf.render("<p>x</p>", "f")


def test_a_render_that_takes_too_long_is_stopped_with_everything_it_started(tmp_path, monkeypatch):
    pidfile = tmp_path / "grandchild.pid"
    monkeypatch.setattr(pdf, "TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(pdf, "WORKER", worker(tmp_path, f"""
        import subprocess, sys, time
        sys.stdin.buffer.read()
        p = subprocess.Popen(["sleep", "60"])
        open({str(pidfile)!r}, "w").write(str(p.pid))
        time.sleep(60)
    """))
    t = time.time()
    with pytest.raises(pdf.PdfError, match="longer than 1 seconds"):
        pdf.render("<p>x</p>", "f")
    assert time.time() - t < 10
    pid = int(pidfile.read_text())
    for _ in range(50):                                                              # the browser is a grandchild: it must not outlive the render
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("the grandchild process is still running")


def test_only_one_export_runs_at_a_time_and_a_page_that_is_too_large_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(pdf, "WORKER", worker(tmp_path, OK))
    monkeypatch.setattr(pdf, "WAIT_SECONDS", 0.2)
    pdf._one_at_a_time.acquire()
    try:
        with pytest.raises(pdf.PdfBusy, match="Another PDF is still being made"):
            pdf.render("<p>x</p>", "f")
    finally:
        pdf._one_at_a_time.release()
    assert pdf.render("<p>x</p>", "f").startswith(b"%PDF")                         # released: the next one runs
    monkeypatch.setattr(pdf, "MAX_PAYLOAD_BYTES", 50)
    with pytest.raises(pdf.PdfError, match="too large"):
        pdf.render("<p>" + "x" * 100 + "</p>", "f")


def test_the_footer_text_is_escaped_in_the_template_the_browser_gets():
    t = pdf._footer_template('BD Signals · <b onload="x">Acme</b> & Co')
    assert "&lt;b onload=&quot;x&quot;&gt;Acme&lt;/b&gt; &amp; Co" in t and "<b onload" not in t and 'class="pageNumber"' in t and 'class="totalPages"' in t


# ---- the routes and the pages ----------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def seen(monkeypatch):
    """Pretend the engine is installed and record what the app hands it."""
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(pdf, "available", lambda: True)
    monkeypatch.setattr(pdf, "render", lambda html, footer: calls.append((html, footer)) or b"%PDF-1.4 fake")
    return calls


def seed(e):
    a = add_account(e.conn, "Readyco", domain="readyco.example")
    add_signal(e.conn, e.cfg, a, "leadership_change", 5)
    add_signal(e.conn, e.cfg, a, "hiring_cluster", 5)
    b = add_account(e.conn, "Lowco", domain="lowco.example")
    add_signal(e.conn, e.cfg, b, "hiring_cluster", 20)
    score_accounts(e.conn, e.cfg, TODAY)
    return a


def test_the_analysis_pdf_is_the_same_page_in_pdf_mode(app_env, seen):  # noqa: F811
    e = app_env
    seed(e)
    e.app.say("ok", "SECRET-NOTICE")
    r = e.get("/opportunities/pdf")
    assert r.code == 200 and r.body == "%PDF-1.4 fake" and r.h["Content-Type"] == "application/pdf"
    assert r.h["Content-Disposition"] == f'attachment; filename="sei-bd-analysis-{TODAY.isoformat()}.pdf"'
    html, footer = seen[0]
    assert footer == f"BD Signals · Analysis · exported {TODAY:%b} {TODAY.day}, {TODAY.year}"
    assert "Readyco" in html and "<title>Analysis" in html
    assert "SECRET-NOTICE" not in html and "SECRET-NOTICE" in e.get("/opportunities").body              # nobody's notice is used up by an export
    assert 'class="tabs"' not in html and "Showing all ranked companies" in html and "Show everyone" not in html
    assert "<details" not in html.replace("<details open", "")                                              # a PDF cannot expand anything
    assert "Export PDF" not in html                                                                         # the link is not part of the document
    assert '<base href="http://' + HOST + '/">' in html


def test_the_analysis_pdf_keeps_the_filters_of_the_page_it_was_opened_from(app_env, seen):  # noqa: F811
    e = app_env
    seed(e)
    page = e.get("/opportunities?band=Watch&industry=Software&tracked=1")
    assert 'href="/opportunities/pdf?band=Watch&amp;industry=Software&amp;tracked=1"' in page.body
    assert 'href="/opportunities/pdf"' in e.get("/opportunities").body
    e.get("/opportunities/pdf?band=Watch&tracked=1&industry=Software")
    html = seen[0][0]
    assert "Only Software companies." in html and "Show everyone" not in html and "Showing everything scanned" in html
    e.get("/opportunities/pdf?band=Hot")
    assert "Showing Hot companies only" in seen[1][0]


def test_the_company_brief_pdf_names_the_company_and_404s_for_an_unknown_one(app_env, seen):  # noqa: F811
    e = app_env
    a = seed(e)
    r = e.get(f"/companies/{a}/pdf")
    assert r.code == 200 and r.h["Content-Type"] == "application/pdf"
    assert r.h["Content-Disposition"] == f'attachment; filename="sei-bd-brief-readyco-{TODAY.isoformat()}.pdf"'
    html, footer = seen[0]
    assert "Readyco" in html and footer.startswith("BD Signals · Readyco · exported ") and "<details" not in html.replace("<details open", "") and "Export PDF" not in html
    assert f'href="/companies/{a}/pdf">Export PDF' in e.get(f"/companies/{a}").body
    n = len(seen)
    r = e.get("/companies/99999/pdf")
    assert r.code == 404 and "No such company" in r.body and len(seen) == n


def test_links_are_offered_only_when_the_engine_is_installed(app_env, monkeypatch):  # noqa: F811
    e = app_env
    a = seed(e)
    monkeypatch.setattr(pdf, "available", lambda: False)
    assert "Export PDF" not in e.get("/opportunities").body
    assert "Export PDF" not in e.get(f"/companies/{a}").body and "Print (Save as PDF)" in e.get(f"/companies/{a}").body        # the old hint stays
    for path in ("/opportunities/pdf", f"/companies/{a}/pdf"):
        r = e.get(path)
        assert r.code == 503 and "PDF export is not installed on this server" in r.body
    monkeypatch.setattr(pdf, "available", lambda: True)
    assert "Export PDF" in e.get("/opportunities").body and "Export PDF" in e.get(f"/companies/{a}").body


def test_a_busy_or_failed_engine_gives_a_plain_503_page(app_env, monkeypatch):  # noqa: F811
    e = app_env
    seed(e)
    monkeypatch.setattr(pdf, "available", lambda: True)

    def busy(html, footer):
        raise pdf.PdfBusy("Another PDF is still being made. Try again in a moment.")

    def broken(html, footer):
        raise pdf.PdfError("The PDF engine failed: Executable does not exist")

    monkeypatch.setattr(pdf, "render", busy)
    r = e.get("/opportunities/pdf")
    assert r.code == 503 and "Another PDF is still being made" in r.body and r.h.get("Content-Disposition") is None
    monkeypatch.setattr(pdf, "render", broken)
    r = e.get("/opportunities/pdf")
    assert r.code == 503 and "The PDF could not be made. The PDF engine failed: Executable does not exist" in r.body


def test_a_signed_out_visitor_gets_no_pdf(app_env, seen):  # noqa: F811
    e = app_env
    a = seed(e)
    for path in ("/opportunities/pdf", f"/companies/{a}/pdf"):
        r = call(e.app, "GET", path)
        assert r.code == 303 and r.h["Location"] == "/login" and not seen


def test_links_in_the_pdf_resolve_against_the_host_the_page_was_asked_from_and_only_a_plain_one(app_env):  # noqa: F811
    e = app_env
    a = seed(e)
    acct = next(x for x in __import__("bd.accounts", fromlist=["list_accounts"]).list_accounts(e.conn, status=None) if x.id == a)

    def doc(**headers):
        req = Request("GET", "/companies/1/pdf", {}, headers, {})
        return e.app.document(req, "company.html", **e.app._company_ctx(e.conn, acct))

    assert '<base href="https://bd.example.app/">' in doc(host="bd.example.app", **{"x-forwarded-proto": "https"})
    assert '<base href="http://127.0.0.1:8080/">' in doc(host="127.0.0.1:8080")
    for bad in ("", "evil.example/<script>", 'a" onload="x', "bd.example.app:99999999", "[::1]:80", "a b"):
        assert "<base" not in doc(host=bad), bad
    assert "<base" not in e.get(f"/companies/{a}").body                                                     # the page itself never carries one


def test_bd_check_says_whether_pdf_export_is_installed(monkeypatch):
    from typer.testing import CliRunner
    from bd.cli import app
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(pdf, "available", lambda: False)
    out = CliRunner().invoke(app, ["check"]).output.splitlines()
    assert out[0].startswith("Code:") and out[1].startswith("PDF export: not installed")
    monkeypatch.setattr(pdf, "available", lambda: True)
    assert CliRunner().invoke(app, ["check"]).output.splitlines()[1].startswith("PDF export: ready")


# ---- the real thing --------------------------------------------------------------------------------------------------------------------------------
def a_chromium():
    return os.environ.get(pdf.CHROME_ENV) or next(iter(sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome"))), None)


@pytest.mark.skipif(not a_chromium() or not shutil.which("pdftotext"), reason="needs a Chromium and poppler's pdftotext")
def test_both_exports_render_for_real(app_env, monkeypatch):  # noqa: F811
    pytest.importorskip("playwright")
    monkeypatch.setattr(pdf, "available", real_available)
    monkeypatch.setenv(pdf.CHROME_ENV, a_chromium())
    e = app_env
    a = seed(e)
    cookie = call(e.app, "POST", "/login", {"password": "correct horse battery staple"}).h["Set-Cookie"].split(";")[0].split("=", 1)[1]

    def raw(path):
        env = {"REQUEST_METHOD": "GET", "PATH_INFO": path, "QUERY_STRING": "", "HTTP_HOST": HOST, "CONTENT_LENGTH": "0", "wsgi.input": __import__("io").BytesIO(b""),
               "REMOTE_ADDR": "9.9.9.9", "HTTP_COOKIE": f"bd_session={cookie}"}
        out = {}
        body = b"".join(e.app(env, lambda s, h: out.update(status=s, headers=dict(h))))
        return out["status"], out["headers"], body

    for path, needles in (("/opportunities/pdf", ("Analysis", "Readyco", "Page 1 of")), (f"/companies/{a}/pdf", ("Readyco", "Page 1 of"))):
        status, headers, body = raw(path)
        assert status.startswith("200") and headers["Content-Type"] == "application/pdf" and body.startswith(b"%PDF") and len(body) > 5000
        f = e.tmp / "out.pdf"
        f.write_bytes(body)
        text = subprocess.run(["pdftotext", "-layout", str(f), "-"], capture_output=True, text=True, check=True).stdout
        for needle in needles:
            assert needle in text, (path, needle)
        assert "Sign out" not in text and "Export PDF" not in text                                          # none of the page's chrome is in the document
