import threading
import time
from datetime import date, timedelta

import pytest

from bd.accounts import Account
from bd.db import connect, init_db
from bd.llm import Usage
from bd.models import SignalExtract
from bd.runner import is_due, run_scans
from bd.scanners.base import AcceptedSignal, Rejection, ScanOutcome, Scanner
from tests.conftest import AS_OF

MODEL = "claude-sonnet-5-5"


class FakeScanner(Scanner):
    """Scanner double: no LLM; records concurrency; behaviour keyed by account name."""
    allowed_types = frozenset({"leadership_change"})

    def __init__(self, cfg, name="leadership", delay=0.0, usage=None, fail=(), boom=(), signals_for=()):
        super().__init__(cfg, None, None)
        self.name = name
        self.delay, self.fail, self.boom, self.signals_for = delay, set(fail), set(boom), set(signals_for)
        self.usage = usage or Usage(input_tokens=1000, output_tokens=500, web_searches=2)
        self.lock = threading.Lock()
        self.active = self.max_active = 0
        self.calls = []

    def scan(self, account, as_of):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls.append(account.name)
        try:
            time.sleep(self.delay)
            if account.name in self.boom:
                raise RuntimeError("scanner bug")
            out = ScanOutcome(account=account, scanner=self.name)
            u = Usage()
            u.add(self.usage)
            out.usage = u
            if account.name in self.fail:
                out.error = "LLMError: down"
            if account.name in self.signals_for:
                e = SignalExtract(type="leadership_change", event_date=as_of - timedelta(days=5), summary="s",
                                  source_url=f"https://{account.domain}/n", source_type="company_release",
                                  confidence="High")
                out.signals = [AcceptedSignal(e, "leadership", f"h-{account.name}-{self.name}")]
                out.rejections = [Rejection("url_not_retrieved")]
            return out
        finally:
            with self.lock:
                self.active -= 1


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.sqlite")
    init_db(c)
    yield c
    c.close()


def make_accounts(conn, n, tier="A", last_scanned=None):
    for i in range(n):
        conn.execute("INSERT INTO accounts(name, domain, tier, status, last_scanned) VALUES (?,?,?,?,?)",
                     (f"Co{i}", f"co{i}.com", tier, "active", last_scanned))
    conn.commit()
    from bd.accounts import list_accounts
    return list_accounts(conn)


def test_concurrency_limit_is_respected_and_work_actually_overlaps(cfg, conn):
    accts = make_accounts(conn, 12)
    sc = FakeScanner(cfg, delay=0.05)
    s = run_scans(conn, cfg, [sc], accts, AS_OF, MODEL, workers=3)
    assert sc.max_active == 3 and len(sc.calls) == 12 and len(s.accounts) == 12


def test_workers_one_is_serial(cfg, conn):
    sc = FakeScanner(cfg, delay=0.01)
    run_scans(conn, cfg, [sc], make_accounts(conn, 4), AS_OF, MODEL, workers=1)
    assert sc.max_active == 1


def test_default_workers_come_from_config(cfg, conn):
    sc = FakeScanner(cfg, delay=0.05)
    run_scans(conn, cfg, [sc], make_accounts(conn, 12), AS_OF, MODEL)
    assert sc.max_active == cfg.llm.concurrency


def test_cost_logged_per_account_per_call_and_total(cfg, conn):
    accts = make_accounts(conn, 2)
    a, b = FakeScanner(cfg, "leadership"), FakeScanner(cfg, "events")
    s = run_scans(conn, cfg, [a, b], accts, AS_OF, MODEL, workers=2)
    per_call = 1000 * 2 / 1e6 + 500 * 10 / 1e6 + 2 * 0.01   # $0.002 + $0.005 + $0.02
    rows = conn.execute("SELECT * FROM runs WHERE account_id IS NOT NULL ORDER BY account_id, step").fetchall()
    assert len(rows) == 4
    assert [r["step"] for r in rows] == ["scan:events", "scan:leadership"] * 2
    assert all(r["tokens"] == 1500 and r["est_cost"] == pytest.approx(per_call) for r in rows)
    total = conn.execute("SELECT * FROM runs WHERE step='scan:total'").fetchone()
    assert total["account_id"] is None and total["tokens"] == 6000
    assert total["est_cost"] == pytest.approx(4 * per_call) and total["accounts_scanned"] == 2
    assert s.est_cost == pytest.approx(4 * per_call)
    assert s.accounts[accts[0].id].est_cost == pytest.approx(2 * per_call)
    assert s.accounts[accts[0].id].web_searches == 4


def test_unpriced_model_logs_null_never_zero(cfg, conn):
    accts = make_accounts(conn, 1)
    s = run_scans(conn, cfg, [FakeScanner(cfg)], accts, AS_OF, "claude-unlisted-1")
    assert conn.execute("SELECT est_cost FROM runs WHERE account_id IS NOT NULL").fetchone()[0] is None
    t = conn.execute("SELECT est_cost, errors FROM runs WHERE step='scan:total'").fetchone()
    assert t["est_cost"] is None and "unpriced" in t["errors"]
    assert s.unpriced_calls == 1 and s.accounts[accts[0].id].est_cost is None


def test_signals_stored_and_counted_with_rejections_noted(cfg, conn):
    accts = make_accounts(conn, 2)
    s = run_scans(conn, cfg, [FakeScanner(cfg, signals_for={"Co0"})], accts, AS_OF, MODEL)
    assert s.new_signals == 1 and s.accounts[accts[0].id].rejected == 1
    row = conn.execute("SELECT event_date, detected_date FROM signals").fetchone()
    assert (row["event_date"], row["detected_date"]) == ((AS_OF - timedelta(days=5)).isoformat(), AS_OF.isoformat())
    note = conn.execute("SELECT errors FROM runs WHERE account_id=?", (accts[0].id,)).fetchone()[0]
    assert "url_not_retrieved=1" in note
    # second run: same hashes -> known, not new
    s2 = run_scans(conn, cfg, [FakeScanner(cfg, signals_for={"Co0"})], accts, AS_OF, MODEL, force=True)
    assert s2.new_signals == 0 and s2.accounts[accts[0].id].known_signals == 1


def test_errors_are_isolated_and_last_scanned_only_set_on_success(cfg, conn):
    accts = make_accounts(conn, 4)
    sc = FakeScanner(cfg, fail={"Co1"}, boom={"Co2"})
    s = run_scans(conn, cfg, [sc], accts, AS_OF, MODEL, workers=2)
    assert len(sc.calls) == 4 and s.errors == 2
    ls = {r["name"]: r["last_scanned"] for r in conn.execute("SELECT name, last_scanned FROM accounts")}
    assert ls == {"Co0": AS_OF.isoformat(), "Co1": None, "Co2": None, "Co3": AS_OF.isoformat()}
    errs = {r.account.name: r.errors for r in s.accounts.values() if r.errors}
    assert "LLMError" in errs["Co1"][0] and "RuntimeError: scanner bug" in errs["Co2"][0]
    assert "2 call errors" in conn.execute("SELECT errors FROM runs WHERE step='scan:total'").fetchone()[0]


def test_tokens_spent_by_a_failed_call_are_still_logged(cfg, conn):
    accts = make_accounts(conn, 1)
    run_scans(conn, cfg, [FakeScanner(cfg, fail={"Co0"})], accts, AS_OF, MODEL)
    assert conn.execute("SELECT tokens FROM runs WHERE account_id IS NOT NULL").fetchone()[0] == 1500


def test_last_scanned_waits_for_all_scanners(cfg, conn):
    accts = make_accounts(conn, 1)
    run_scans(conn, cfg, [FakeScanner(cfg, "leadership"), FakeScanner(cfg, "events", fail={"Co0"})],
              accts, AS_OF, MODEL)
    assert conn.execute("SELECT last_scanned FROM accounts").fetchone()[0] is None


@pytest.mark.parametrize("tier,age,due", [
    ("A", 7, True), ("A", 6, True), ("A", 5, False),        # weekly, 1-day grace
    ("B", 13, True), ("B", 12, False), ("B", 7, False),     # every other week
])
def test_cadence(cfg, tier, age, due):
    acc = Account(id=1, name="X", tier=tier, last_scanned=(AS_OF - timedelta(days=age)).isoformat())
    assert is_due(acc, AS_OF, cfg) is due
    assert is_due(acc, AS_OF, cfg, force=True)
    assert is_due(Account(id=2, name="Y", tier=tier), AS_OF, cfg)   # never scanned


def test_not_due_accounts_are_skipped_without_calls(cfg, conn):
    recent = (AS_OF - timedelta(days=2)).isoformat()
    accts = make_accounts(conn, 3, last_scanned=recent)
    sc = FakeScanner(cfg)
    s = run_scans(conn, cfg, [sc], accts, AS_OF, MODEL)
    assert sc.calls == [] and len(s.skipped_not_due) == 3
    run_scans(conn, cfg, [sc], accts, AS_OF, MODEL, force=True)
    assert len(sc.calls) == 3


def test_budget_cap_stops_new_calls_and_reports(cfg, conn):
    accts = make_accounts(conn, 10)
    per_call = 0.027
    sc = FakeScanner(cfg, delay=0.01)
    s = run_scans(conn, cfg, [sc], accts, AS_OF, MODEL, workers=1, max_cost=per_call * 3 + 0.001)   # the limit holds: a fourth call would pass it
    assert s.budget_hit and len(sc.calls) == 3 and s.skipped_budget == 7
    t = conn.execute("SELECT errors FROM runs WHERE step='scan:total'").fetchone()[0]
    assert "budget cap hit, 7 calls not started" in t
    # accounts that never ran keep last_scanned NULL so the next run picks them up
    assert conn.execute("SELECT COUNT(*) FROM accounts WHERE last_scanned IS NULL").fetchone()[0] == 7


def test_config_cap_applies_by_default_and_is_not_hit_here(cfg, conn):
    accts = make_accounts(conn, 5)
    s = run_scans(conn, cfg, [FakeScanner(cfg)], accts, AS_OF, MODEL, workers=1, max_cost=None)
    # config default cap is $25, far above this run
    assert not s.budget_hit and s.skipped_budget == 0


def test_limit_holds_with_calls_in_flight(cfg, conn):
    """Four workers used to start four ~$0.30 calls before the first finished, ending about $1 over the limit."""
    accts = make_accounts(conn, 12)
    sc = FakeScanner(cfg, delay=0.05, usage=Usage(input_tokens=100_000, output_tokens=10_000, web_searches=3))   # about $0.30 a call
    s = run_scans(conn, cfg, [sc], accts, AS_OF, MODEL, workers=4, max_cost=1.0)
    assert s.budget_hit and s.skipped_budget > 0
    assert s.est_cost <= 1.0 + 1e-9, s.est_cost


def test_reserve_rules():
    from bd.budget import Reserve
    r = Reserve(0.35)
    assert r.allows(0, 0, None) and not r.allows(5.0, 0, 5.0)
    assert r.allows(0.0, 0, 0.1)                      # one call is always let through when nothing is running
    assert not r.allows(0.5, 3, 1.0) and r.allows(0.2, 1, 1.0) and not r.allows(0.9, 0, 1.0)
    r.note(0.01, searched=False)                      # a cheap SEC lookup says nothing about a search call
    assert r.per_call == 0.35
    r.note(0.25), r.note(0.35)
    assert abs(r.per_call - 0.30) < 1e-9
