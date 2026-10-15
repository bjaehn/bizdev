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
from datetime import date
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlparse

from jinja2 import Environment, PackageLoader, select_autoescape

from bd.accounts import Account, list_accounts
from bd.brief_core import fallback_brief
from bd.config import Config
from bd.curate import (EXCLUSION_REASONS, add_exclusion, apply_selected, create_account, load_proposals,
                       remove_exclusion, update_account)
from bd.db import connect, init_db
from bd.fit_gate import check_fit, load_exclusions
from bd.labels import TYPE_LABELS, fmt_date
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


def money(v: object) -> str:
    if v is None or v == "":
        return "unknown"
    v = float(v)  # type: ignore[arg-type]
    if v >= 1e9:
        return f"${v / 1e9:.1f}B"
    if v >= 1e6:
        return f"${v / 1e6:.0f}M"
    return f"${v:,.0f}"


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


class App:
    def __init__(self, cfg: Config, db_path: str, token: str, root: Path | None = None) -> None:
        self.cfg, self.db_path, self._token = cfg, db_path, token
        self.root = (root or Path.cwd()).resolve()
        self.port: int | None = None
        self._csrf = hmac.new(token.encode(), b"csrf", hashlib.sha256).hexdigest()
        self._flash: list[tuple[str, str]] = []
        self.env = Environment(loader=PackageLoader("bd.ui", "templates"), autoescape=select_autoescape(["html"]),
                               trim_blocks=True, lstrip_blocks=True)
        self.env.filters.update(money=money, show=show, fact=fact, host=host, safe_url=safe_url, fdate=lambda d: fmt_date(date.fromisoformat(d)) if isinstance(d, str) else fmt_date(d))
        self.env.globals.update(csrf=self._csrf, reasons=EXCLUSION_REASONS, labels=TYPE_LABELS)
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
            resp = self.error("500 Internal Server Error", "Something went wrong. Check the terminal running `bd ui`.")
            import traceback
            traceback.print_exc()
        resp.headers += [("Content-Security-Policy", CSP), ("X-Content-Type-Options", "nosniff"),
                         ("Referrer-Policy", "no-referrer"), ("Cache-Control", "no-store"),
                         ("X-Frame-Options", "DENY")]
        start_response(resp.status, resp.headers)
        return [resp.body]

    def _request(self, environ) -> Request:
        headers = {k[5:].replace("_", "-").lower(): v for k, v in environ.items() if k.startswith("HTTP_")}
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

    def error(self, status: str, message: str) -> Response:
        body = self.env.get_template("error.html").render(status=status, message=message, flash=[]).encode()
        return Response(status, body, [("Content-Type", "text/html; charset=utf-8")])

    def redirect(self, to: str, extra: list[tuple[str, str]] | None = None) -> Response:
        return Response("303 See Other", b"", [("Location", to), *(extra or [])])

    def say(self, kind: str, text: str) -> None:
        self._flash.append((kind, text))

    # ---- guard + routing ---------------------------------------------------------------------------------------
    def handle(self, req: Request) -> Response:
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
            if origin and urlparse(origin).netloc not in self.allowed_hosts():
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

    def route(self, req: Request, conn: sqlite3.Connection) -> Response:
        p, m = req.path.rstrip("/") or "/", req.method
        parts = p.strip("/").split("/")
        table = {
            ("GET", "/"): lambda: self.leads(req, conn),
            ("GET", "/accounts"): lambda: self.accounts(req, conn),
            ("GET", "/accounts/new"): lambda: self.render("account_new.html", page="accounts", form={}, errors=[]),
            ("POST", "/accounts/new"): lambda: self.account_create(req, conn),
            ("GET", "/enrich"): lambda: self.enrich(req, conn),
            ("POST", "/enrich/apply"): lambda: self.enrich_apply(req, conn),
            ("GET", "/exclusions"): lambda: self.exclusions(conn),
            ("POST", "/exclusions/add"): lambda: self.exclusion_add(req, conn),
            ("GET", "/runs"): lambda: self.runs(conn),
            ("GET", "/audit"): lambda: self.audit(conn),
        }
        if (m, p) in table:
            return table[(m, p)]()
        if len(parts) == 2 and parts[0] == "accounts" and parts[1].isdigit():
            return self.account_page(req, conn, int(parts[1])) if m == "GET" else self.account_save(req, conn, int(parts[1]))
        if m == "POST" and len(parts) == 3 and parts[0] == "exclusions" and parts[1].isdigit() and parts[2] == "remove":
            ok = remove_exclusion(conn, self.cfg, int(parts[1]))
            self.say("ok" if ok else "err", "Exclusion removed." if ok else "No such exclusion.")
            return self.redirect("/exclusions")
        return self.error("404 Not Found", "No such page.")

    # ---- pages ------------------------------------------------------------------------------------------------
    def leads(self, req: Request, conn: sqlite3.Connection) -> Response:
        dates = [r[0] for r in conn.execute("SELECT DISTINCT run_date FROM scores ORDER BY run_date DESC")]
        if not dates:
            return self.render("leads.html", page="leads", dates=[], run_date=None, leads=[], skipped=[], overflow=0, error=None)
        chosen = req.q("date") if req.q("date") in dates else dates[0]
        try:
            sl = build_shortlist(conn, self.cfg, date.fromisoformat(chosen))
        except ShortlistError as e:
            return self.render("leads.html", page="leads", dates=dates, run_date=chosen, leads=[], skipped=[], overflow=0, error=str(e))
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
                "posted": posted[lead.account.id]["posted"] if lead.account.id in posted else 0,
            })
        tracked = sorted((s for s in sl.skipped if s.score >= self.cfg.scoring.bands.watch), key=lambda s: -s.score)
        return self.render("leads.html", page="leads", dates=dates, run_date=chosen, leads=cards, skipped=tracked,
                           overflow=len(sl.overflow), error=None)

    def accounts(self, req: Request, conn: sqlite3.Connection) -> Response:
        status = req.q("status")
        q = req.q("q").strip().lower()
        accts = list_accounts(conn, status=None)
        counts = {s: sum(a.status == s for a in accts) for s in ("active", "hold_review", "excluded")}
        excl = load_exclusions(conn)
        rows = []
        for a in accts:
            if (status in counts and a.status != status) or (q and q not in a.name.lower()):
                continue
            rows.append({"a": a, "fit": check_fit(a, excl, self.cfg)})
        return self.render("accounts.html", page="accounts", rows=rows, counts=counts, status=status, q=req.q("q"), total=len(accts))

    def _account_ctx(self, conn: sqlite3.Connection, a: Account, form: dict[str, str], errors: list[str]) -> dict:
        fit = check_fit(a, load_exclusions(conn), self.cfg)
        signals = conn.execute("SELECT * FROM signals WHERE account_id = ? ORDER BY event_date DESC, id DESC", (a.id,)).fetchall()
        scores = conn.execute("SELECT run_date, score, band, posted, cooldown_until, rationale FROM scores WHERE account_id = ? ORDER BY run_date DESC LIMIT 12", (a.id,)).fetchall()
        history = conn.execute("SELECT ts, actor, action, field, old, new FROM audit_log WHERE account_id = ? ORDER BY id DESC LIMIT 25", (a.id,)).fetchall()
        return dict(page="accounts", a=a, fit=fit, signals=signals, scores=scores, history=history, form=form, errors=errors,
                    conf_scored={"High", "Medium"})

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
            "FROM runs WHERE account_id IS NOT NULL GROUP BY step ORDER BY step").fetchall()
        return self.render("runs.html", page="runs", rows=rows, steps=steps)

    def audit(self, conn: sqlite3.Connection) -> Response:
        rows = conn.execute("SELECT l.*, a.name AS account FROM audit_log l LEFT JOIN accounts a ON a.id = l.account_id ORDER BY l.id DESC LIMIT 300").fetchall()
        return self.render("audit.html", page="audit", rows=rows)


class _BadRequest(Exception):
    pass
