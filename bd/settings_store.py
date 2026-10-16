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


def refresh_enabled(conn: sqlite3.Connection) -> bool:
    """Whether the scheduler runs the daily light refresh (default on). Stored as settings key 'refresh'."""
    r = conn.execute("SELECT value_json FROM settings WHERE key = 'refresh'").fetchone()
    try:
        return bool(json.loads(r["value_json"]).get("enabled", True)) if r else True
    except (ValueError, AttributeError):
        return True


def save_refresh(conn: sqlite3.Connection, enabled: bool, actor: str = "ui") -> None:
    old = refresh_enabled(conn)
    with conn:
        conn.execute("INSERT INTO settings(key, value_json, updated) VALUES ('refresh', ?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated = excluded.updated",
                     (json.dumps({"enabled": enabled}), datetime.now(timezone.utc).isoformat(timespec="seconds")))
        if old != enabled:
            log_audit(conn, actor, "update", None, "settings.refresh", old, enabled)


# ---- "Update my list": the numbers behind the one button ---------------------------------------------------------------
UPDATE_DEFAULTS = {"research": 25, "segments": 2, "scan": 25, "budget": 8}
UPDATE_FIELDS = {  # name -> (label, min, max)
    "research": ("Companies to research per update", 0, 100),
    "segments": ("Industry segments to search for new companies", 0, 30),
    "scan": ("Companies to scan for signals per update", 1, 100),
    "budget": ("Spending limit per update (US dollars)", 1, 100),
}


def load_update(conn: sqlite3.Connection) -> dict[str, int]:
    r = conn.execute("SELECT value_json FROM settings WHERE key = 'update'").fetchone()
    out = dict(UPDATE_DEFAULTS)
    try:
        saved = json.loads(r["value_json"]) if r else {}
    except ValueError:
        saved = {}
    for k, (_, lo, hi) in UPDATE_FIELDS.items():
        v = saved.get(k)
        if isinstance(v, int) and lo <= v <= hi:
            out[k] = v
    return out


def parse_update(form: dict[str, str]) -> tuple[dict[str, int], list[str]]:
    out, errs = {}, []
    for k, (label, lo, hi) in UPDATE_FIELDS.items():
        raw = (form.get(k) or "").strip()
        if not raw.isdigit() or not lo <= int(raw) <= hi:
            errs.append(f"{label} must be a whole number from {lo} to {hi}")
        else:
            out[k] = int(raw)
    return out, errs


def save_update(conn: sqlite3.Connection, form: dict[str, str], actor: str = "ui") -> list[str]:
    data, errs = parse_update(form)
    if errs:
        return errs
    old = load_update(conn)
    with conn:
        conn.execute("INSERT INTO settings(key, value_json, updated) VALUES ('update', ?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated = excluded.updated",
                     (json.dumps(data), datetime.now(timezone.utc).isoformat(timespec="seconds")))
        if old != data:
            log_audit(conn, actor, "update", None, "settings.update", json.dumps(old), json.dumps(data))
    return []
