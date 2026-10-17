"""Ask your data: a plain-English question becomes one read-only query over your own database, and the rows it returns become a short
answer. Two text-only model calls (no web search):

  1. the model sees only the table layout below and writes one SELECT;
  2. CODE checks it and runs it on a separate read-only connection through an authorizer (allow-listed tables and columns, safe
     functions only, a time limit, a row cap), so the model's SQL can read nothing outside the list and change nothing;
  3. the model phrases an answer from the returned rows only.

The question, the SQL and the rows are stored (table `questions`) so every answer can be audited. Never imported by the local UI
(it calls a model); the UI reads results through bd.ask_store."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from importlib import resources
from pathlib import Path

from pydantic import BaseModel

from bd.config import Config
from bd.cost import estimate_cost
from bd.llm import LLM, LLMError, Usage, generate_validated
from bd.models import SIGNAL_TYPES

ANSWER_ROWS_SENT = 100       # rows shown to the model for the answer (all kept rows are still stored and displayed)
MAX_CELL_CHARS = 400

# table -> (columns the model may read, note). Any other column reads as NULL; any other table is refused.
ALLOWED: dict[str, tuple[tuple[str, ...], str]] = {
    "accounts": (("id", "name", "domain", "hq_city", "chicagoland", "revenue", "employees", "ownership", "sponsor", "industry", "cik",
                  "tier", "status", "last_scanned", "origin", "discovered_at"),
                 "one row per company. chicagoland is yes/no/unknown; revenue is USD; ownership is public/private/pe-backed; "
                 "status is active (ready to scan), hold_review (facts missing) or excluded; tier A is scanned weekly, B every other week"),
    "signals": (("id", "account_id", "family", "type", "event_date", "detected_date", "summary", "source_url", "source_type", "confidence",
                 "service_lines", "role_or_person"),
                "one row per event found about a company. event_date is when it happened (from the source), detected_date when we found it; "
                "confidence is High/Medium/Low; service_lines is a JSON array text"),
    "scores": (("account_id", "run_date", "score", "band", "rationale", "posted"),
               "one row per company per scoring run; the latest run_date is the current score. band is Hot/Warm/Watch/Stored"),
    "briefs": (("account_id", "run_date", "why_now", "best_service_line"), "the one-sentence why-now line written for a company on a run"),
    "postings": (("id", "account_id", "url", "title", "posted_date", "theme", "seniority", "interim", "source", "first_seen", "last_seen", "open"),
                 "job postings on a company's job board; open = 1 while still listed; theme is ai_technology, data_analytics, "
                 "strategy_operations, transformation_change or product_engineering; seniority is c_suite, svp_vp, director, ..."),
    "pe_firms": (("id", "name", "domain", "hq_city", "status", "last_scanned"), "private equity firms"),
    "portfolio_links": (("id", "firm_id", "account_id", "company_name", "status", "origin", "found_date"),
                        "which firm owns which company; status is current or exited; account_id is null when the company is not an account"),
    "firm_signals": (("id", "firm_id", "type", "event_date", "detected_date", "summary", "source_url", "source_type", "confidence"),
                     "events at a PE firm itself (firm_new_fund, firm_exit, firm_operating_partner)"),
    "solicitations": (("id", "url", "title", "agency", "grp", "due_date", "summary", "service_lines", "first_seen", "last_seen"),
                      "open public RFPs from Chicago-area agencies; open when due_date is today or later"),
    "lead_state": (("account_id", "stage", "watching", "updated"),
                   "the user's own pipeline stage per company: new, reviewing, pursuing, passed, won, lost; watching = 1 if followed. "
                   "(The user's free-text notes are deliberately not available.)"),
    "runs": (("id", "run_date", "step", "account_id", "signals_found", "tokens", "est_cost"),
             "what each pipeline step cost; step like 'scan:leadership', 'enrich', 'rfps'; est_cost in USD"),
}

SAFE_FUNCTIONS = frozenset({
    "count", "sum", "avg", "min", "max", "total", "abs", "round", "length", "lower", "upper", "substr", "substring", "trim", "ltrim", "rtrim",
    "replace", "coalesce", "ifnull", "nullif", "iif", "instr", "like", "glob", "typeof", "date", "datetime", "julianday", "strftime", "time",
    "json_extract", "group_concat", "printf", "row_number", "rank", "dense_rank", "lag", "lead", "first_value", "last_value", "ntile",
    "cume_dist", "percent_rank", "random_unused_placeholder"})


class AskError(Exception):
    pass


class SqlResponse(BaseModel):
    sql: str
    explanation: str = ""


class AnswerResponse(BaseModel):
    answer: str


def schema_text(cfg: Config) -> str:
    lines = ["## Tables"]
    for table, (cols, note) in ALLOWED.items():
        lines.append(f"- {table}({', '.join(cols)}): {note}.")
    lines.append("")
    lines.append("signals.type is one of: " + ", ".join(SIGNAL_TYPES) + ".")
    lines.append("signals.family is one of: " + ", ".join(cfg.scoring.families) + " (each type belongs to one family: "
                 + "; ".join(f"{f}: {', '.join(t)}" for f, t in cfg.scoring.families.items()) + ").")
    lines.append("Service lines: " + ", ".join(cfg.service_lines) + ".")
    return "\n".join(lines)


def sql_system_prompt(cfg: Config) -> str:
    return resources.files("bd.prompts").joinpath("ask_sql.md").read_text(encoding="utf-8") + "\n" + schema_text(cfg)


def validate_sql(sql: str) -> str:
    """The query with a trailing semicolon removed, or AskError. One SELECT / WITH statement, no comments."""
    s = (sql or "").strip()
    if s.endswith(";"):
        s = s[:-1].rstrip()
    if not s:
        raise AskError("the query is empty")
    if ";" in s:
        raise AskError("only one statement is allowed")
    if "--" in s or "/*" in s or "*/" in s:
        raise AskError("comments are not allowed")
    if not re.match(r"(?is)^\s*(select|with)\b", s):
        raise AskError("only SELECT queries are allowed")
    return s


def _authorizer(action: int, arg1: str | None, arg2: str | None, dbname: str | None, source: str | None) -> int:
    if action == sqlite3.SQLITE_SELECT:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ:
        table = (arg1 or "").lower()
        if table not in ALLOWED:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK if (arg2 or "").lower() in ALLOWED[table][0] else sqlite3.SQLITE_IGNORE   # reads as NULL
    if action == sqlite3.SQLITE_FUNCTION:
        return sqlite3.SQLITE_OK if (arg2 or "").lower() in SAFE_FUNCTIONS else sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_DENY


def _cell(v: object) -> object:
    if v is None or isinstance(v, (int, float)):
        return v
    if isinstance(v, bytes):
        return "<binary>"
    s = str(v)
    return s if len(s) <= MAX_CELL_CHARS else s[: MAX_CELL_CHARS - 1] + "…"


def run_query(db_path: str | Path, sql: str, cfg: Config) -> tuple[list[str], list[list[object]], bool]:
    """Run a validated query read-only. Returns (columns, rows, truncated). Raises AskError (rejected) or sqlite3.Error (failed)."""
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.set_authorizer(_authorizer)
        deadline = time.monotonic() + cfg.ask.timeout_seconds
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 2000)
        try:
            cur = conn.execute(sql)
            fetched = cur.fetchmany(cfg.ask.max_rows + 1)
        except sqlite3.DatabaseError as e:
            msg = str(e)
            if "not authorized" in msg or "prohibited" in msg:
                raise AskError("the query uses a table or function that is not available") from e
            if "interrupted" in msg:
                raise AskError(f"the query took longer than {cfg.ask.timeout_seconds:g} seconds") from e
            raise
        cols = [d[0] for d in (cur.description or [])]
        truncated = len(fetched) > cfg.ask.max_rows
        return cols, [[_cell(v) for v in r] for r in fetched[: cfg.ask.max_rows]], truncated
    finally:
        conn.close()


@dataclass
class AskOutcome:
    status: str
    answer: str | None = None
    sql: str | None = None
    explanation: str | None = None
    columns: list[str] | None = None
    rows: list[list[object]] | None = None
    truncated: bool = False
    error: str | None = None
    usage: Usage | None = None
    cost: float | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def answer_question(db_path: str | Path, cfg: Config, llm: LLM, model: str | None, question: str, today: date,
                    max_cost: float | None = None) -> AskOutcome:
    cap = cfg.ask.max_cost_usd if max_cost is None else min(cfg.ask.max_cost_usd, max_cost)
    usage = Usage()
    out = AskOutcome("error", usage=usage)

    def spent() -> float | None:
        return estimate_cost(usage, model, cfg)

    system = sql_system_prompt(cfg)
    base = f"Today's date: {today.isoformat()}\nQuestion: {question}\n\nReturn the JSON object only."
    problem: str | None = None
    previous = ""
    cols = rows = None
    truncated = False
    for attempt in range(2):
        prompt = base if problem is None else (base + f"\n\nYour previous query was rejected or failed: {problem}\n"
                                                      f"Previous query: {previous}\nReturn a corrected JSON object.")
        try:
            resp, res = generate_validated(llm, system, prompt, SqlResponse, retries=cfg.llm.json_retries, key="sql")
        except LLMError as e:
            usage.add(e.usage)
            out.error = f"{type(e).__name__}: {e}"
            out.cost = spent()
            return out
        usage.add(res.usage)
        out.explanation = resp.explanation
        if not resp.sql.strip():
            out.error = "The data cannot answer this: " + (resp.explanation or "the tables do not hold what was asked.")
            out.cost = spent()
            return out
        previous = resp.sql
        try:
            sql = validate_sql(resp.sql)
            cols, rows, truncated = run_query(db_path, sql, cfg)
            out.sql = sql
            break
        except (AskError, sqlite3.Error) as e:
            problem = str(e)
            out.sql = resp.sql
    else:
        out.error = f"The question could not be turned into a safe query ({problem})."
        out.cost = spent()
        return out

    out.columns, out.rows, out.truncated = cols, rows, truncated
    c = spent()
    if c is not None and c >= cap:
        out.error = f"Stopped before writing the answer: the cost limit of ${cap:.2f} per question was reached."
        out.cost = c
        return out
    payload = json.dumps({"question": question, "columns": cols, "rows": rows[:ANSWER_ROWS_SENT], "row_count": len(rows),
                          "truncated": truncated or len(rows) > ANSWER_ROWS_SENT}, ensure_ascii=False, default=str)
    try:
        ans, res = generate_validated(llm, resources.files("bd.prompts").joinpath("ask_answer.md").read_text(encoding="utf-8"), payload,
                                      AnswerResponse, retries=cfg.llm.json_retries, key="answer")
    except LLMError as e:
        usage.add(e.usage)
        out.error = f"The query ran ({len(rows)} rows) but the answer could not be written: {e}"
        out.cost = spent()
        return out
    usage.add(res.usage)
    out.status, out.answer, out.cost = "done", " ".join(ans.answer.split()), spent()
    return out


def run_ask(conn: sqlite3.Connection, db_path: str | Path, cfg: Config, llm: LLM, model: str | None, question_id: int, today: date,
            max_cost: float | None = None) -> AskOutcome | None:
    """Answer one stored question and write the result back. None if there is no such pending question."""
    row = conn.execute("SELECT question, status FROM questions WHERE id = ?", (question_id,)).fetchone()
    if row is None or row["status"] != "pending":
        return None
    o = answer_question(db_path, cfg, llm, model, row["question"], today, max_cost)
    with conn:
        conn.execute(
            "UPDATE questions SET status=?, sql=?, explanation=?, columns_json=?, rows_json=?, truncated=?, answer=?, error=?, tokens=?, "
            "cost=?, finished=? WHERE id=?",
            (o.status, o.sql, o.explanation, json.dumps(o.columns) if o.columns is not None else None,
             json.dumps(o.rows, default=str) if o.rows is not None else None, int(o.truncated), o.answer, o.error,
             o.usage.total_tokens if o.usage else 0, o.cost, _now(), question_id))
        conn.execute("INSERT INTO runs(run_date,step,account_id,accounts_scanned,signals_found,tokens,est_cost,errors) VALUES (?,?,?,?,?,?,?,?)",
                     (today.isoformat(), "ask", None, 0, 0, o.usage.total_tokens if o.usage else 0, o.cost, o.error))
    return o
