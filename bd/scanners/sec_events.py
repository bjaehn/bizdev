"""SEC event scanner: restructuring and deal signals read straight from 8-K item codes. No model, so no cost.

EDGAR says which items each 8-K reports (Item 2.05 costs of exit or disposal activities, Item 2.01 completion of an acquisition
or disposition). Code does the rest: the event date is the filing's report date, the filing document is fetched (so the URL has
been retrieved) and must carry the item's own heading, and the summary is written from the label in config.yaml `sec_events`. A
signal is sec_filing / High by construction. Which items count, and the signal type and service lines each one maps to, are
configuration; nothing is inferred from the filing's wording."""

from __future__ import annotations

from datetime import date, timedelta

from bd.accounts import Account
from bd.config import Config
from bd.edgar import EdgarClient, EdgarError
from bd.hashing import signal_hash
from bd.models import SignalExtract
from bd.scanners.base import AcceptedSignal, Rejection, ScanOutcome, Scanner


class SecEventsScanner(Scanner):
    name = "sec_events"
    free = True
    reads_filings = True

    def __init__(self, cfg: Config, edgar: EdgarClient | None) -> None:
        super().__init__(cfg, None, None)
        self.edgar = edgar
        self.allowed_types = frozenset(i.type for i in cfg.sec_events.items.values())

    @classmethod
    def create(cls, cfg, web, text, verifier, edgar, ats=None):  # type: ignore[override]
        return cls(cfg, edgar)

    def applies_to(self, account: Account) -> bool:
        return bool(account.cik)

    def scan(self, account: Account, as_of: date, known_accessions: frozenset[str] = frozenset()) -> ScanOutcome:
        out = ScanOutcome(account=account, scanner=self.name)
        if not account.cik:
            return out
        assert self.edgar is not None
        cfg = self.cfg
        items = cfg.sec_events.items
        lo = as_of - timedelta(days=cfg.scan.window_days)
        hi = as_of + timedelta(days=cfg.scan.max_future_days)
        try:
            filings = self.edgar.recent_filings(account.cik, lo, hi, cfg.sec_events.max_filings_per_scan, set(items))
        except EdgarError as e:
            out.error = f"EdgarError: {e}"
            return out
        seen: set[str] = set()
        for f in filings:
            if f.accession in known_accessions:           # read on an earlier run
                continue
            wanted = {i for i in f.items if i in items}
            out.candidates += 1
            try:
                present = self.edgar.filing_items(f, wanted)
            except EdgarError as e:
                out.rejections.append(Rejection(f"unreachable:{e}", None, f.url))
                continue
            out.read_filings.append(f.accession)
            for item in sorted(wanted):
                spec = items[item]
                if item not in present:
                    out.rejections.append(Rejection("item_not_in_filing", spec.type, f.url, f"Item {item}"))
                    continue
                extract = SignalExtract(
                    type=spec.type, event_date=f.event_date, summary=cfg.sec_events.summary(item), source_url=f.url,
                    source_type="sec_filing", confidence="High", service_lines=self._canonical_lines(spec.service_lines),
                    role_or_person=None)
                h = signal_hash(account.id, extract.type, extract.event_date, extract.source_url)
                if h in seen:
                    out.rejections.append(Rejection("duplicate_in_batch", extract.type, f.url))
                    continue
                seen.add(h)
                out.signals.append(AcceptedSignal(extract, cfg.scoring.family_of(extract.type), h,
                                                  [f"EDGAR {f.form} {f.accession} Item {item}"]))
        return out
