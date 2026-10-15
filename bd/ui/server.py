"""Run the local UI: loopback only, one random token per run."""

from __future__ import annotations

import secrets
import webbrowser
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from bd.config import Config
from bd.ui.app import App

HOST = "127.0.0.1"   # not configurable on purpose: this UI must never listen on the network


class _Quiet(WSGIRequestHandler):
    def log_message(self, format, *args):   # noqa: A002  - log the request line WITHOUT its query string (the login token)
        line = (self.requestline or "").split("?")[0]
        print(f"  {line} -> {args[1] if len(args) > 1 else ''}")


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
