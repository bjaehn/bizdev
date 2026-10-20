"""WSGI app for the local review-and-curate UI.

What it can do: show this week's leads with their sources and score breakdown, list and edit account
facts, review `bd enrich` proposals fact by fact, manage exclusions, and show run history and cost.
What it can NOT do, by construction: scan, score, call an LLM, or post to Teams. This module imports none of
that code (a test enforces it).

Security model (it listens on 127.0.0.1 only, but a local web server is still reachable from any web page you
happen to have open, so it is hardened as such):
  * a random per-run token: you open the URL `bd ui` prints once; it sets an HttpOnly, SameSite=Strict cookie,
    and every other request must carry that cookie;
  * the Host header must be 127.0.0.1 or localhost on our port (defeats DNS rebinding);
  * every POST needs a CSRF token that only our own pages contain, and a same-origin Origin/Sec-Fetch-Site;
  * pages carry a Content-Security-Policy that forbids all scripts, and Jinja autoescaping is on;
  * links to the web are only ever http(s), opened with rel="noopener noreferrer";
  * enrich files are read only from under the directory `bd ui` was started in, and only *.csv;
  * request bodies are size-limited."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import os
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlparse

from jinja2 import Environment, PackageLoader, select_autoescape

from bd.accounts import Account, list_accounts
from bd.brief_core import fallback_brief
from bd.config import Config
from bd.curate import (EXCLUSION_REASONS, accept_candidate, add_exclusion, apply_all_new, apply_selected, create_account, load_proposals,
                       mark_reviewed, reject_candidate, remove_exclusion, update_account)
from bd.db import connect, init_db
from bd.fit_gate import check_fit, load_exclusions
from bd.charts import spark_svg, trend_svg, weekly_svg
from bd.signal_coverage import signal_coverage
from bd.feed import FEED_DAYS, GROUP_LABELS, GROUPS as FEED_GROUPS, load_items as load_feed_items
from bd.trend import KIND_LABELS, TREND_KINDS
from bd.week import build_week, newest_signal, signs_line
from bd.profile import brief_text, build_profile
from bd.health import build_health
from bd.market import build_market
from bd.jobs import COMPARE_BUDGET, FIND_BUDGET, KINDS, LOOKUP_BUDGET, SCANNER_NAMES, JobError, JobRunner, company_name
from bd.scheduler import next_run
from bd.ui.auth import Hosted, Throttle
from bd.lookup import find as find_company
from bd.labels import FAMILY_LABELS, TYPE_LABELS, fmt_date, money
from bd.pe_store import add_firm, set_firm_status
from bd.ask_store import add_question, clean_question, recent as recent_questions
from bd.rfp_store import list_open as list_open_rfps
from bd.pipeline import (MAX_CLIENT_NAMES, STAGE_LABELS, STAGES, add_note, client_rows, default_since, get_state, mark_clients, notes_for, set_client,
                         set_stage, set_watching, stage_counts, watchlist_changes)
from bd.opportunities import build_opportunities, filter_opportunities, score_dates
from bd.opportunity_model import assess_account, kind_label, rules_text
from bd.pe_view import GEO, collection_status, company_pe, firm_detail, firm_directory, fmt_ts, ownership_detail, run_detail, run_history as pe_run_history
from bd.setup_check import static_problems
from bd.settings_store import (UPDATE_FIELDS, load_update, save_update, INT_FIELDS, MAX_SEGMENTS, apply_overrides, load_discovery_overrides, parse_discovery,
                               refresh_enabled, reset_discovery, save_discovery, save_refresh)
from bd.shortlist import ShortlistError, build_shortlist

MAX_BODY = 64 * 1024
CSP = ("default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; "
       "frame-ancestors 'none'; img-src 'none'; script-src 'none'")


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    cookies: dict[str, str]
    form: dict[str, list[str]] = field(default_factory=dict)

    def q(self, k: str, default: str = "") -> str:
        return (self.query.get(k) or [default])[0]

    def f(self, k: str, default: str = "") -> str:
        return (self.form.get(k) or [default])[0]

    def flist(self, k: str) -> list[str]:
        return self.form.get(k) or []


@dataclass
class Response:
    status: str = "200 OK"
    body: bytes = b""
    headers: list[tuple[str, str]] = field(default_factory=list)


def show(v: object) -> str:
    return "unknown" if v is None or v == "" else str(v)


def fact(v: object, column: str = "") -> str:
    """Show a fact for review: whole numbers with thousands separators, never '780000000.0'."""
    if v is None or v == "":
        return "unknown"
    if column in ("revenue", "employees") or isinstance(v, (int, float)):
        try:
            return f"{float(v):,.0f}"  # type: ignore[arg-type]
        except (TypeError, ValueError):
            pass
    return str(v)


def host(u: object) -> str:
    return (urlparse(str(u or "")).hostname or "").removeprefix("www.")


def safe_url(u: object) -> str:
    """Only http(s) URLs may become links."""
    u = str(u or "")
    return u if urlparse(u).scheme in ("http", "https") and urlparse(u).netloc else ""


# Two areas on one login. ANALYSIS is what a viewer uses: This week, the Companies explorer, a company's brief and the extra reports.
# ADMIN is everything that maintains the data or runs the machine. (page key, href, label, hint, hosted only)
REPORT_ITEMS = (
    ("market", "/market", "Market view", "Where signals are appearing across industries"),
    ("firms", "/firms", "Private equity firms", "Owners and their portfolio companies"),
    ("rfps", "/rfps", "Open RFPs", "Public solicitations from Chicago-area agencies"),
    ("ask", "/ask", "Ask your data", "Plain-English questions about your own data"),
    ("card", "/monday", "Monday card", "What the weekly post would contain"),
)
ADMIN_GROUPS = (
    ("Update", (("home", "/", "Update my list", "One press fills in facts, scans, scores and ranks", True),
                ("operate", "/operate", "Advanced tools", "Run one step at a time", True))),
    ("Companies", (("accounts", "/accounts", "All companies", "Every company on your list and its facts", False),
                   ("lookup", "/lookup", "Look up a company", "Find any company, or add one and check the SEC", False),
                   ("candidates", "/candidates", "New companies to review", "Found by research; add or reject", False),
                   ("enrich", "/enrich", "Researched facts to review", "Check facts before applying them", False),
                   ("exclusions", "/exclusions", "Do-not-pursue list", "Clients and companies to skip", False),
                   ("clients", "/clients", "Existing clients", "SEI clients, kept apart from new business", False),
                   ("watchlist", "/watchlist", "Watchlist and pursuits", "Companies you follow, and where each pursuit stands", False))),
    # The extra reports are not part of the everyday screens; the Admin list is where they live. They open in the analysis layout.
    ("Reports", tuple((key, href, label, hint, False) for key, href, label, hint in REPORT_ITEMS)),
    ("System", (("health", "/health", "Data health", "What is producing, what it costs, what is missing", False),
                ("runs", "/runs", "Run history and cost", "What ran and what it cost", False),
                ("audit", "/audit", "Change log", "Who changed what, with sources", False),
                ("settings", "/settings", "Settings", "Numbers behind the Update button", False))),
)
KIND_COLORS = {"leadership": "var(--k-lead)", "hiring": "var(--k-hire)", "press": "var(--k-press)", "events": "var(--k-event)"}
SIGNALS_MAX = 200               # rows on the Signals page
ADMIN_PAGES = frozenset(key for g, items in ADMIN_GROUPS if g != "Reports" for key, *_ in items) | {"admin", "jobs"}

class App:
    def __init__(self, cfg: Config, db_path: str, token: str = "", root: Path | None = None,
                 hosted: Hosted | None = None, jobs: JobRunner | None = None,
                 throttle: Throttle | None = None) -> None:
        self.cfg, self.db_path, self._token = cfg, db_path, token
        self.hosted, self.jobs = hosted, jobs
        self.throttle = throttle or Throttle()
        self.root = (root or Path.cwd()).resolve()
        self.port: int | None = None
        self._csrf = hosted.csrf if hosted else hmac.new(token.encode(), b"csrf", hashlib.sha256).hexdigest()
        self._flash: list[tuple[str, str]] = []
        self.env = Environment(loader=PackageLoader("bd.ui", "templates"), autoescape=select_autoescape(["html"]),
                               trim_blocks=True, lstrip_blocks=True)
        self.env.filters.update(band_name=lambda b: {'Stored': 'Below Watch'}.get(b, b), status_label=lambda v: {'active': 'Scanned for signals', 'hold_review': 'Unverified', 'unverified': 'Unverified', 'excluded': 'Out of scope'}.get(v, v), money=money, show=show, fact=fact, host=host, safe_url=safe_url, fdate=lambda d: fmt_date(date.fromisoformat(d)) if isinstance(d, str) else fmt_date(d))
        self.env.globals.update(csrf=self._csrf, family_labels=FAMILY_LABELS, reasons=EXCLUSION_REASONS, labels=TYPE_LABELS,
                                hosted=hosted is not None, operate=jobs is not None, signs_line=signs_line, newest_signal=newest_signal, spark=spark_svg, kind_colors=KIND_COLORS, admin_pages=ADMIN_PAGES, admin_groups=ADMIN_GROUPS,
                                report_pages=frozenset(k for k, *_ in REPORT_ITEMS) | {"reports"}, find_budget=FIND_BUDGET,
                                fmt_ts=fmt_ts, priority_rules=rules_text(self.cfg), kind_label=kind_label)
        init_conn = connect(db_path)
        init_db(init_conn)
        init_conn.close()

    # ---- plumbing -----------------------------------------------------------------------------------------
    def allowed_hosts(self) -> set[str]:
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}

    def __call__(self, environ, start_response):
        try:
            resp = self.handle(self._request(environ))
        except _BadRequest as e:
            resp = self.error("400 Bad Request", str(e))
        except Exception:                                   # never leak a traceback (paths, SQL) to the page
            resp = self.error("500 Internal Server Error", "Something went wrong. Check the server log.")
            import traceback
            traceback.print_exc()
        resp.headers += [("Content-Security-Policy", CSP), ("X-Content-Type-Options", "nosniff"),
                         ("Referrer-Policy", "same-origin"), ("Cache-Control", "no-store"),
                         ("X-Frame-Options", "DENY")]
        if self.hosted and environ.get("HTTP_X_FORWARDED_PROTO", "") == "https":
            resp.headers.append(("Strict-Transport-Security", "max-age=31536000"))
        start_response(resp.status, resp.headers)
        return [resp.body]

    def _request(self, environ) -> Request:
        headers = {k[5:].replace("_", "-").lower(): v for k, v in environ.items() if k.startswith("HTTP_")}
        headers["remote-addr"] = environ.get("REMOTE_ADDR", "")
        cookies = {k: m.value for k, m in SimpleCookie(headers.get("cookie", "")).items()}
        req = Request(environ["REQUEST_METHOD"], environ.get("PATH_INFO", "/"),
                      parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True), headers, cookies)
        if req.method == "POST":
            try:
                n = int(environ.get("CONTENT_LENGTH") or 0)
            except ValueError:
                raise _BadRequest("bad Content-Length")
            if n > MAX_BODY:
                raise _BadRequest("request too large")
            req.form = parse_qs(environ["wsgi.input"].read(n).decode("utf-8", "replace"), keep_blank_values=True)
        return req

    def render(self, template: str, http_status: str = "200 OK", **ctx) -> Response:
        flash, self._flash = self._flash, []
        body = self.env.get_template(template).render(flash=flash, **ctx).encode()
        return Response(http_status, body, [("Content-Type", "text/html; charset=utf-8")])

    def error(self, status: str, message: str, nav: bool = True) -> Response:
        body = self.env.get_template("error.html").render(status=status, message=message, flash=[], nav=nav).encode()
        return Response(status, body, [("Content-Type", "text/html; charset=utf-8")])

    def redirect(self, to: str, extra: list[tuple[str, str]] | None = None) -> Response:
        return Response("303 See Other", b"", [("Location", to), *(extra or [])])

    def say(self, kind: str, text: str) -> None:
        self._flash.append((kind, text))

    # ---- guard + routing ---------------------------------------------------------------------------------------
    def handle(self, req: Request) -> Response:
        if self.hosted:
            return self.handle_hosted(req)
        if req.headers.get("host", "") not in self.allowed_hosts():
            return self.error("403 Forbidden", "Unexpected Host header. This UI only answers on 127.0.0.1 or localhost.")
        if req.path == "/auth" and req.method == "GET":
            if hmac.compare_digest(req.q("t"), self._token):
                cookie = f"bd_session={self._token}; HttpOnly; SameSite=Strict; Path=/"
                return self.redirect("/", [("Set-Cookie", cookie)])
            return self.error("403 Forbidden", "That link is not valid. Use the URL printed by `bd ui` in your terminal.")
        if not hmac.compare_digest(req.cookies.get("bd_session", ""), self._token):
            return self.error("403 Forbidden", "Not signed in. Open the URL printed by `bd ui` in your terminal.")
        if req.method == "POST":
            origin = req.headers.get("origin")
            if origin == "null":
                if req.headers.get("sec-fetch-site") != "same-origin":
                    return self.error("403 Forbidden", "Cross-origin request refused.")
            elif origin and urlparse(origin).netloc not in self.allowed_hosts():
                return self.error("403 Forbidden", "Cross-origin request refused.")
            if req.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
                return self.error("403 Forbidden", "Cross-site request refused.")
            if not hmac.compare_digest(req.f("csrf"), self._csrf):
                return self.error("403 Forbidden", "Form token missing or wrong. Reload the page and try again.")
        elif req.method != "GET":
            return self.error("405 Method Not Allowed", "Only GET and POST are used.")

        conn = connect(self.db_path)
        try:
            return self.route(req, conn)
        finally:
            conn.close()

    # ---- hosted mode ------------------------------------------------------------------------------------------
    @staticmethod
    def client_id(req: Request) -> str:
        """The caller's address as the platform proxy reports it (first X-Forwarded-For hop)."""
        return (req.headers.get("x-forwarded-for", "").split(",")[0].strip() or req.headers.get("x-real-ip", "")
                or req.headers.get("remote-addr", "") or "unknown")[:64]

    @staticmethod
    def same_origin(req: Request) -> bool:
        """Origin must match our Host. Some browsers send `Origin: null` on form posts; that is accepted only when
        the browser itself says the request is same-origin (Sec-Fetch-Site), and the CSRF token still applies."""
        origin = req.headers.get("origin")
        if not origin:
            return True
        if origin == "null":
            return req.headers.get("sec-fetch-site") == "same-origin"
        return urlparse(origin).netloc == req.headers.get("host", "")

    def _secure(self, req: Request) -> bool:
        return req.headers.get("x-forwarded-proto", "") == "https"

    def _cookie(self, req: Request, value: str, max_age: int) -> str:
        return (f"bd_session={value}; HttpOnly; SameSite=Lax; Path=/; Max-Age={max_age}"
                + ("; Secure" if self._secure(req) else ""))

    def login_page(self, error: str = "", status: str = "200 OK") -> Response:
        # Public page: never touches the shared flash list (that belongs to the signed-in user).
        body = self.env.get_template("login.html").render(error=error, flash=[], nav=False, csrf="").encode()
        return Response(status, body, [("Content-Type", "text/html; charset=utf-8")])

    def handle_hosted(self, req: Request) -> Response:
        assert self.hosted is not None
        if req.path == "/healthz" and req.method in ("GET", "HEAD"):
            return Response("200 OK", b"ok", [("Content-Type", "text/plain; charset=utf-8")])
        if req.method not in ("GET", "POST"):
            return self.error("405 Method Not Allowed", "Only GET and POST are used.", nav=False)
        signed_in = self.hosted.session_ok(req.cookies.get("bd_session", ""))
        if req.path == "/login":
            if req.method == "GET":
                return self.redirect("/opportunities") if signed_in else self.login_page()
            return self.login(req)
        if not signed_in:
            if req.method == "GET":
                return self.redirect("/login")
            return self.error("403 Forbidden", "Not signed in.", nav=False)
        if req.method == "POST":
            if not self.same_origin(req):
                return self.error("403 Forbidden", "Cross-origin request refused.")
            if req.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
                return self.error("403 Forbidden", "Cross-site request refused.")
            if not hmac.compare_digest(req.f("csrf"), self._csrf):
                return self.error("403 Forbidden", "Form token missing or wrong. Reload the page and try again.")
            if req.path == "/logout":
                return self.redirect("/login", [("Set-Cookie", self._cookie(req, "", 0))])
        conn = connect(self.db_path)
        try:
            return self.route(req, conn)
        finally:
            conn.close()

    def login(self, req: Request) -> Response:
        assert self.hosted is not None
        if not self.same_origin(req) or \
                req.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
            return self.error("403 Forbidden", "Cross-origin request refused.", nav=False)
        client = self.client_id(req)
        wait = self.throttle.retry_after(client)
        if wait:
            return self.login_page(f"Too many attempts. Try again in {wait} seconds.", "429 Too Many Requests")
        if self.hosted.check_password(req.f("password")):
            self.throttle.succeeded(client)
            return self.redirect("/opportunities", [("Set-Cookie", self._cookie(req, self.hosted.make_session(),
                                                                   self.hosted.session_hours * 3600))])
        self.throttle.failed(client)
        import time
        time.sleep(self.fail_delay)
        return self.login_page("That password is not right.", "401 Unauthorized")

    fail_delay = 1.0

    # ---- operate (hosted only) --------------------------------------------------------------------------------
    def operate(self, conn: sqlite3.Connection) -> Response:
        assert self.jobs is not None
        env = os.environ
        ready = [("Model access (ANTHROPIC_API_KEY, ANTHROPIC_MODEL)", bool(env.get("ANTHROPIC_API_KEY") and env.get("ANTHROPIC_MODEL"))),
                 ("SEC contact (BD_USER_AGENT), needed for the edgar scanner", bool(env.get("BD_USER_AGENT"))),
                 ("Teams webhook (TEAMS_WEBHOOK_URL), needed for the Monday post", bool(env.get("TEAMS_WEBHOOK_URL")))]
        last = self.jobs.last_scheduled()
        return self.render("operate.html", page="operate", ready=ready, kinds=KINDS, scanners=SCANNER_NAMES,
                           jobs=self.jobs.recent(), busy=self.jobs.busy(), cap=self.cfg.llm.max_cost_per_run_usd, compare_budget=COMPARE_BUDGET,
                           next_run=next_run(datetime.now(timezone.utc)),
                           posts=bool(env.get("TEAMS_WEBHOOK_URL")) and env.get("BD_DRY_RUN") != "1", last=last)

    def job_start(self, req: Request) -> Response:
        assert self.jobs is not None
        kind = req.f("kind")
        form = {k: req.f(k) for k in ("limit", "only", "tier", "no_llm", "scanner", "firm", "force")}
        form["scanners"] = ",".join(req.flist("scanners"))
        try:
            job_id = self.jobs.submit(kind, form, confirmed=bool(req.f("confirm")))
        except JobError as e:
            self.say("err", str(e))
            return self.redirect("/operate")
        return self.redirect(f"/jobs/{job_id}")

    def job_page(self, job_id: int, conn: sqlite3.Connection) -> Response:
        assert self.jobs is not None
        job = self.jobs.get(job_id)
        if job is None:
            return self.error("404 Not Found", "No such job.")
        company = None
        if job.kind in ("lookup", "sec_fill") and "--only" in job.args:        # a one-company job: link straight to the company
            company = conn.execute("SELECT id, name FROM accounts WHERE lower(name) = lower(?)", (job.args[job.args.index("--only") + 1],)).fetchone()
        return self.render("job.html", page="operate", job=job, log=self.jobs.log_tail(job_id), company=company,
                           running=job.status in ("queued", "running"), label=KINDS.get(job.kind, (job.kind,))[0])

    def route(self, req: Request, conn: sqlite3.Connection) -> Response:
        p, m = req.path.rstrip("/") or "/", req.method
        if self.jobs is not None and self.hosted:
            if (m, p) == ("GET", "/operate"):
                return self.operate(conn)
            if (m, p) == ("POST", "/operate/run"):
                return self.job_start(req)
            if (m, p) == ("POST", "/lookup/run"):
                return self.lookup_run(req, conn)
            if (m, p) == ("POST", "/find/run"):
                return self.find_run(req, conn)
            if m == "GET" and p.startswith("/find/wait/") and p[11:].isdigit():
                return self.find_wait(int(p[11:]), conn)
            if m == "GET" and p.startswith("/jobs/") and p[6:].isdigit():
                return self.job_page(int(p[6:]), conn)
        parts = p.strip("/").split("/")
        table = {
            ("GET", "/"): lambda: self.home(conn) if self.jobs is not None else self.opportunities(req, conn),
            ("GET", "/week"): lambda: self.redirect("/opportunities"),
            ("GET", "/signals"): lambda: self.signals_page(req, conn),
            ("GET", "/help"): lambda: self.help_page(),
            ("GET", "/find"): lambda: self.find_page(req, conn),
            ("GET", "/opportunities"): lambda: self.opportunities(req, conn),
            ("POST", "/update"): lambda: self.update_start(req, conn),
            ("POST", "/check"): lambda: self.check_start(),
            ("POST", "/settings/update"): lambda: self.settings_update(req, conn),
            ("GET", "/monday"): lambda: self.leads(req, conn),
            ("GET", "/watchlist"): lambda: self.watchlist(req, conn),
            ("GET", "/admin"): lambda: self.admin_page(),
            ("GET", "/leadership"): lambda: self.redirect("/opportunities"),
            ("GET", "/reports"): lambda: self.reports_page(),
            ("GET", "/lookup"): lambda: self.lookup_page(req, conn),
            ("GET", "/clients"): lambda: self.clients_page(conn),
            ("POST", "/clients/mark"): lambda: self.clients_mark(req, conn),
            ("GET", "/rfps"): lambda: self.rfps_page(req, conn),
            ("GET", "/ask"): lambda: self.ask_page(conn),
            ("POST", "/ask"): lambda: self.ask_start(req, conn),
            ("GET", "/health"): lambda: self.health_page(req, conn),
            ("GET", "/market"): lambda: self.market_page(conn),
            ("GET", "/accounts"): lambda: self.accounts(req, conn),
            ("GET", "/accounts/new"): lambda: self.render("account_new.html", page="accounts", form={}, errors=[]),
            ("POST", "/accounts/new"): lambda: self.account_create(req, conn),
            ("GET", "/enrich"): lambda: self.enrich(req, conn),
            ("POST", "/enrich/apply"): lambda: self.enrich_apply(req, conn),
            ("POST", "/enrich/apply-all"): lambda: self.enrich_apply_all(req, conn),
            ("GET", "/candidates"): lambda: self.candidates(conn),
            ("GET", "/hiring"): lambda: self.redirect("/opportunities" + (f"?theme={quote(req.q('theme'))}" if req.q("theme") else "") + "#hiring"),
            ("POST", "/settings/refresh"): lambda: self.refresh_save(req, conn),
            ("GET", "/firms"): lambda: self.firms(conn),
            ("POST", "/firms/add"): lambda: self.firm_add(req, conn),
            ("GET", "/settings"): lambda: self.settings_page(conn),
            ("POST", "/settings/discovery"): lambda: self.settings_save(req, conn),
            ("POST", "/settings/discovery/reset"): lambda: self.settings_reset(conn),
            ("POST", "/accounts/reviewed-all"): lambda: self.reviewed(conn, None),
            ("GET", "/exclusions"): lambda: self.exclusions(conn),
            ("POST", "/exclusions/add"): lambda: self.exclusion_add(req, conn),
            ("GET", "/runs"): lambda: self.runs(conn),
            ("GET", "/audit"): lambda: self.audit(conn),
        }
        if (m, p) in table:
            return table[(m, p)]()
        if len(parts) == 2 and parts[0] == "accounts" and parts[1].isdigit():
            return self.account_page(req, conn, int(parts[1])) if m == "GET" else self.account_save(req, conn, int(parts[1]))
        if parts[0] == "firms" and len(parts) == 2 and parts[1].isdigit() and m == "GET":
            return self.firm_page(req, conn, int(parts[1]))
        if parts[0] == "firms" and len(parts) == 3 and parts[1] == "runs" and parts[2].isdigit() and m == "GET":
            return self.pe_run_page(conn, int(parts[2]))
        if parts[0] == "ownership" and len(parts) == 2 and parts[1].isdigit() and m == "GET":
            return self.ownership_page(conn, int(parts[1]))
        if parts[0] == "portfolio" and len(parts) == 2 and parts[1].isdigit() and m == "GET":
            return self.portfolio_company_page(conn, int(parts[1]))
        if m == "POST" and len(parts) == 3 and parts[0] == "firms" and parts[1].isdigit() and parts[2] == "status":
            ok = set_firm_status(conn, int(parts[1]), req.f("status"))
            self.say("ok" if ok else "err", "Firm updated." if ok else "Could not update that firm.")
            return self.redirect(f"/firms/{int(parts[1])}")
        if m == "GET" and len(parts) == 3 and parts[0] == "accounts" and parts[1].isdigit() and parts[2] == "onepager":
            return self.redirect(f"/companies/{int(parts[1])}")                                        # the profile is the printable summary now
        if m == "GET" and len(parts) == 2 and parts[0] == "companies" and parts[1].isdigit():
            return self.company_profile(conn, int(parts[1]))
        if m == "GET" and len(parts) == 3 and parts[0] == "companies" and parts[1].isdigit() and parts[2] == "text":
            return self.company_text(conn, int(parts[1]))
        if m == "POST" and len(parts) == 3 and parts[0] == "accounts" and parts[1].isdigit() and parts[2] in ("stage", "note", "watch", "client"):
            return self.pipeline_update(req, conn, int(parts[1]), parts[2])
        if m == "POST" and len(parts) == 3 and parts[0] == "accounts" and parts[1].isdigit() and parts[2] == "reviewed":
            return self.reviewed(conn, int(parts[1]))
        if m == "POST" and len(parts) == 3 and parts[0] == "candidates" and parts[1].isdigit() and parts[2] in ("accept", "reject"):
            return self.candidate_decide(conn, int(parts[1]), parts[2])
        if m == "POST" and len(parts) == 3 and parts[0] == "exclusions" and parts[1].isdigit() and parts[2] == "remove":
            ok = remove_exclusion(conn, self.cfg, int(parts[1]))
            self.say("ok" if ok else "err", "Exclusion removed." if ok else "No such exclusion.")
            return self.redirect("/exclusions")
        return self.error("404 Not Found", "No such page.")

    # ---- pages ------------------------------------------------------------------------------------------------
    def home(self, conn: sqlite3.Connection) -> Response:
        """What state is the app in, and what is the one thing to click next."""
        assert self.jobs is not None
        problems = static_problems(self.cfg, os.environ)
        rows = conn.execute("SELECT status, COUNT(*) n FROM accounts GROUP BY status").fetchall()
        by = {r["status"]: r["n"] for r in rows}
        total, ready = sum(by.values()), by.get("active", 0)
        top, opps = [], 0
        dates = score_dates(conn)
        if dates:
            try:
                ol = build_opportunities(conn, self.cfg, date.fromisoformat(dates[0]))
                top, opps = ol.items[:5], len(ol.items)
            except ShortlistError:
                pass
        u = load_update(conn)
        if not (os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("ANTHROPIC_MODEL")):
            step = ("Finish setup", "The app needs your Anthropic key and model name before it can research anything. See Setup below.", "setup")
        elif total == 0:
            step = ("Press Update my list", "There are no companies yet. The update searches for Chicagoland companies to add.", "update")
        elif ready == 0:
            step = ("Add companies", "Every company is known to be out of scope. Add companies on the Companies page.", "accounts")
        elif opps == 0:
            step = ("Press Update my list", f"{ready} companies are on the scan list. The update scans them for leadership changes, hiring and press.", "update")
        else:
            step = ("Open Analysis", f"{opps} companies are ranked. Press Update my list any time to refresh them.", "opportunities")
        last = self.jobs.last_job(("update",))
        return self.render("home.html", page="home", problems=problems, total=total, ready=ready, opps=opps, top=top, u=u,
                           step=step, last=last, busy=self.jobs.busy(), fields=UPDATE_FIELDS, coverage=signal_coverage(conn, self.cfg, date.today(), u),
                           last_log=self.jobs.log_tail(last.id)[-1500:] if last and last.status == "failed" else "")

    def update_start(self, req: Request, conn: sqlite3.Connection) -> Response:
        assert self.jobs is not None
        form = {k: str(v) for k, v in load_update(conn).items()}
        try:
            job_id = self.jobs.submit("update", form, confirmed=bool(req.f("confirm")))
        except JobError as e:
            self.say("err", str(e))
            return self.redirect("/")
        return self.redirect(f"/jobs/{job_id}")

    def check_start(self) -> Response:
        assert self.jobs is not None
        try:
            return self.redirect(f"/jobs/{self.jobs.submit('check', {})}")
        except JobError as e:
            self.say("err", str(e))
            return self.redirect("/")

    def settings_update(self, req: Request, conn: sqlite3.Connection) -> Response:
        errs = save_update(conn, {k: req.f(k) for k in UPDATE_FIELDS if k in req.form or k != "rfps"})
        if errs:
            self.say("err", "Not saved: " + "; ".join(errs))
        else:
            self.say("ok", "Saved. The next update uses these numbers.")
        return self.redirect("/settings")

    def opportunities(self, req: Request, conn: sqlite3.Connection) -> Response:
        """The one analysis page: the top three, whether things are heating up, everyone ranked, what you track and the hiring at the bottom.
        One build (bd.week) feeds all of it, so the cards and the numbered list cannot disagree."""
        w = build_week(conn, self.cfg, date.today())
        band = req.q("band") if req.q("band") in ("Hot", "Warm", "Watch") else ""
        family = req.q("family") if req.q("family") in FAMILY_LABELS else ""       # the Market heatmap links here with an industry and a signal type
        industry = req.q("industry")[:80]
        everything = req.q("tracked") == "1"
        shown = filter_opportunities(w.items if everything else w.ranked, band=band, family=family, industry=industry)
        link = lambda **kw: "/opportunities?" + urlencode({k: v for k, v in {**kw, "family": family, "industry": industry}.items() if v}) + "#ranked"  # noqa: E731
        tabs = [(label, link(band=b), b == band and not everything) for label, b in (("All", ""), ("Hot", "Hot"), ("Warm", "Warm"), ("Watch", "Watch"))]
        tabs.append(("Everything scanned", link(tracked="1"), everything))
        return self.render("opportunities.html", page="opportunities", w=w, shown=shown, tabs=tabs, band=band, everything=everything, family=family, industry=industry,
                           stage_labels=STAGE_LABELS, kinds=[(k, label) for k, _, label in TREND_KINDS], kind_labels=KIND_LABELS,
                           trend_chart=weekly_svg(w.trend.weeks, w.trend.series, KIND_LABELS),
                           trend_chart_phone=weekly_svg(w.trend.weeks, w.trend.series, KIND_LABELS, width=380, height=250, last=6, fs=14), h=self.hiring_data(conn, req.q("theme")))

    def pipeline_update(self, req: Request, conn: sqlite3.Connection, account_id: int, what: str) -> Response:
        if what == "stage":
            ok = set_stage(conn, account_id, req.f("stage"), req.f("note"))
            self.say("ok" if ok else "err", "Stage saved." if ok else "Nothing to save (unknown stage, or no change).")
        elif what == "note":
            ok = add_note(conn, account_id, req.f("body"))
            self.say("ok" if ok else "err", "Note added." if ok else "Write something first.")
        elif what == "client":
            on = req.f("on") == "1"
            ok = set_client(conn, account_id, on)
            self.say("ok" if ok else "err", ("Marked as an existing client." if on else "No longer marked as a client.") if ok else "No such account.")
        else:
            on = req.f("on") == "1"
            ok = set_watching(conn, account_id, on)
            self.say("ok" if ok else "err", ("Now tracking this company." if on else "No longer tracking this company.") if ok else "No such account.")
        back = req.f("back")
        return self.redirect(back if back.startswith("/") and not back.startswith("//") else f"/accounts/{account_id}")

    def admin_page(self) -> Response:
        groups = [(name, [i for i in items if not i[4] or self.jobs is not None]) for name, items in ADMIN_GROUPS]
        return self.render("admin.html", page="admin", groups=[g for g in groups if g[1]])

    def market_page(self, conn: sqlite3.Connection) -> Response:
        m = build_market(conn, date.today(), families=self.cfg.scoring.families)
        label = lambda d: f"{date.fromisoformat(d):%b} {date.fromisoformat(d).day}"  # noqa: E731
        trend = trend_svg([(label(p.run_date), p.hot, p.warm, p.watch) for p in m.trend])
        return self.render("market.html", page="market", m=m, trend_chart=trend, run_label=label)

    def signals_page(self, req: Request, conn: sqlite3.Connection) -> Response:
        """The whole signal feed: every stored signal in the window, newest first, filtered by kind and by how far back to look."""
        days = int(req.q("days")) if req.q("days") in ("7", "30", "90") else FEED_DAYS
        group = req.q("group") if req.q("group") in GROUP_LABELS else ""
        items = load_feed_items(conn, date.today(), days)
        counts = {key: sum(i.group == key for i in items) for key, _ in FEED_GROUPS}
        mine = [i for i in items if not group or i.group == group]
        return self.render("signals.html", page="signals", groups=FEED_GROUPS, group=group, days=days, counts=counts, total=len(items),
                           items=mine[:SIGNALS_MAX], shown_total=len(mine))

    def help_page(self) -> Response:
        return self.render("help.html", page="help", sc=self.cfg.scoring, bands=self.cfg.scoring.bands, opp=self.cfg.opportunity_model, schedule_note="Mondays 05:00 America/Chicago")

    def reports_page(self) -> Response:
        return self.render("reports.html", page="reports", items=REPORT_ITEMS)

    def lookup_page(self, req: Request, conn: sqlite3.Connection) -> Response:
        q = " ".join(req.q("q").split())[:120]
        hits = find_company(conn, self.cfg, q, date.today().isoformat()) if q else []
        return self.render("lookup.html", page="lookup", q=q, hits=hits, exact=any(h.exact for h in hits), labels=TYPE_LABELS,
                           stage_labels=STAGE_LABELS, can_run=bool(self.jobs is not None and self.hosted),
                           busy=bool(self.jobs and self.jobs.busy()), budget=LOOKUP_BUDGET)

    def _company_for_job(self, conn: sqlite3.Connection, typed: str) -> tuple[str, bool]:
        """The one company a one-company job acts on: the exact match on the list, or the company added now (as a manual entry).
        Raises JobError (with a plain message) when the name is unusable, ambiguous, or cannot be added."""
        exact = [h for h in find_company(conn, self.cfg, typed, date.today().isoformat()) if h.exact]
        if len(exact) > 1:
            raise JobError("More than one company on your list has that name; open the one you mean and use its page.")
        name = company_name({"only": exact[0].account.name if exact else typed}, required=True)
        if exact:
            return name, False
        res = create_account(conn, self.cfg, name, {"tier": "B"}, "ui", extra={"origin": "manual"})
        if not res.ok:
            raise JobError("; ".join(res.errors))
        return name, True

    def lookup_run(self, req: Request, conn: sqlite3.Connection) -> Response:
        """Check one company: add it if it is not on the list, then run the free SEC check or the paid research-and-scan job."""
        assert self.jobs is not None
        typed = " ".join(req.f("name").split())
        back = "/lookup?q=" + quote(typed[:120])
        added = False
        try:
            name, added = self._company_for_job(conn, typed)
            job_id = self.jobs.submit("lookup" if req.f("deep") == "1" else "sec_fill", {"only": name}, confirmed=bool(req.f("confirm")))
        except JobError as e:
            self.say("err", ("Added the company. " if added else "") + str(e))
            return self.redirect(back)
        return self.redirect(f"/jobs/{job_id}")

    def find_page(self, req: Request, conn: sqlite3.Connection) -> Response:
        """The analysis-side search: where does a company stand, and, if it is not on the list or not ready, a free check."""
        q = " ".join(req.q("q").split())[:120]
        hits = find_company(conn, self.cfg, q, date.today().isoformat()) if q else []
        return self.render("find.html", page="find", q=q, hits=hits, exact=any(h.exact for h in hits), labels=TYPE_LABELS, job=None, name="", company=None, running=False,
                           can_run=bool(self.jobs is not None and self.hosted), busy=bool(self.jobs and self.jobs.busy()))

    def find_run(self, req: Request, conn: sqlite3.Connection) -> Response:
        """Anyone signed in can run a signal scan of one company: add it if needed, read its job board for free, search for leadership
        changes, hiring, press and company events (up to FIND_BUDGET dollars of model spend, with the tick-box confirmation), and score it."""
        assert self.jobs is not None
        typed = " ".join(req.f("name").split())
        back = "/find?q=" + quote(typed[:120])
        added = False
        try:
            name, added = self._company_for_job(conn, typed)
            job_id = self.jobs.submit("find", {"only": name}, confirmed=bool(req.f("confirm")))
        except JobError as e:
            self.say("err", ("Added the company. " if added else "") + str(e))
            return self.redirect(back)
        return self.redirect(f"/find/wait/{job_id}")

    def find_wait(self, job_id: int, conn: sqlite3.Connection) -> Response:
        """Progress page for a signal scan; when it finishes it opens the company's brief."""
        assert self.jobs is not None
        job = self.jobs.get(job_id)
        if job is None or job.kind != "find" or "--only" not in job.args:
            return self.error("404 Not Found", "No such scan.")
        name = job.args[job.args.index("--only") + 1]
        acct = conn.execute("SELECT id FROM accounts WHERE lower(name) = lower(?)", (name,)).fetchone()
        if job.status == "ok" and acct:
            return self.redirect(f"/companies/{acct['id']}")
        return self.render("find.html", page="find", q=name, hits=[], exact=False, labels=TYPE_LABELS, job=job, name=name, company=acct,
                           running=job.status in ("queued", "running"), can_run=True, busy=False)

    def clients_page(self, conn: sqlite3.Connection, result=None) -> Response:
        return self.render("clients.html", page="clients", rows=client_rows(conn, date.today()), result=result, labels=TYPE_LABELS,
                           max_names=MAX_CLIENT_NAMES)

    def clients_mark(self, req: Request, conn: sqlite3.Connection) -> Response:
        raw = req.f("names")
        if not raw.strip():
            self.say("err", "Paste at least one company name.")
            return self.redirect("/clients")
        return self.clients_page(conn, result=mark_clients(conn, raw))

    def watchlist(self, req: Request, conn: sqlite3.Connection) -> Response:
        try:
            days = max(1, min(90, int(req.q("days") or 7)))
        except ValueError:
            days = 7
        since = default_since(days=days)
        changes = watchlist_changes(conn, since)
        return self.render("watchlist.html", page="watchlist", changes=changes, days=days, since=since.isoformat(),
                           stage_labels=STAGE_LABELS, labels=TYPE_LABELS, counts=stage_counts(conn))

    def company_profile(self, conn: sqlite3.Connection, account_id: int) -> Response:
        a = next((x for x in list_accounts(conn, status=None) if x.id == account_id), None)
        if a is None:
            return self.error("404 Not Found", "No such company.")
        today = date.today()
        return self.render("company.html", page="company", d=build_profile(conn, self.cfg, a, today), stage_labels=STAGE_LABELS, stages=STAGES,
                           cfg_hiring_days=self.cfg.hiring.window_days, pe=company_pe(conn, self.cfg, today, account_id=account_id), ap=assess_account(conn, self.cfg, account_id, today))

    def company_text(self, conn: sqlite3.Connection, account_id: int) -> Response:
        a = next((x for x in list_accounts(conn, status=None) if x.id == account_id), None)
        if a is None:
            return self.error("404 Not Found", "No such company.")
        text = brief_text(build_profile(conn, self.cfg, a, date.today()), self.cfg.hiring.window_days, safe_url)
        return Response("200 OK", text.encode(), [("Content-Type", "text/plain; charset=utf-8")])

    def health_page(self, req: Request, conn: sqlite3.Connection) -> Response:
        try:
            days = max(1, min(365, int(req.q("days") or 30)))
        except ValueError:
            days = 30
        return self.render("health.html", page="health", h=build_health(conn, self.cfg, date.today(), days))

    def ask_page(self, conn: sqlite3.Connection) -> Response:
        return self.render("ask.html", page="ask", items=recent_questions(conn), available=self.jobs is not None,
                           max_chars=self.cfg.ask.max_question_chars, busy=bool(self.jobs and self.jobs.busy()))

    def ask_start(self, req: Request, conn: sqlite3.Connection) -> Response:
        if self.jobs is None:
            return self.error("404 Not Found", "No such page.")
        q = clean_question(req.f("question"), self.cfg.ask.max_question_chars)
        if q is None:
            self.say("err", f"Write a question of 1 to {self.cfg.ask.max_question_chars} characters.")
            return self.redirect("/ask")
        if not req.f("confirm"):
            self.say("err", "Tick the box to confirm this uses paid model calls.")
            return self.redirect("/ask")
        qid = add_question(conn, q)
        try:
            self.jobs.submit("ask", {"id": str(qid)}, confirmed=True)
        except JobError as e:
            with conn:
                conn.execute("DELETE FROM questions WHERE id = ?", (qid,))
            self.say("err", str(e))
            return self.redirect("/ask")
        self.say("ok", "Question sent. It usually takes under a minute; reload this page to see the answer.")
        return self.redirect("/ask")

    def rfps_page(self, req: Request, conn: sqlite3.Connection) -> Response:
        today = date.today()
        q = {k: req.q(k) for k in ("service_line", "group")}
        q["closed"] = req.q("closed") == "1"
        groups = [g.name for g in self.cfg.rfps.groups]
        rows = list_open_rfps(conn, today, include_closed=q["closed"], service_line=q["service_line"] if q["service_line"] in self.cfg.service_lines else "",
                              grp=q["group"] if q["group"] in groups else "")
        items = [dict(r, lines=json.loads(r["service_lines"]), days_left=(date.fromisoformat(r["due_date"]) - today).days) for r in rows]
        return self.render("rfps.html", page="rfps", items=items, q=q, groups=groups, lines=self.cfg.service_lines,
                           total=conn.execute("SELECT COUNT(*) FROM solicitations").fetchone()[0])

    def leads(self, req: Request, conn: sqlite3.Connection) -> Response:
        dates = [r[0] for r in conn.execute("SELECT DISTINCT run_date FROM scores ORDER BY run_date DESC")]
        if not dates:
            return self.render("leads.html", page="card", dates=[], run_date=None, leads=[], skipped=[], overflow=0, error=None)
        chosen = req.q("date") if req.q("date") in dates else dates[0]
        try:
            sl = build_shortlist(conn, self.cfg, date.fromisoformat(chosen))
        except ShortlistError as e:
            return self.render("leads.html", page="card", dates=dates, run_date=chosen, leads=[], skipped=[], overflow=0, error=str(e))
        briefs = {r["account_id"]: r for r in conn.execute("SELECT * FROM briefs WHERE run_date = ?", (chosen,))}
        posted = {r["account_id"]: r for r in conn.execute("SELECT account_id, posted, cooldown_until FROM scores WHERE run_date = ?", (chosen,))}
        cards = []
        for lead in sl.leads:
            b = briefs.get(lead.account.id)
            fb = fallback_brief(lead, cfg=self.cfg)
            cards.append({
                "lead": lead,
                "why_now": b["why_now"] if b else fb.why_now,
                "why_source": "written brief" if b else "plain source summary (no brief generated yet)",
                "service_line": (b["best_service_line"] if b else fb.best_service_line) or "not determined",
                "families": lead.result.components.get("families", {}),
                "bonus": lead.result.components.get("stacking_bonus", 0),
                "sponsor": lead.result.components.get("sponsor", {}),
                "sequences": lead.result.components.get("sequences", {}),
                "posted": posted[lead.account.id]["posted"] if lead.account.id in posted else 0,
            })
        tracked = sorted((s for s in sl.skipped if s.score >= self.cfg.scoring.bands.watch), key=lambda s: -s.score)
        return self.render("leads.html", page="card", dates=dates, run_date=chosen, leads=cards, skipped=tracked,
                           overflow=len(sl.overflow), error=None)

    def accounts(self, req: Request, conn: sqlite3.Connection) -> Response:
        status = req.q("status")
        q = req.q("q").strip().lower()
        accts = list_accounts(conn, status=None)
        unreviewed = sum(not a.reviewed for a in accts)
        only_new = req.q("origin") == "unreviewed"
        excl = load_exclusions(conn)
        fits = {a.id: check_fit(a, excl, self.cfg) for a in accts}
        counts = {"active": sum(a.status == "active" for a in accts), "excluded": sum(a.status == "excluded" for a in accts),
                  "unverified": sum(a.status == "active" and fits[a.id].unverified for a in accts)}
        rows = []
        for a in accts:
            if status == "unverified":
                if a.status != "active" or not fits[a.id].unverified:
                    continue
            elif status in counts and a.status != status:
                continue
            if (q and q not in a.name.lower()) or (only_new and a.reviewed):
                continue
            rows.append({"a": a, "fit": fits[a.id]})
        return self.render("accounts.html", page="accounts", rows=rows, counts=counts, status=status, q=req.q("q"), total=len(accts),
                           unreviewed=unreviewed, only_new=only_new)

    def _account_ctx(self, conn: sqlite3.Connection, a: Account, form: dict[str, str], errors: list[str]) -> dict:
        fit = check_fit(a, load_exclusions(conn), self.cfg)
        signals = conn.execute("SELECT * FROM signals WHERE account_id = ? ORDER BY event_date DESC, id DESC", (a.id,)).fetchall()
        scores = conn.execute("SELECT run_date, score, band, posted, cooldown_until, rationale FROM scores WHERE account_id = ? ORDER BY run_date DESC LIMIT 12", (a.id,)).fetchall()
        history = conn.execute("SELECT ts, actor, action, field, old, new FROM audit_log WHERE account_id = ? ORDER BY id DESC LIMIT 25", (a.id,)).fetchall()
        firms = conn.execute(
            "SELECT f.id, f.name, f.status, l.status AS link_status FROM portfolio_links l JOIN pe_firms f ON f.id = l.firm_id "
            "WHERE l.account_id = ? ORDER BY f.name", (a.id,)).fetchall()
        postings = conn.execute("SELECT * FROM postings WHERE account_id = ? AND open = 1 ORDER BY posted_date DESC LIMIT 40", (a.id,)).fetchall()
        brief = conn.execute("SELECT why_now, best_service_line, run_date FROM briefs WHERE account_id = ? ORDER BY run_date DESC LIMIT 1",
                             (a.id,)).fetchone()
        top = next((x for x in signals if x["confidence"] in ("High", "Medium")), None)       # newest signal that counts toward the score
        headline = brief["why_now"] if brief else (f"{top['summary']} ({fmt_date(date.fromisoformat(top['event_date']))})" if top else None)
        return dict(headline=headline, state=get_state(conn, a.id), notes=notes_for(conn, a.id), stages=STAGES, stage_labels=STAGE_LABELS, brief=brief,
                    page="accounts", a=a, fit=fit, signals=signals, scores=scores, history=history, form=form, errors=errors,
                    conf_scored={"High", "Medium"}, firms=firms, postings=postings, lead=set(self.cfg.hiring.leadership_levels))

    def account_page(self, req: Request, conn: sqlite3.Connection, account_id: int) -> Response:
        a = next((x for x in list_accounts(conn, status=None) if x.id == account_id), None)
        if a is None:
            return self.error("404 Not Found", "No such account.")
        form = {k: ("" if v is None else str(v)) for k, v in a.model_dump().items()}
        if a.revenue is not None:
            form["revenue"] = f"{a.revenue:.0f}"
        return self.render("account.html", **self._account_ctx(conn, a, form, []))

    def account_save(self, req: Request, conn: sqlite3.Connection, account_id: int) -> Response:
        a = next((x for x in list_accounts(conn, status=None) if x.id == account_id), None)
        if a is None:
            return self.error("404 Not Found", "No such account.")
        form = {k: req.f(k) for k in ("domain", "hq_city", "chicagoland", "revenue", "employees", "ownership", "sponsor", "industry", "tier", "cik")}
        res = update_account(conn, self.cfg, account_id, form)
        if not res.ok:
            return self.render("account.html", http_status="400 Bad Request", **self._account_ctx(conn, a, form, res.errors))
        n = len(res.changed)
        self.say("ok", f"Saved ({n} change{'s' if n != 1 else ''})." if n else "No changes.")
        if res.fit is not None:
            self.say("info", f"Fit gate: {res.fit.verdict}" + (f" ({'; '.join(res.fit.reasons)})" if res.fit.reasons else "") + f" → status {res.fit.status}.")
        return self.redirect(f"/accounts/{account_id}")

    def account_create(self, req: Request, conn: sqlite3.Connection) -> Response:
        form = {k: req.f(k) for k in ("name", "domain", "hq_city", "chicagoland", "revenue", "employees", "ownership", "sponsor", "industry", "tier", "cik")}
        res = create_account(conn, self.cfg, form["name"], {k: v for k, v in form.items() if k != "name"})
        if not res.ok:
            return self.render("account_new.html", http_status="400 Bad Request", page="accounts", form=form, errors=res.errors)
        self.say("ok", f"Added {' '.join(form['name'].split())}.")
        if res.fit is not None:
            self.say("info", f"Fit gate: {res.fit.verdict}" + (f" ({'; '.join(res.fit.reasons)})" if res.fit.reasons else "") + f" → status {res.fit.status}.")
        return self.redirect(f"/accounts/{res.account_id}")

    def _proposal_file(self, raw: str) -> Path | None:
        """A *.csv file under the directory `bd ui` was started in; anything else is refused."""
        try:
            p = (self.root / raw).resolve()
        except (OSError, ValueError):
            return None
        return p if p.suffix.lower() == ".csv" and p.is_file() and self.root in p.parents else None

    def enrich(self, req: Request, conn: sqlite3.Connection) -> Response:
        raw = req.q("file", "enrich_proposals.csv")
        path = self._proposal_file(raw)
        if path is None:
            return self.render("enrich.html", page="enrich", file=raw, props=[], errors=[], missing=True, root=str(self.root))
        props, errors = load_proposals(conn, path)
        return self.render("enrich.html", page="enrich", file=raw, props=props, errors=errors, missing=False, root=str(self.root))

    def enrich_apply(self, req: Request, conn: sqlite3.Connection) -> Response:
        raw = req.f("file")
        path = self._proposal_file(raw)
        dest = f"/enrich?{urlencode({'file': raw})}"
        if path is None or not req.f("account_id").isdigit():
            self.say("err", "That proposals file could not be read.")
            return self.redirect(dest)
        props, _ = load_proposals(conn, path)
        # Values come from re-reading the file, never from the submitted form; the form only says which columns.
        prop = next((x for x in props if x.account_id == int(req.f("account_id"))), None)
        chosen = set(req.flist("apply"))
        picks = {d.column: str(d.proposed) for d in (prop.diffs if prop else []) if d.column in chosen and d.status != "same"}
        if not picks:
            self.say("info", "Nothing was ticked, so nothing changed.")
            return self.redirect(dest)
        res = apply_selected(conn, self.cfg, prop.account_id, picks)  # type: ignore[union-attr]
        if res.ok:
            self.say("ok", f"Applied {len(res.changed)} fact(s) to {prop.name}." + (f" Fit gate: {res.fit.verdict} → {res.fit.status}." if res.fit else ""))  # type: ignore[union-attr]
        else:
            self.say("err", "; ".join(res.errors))
        return self.redirect(dest)

    def enrich_apply_all(self, req: Request, conn: sqlite3.Connection) -> Response:
        raw = req.f("file")
        path = self._proposal_file(raw)
        dest = f"/enrich?{urlencode({'file': raw})}"
        if path is None:
            self.say("err", "That proposals file could not be read.")
            return self.redirect(dest)
        r = apply_all_new(conn, self.cfg, path)
        self.say("ok" if r["facts"] else "info",
                 f"Applied {r['facts']} new fact(s) to {r['accounts']} account(s). Left alone: {r['conflicts_skipped']} fact(s) that differ from "
                 f"what is already stored, {r['not_in_database']} row(s) for companies not in the database"
                 + (f"; {r['failed']} account(s) failed validation" if r["failed"] else "") + ".")
        return self.redirect(dest)

    def reviewed(self, conn: sqlite3.Connection, account_id: int | None) -> Response:
        n = mark_reviewed(conn, account_id)
        self.say("ok", f"{n} account(s) marked reviewed; they now scan like any other.")
        return self.redirect(f"/accounts/{account_id}" if account_id is not None else "/accounts")

    def settings_page(self, conn: sqlite3.Connection, form: dict[str, str] | None = None, errors: list[str] | None = None) -> Response:
        d = apply_overrides(self.cfg, conn).discovery
        form = form or {"segments": "\n".join(d.segments), **{k: str(getattr(d, k)) for k in INT_FIELDS}}
        return self.render("settings.html", page="settings", form=form, errors=errors or [], fields=INT_FIELDS,
                           customized=bool(load_discovery_overrides(conn)), max_segments=MAX_SEGMENTS,
                           refresh_on=refresh_enabled(conn), can_schedule=self.jobs is not None,
                           update=load_update(conn), update_fields=UPDATE_FIELDS)

    def refresh_save(self, req: Request, conn: sqlite3.Connection) -> Response:
        save_refresh(conn, bool(req.f("enabled")))
        self.say("ok", "Daily refresh is on." if req.f("enabled") else "Daily refresh is off. The Monday run is unchanged.")
        return self.redirect("/settings")

    def hiring_data(self, conn: sqlite3.Connection, theme: str) -> dict:
        """Open job postings at companies on the list, for the Hiring section at the bottom of the Analysis page."""
        themes = list(self.cfg.hiring.theme_keywords)
        theme = theme if theme in themes else ""
        where, args = "p.open = 1 AND p.posted_date >= date(?, ?) ", [date.today().isoformat(), f"-{self.cfg.hiring.window_days} days"]
        if theme:
            where, args = where + "AND p.theme = ? ", args + [theme]
        lead = tuple(self.cfg.hiring.leadership_levels)
        rows = conn.execute(
            f"SELECT a.id, a.name, a.status, a.ats_provider, COUNT(*) AS n, MAX(p.posted_date) AS newest, "
            f"SUM(p.seniority IN ({','.join('?' * len(lead))})) AS leaders, SUM(p.interim) AS interim "
            f"FROM postings p JOIN accounts a ON a.id = p.account_id WHERE {where}"
            f"GROUP BY a.id ORDER BY leaders DESC, n DESC, newest DESC LIMIT 200", (*lead, *args)).fetchall()
        recent = conn.execute(
            f"SELECT p.*, a.name AS account FROM postings p JOIN accounts a ON a.id = p.account_id WHERE {where}"
            "ORDER BY p.posted_date DESC, p.id DESC LIMIT 60", args).fetchall()
        return {"rows": rows, "recent": recent, "themes": themes, "theme": theme, "lead": set(lead), "window": self.cfg.hiring.window_days}

    def settings_save(self, req: Request, conn: sqlite3.Connection) -> Response:
        form = {k: req.f(k) for k in ("segments", *INT_FIELDS)}
        errs = save_discovery(conn, form)
        if errs:
            return self.settings_page(conn, form, errs)
        self.say("ok", "Discovery settings saved. They apply to the next run.")
        return self.redirect("/settings")

    def settings_reset(self, conn: sqlite3.Connection) -> Response:
        reset_discovery(conn)
        self.say("ok", "Discovery settings reset to the defaults in config.yaml.")
        return self.redirect("/settings")

    def firms(self, conn: sqlite3.Connection, form: dict[str, str] | None = None, errors: list[str] | None = None) -> Response:
        today = date.today()
        last_job = next((j for j in (self.jobs.recent() if self.jobs else []) if j.kind in ("pe_collect", "pe_deals", "pe_promote")), None)
        return self.render("firms.html", page="firms", rows=firm_directory(conn, self.cfg, today), st=collection_status(conn, self.cfg, today),
                           history=pe_run_history(conn, 6), form=form or {}, errors=errors or [], last_job=last_job)

    def firm_add(self, req: Request, conn: sqlite3.Connection) -> Response:
        form = {k: req.f(k) for k in ("name", "domain", "hq_city", "portfolio_url")}
        errs = add_firm(conn, form["name"], form["domain"], form["hq_city"], form["portfolio_url"])
        if errs:
            return self.firms(conn, form, errs)
        self.say("ok", f"Added {form['name'].strip()}. Run PE portfolio research from Operate to find its companies.")
        return self.redirect("/firms")

    def firm_page(self, req: Request, conn: sqlite3.Connection, firm_id: int) -> Response:
        geo = req.q("geo")
        d = firm_detail(conn, self.cfg, firm_id, date.today(), geo)
        if d is None:
            return self.error("404 Not Found", "No such firm.")
        return self.render("firm.html", page="firms", d=d, geo=geo if geo in dict(GEO) else "", geo_tabs=GEO, conf_scored={"High", "Medium"})

    def pe_run_page(self, conn: sqlite3.Connection, run_id: int) -> Response:
        d = run_detail(conn, run_id)
        if d is None:
            return self.error("404 Not Found", "No such run.")
        return self.render("pe_run.html", page="firms", d=d)

    def ownership_page(self, conn: sqlite3.Connection, ownership_id: int) -> Response:
        d = ownership_detail(conn, self.cfg, ownership_id, date.today())
        if d is None:
            return self.error("404 Not Found", "No such relationship.")
        return self.render("ownership.html", page="firms", d=d)

    def portfolio_company_page(self, conn: sqlite3.Connection, company_id: int) -> Response:
        pe = company_pe(conn, self.cfg, date.today(), company_id=company_id)
        if pe is None:
            return self.error("404 Not Found", "No such portfolio company.")
        if pe.company["account_id"]:
            return self.redirect(f"/companies/{pe.company['account_id']}")
        return self.render("pe_company.html", page="firms", pe=pe)

    def candidates(self, conn: sqlite3.Connection) -> Response:
        import json
        rows = []
        for r in conn.execute("SELECT * FROM candidates ORDER BY status = 'queued' DESC, id DESC LIMIT 300"):
            rows.append({"c": r, "facts": json.loads(r["facts_json"] or "{}")})
        return self.render("candidates.html", page="candidates", rows=rows)

    def candidate_decide(self, conn: sqlite3.Connection, candidate_id: int, action: str) -> Response:
        if action == "reject":
            ok = reject_candidate(conn, candidate_id)
            self.say("ok" if ok else "err", "Candidate rejected." if ok else "No such queued candidate.")
        else:
            res = accept_candidate(conn, self.cfg, candidate_id)
            self.say("ok" if res.ok else "err", "Added to accounts." if res.ok else "; ".join(res.errors))
        return self.redirect("/candidates")

    def exclusions(self, conn: sqlite3.Connection) -> Response:
        rows = conn.execute("SELECT * FROM exclusions ORDER BY lower(name)").fetchall()
        return self.render("exclusions.html", page="exclusions", rows=rows, errors=[], form={})

    def exclusion_add(self, req: Request, conn: sqlite3.Connection) -> Response:
        form = {k: req.f(k) for k in ("name", "domain", "reason")}
        errs = add_exclusion(conn, self.cfg, form["name"], form["domain"], form["reason"])
        if errs:
            rows = conn.execute("SELECT * FROM exclusions ORDER BY lower(name)").fetchall()
            return self.render("exclusions.html", http_status="400 Bad Request", page="exclusions", rows=rows, errors=errs, form=form)
        self.say("ok", f"Excluded {form['name']}; the fit gate was re-run for every account.")
        return self.redirect("/exclusions")

    def runs(self, conn: sqlite3.Connection) -> Response:
        rows = conn.execute("SELECT r.*, a.name AS account FROM runs r LEFT JOIN accounts a ON a.id = r.account_id ORDER BY r.id DESC LIMIT 300").fetchall()
        steps = conn.execute(
            "SELECT step, COUNT(*) n, SUM(tokens) tokens, SUM(est_cost) cost, SUM(est_cost IS NULL) unpriced "
            "FROM runs WHERE account_id IS NOT NULL OR step LIKE 'discover%' GROUP BY step ORDER BY step").fetchall()
        return self.render("runs.html", page="runs", rows=rows, steps=steps)

    def audit(self, conn: sqlite3.Connection) -> Response:
        rows = conn.execute("SELECT l.*, a.name AS account FROM audit_log l LEFT JOIN accounts a ON a.id = l.account_id ORDER BY l.id DESC LIMIT 300").fetchall()
        return self.render("audit.html", page="audit", rows=rows)


class _BadRequest(Exception):
    pass
