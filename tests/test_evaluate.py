from datetime import date

import pytest

from bd.accounts import Account
from bd.evaluate import GOLD_COLUMNS, GoldError, GoldRow, evaluate, load_gold
from bd.llm import Usage
from bd.models import SignalExtract
from bd.scanners.base import AcceptedSignal, Rejection, ScanOutcome
from tests.conftest import AS_OF

HDR = ",".join(GOLD_COLUMNS) + "\n"


def write(tmp_path, body, header=HDR):
    p = tmp_path / "gold.csv"
    p.write_text(header + body)
    return p


def test_load_gold_ok(tmp_path):
    p = write(tmp_path, "Acme,www.acme.com,leadership_change,2026-09-20,https://acme.com/a\n"
                        "Beta Co,beta.com,none,,\n")
    rows = load_gold(p)
    assert rows[0] == GoldRow("Acme", "acme.com", "leadership_change", date(2026, 9, 20), "https://acme.com/a")
    assert rows[1].expected_type is None and rows[1].expected_date is None


@pytest.mark.parametrize("body,msg", [
    ("Acme,acme.com,,,\n", "blank"),                                           # unfilled template row
    ("Acme,acme.com,made_up,2026-09-20,https://a.com\n", "unknown expected_type"),
    ("Acme,acme.com,leadership_change,20-09-2026,https://a.com\n", "YYYY-MM-DD"),
    ("Acme,acme.com,leadership_change,2026-09-20,acme.com/a\n", r"http\(s\)"),
    ("Acme,acme.com,none,2026-09-20,\n", "leave date and url blank"),
    (",acme.com,none,,\n", "account is required"),
])
def test_load_gold_rejects_bad_rows(tmp_path, body, msg):
    with pytest.raises(GoldError, match=msg):
        load_gold(write(tmp_path, body))


def test_load_gold_missing_file_header_and_empty(tmp_path):
    with pytest.raises(GoldError, match="not found"):
        load_gold(tmp_path / "nope.csv")
    with pytest.raises(GoldError, match="header"):
        load_gold(write(tmp_path, "", header="a,b\n"))
    with pytest.raises(GoldError, match="no rows"):
        load_gold(write(tmp_path, ""))


def test_template_is_not_a_valid_gold_file():
    from tests.conftest import ROOT
    with pytest.raises(GoldError, match="blank"):
        load_gold(ROOT / "tests" / "gold.template.csv")


def acc_sig(type_, d, url, conf="High"):
    e = SignalExtract(type=type_, event_date=d, summary="s", source_url=url, source_type="company_release",
                      confidence=conf)
    return AcceptedSignal(e, "leadership", "h" + url)


def fake_scan(table):
    """table: account name -> (signals, rejections, error)"""
    def scan(account: Account, as_of):
        sigs, rejs, err = table.get(account.name, ([], [], None))
        return ScanOutcome(account=account, scanner="leadership", signals=sigs, rejections=rejs, error=err,
                           usage=Usage(input_tokens=10, output_tokens=5, web_searches=1))
    return scan


def G(acct, t, d, u, dom="x.com"):
    return GoldRow(acct, dom, t, date.fromisoformat(d) if d else None, u)


def test_evaluate_reports_found_missed_wrong_date_and_bad_urls():
    gold = [
        G("A", "leadership_change", "2026-09-20", "https://a.com/1"),          # found, exact, same url
        G("B", "leadership_change", "2026-09-20", "https://b.com/1"),          # found, wrong date (+5d)
        G("C", "open_exec_search", "2026-09-01", "https://c.com/1"),           # missed
        G("D", None, None, None),                                              # no signal; clean
        G("E", None, None, None),                                              # no signal; false positive
        G("F", "leadership_change", "2026-09-10", "https://f.com/1"),          # found + unexpected extra
    ]
    table = {
        "A": ([acc_sig("leadership_change", date(2026, 9, 20), "https://www.a.com/1/")], [], None),
        "B": ([acc_sig("leadership_change", date(2026, 9, 25), "https://b.com/other")], [], None),
        "C": ([acc_sig("leadership_change", date(2026, 9, 1), "https://c.com/1")], [  # wrong type -> missed
            Rejection("url_not_retrieved", "open_exec_search", "https://c.com/made-up"),
            Rejection("unreachable:http_404", "open_exec_search", "https://c.com/gone"),
            Rejection("blocked_domain", "open_exec_search", "https://linkedin.com/x")], None),
        "E": ([acc_sig("leadership_change", date(2026, 9, 2), "https://e.com/1")], [], None),
        "F": ([acc_sig("leadership_change", date(2026, 9, 10), "https://f.com/1"),
               acc_sig("open_exec_search", date(2026, 9, 12), "https://f.com/2")], [], None),
    }
    rep = evaluate(gold, fake_scan(table), AS_OF)
    t = rep.totals()
    assert t["expected_signals"] == 4
    assert (t["found"], t["found_exact_date"], t["wrong_date"], t["missed"]) == (3, 2, 1, 1)
    assert (t["no_signal_accounts"], t["no_signal_clean"], t["false_positives"]) == (2, 1, 1)
    assert (t["fabricated_urls"], t["unreachable_urls"], t["blocked_urls"]) == (1, 1, 1)
    assert t["unexpected_extra"] == 2  # C's wrong-type signal + F's extra
    assert t["tokens"] == 15 * 6 and t["web_searches"] == 6
    b = next(a for a in rep.accounts if a.account == "B").rows[0]
    assert b.status == "wrong_date" and b.date_diff_days == 5 and b.url_match is False
    a = next(a for a in rep.accounts if a.account == "A").rows[0]
    assert a.status == "found" and a.url_match is True
    text = rep.render()
    assert "wrong date 1" in text and "Fabricated URLs  : 1" in text and "false_pos" in text


def test_date_tolerance(cfg):
    gold = [G("B", "leadership_change", "2026-09-20", "https://b.com/1")]
    table = {"B": ([acc_sig("leadership_change", date(2026, 9, 22), "https://b.com/1")], [], None)}
    assert evaluate(gold, fake_scan(table), AS_OF, tolerance_days=0).totals()["wrong_date"] == 1
    assert evaluate(gold, fake_scan(table), AS_OF, tolerance_days=2).totals()["wrong_date"] == 0


def test_errored_account_is_not_counted_clean_and_gold_missed():
    gold = [G("A", None, None, None), G("B", "leadership_change", "2026-09-20", "https://b.com/1")]
    table = {"A": ([], [], "LLMError: down"), "B": ([], [], "LLMError: down")}
    t = evaluate(gold, fake_scan(table), AS_OF).totals()
    assert t["no_signal_clean"] == 0 and t["accounts_with_errors"] == 2 and t["missed"] == 1


def test_limit_and_multi_row_accounts_and_domain_passed():
    seen = []
    def scan(account, as_of):
        seen.append((account.name, account.domain))
        return ScanOutcome(account=account, scanner="leadership")
    gold = [G("A", "leadership_change", "2026-09-20", "https://a.com/1", dom="a.com"),
            G("A", "open_exec_search", "2026-09-21", "https://a.com/2", dom="a.com"),
            G("B", None, None, None, dom="")]
    rep = evaluate(gold, scan, AS_OF, limit=1)
    assert seen == [("A", "a.com")] and len(rep.accounts) == 1 and len(rep.accounts[0].rows) == 2
    assert evaluate(gold, scan, AS_OF).accounts[1].expects_none
    assert seen[-1] == ("B", None)


def test_json_report_roundtrip():
    import json
    rep = evaluate([G("A", None, None, None)], fake_scan({}), AS_OF)
    d = json.loads(rep.to_json())
    assert d["totals"]["no_signal_clean"] == 1 and d["accounts"][0]["account"] == "A"


def test_combined_scan_merges_scanners_usage_and_errors(cfg):
    from bd.evaluate import combined_scan
    from tests.test_runner import FakeScanner
    a, b = FakeScanner(cfg, "leadership", signals_for={"Acme"}), FakeScanner(cfg, "events", fail={"Acme"})
    acct = Account(id=-1, name="Acme", domain="acme.com")
    out = combined_scan([a, b])(acct, AS_OF)
    assert out.scanner == "leadership+events" and len(out.signals) == 1 and len(out.rejections) == 1
    assert out.usage.total_tokens == 3000 and out.usage.web_searches == 4
    assert out.error == "events: LLMError: down"
