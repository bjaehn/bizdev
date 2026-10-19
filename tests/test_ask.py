import json
import sqlite3
from datetime import timedelta

import pytest

from bd.ask import ALLOWED, AskError, answer_question, run_ask, run_query, schema_text, sql_system_prompt, validate_sql
from bd.ask_store import add_question, clean_question, link_columns, recent
from bd.llm import LLMResult, Usage
from tests.conftest import AS_OF, FakeLLM, add_account, add_signal, make_db
from tests.test_update import app_env  # noqa: F401  (fixture)

MODEL = "claude-sonnet-5-5"


@pytest.fixture
def db(tmp_path, cfg):
    conn = make_db(tmp_path)
    a = add_account(conn, "Acme", industry="Health care")
    b = add_account(conn, "Beta", industry="Energy")
    add_signal(conn, cfg, a, "leadership_change", 5)
    add_signal(conn, cfg, b, "hiring_cluster", 9)
    conn.execute("INSERT INTO settings(key, value_json, updated) VALUES ('secret_setting', '\"x\"', 'now')")
    conn.commit()
    return conn, str(tmp_path / "t.sqlite")


def sql_reply(sql, explanation="x"):
    return json.dumps({"sql": sql, "explanation": explanation})


def ans_reply(text="Two companies."):
    return json.dumps({"answer": text})


# ---- the guard ----------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("sql", ["SELECT name FROM accounts", "  select 1;  ", "WITH x AS (SELECT id FROM accounts) SELECT * FROM x"])
def test_plain_selects_pass(sql):
    assert validate_sql(sql)


@pytest.mark.parametrize("sql", [
    "", "DELETE FROM accounts", "UPDATE accounts SET name='x'", "DROP TABLE accounts", "INSERT INTO accounts(name) VALUES ('x')",
    "PRAGMA table_info(accounts)", "ATTACH DATABASE 'x.db' AS x", "SELECT 1; DELETE FROM accounts", "SELECT 1 -- hide",
    "SELECT /* x */ 1", "VACUUM", "EXPLAIN SELECT 1"])
def test_everything_else_is_refused(sql):
    with pytest.raises(AskError):
        validate_sql(sql)


def test_unlisted_tables_and_functions_are_refused_by_the_authorizer(db, cfg):
    _, path = db
    for sql in ("SELECT * FROM settings", "SELECT * FROM audit_log", "SELECT * FROM sqlite_master", "SELECT * FROM lead_notes",
                "SELECT load_extension('x')", "SELECT randomblob(10)"):
        with pytest.raises(AskError):
            run_query(path, sql, cfg)
    with pytest.raises((AskError, sqlite3.Error)):                  # CLI-only functions do not exist in this build; either way nothing is read
        run_query(path, "SELECT readfile('/etc/passwd')", cfg)


def test_unlisted_columns_read_as_null_not_data(db, cfg):
    _, path = db
    cols, rows, _ = run_query(path, "SELECT name, source_url, ats_board FROM accounts ORDER BY id", cfg)
    assert rows == [["Acme", None, None], ["Beta", None, None]]


def test_the_connection_cannot_write_even_if_the_guard_were_bypassed(db, cfg):
    _, path = db
    for sql in ("DELETE FROM accounts", "UPDATE accounts SET name = 'x'", "DROP TABLE accounts", "INSERT INTO accounts(name) VALUES ('z')"):
        with pytest.raises((AskError, sqlite3.Error)):
            run_query(path, sql, cfg)                      # straight to the runner, skipping validate_sql
    raw = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        raw.execute("DELETE FROM accounts")
    raw.close()
    assert make_db_count(path) == 2


def make_db_count(path):
    c = sqlite3.connect(path)
    n = c.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    c.close()
    return n


def test_row_cap_and_timeout(db, cfg):
    _, path = db
    c2 = cfg.model_copy(deep=True)
    c2.ask = c2.ask.model_copy(update={"max_rows": 1})
    cols, rows, truncated = run_query(path, "SELECT name FROM accounts ORDER BY id", c2)
    assert rows == [["Acme"]] and truncated and cols == ["name"]
    c3 = cfg.model_copy(deep=True)
    c3.ask = c3.ask.model_copy(update={"timeout_seconds": 0.05})
    slow = ("WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 100000000) SELECT count(*) FROM n")
    with pytest.raises(AskError):
        run_query(path, slow, c3)


def test_every_listed_column_exists_in_the_real_schema(db):
    conn, _ = db
    for table, (cols, note) in ALLOWED.items():
        real = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        assert set(cols) <= real, (table, set(cols) - real)
        assert note


def test_the_prompt_lists_tables_types_and_families(cfg):
    p = sql_system_prompt(cfg)
    assert "accounts(" in p and "leadership_change" in p and "leadership:" in p and "AI & Technology" in p
    assert "settings" not in schema_text(cfg) and "audit_log" not in schema_text(cfg) and "lead_notes(" not in schema_text(cfg)


# ---- the flow -----------------------------------------------------------------------------------------------------------
def test_a_question_is_answered_from_the_rows(db, cfg):
    conn, path = db
    llm = FakeLLM([sql_reply("SELECT a.name, a.id AS account_id FROM accounts a JOIN signals s ON s.account_id = a.id "
                             "WHERE s.type = 'leadership_change'", "Companies with a leadership change."), ans_reply("Acme had a leadership change.")])
    o = answer_question(path, cfg, llm, MODEL, "Who had a leadership change?", AS_OF)
    assert o.status == "done" and o.answer == "Acme had a leadership change." and o.columns == ["name", "account_id"] and o.rows == [["Acme", 1]]
    assert len(llm.calls) == 2
    payload = json.loads(llm.calls[1][1][0]["content"])
    assert payload["rows"] == [["Acme", 1]] and payload["row_count"] == 1 and payload["truncated"] is False
    assert "Today's date: 2026-10-15" in llm.calls[0][1][0]["content"]
    assert o.cost is not None and o.usage.total_tokens > 0


def test_a_bad_query_is_retried_once_with_the_error(db, cfg):
    _, path = db
    llm = FakeLLM([sql_reply("SELECT nope FROM accounts"), sql_reply("SELECT name FROM accounts ORDER BY id"), ans_reply("Acme and Beta.")])
    o = answer_question(path, cfg, llm, MODEL, "List companies", AS_OF)
    assert o.status == "done" and o.rows == [["Acme"], ["Beta"]]
    assert "no such column" in llm.calls[1][1][0]["content"] and "SELECT nope" in llm.calls[1][1][0]["content"]


def test_two_failures_end_plainly_without_running_anything_unsafe(db, cfg):
    _, path = db
    llm = FakeLLM([sql_reply("DELETE FROM accounts"), sql_reply("SELECT * FROM settings")])
    o = answer_question(path, cfg, llm, MODEL, "x", AS_OF)
    assert o.status == "error" and "safe query" in o.error and o.rows is None and len(llm.calls) == 2
    assert make_db_count(path) == 2


def test_an_unanswerable_question_says_what_is_missing(db, cfg):
    _, path = db
    llm = FakeLLM([sql_reply("", "There is no weather data.")])
    o = answer_question(path, cfg, llm, MODEL, "Will it rain?", AS_OF)
    assert o.status == "error" and o.error == "The data cannot answer this: There is no weather data." and len(llm.calls) == 1


def test_the_cost_limit_stops_before_the_answer_call(db, cfg):
    _, path = db
    big = Usage(input_tokens=5_000_000, output_tokens=0)
    llm = FakeLLM([sql_reply("SELECT 1 AS n"), ans_reply()], usage=big)
    o = answer_question(path, cfg, llm, MODEL, "x", AS_OF)
    assert o.status == "error" and "cost limit" in o.error and len(llm.calls) == 1 and o.rows == [[1]]


def test_empty_results_and_truncation_are_passed_on(db, cfg):
    _, path = db
    llm = FakeLLM([sql_reply("SELECT name FROM accounts WHERE 1 = 0"), ans_reply("No match.")])
    answer_question(path, cfg, llm, MODEL, "x", AS_OF)
    assert json.loads(llm.calls[1][1][0]["content"])["row_count"] == 0
    c2 = cfg.model_copy(deep=True)
    c2.ask = c2.ask.model_copy(update={"max_rows": 1})
    llm = FakeLLM([sql_reply("SELECT name FROM accounts"), ans_reply("One shown.")])
    o = answer_question(path, c2, llm, MODEL, "x", AS_OF)
    assert o.truncated and json.loads(llm.calls[1][1][0]["content"])["truncated"] is True


def test_run_ask_stores_the_result_and_logs_the_cost(db, cfg):
    conn, path = db
    qid = add_question(conn, "Who is in Energy?")
    llm = FakeLLM([sql_reply("SELECT name, id AS account_id FROM accounts WHERE industry = 'Energy'"), ans_reply("Beta.")])
    o = run_ask(conn, path, cfg, llm, MODEL, qid, AS_OF)
    assert o.status == "done"
    q = recent(conn)[0]
    assert (q["status"], q["answer"], q["columns"], q["rows"]) == ("done", "Beta.", ["name", "account_id"], [["Beta", 2]])
    assert q["sql"].startswith("SELECT name") and q["cost"] and q["finished"]
    assert (q["id_col"], q["name_col"]) == (1, 0)
    assert conn.execute("SELECT COUNT(*) FROM runs WHERE step = 'ask'").fetchone()[0] == 1
    assert run_ask(conn, path, cfg, llm, MODEL, qid, AS_OF) is None and run_ask(conn, path, cfg, llm, MODEL, 999, AS_OF) is None


def test_question_cleaning_and_link_columns():
    assert clean_question("  who   is\n here? ", 50) == "who is here?" and clean_question("", 50) is None and clean_question("x" * 51, 50) is None
    assert link_columns(["name", "account_id"]) == (1, 0) and link_columns(["name", "id"]) == (None, None) and link_columns(["account_id"]) == (None, None)


# ---- job and page ---------------------------------------------------------------------------------------------------------
def test_job_validation_and_paid_tick(tmp_path):
    from bd.jobs import JobError, JobRunner, validate
    assert validate("ask", {"id": "7"}) == ["ask", "--id", "7"]
    for bad in ({}, {"id": "0"}, {"id": "x"}, {"id": "-1"}, {"id": "1; rm -rf"}):
        with pytest.raises(JobError):
            validate("ask", bad)
    make_db(tmp_path).close()
    with pytest.raises(JobError, match="confirm"):
        JobRunner(str(tmp_path / "t.sqlite"), tmp_path, ["true"]).submit("ask", {"id": "1"})


def test_the_page_flow(app_env):  # noqa: F811
    e = app_env
    assert "No questions yet" in e.get("/ask").body and "Ask your data" in e.get("/ask").body
    assert e.post("/ask", {"question": "   ", "confirm": "1"}).code == 303 and not e.conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
    e.post("/ask", {"question": "How many companies?"})                                   # no paid-call tick
    assert not e.conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
    assert e.post("/ask", {"question": "x"}, csrf=False).code == 403
    r = e.post("/ask", {"question": "How many companies?", "confirm": "1"})
    assert r.code == 303 and r.h["Location"] == "/ask"
    e.app.jobs.wait()
    assert e.conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 1
    assert e.conn.execute("SELECT kind, args_json FROM jobs").fetchone()[0] == "ask"


def test_answers_are_escaped_and_company_names_link(app_env):  # noqa: F811
    e = app_env
    a = add_account(e.conn, "Acme")
    qid = add_question(e.conn, "Who? <script>alert(1)</script>")
    with e.conn:
        e.conn.execute("UPDATE questions SET status='done', answer=?, sql=?, columns_json=?, rows_json=?, explanation=?, cost=0.01 WHERE id=?",
                       ("Acme <b>is</b> here.", "SELECT name FROM accounts", json.dumps(["name", "account_id"]), json.dumps([["Acme", a]]), "Companies.", qid))
    body = e.get("/ask").body
    assert "Acme &lt;b&gt;is&lt;/b&gt; here." in body and "&lt;script&gt;alert(1)&lt;/script&gt;" in body and "<script>alert" not in body
    assert f'<a href="/companies/{a}">Acme</a>' in body and "SELECT name FROM accounts" in body and "answered" in body
    add_question(e.conn, "Pending one")
    assert 'http-equiv="refresh"' in e.get("/ask").body and "working" in e.get("/ask").body


def test_the_local_viewer_cannot_ask(cfg, tmp_path):
    from bd.ui.app import App
    from tests.test_hosted import call
    make_db(tmp_path).close()
    app = App(cfg, str(tmp_path / "t.sqlite"), token="tok", root=tmp_path)
    app.port = 1234
    r = call(app, "GET", "/ask", host="127.0.0.1:1234")
    assert "needs the hosted app" in r.body if r.code == 200 else r.code in (200, 403)
