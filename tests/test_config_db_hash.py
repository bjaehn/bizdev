import sqlite3
from datetime import date

import pytest
import yaml
from pydantic import ValidationError

from bd.config import Config, Settings, load_config
from bd.db import connect, init_db
from bd.hashing import signal_hash, source_domain
from bd.models import SIGNAL_TYPES, SignalExtract
from tests.conftest import ROOT


# ---- config ---------------------------------------------------------------------

def test_repo_config_loads_and_covers_every_type(cfg):
    assert set(cfg.scoring.base_points) == set(SIGNAL_TYPES)
    assert cfg.fit_gate.counties == ["Cook", "DuPage", "Lake", "Will", "Kane", "McHenry"]
    assert cfg.fit_gate.size_floor["pe-backed"].revenue_usd == 75_000_000


def _raw():
    return yaml.safe_load((ROOT / "config.yaml").read_text())


def test_config_rejects_missing_type():
    raw = _raw()
    del raw["scoring"]["base_points"]["rfp_match"]
    with pytest.raises(ValidationError):
        Config.model_validate(raw)


def test_config_rejects_type_in_two_families():
    raw = _raw()
    raw["scoring"]["families"]["filings"].append("pe_addon")
    with pytest.raises(ValidationError):
        Config.model_validate(raw)


def test_config_rejects_unknown_keys_and_bad_bands():
    raw = _raw()
    raw["scoring"]["typo_key"] = 1
    with pytest.raises(ValidationError):
        Config.model_validate(raw)
    raw = _raw()
    raw["scoring"]["bands"] = {"hot": 50, "warm": 60, "watch": 40}
    with pytest.raises(ValidationError):
        Config.model_validate(raw)


def test_secrets_and_model_only_from_env(monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "TEAMS_WEBHOOK_URL"):
        monkeypatch.delenv(k, raising=False)
    s = Settings.from_env()
    assert s.teams_webhook_url is None
    with pytest.raises(RuntimeError):
        s.require_llm()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_MODEL", "m")
    assert Settings.from_env().require_llm() == ("k", "m")
    text = (ROOT / "config.yaml").read_text().lower()
    assert "api_key" not in text and "webhook" not in text and "claude-" not in text


# ---- hash -----------------------------------------------------------------------

def test_hash_dedupes_on_domain_not_url():
    d = date(2026, 9, 1)
    a = signal_hash(1, "leadership_change", d, "https://www.acme.com/news/ceo")
    b = signal_hash(1, "leadership_change", d, "http://ACME.com:8080/press/other")
    assert a == b
    assert a != signal_hash(2, "leadership_change", d, "https://acme.com/x")
    assert a != signal_hash(1, "open_exec_search", d, "https://acme.com/x")
    assert a != signal_hash(1, "leadership_change", date(2026, 9, 2), "https://acme.com/x")
    assert a != signal_hash(1, "leadership_change", d, "https://sec.gov/x")
    assert source_domain("https://www.sec.gov/Archives/x") == "sec.gov"


# ---- extract model ----------------------------------------------------------------

def _extract(**over):
    base = dict(type="leadership_change", event_date="2026-09-01", summary="New CIO named.",
                source_url="https://acme.com/news", source_type="company_release", confidence="High",
                service_lines=["AI & Technology"])
    return SignalExtract(**{**base, **over})


def test_extract_valid_and_summary_limit():
    assert _extract().event_date == date(2026, 9, 1)
    assert len(_extract(summary=" ".join(["w"] * 25)).summary.split()) == 25
    with pytest.raises(ValidationError):
        _extract(summary=" ".join(["w"] * 26))
    with pytest.raises(ValidationError):
        _extract(summary="  ")


@pytest.mark.parametrize("bad", ["acme.com/news", "ftp://acme.com/x", "https://", ""])
def test_extract_rejects_bad_url(bad):
    with pytest.raises(ValidationError):
        _extract(source_url=bad)


def test_extract_rejects_bad_enums_and_bad_date():
    for over in ({"type": "made_up"}, {"confidence": "Certain"}, {"source_type": "blog"},
                 {"event_date": "not-a-date"}):
        with pytest.raises(ValidationError):
            _extract(**over)


# ---- db -------------------------------------------------------------------------

@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.sqlite")
    init_db(c)
    yield c
    c.close()


def test_schema_tables_and_idempotent_init(conn):
    init_db(conn)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"accounts", "signals", "scores", "exclusions", "runs"} <= names


def _account(conn, name="Acme", domain="acme.com"):
    return conn.execute("INSERT INTO accounts(name, domain) VALUES (?,?)", (name, domain)).lastrowid


def _signal(conn, aid, h="h1", conf="High"):
    conn.execute(
        "INSERT INTO signals(account_id,family,type,event_date,detected_date,summary,source_url,"
        "source_type,confidence,hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (aid, "leadership", "leadership_change", "2026-09-01", "2026-10-15", "s", "https://acme.com/x",
         "company_release", conf, h))


def test_signal_hash_unique_and_fk_enforced(conn):
    aid = _account(conn)
    _signal(conn, aid)
    with pytest.raises(sqlite3.IntegrityError):
        _signal(conn, aid)  # duplicate hash
    with pytest.raises(sqlite3.IntegrityError):
        _signal(conn, 999, h="h2")  # no such account


def test_check_constraints(conn):
    aid = _account(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _signal(conn, aid, h="x", conf="Certain")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO exclusions(name, reason) VALUES ('X','because')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO accounts(name, tier) VALUES ('X','C')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO accounts(name, ownership) VALUES ('X','nonprofit')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO accounts(name, status) VALUES ('X','paused')")
    row = conn.execute("SELECT chicagoland FROM accounts WHERE id=?", (aid,)).fetchone()
    assert row["chicagoland"] == "unknown"  # default is unknown, never assumed


def test_scores_pk_and_runs_per_account(conn):
    aid = _account(conn)
    conn.execute("INSERT INTO scores(account_id,run_date,score,band,components_json,rationale) "
                 "VALUES (?,?,?,?,?,?)", (aid, "2026-10-15", 91, "Hot", "{}", "r"))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO scores(account_id,run_date,score,band,components_json,rationale) "
                     "VALUES (?,?,?,?,?,?)", (aid, "2026-10-15", 1, "Stored", "{}", "r"))
    conn.execute("INSERT INTO runs(run_date,step,account_id,tokens,est_cost) VALUES (?,?,?,?,?)",
                 ("2026-10-15", "scan:leadership", aid, 1234, 0.0))
    assert conn.execute("SELECT COUNT(*) FROM runs WHERE account_id=?", (aid,)).fetchone()[0] == 1
