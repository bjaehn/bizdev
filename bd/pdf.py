"""PDF export of a page: the page's own print view (the `@media print` block in design.css), drawn by headless Chromium in a child process.

The web process never starts a browser: `render` runs `python -m bd.pdf` with the page's HTML on stdin and reads the PDF from stdout, so a browser crash or a
memory spike cannot take the server with it. The browser sees only our own escaped HTML: JavaScript is off, every network request is refused, and the child
gets a short allow-list of environment variables (never the API key, the sign-in password or a session secret) and an empty temporary home. `--no-sandbox` is
needed because the container gives the unprivileged user no namespaces for Chromium's sandbox; that is acceptable only because of what the browser is given.
Nothing here imports the network or model modules (tests/test_ui.py), and Playwright is imported only inside the child.

`available()` is a cheap file check, so a page can decide whether to show its Export link without starting anything."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import unicodedata
from datetime import date
from html import escape
from pathlib import Path

CHROME_ENV = "BD_CHROMIUM_PATH"             # a Chromium (or headless shell) executable to use instead of the one Playwright installed
TIMEOUT_SECONDS = 60                        # one render, start to finish
WAIT_SECONDS = 30                           # how long a second export waits for the first
MAX_PAYLOAD_BYTES = 8_000_000
CHILD_ENV = ("PATH", "LANG", "LC_ALL", "PLAYWRIGHT_BROWSERS_PATH", CHROME_ENV, "FONTCONFIG_FILE", "FONTCONFIG_PATH")   # all the child is given, plus a temporary HOME
WORKER = [sys.executable, "-m", "bd.pdf"]
PAGE_MARGINS = {"top": "0.55in", "bottom": "0.75in", "left": "0.55in", "right": "0.55in"}

_one_at_a_time = threading.Semaphore(1)


class PdfError(Exception):
    """The PDF could not be made; the message is one plain sentence for the person who clicked."""


class PdfBusy(PdfError):
    """Another export is still running."""


# ---- the web side ----------------------------------------------------------------------------------------------------------------------------------
def _browser_installed() -> bool:
    explicit = os.environ.get(CHROME_ENV)
    if explicit:
        return Path(explicit).is_file()
    try:
        root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / ".cache" / "ms-playwright")
        return any(root.glob("chromium_headless_shell-*")) or any(root.glob("chromium-*"))
    except (OSError, RuntimeError):
        return False


def available() -> bool:
    """Is the engine installed on this server (the Playwright package and a Chromium)? Says nothing about whether a render will succeed, only whether to offer it."""
    return importlib.util.find_spec("playwright") is not None and _browser_installed()


def status_line() -> str:
    return ("PDF export: ready (a Chromium is installed)." if available()
            else "PDF export: not installed on this server, so the Export PDF links are hidden (install the 'pdf' extra and a Chromium).")


def filename(kind: str, day: date, name: str = "") -> str:
    """An ASCII file name safe for a Content-Disposition header, such as sei-bd-brief-lakeshore-software-2026-10-26.pdf."""
    slug = re.sub(r"[^a-z0-9]+", "-", unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()).strip("-")[:40].strip("-")
    return "sei-bd-" + "-".join(x for x in (kind, slug, day.isoformat()) if x) + ".pdf"


def _last_line(text: bytes) -> str:
    lines = [ln.strip() for ln in text.decode("utf-8", "replace").splitlines() if ln.strip()]
    return lines[-1][:300] if lines else ""


def render(html: str, footer: str) -> bytes:
    """The PDF of this HTML, with `footer` (plain text) and the page number at the foot of every page. One at a time; raises PdfBusy or PdfError."""
    payload = json.dumps({"html": html, "footer": footer}).encode()
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise PdfError("This page is too large to export.")
    if not _one_at_a_time.acquire(timeout=WAIT_SECONDS):
        raise PdfBusy("Another PDF is still being made. Try again in a moment.")
    try:
        with tempfile.TemporaryDirectory(prefix="bd-pdf-") as home:
            env = {k: os.environ[k] for k in CHILD_ENV if k in os.environ}
            env["HOME"] = home
            env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)          # the child finds this package wherever it is installed
            try:
                child = subprocess.Popen(WORKER, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=home, start_new_session=True)
            except OSError as e:
                raise PdfError(f"The PDF engine could not start ({type(e).__name__}).") from e
            try:
                out, err = child.communicate(payload, timeout=TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)                                # the browser is a grandchild: end the whole group
                child.communicate()
                raise PdfError(f"The PDF took longer than {TIMEOUT_SECONDS} seconds and was stopped.") from None
            finally:
                try:
                    os.killpg(child.pid, signal.SIGKILL)                            # nothing outlives the render
                except (ProcessLookupError, PermissionError):
                    pass
    finally:
        _one_at_a_time.release()
    if child.returncode != 0:
        raise PdfError("The PDF engine failed" + (f": {_last_line(err)}" if _last_line(err) else "."))
    if not out.startswith(b"%PDF"):
        raise PdfError("The PDF engine returned no document.")
    return out


# ---- the child: `python -m bd.pdf` -----------------------------------------------------------------------------------------------------------------
def _footer_template(text: str) -> str:
    return ('<div style="width:100%;padding:0 0.55in;font:8px Arial,Helvetica,sans-serif;color:#555;display:flex;justify-content:space-between;'
            '-webkit-print-color-adjust:exact">'
            f'<span>{escape(text)}</span><span>Page <span class="pageNumber"></span> of <span class="totalPages"></span></span></div>')


def _worker() -> int:
    try:
        job = json.loads(sys.stdin.buffer.read())
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=os.environ.get(CHROME_ENV) or None, args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"])
            try:
                page = browser.new_context(java_script_enabled=False, color_scheme="light").new_page()
                page.route("**/*", lambda route: route.abort())                      # the page is self-contained; nothing may be fetched
                page.set_content(job["html"], wait_until="load")
                page.emulate_media(media="print")
                data = page.pdf(format="Letter", print_background=True, display_header_footer=True, header_template="<span></span>",
                                footer_template=_footer_template(job["footer"]), margin=PAGE_MARGINS)
            finally:
                browser.close()
    except Exception as e:                                                          # one plain line on stderr is what the web side shows
        sys.stderr.write(f"{type(e).__name__}: {str(e).strip().splitlines()[0] if str(e).strip() else 'no detail'}\n")
        return 2
    sys.stdout.buffer.write(data)
    return 0


if __name__ == "__main__":
    sys.exit(_worker())
