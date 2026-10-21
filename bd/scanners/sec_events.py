"""SEC event scanner: restructuring and deal signals read straight from 8-K item codes. No model, so no cost.

EDGAR says which items each 8-K reports (Item 2.05 costs of exit or disposal activities, Item 2.01 completion of an acquisition
or disposition). Code does the rest: the event date is the filing's report date, the filing document is fetched (so the URL has
been retrieved) and must carry the item's own heading, and the summary is written from the label in config.yaml `sec_events`. A
signal is sec_filing / High by construction. Which items count, and the signal type and service lines each one maps to, are
configuration; nothing is inferred from the filing's wording."""

from __future__ import annotations

import re
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
        self.allowed_types = (frozenset(i.type for i in cfg.sec_events.items.values()) | frozenset(cfg.sec_events.officer_titles)
                              | (frozenset({"activist"}) if cfg.sec_events.schedule_13d.activists else frozenset()))

    @classmethod
    def create(cls, cfg, web, text, verifier, edgar, ats=None):  # type: ignore[override]
        return cls(cfg, edgar)

    def applies_to(self, account: Account) -> bool:
        return bool(account.cik)

    @staticmethod
    def _body(text: str) -> str:
        """The item's text without its own heading. Every Item 5.02 heading contains "Departure" and "Appointment" ("Departure of Directors or Certain Officers;
        Election of Directors; Appointment of Certain Officers; Compensatory Arrangements of Certain Officers"), so left in, it would put a change word next to
        whatever title follows it."""
        m = re.search(r"compensatory\s+arrangements?(\s+of\s+certain\s+officers)?", text[:500], re.I)
        if m:
            return text[m.end():]
        m = re.match(r"\s*item\s*5\.02[^.]{0,250}?certain\s+officers", text, re.I)
        return text[m.end():] if m else text

    def officer_hits(self, text: str) -> list[tuple[str, str]]:
        """(signal type, title) for each class of officer title in an Item 5.02 text that sits within officer_window_chars of a change word. The phrase
        "named executive officers" is removed first (it is about pay, and contains a change word). At most one hit per type."""
        c = self.cfg.sec_events
        clean = re.sub(r"named\s+executive\s+officers?", " ", " ".join(self._body(text).split()), flags=re.I)
        words = [m for w in c.officer_change_words for m in re.finditer(r"(?<![A-Za-z])" + re.escape(w) + r"(?![A-Za-z])", clean, re.I)]
        hits: list[tuple[str, str]] = []
        for type_, titles in c.officer_titles.items():
            for title in titles:
                for t in re.finditer(r"(?<![A-Za-z])" + re.escape(title) + r"(?![A-Za-z])", clean, re.I):
                    if any(abs(w.start() - t.start()) <= c.officer_window_chars for w in words):
                        hits.append((type_, title))
                        break
                else:
                    continue
                break
        return hits

    def scan(self, account: Account, as_of: date, known_accessions: frozenset[str] = frozenset()) -> ScanOutcome:
        out = ScanOutcome(account=account, scanner=self.name)
        if not account.cik:
            return out
        assert self.edgar is not None
        cfg = self.cfg
        items = cfg.sec_events.items
        lo = as_of - timedelta(days=cfg.scan.window_days)
        hi = as_of + timedelta(days=cfg.scan.max_future_days)
        d13 = cfg.sec_events.schedule_13d
        forms = set(d13.forms) if d13.activists else set()
        try:
            filings = self.edgar.recent_filings(account.cik, lo, hi, cfg.sec_events.max_filings_per_scan, set(items), forms)
        except EdgarError as e:
            out.error = f"EdgarError: {e}"
            return out
        seen: set[str] = set()
        for f in filings:
            if f.accession in known_accessions:           # read on an earlier run
                out.already_read += 1
                continue
            if f.form in forms:
                self._schedule_13d(account, f, out, seen)
                continue
            wanted = {i for i in f.items if i in items}
            out.candidates += 1
            try:
                sections = self.edgar.filing_sections(f, wanted)
                present = set(sections)
            except EdgarError as e:
                out.rejections.append(Rejection(f"unreachable:{e}", None, f.url))
                continue
            out.read_filings.append(f.accession)
            for item in sorted(wanted):
                spec = items[item]
                if item not in present:
                    out.rejections.append(Rejection("item_not_in_filing", spec.type, f.url, f"Item {item}"))
                    continue
                if spec.officers:
                    hits = self.officer_hits(sections[item])
                    if not hits:
                        out.rejections.append(Rejection("no_officer_title_in_item", spec.type, f.url, f"Item {item}: no officer title next to a change word (a director or pay item)"))
                    for type_, title in hits:
                        extract = SignalExtract(
                            type=type_, event_date=f.event_date, summary=cfg.sec_events.officer_summary(item, title), source_url=f.url, source_type="sec_filing",
                            confidence="Medium", service_lines=self._canonical_lines(cfg.sec_events.officer_service_lines.get(type_, spec.service_lines)),
                            role_or_person=title)
                        h = signal_hash(account.id, extract.type, extract.event_date, extract.source_url)
                        if h in seen:
                            out.rejections.append(Rejection("duplicate_in_batch", extract.type, f.url))
                            continue
                        seen.add(h)
                        out.signals.append(AcceptedSignal(extract, cfg.scoring.family_of(extract.type), h,
                                                          [f"EDGAR {f.form} {f.accession} Item {item}; title and change word within {cfg.sec_events.officer_window_chars} characters"]))
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

    def _schedule_13d(self, account: Account, f, out: ScanOutcome, seen: set[str]) -> None:
        """An initial Schedule 13D is an `activist` signal only when its cover page names an investor from the configured list (never the company itself)."""
        d = self.cfg.sec_events.schedule_13d
        out.candidates += 1
        assert self.edgar is not None
        try:
            head = self.edgar.filing_head(f)
        except EdgarError as e:
            out.rejections.append(Rejection(f"unreachable:{e}", "activist", f.url))
            return
        out.read_filings.append(f.accession)
        named = next((n for n in d.activists if n.lower() not in account.name.lower()
                      and re.search(r"(?<![A-Za-z])" + re.escape(n) + r"(?![A-Za-z])", head, re.I)), None)
        if named is None:
            out.rejections.append(Rejection("no_listed_activist_named", "activist", f.url, f"{f.form}: the cover page names none of the {len(d.activists)} listed investors"))
            return
        extract = SignalExtract(type="activist", event_date=f.event_date, summary=d.summary(named), source_url=f.url, source_type="sec_filing", confidence="High",
                                service_lines=self._canonical_lines(d.service_lines), role_or_person=None)
        h = signal_hash(account.id, extract.type, extract.event_date, extract.source_url)
        if h in seen:
            out.rejections.append(Rejection("duplicate_in_batch", extract.type, f.url))
            return
        seen.add(h)
        out.signals.append(AcceptedSignal(extract, self.cfg.scoring.family_of(extract.type), h, [f"EDGAR {f.form} {f.accession}; cover page names {named}"]))
