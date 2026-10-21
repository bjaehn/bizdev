"""Run the local UI: loopback only, one random token per run."""

from __future__ import annotations

import re
import secrets
import webbrowser
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from bd.config import Config
from bd.ui.app import App

HOST = "127.0.0.1"   # not configurable on purpose: this UI must never listen on the network


def request_line_for_log(requestline: str) -> str:
    """The request line without its query string (the local login token) and without a sign-up token (/join/<token>), so neither reaches a log."""
    return re.sub(r"(/join/)[^\s?]+", r"\1…", (requestline or "").split("?")[0])


class _Quiet(WSGIRequestHandler):
    def log_message(self, format, *args):   # noqa: A002  - log the request line WITHOUT its query string (the login token)
        print(f"  {request_line_for_log(self.requestline)} -> {args[1] if len(args) > 1 else ''}")


class _Server(ThreadingMixIn, WSGIServer):
    daemon_threads = True


def build(cfg: Config, db_path: str, port: int = 8765):
    token = secrets.token_urlsafe(32)
    app = App(cfg, db_path, token)
    httpd = make_server(HOST, port, app, server_class=_Server, handler_class=_Quiet)
    app.port = httpd.server_port
    return httpd, f"http://{HOST}:{app.port}/auth?t={token}"


def serve(cfg: Config, db_path: str, port: int = 8765, open_browser: bool = True) -> None:
    httpd, url = build(cfg, db_path, port)
    print(f"BD Signals UI for {db_path}")
    print(f"Open this link (it signs your browser in; keep it private):\n\n  {url}\n")
    print("Listening on 127.0.0.1 only. Press Ctrl-C to stop.")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()


class _HostedHandler(WSGIRequestHandler):
    timeout = 30            # per-connection socket timeout: a stalled client cannot hold a thread forever

    def log_message(self, format, *args):   # noqa: A002
        print(f"  {request_line_for_log(self.requestline)} -> {args[1] if len(args) > 1 else ''}", flush=True)


def build_hosted(cfg: Config, db_path: str, data_dir: str, password: str, port: int, host: str = "0.0.0.0"):
    """The internet-facing variant: password login, Operate page, Monday scheduler. Returns (httpd, app, scheduler)."""
    from pathlib import Path

    from bd.jobs import JobRunner
    from bd.scheduler import Scheduler
    from bd.ui.auth import Hosted

    hosted = Hosted(password)                       # raises AuthConfigError for a weak/missing password
    from bd.db import connect, init_db
    from bd.settings_store import apply_release_changes

    boot = connect(db_path)                         # a release may change a saved setting once; it is audited and shown on Admin Home and Settings
    init_db(boot)
    apply_release_changes(boot)
    boot.close()
    runner = JobRunner(db_path, data_dir)
    app = App(cfg, db_path, root=Path(data_dir), hosted=hosted, jobs=runner)
    httpd = make_server(host, port, app, server_class=_Server, handler_class=_HostedHandler)
    app.port = httpd.server_port
    return httpd, app, Scheduler(runner)
