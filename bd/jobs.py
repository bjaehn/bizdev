"""Server-side jobs for the hosted UI: a small whitelist of `bd` commands run as subprocesses.

Rules (the UI is reachable from the internet behind one password, so this is deliberately narrow):
  * only the kinds listed in KINDS exist; every argument is validated and passed as an argv element, never via a
    shell; there is NO kind that posts to Teams (only the scheduled `weekly` job can, exactly like scripts/weekly.sh:
    it posts only when TEAMS_WEBHOOK_URL is set, and never when BD_DRY_RUN=1);
  * one job at a time; a job has a timeout; its output goes to a log file and secrets are redacted when displayed;
  * jobs that spend money (model calls) must be confirmed by the caller (`confirmed=True`).
This module imports no scanner, LLM or posting code: the work happens in a child `python -m bd.cli` process."""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# Names are duplicated from bd.scanners on purpose (the UI must not import scanner code); a test keeps them in sync.
PAID_STEPS = frozenset({"enrich", "discover", "scan", "pe-portfolio", "pe-signals", "rfps", "ask", "card"})
SCANNER_NAMES = ("leadership", "edgar", "earnings", "events", "press", "pe", "hiring", "ats", "tech_procurement", "warn", "financials")
SECRET_ENV = ("ANTHROPIC_API_KEY", "TEAMS_WEBHOOK_URL", "BD_UI_PASSWORD")
TIMEOUT_SECONDS = 2 * 3600
LOG_TAIL_BYTES = 20_000

JOBS_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id        INTEGER PRIMARY KEY,
    kind      TEXT NOT NULL,
    args_json TEXT NOT NULL,
    status    TEXT NOT NULL CHECK (status IN ('queued','running','ok','failed','interrupted')),
    created   TEXT NOT NULL,
    started   TEXT,
    finished  TEXT,
    exit_code INTEGER,
    note      TEXT
);
"""

# kind -> (label, spends model tokens?, scheduled-only?)
KINDS: dict[str, tuple[str, bool, bool]] = {
    "update": ("Update my list (research, find, scan, score)", True, False),
    "check": ("Test the model connection", False, False),
    "discover": ("Discover companies (market research)", True, False),
    "pe_sync": ("Sync PE firms from accounts", False, False),
    "pe_portfolio": ("Research PE portfolios", True, False),
    "pe_signals": ("Scan PE firms for signals", True, False),
    "rfps": ("Find open public RFPs", True, False),
    "ask": ("Ask a question about your data", True, False),
    "enrich": ("Enrich accounts (propose missing facts)", True, False),
    "scan": ("Scan for signals", True, False),
    "score": ("Score accounts", False, False),
    "card_dry": ("Build the card (dry run, never posts)", True, False),
    "weekly_dry": ("Full weekly run, dry run (scan, score, card; never posts)", True, False),
    "weekly": ("Scheduled weekly run", True, True),
    "refresh": ("Scheduled daily light refresh", True, True),
    "refresh_now": ("Light refresh now (job boards and SEC filings)", True, False),
}


class JobError(Exception):
    pass


@dataclass
class Job:
    id: int
    kind: str
    args: list[str]
    status: str
    created: str
    started: str | None
    finished: str | None
    exit_code: int | None
    note: str | None


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def chicago_today() -> str:
    return datetime.now(ZoneInfo("America/Chicago")).date().isoformat()


def validate(kind: str, form: dict[str, str], *, scheduled: bool = False) -> list[str]:
    """Turn user input into a fixed, safe argv tail for `bd`. Raises JobError on anything unexpected."""
    if kind not in KINDS:
        raise JobError("Unknown job.")
    if KINDS[kind][2] and not scheduled:
        raise JobError("That job can only run on the schedule.")

    def num(key: str, lo: int, hi: int, required: bool = False) -> int | None:
        raw = (form.get(key) or "").strip()
        if not raw:
            if required:
                raise JobError(f"{key} is required.")
            return None
        if not raw.isdigit() or not lo <= int(raw) <= hi:
            raise JobError(f"{key} must be a whole number from {lo} to {hi}.")
        return int(raw)

    if kind == "pe_sync":
        return ["pe-sync"]
    if kind in ("pe_portfolio", "pe_signals"):
        return [kind.replace("_", "-"), "--limit", str(num("limit", 1, 50, required=True))]
    if kind == "check":
        return ["check"]
    if kind == "ask":
        return ["ask", "--id", str(num("id", 1, 10**9, required=True))]
    if kind == "rfps":
        return ["rfps", "--limit", str(num("limit", 1, 12, required=True))]
    if kind == "update":
        research, segs = num("research", 0, 100, required=True), num("segments", 0, 30, required=True)
        scan, budget = num("scan", 1, 100, required=True), num("budget", 1, 100, required=True)
        return ["update", "--research", str(research), "--segments", str(segs), "--scan", str(scan), "--budget", str(budget)]
    if kind in ("refresh", "refresh_now"):
        return []   # steps are fixed in sequence_for()
    if kind == "discover":
        return ["discover", "--limit", str(num("limit", 1, 30, required=True))]
    if kind == "enrich":
        args = ["enrich", "--limit", str(num("limit", 1, 100, required=True))]
        only = (form.get("only") or "").strip()
        if only:
            if len(only) > 60 or not re.fullmatch(r"[A-Za-z0-9 .,&'()/-]+", only) or only.startswith("-"):
                raise JobError("'Only names containing' may use letters, digits and basic punctuation (60 max).")
            args += ["--only", only]
        return args
    if kind == "scan":
        args = ["scan", "--limit", str(num("limit", 1, 100, required=True))]
        tier = (form.get("tier") or "all").strip()
        if tier not in ("A", "B", "all"):
            raise JobError("Tier must be A, B or all.")
        args += ["--tier", tier]
        chosen = [s for s in (form.get("scanners") or "").split(",") if s]
        if chosen:
            if any(s not in SCANNER_NAMES for s in chosen):
                raise JobError("Unknown scanner.")
            args += ["--scanners", ",".join(chosen)]
        return args
    if kind == "score":
        return ["score"]
    if kind == "card_dry":
        return ["card", "--dry-run"] + (["--no-llm"] if form.get("no_llm") else [])
    return []   # weekly / weekly_dry: steps are fixed in sequence_for()


def refresh_scanners() -> str:
    """Scanners for the light refresh: the free job-board feed always; SEC filings only when BD_USER_AGENT is set (the SEC
    requires a declared contact). Only filings not read before cost a model call."""
    return "ats" + (",edgar,earnings,financials" if os.environ.get("BD_USER_AGENT") else "")


def sequence_for(kind: str, args: list[str], as_of: str) -> list[list[str]]:
    """The `bd` invocations (after the executable) a job runs, in order."""
    if kind == "update":
        a = dict(zip(args[1::2], args[2::2]))
        steps = [["check"]]
        if int(a["--research"]):
            steps += [["enrich", "--limit", a["--research"], "--allow-empty", "--as-of", as_of], ["apply-proposals", "enrich_proposals.csv"]]
        if int(a["--segments"]):
            steps.append(["discover", "--limit", a["--segments"], "--as-of", as_of])
        return steps + [["pe-sync"], ["scan", "--limit", a["--scan"], "--allow-empty", "--as-of", as_of],
                        ["rfps", "--allow-empty", "--as-of", as_of], ["score", "--allow-empty", "--as-of", as_of]]
    if kind in ("refresh", "refresh_now"):
        return [["scan", "--scanners", refresh_scanners(), "--force", "--light", "--as-of", as_of], ["score", "--as-of", as_of]]
    if kind in ("weekly", "weekly_dry"):
        dry = kind == "weekly_dry" or os.environ.get("BD_DRY_RUN") == "1"
        return [["scan", "--as-of", as_of], ["pe-signals", "--as-of", as_of], ["rfps", "--allow-empty", "--as-of", as_of], ["score", "--as-of", as_of],
                ["card", "--as-of", as_of] + (["--dry-run"] if dry else [])]
    return [args + ["--as-of", as_of]] if args[0] in ("scan", "score", "card", "enrich", "discover", "pe-portfolio", "pe-signals", "rfps") else [args]


def redact(text: str, env: dict[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    for name in SECRET_ENV:
        v = env.get(name)
        if v and len(v) >= 8:
            text = text.replace(v, f"[{name} hidden]")
    return text


class JobRunner:
    """Runs one job at a time in a background thread and records it in the `jobs` table."""

    def __init__(self, db_path: str, data_dir: str | Path, bd_cmd: list[str] | None = None,
                 timeout: int = TIMEOUT_SECONDS) -> None:
        self.db_path, self.data_dir = db_path, Path(data_dir)
        self.bd_cmd = bd_cmd or [sys.executable, "-m", "bd.cli"]
        self.timeout = timeout
        self.log_dir = self.data_dir / "jobs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._busy = False
        self._threads: list[threading.Thread] = []
        with self._conn() as c:
            c.executescript(JOBS_SCHEMA)
            c.execute("UPDATE jobs SET status='interrupted', finished=?, note='server restarted while this was running' "
                      "WHERE status IN ('running','queued')", (_now(),))

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, timeout=15)
        c.row_factory = sqlite3.Row
        return c

    # ---- reads -----------------------------------------------------------------------------------------------
    def busy(self) -> bool:
        return self._busy

    def recent(self, n: int = 15) -> list[Job]:
        with self._conn() as c:
            return [self._row(r) for r in c.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (n,))]

    def get(self, job_id: int) -> Job | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._row(r) if r else None

    def last_job(self, kinds: tuple[str, ...]) -> Job | None:
        with self._conn() as c:
            r = c.execute(f"SELECT * FROM jobs WHERE kind IN ({','.join('?' * len(kinds))}) ORDER BY id DESC LIMIT 1", kinds).fetchone()
        return self._row(r) if r else None

    def last_scheduled(self) -> Job | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM jobs WHERE kind='weekly' ORDER BY id DESC LIMIT 1").fetchone()
        return self._row(r) if r else None

    @staticmethod
    def _row(r: sqlite3.Row) -> Job:
        import json
        return Job(r["id"], r["kind"], json.loads(r["args_json"]), r["status"], r["created"], r["started"],
                   r["finished"], r["exit_code"], r["note"])

    def log_tail(self, job_id: int) -> str:
        p = self.log_dir / f"{int(job_id)}.log"
        if not p.exists():
            return ""
        with p.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - LOG_TAIL_BYTES))
            text = f.read().decode("utf-8", "replace")
        return redact(text)

    # ---- start -----------------------------------------------------------------------------------------------
    def submit(self, kind: str, form: dict[str, str], *, confirmed: bool = False, scheduled: bool = False) -> int:
        import json
        args = validate(kind, form, scheduled=scheduled)
        if KINDS[kind][1] and not confirmed and not scheduled:
            raise JobError("Tick the box to confirm this uses paid model calls.")
        with self._lock:
            if self._busy:
                raise JobError("Another job is still running. Wait for it to finish.")
            self._busy = True
        try:
            with self._conn() as c:
                cur = c.execute("INSERT INTO jobs(kind,args_json,status,created) VALUES (?,?,?,?)",
                                (kind, json.dumps(args), "queued", _now()))
                job_id = cur.lastrowid
        except Exception:
            self._busy = False
            raise
        t = threading.Thread(target=self._run, args=(job_id, kind, args), daemon=True, name=f"job-{job_id}")
        self._threads.append(t)
        t.start()
        return job_id

    def wait(self, timeout: float = 30) -> None:     # for tests
        end = time.time() + timeout
        for t in list(self._threads):
            t.join(max(0, end - time.time()))

    # ---- run -------------------------------------------------------------------------------------------------
    def _max_run_id(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COALESCE(MAX(id), 0) FROM runs").fetchone()[0]

    def _spent_since(self, run_id: int) -> float:
        """Estimated cost logged since `run_id`. The scan total row repeats the per-call rows, so it is left out; calls to a model
        with no price log NULL and count as 0 (the Home page warns when the model is unpriced)."""
        with self._conn() as c:
            return c.execute("SELECT COALESCE(SUM(est_cost), 0) FROM runs WHERE id > ? AND step != 'scan:total'", (run_id,)).fetchone()[0]

    def _set(self, job_id: int, **cols) -> None:
        sets = ", ".join(f"{k}=?" for k in cols)
        with self._conn() as c:
            c.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*cols.values(), job_id))

    def _run(self, job_id: int, kind: str, args: list[str]) -> None:
        env = {**os.environ, "BD_DB_PATH": self.db_path, "PYTHONUNBUFFERED": "1"}
        deadline = time.monotonic() + self.timeout
        status, code, note = "ok", 0, None
        try:
            self._set(job_id, status="running", started=_now())
            as_of = chicago_today()
            budget = int(dict(zip(args[1::2], args[2::2])).get("--budget", 0)) if kind == "update" else None
            start_id = self._max_run_id() if budget else 0
            skipped_for_budget = False
            with (self.log_dir / f"{job_id}.log").open("ab") as log:
                for step in sequence_for(kind, args, as_of):
                    step_env = env
                    if budget and step[0] in PAID_STEPS:
                        remaining = budget - self._spent_since(start_id)
                        if remaining <= 0:
                            log.write(f"\n(skipped `bd {step[0]}`: the ${budget} spending limit for this update is used up)\n".encode())
                            skipped_for_budget = True
                            continue
                        step_env = {**env, "BD_MAX_COST_USD": f"{remaining:.4f}"}
                    log.write(f"\n$ bd {' '.join(step)}\n".encode())
                    log.flush()
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise subprocess.TimeoutExpired(step, self.timeout)
                    p = subprocess.run([*self.bd_cmd, *step], cwd=self.data_dir, env=step_env, stdout=log,
                                       stderr=subprocess.STDOUT, timeout=left, stdin=subprocess.DEVNULL)
                    if p.returncode != 0:
                        status, code = "failed", p.returncode
                        note = f"`bd {step[0]}` exited with {p.returncode}; later steps were not run."
                        break
                if budget:
                    spent = self._spent_since(start_id)
                    extra = f"Spent about ${spent:.2f} of the ${budget} limit." + (" Some steps were skipped: the limit was used up." if skipped_for_budget else "")
                    note = f"{note} {extra}" if note else extra
        except subprocess.TimeoutExpired:
            status, code, note = "failed", -1, f"Stopped after {self.timeout // 60} minutes."
        except Exception as e:      # never let the thread die silently with the job stuck on "running"
            status, code, note = "failed", -2, f"{type(e).__name__}: {e}"[:300]
        finally:
            self._set(job_id, status=status, exit_code=code, finished=_now(), note=note)
            self._busy = False
