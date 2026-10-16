"""Settings managed in the app. config.yaml holds the defaults; values saved here override them.

Only the discovery settings are editable (what to research and how much to add per run). Everything else in
config.yaml (scoring weights, fit-gate floors, pricing, sources) stays in code review, and secrets are never stored
here. Nothing in this module calls an LLM."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from bd.accounts import log_audit
from bd.config import Config, Discovery

INT_FIELDS = {  # name -> (label, min, max)
    "per_segment": ("Companies requested per segment", 1, 20),
    "max_candidates_per_run": ("New companies researched per run", 1, 100),
    "max_auto_add_per_run": ("Auto-adds per run", 0, 100),
    "scan_unreviewed_per_run": ("Unreviewed accounts scanned per run", 0, 100),
}
MAX_SEGMENTS, SEGMENT_MIN, SEGMENT_MAX = 30, 3, 80


def load_discovery_overrides(conn: sqlite3.Connection) -> dict:
    r = conn.execute("SELECT value_json FROM settings WHERE key = 'discovery'").fetchone()
    try:
        return json.loads(r["value_json"]) if r else {}
    except ValueError:
        return {}


def apply_overrides(cfg: Config, conn: sqlite3.Connection) -> Config:
    """cfg with any saved discovery overrides applied (invalid saved values are ignored, never crash a run)."""
    ov = load_discovery_overrides(conn)
    if not ov:
        return cfg
    try:
        merged = Discovery.model_validate({**cfg.discovery.model_dump(), **ov})
    except ValueError:
        return cfg
    return cfg.model_copy(update={"discovery": merged})


def parse_discovery(form: dict[str, str]) -> tuple[dict, list[str]]:
    errs: list[str] = []
    out: dict = {}
    segs, seen = [], set()
    for line in (form.get("segments") or "").splitlines():
        t = " ".join(line.split())
        if not t or t.lower() in seen:
            continue
        if not SEGMENT_MIN <= len(t) <= SEGMENT_MAX:
            errs.append(f"segment {t[:30]!r}: use {SEGMENT_MIN}-{SEGMENT_MAX} characters")
        seen.add(t.lower())
        segs.append(t)
    if not segs:
        errs.append("at least one segment is required")
    if len(segs) > MAX_SEGMENTS:
        errs.append(f"at most {MAX_SEGMENTS} segments")
    out["segments"] = segs
    for name, (label, lo, hi) in INT_FIELDS.items():
        raw = (form.get(name) or "").strip()
        if not raw.isdigit() or not lo <= int(raw) <= hi:
            errs.append(f"{label} must be a whole number from {lo} to {hi}")
        else:
            out[name] = int(raw)
    return out, errs


def save_discovery(conn: sqlite3.Connection, form: dict[str, str], actor: str = "ui") -> list[str]:
    data, errs = parse_discovery(form)
    if errs:
        return errs
    old = load_discovery_overrides(conn)
    with conn:
        conn.execute("INSERT INTO settings(key, value_json, updated) VALUES ('discovery', ?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated = excluded.updated",
                     (json.dumps(data), datetime.now(timezone.utc).isoformat(timespec="seconds")))
        log_audit(conn, actor, "update", None, "settings.discovery", json.dumps(old) if old else None, json.dumps(data))
    return []


def reset_discovery(conn: sqlite3.Connection, actor: str = "ui") -> None:
    with conn:
        old = load_discovery_overrides(conn)
        conn.execute("DELETE FROM settings WHERE key = 'discovery'")
        if old:
            log_audit(conn, actor, "update", None, "settings.discovery", json.dumps(old), None)
