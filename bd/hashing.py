"""Signal dedupe hash: hash(account, type, event_date, source domain)."""

from __future__ import annotations

import hashlib
from datetime import date

from bd.urls import hostname


def source_domain(url: str) -> str:
    return hostname(url)


def signal_hash(account_id: int | str, type_: str, event_date: date, source_url: str) -> str:
    key = "|".join([str(account_id), type_, event_date.isoformat(), source_domain(source_url)])
    return hashlib.sha256(key.encode()).hexdigest()
