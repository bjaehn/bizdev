"""Settings managed in the app. config.yaml holds the defaults; values saved here override them.

Only the discovery settings are editable (what to research and how much to add per run). Everything else in
config.yaml (scoring weights, fit-gate floors, pricing, sources) stays in code review, and secrets are never stored
here. Nothing in this module calls an LLM."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from bd.accounts import log_audit
from bd.config import Config, Discovery

INT_FIELDS = {  # name -> (label, min, max)
    "per_segment": ("Companies requested per segment", 1, 20),
    "max_candidates_per_run": ("New companies researched per run", 1, 100),
    "max_auto_add_per_run": ("Auto-adds per run", 0, 100),
    "scan_unreviewed_per_run": ("Unreviewed accounts scanned per run", 0, 100),
}
MAX_SEGMENTS, SEGMENT_MIN, SEGMENT_MAX = 30, 3, 80
# How often the free and deep looks repeat. config.yaml holds the defaults; a value saved on the Settings page overrides it for every run.
CYCLE_FIELDS = {  # name -> (label, min, max, the config section it overrides)
    "deep_every_weeks": ("Deep technology and procurement scan: every scanned company this often, in weeks (0 = only where a signal shows)", 0, 26, "scan"),
    "ats_find_every_weeks": ("Look for a company's own job board this often, in weeks (0 = never)", 0, 26, "hiring"),
    "ats_find_per_run": ("Companies whose careers page is read per run", 1, 100, "hiring"),
    "enrich_retry_every_weeks": ("Facts research: a company looked at without an error waits this long before another look, in weeks (0 = every run)", 0, 26, "enrich"),
}


def load_discovery_overrides(conn: sqlite3.Connection) -> dict:
    r = conn.execute("SELECT value_json FROM settings WHERE key = 'discovery'").fetchone()
    try:
        return json.loads(r["value_json"]) if r else {}
    except ValueError:
        return {}


def apply_overrides(cfg: Config, conn: sqlite3.Connection) -> Config:
    """cfg with any saved discovery and scan-cycle overrides applied (invalid saved values are ignored, never crash a run)."""
    ov = load_discovery_overrides(conn)
    if ov:
        try:
            cfg = cfg.model_copy(update={"discovery": Discovery.model_validate({**cfg.discovery.model_dump(), **ov})})
        except ValueError:
            pass
    cyc = load_cycle_overrides(conn)
    if cyc:
        by_section: dict[str, dict[str, int]] = {}
        for name, v in cyc.items():
            by_section.setdefault(CYCLE_FIELDS[name][3], {})[name] = v
        cfg = cfg.model_copy(update={sec: getattr(cfg, sec).model_copy(update=vals) for sec, vals in by_section.items()})
    return cfg


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
UPDATE_DEFAULTS = {"research": 25, "segments": 2, "scan": 25, "budget": 8, "rfps": 0}
UPDATE_FIELDS = {  # name -> (label, min, max)
    "research": ("Companies to research per update", 0, 100),
    "segments": ("Industry segments to search for new companies", 0, 30),
    "scan": ("Companies to scan for signals per update", 1, 100),
    "budget": ("Spending limit per update (US dollars)", 1, 100),
    "rfps": ("Search agency sites for open RFPs (1 = yes, 0 = no; about $0.30 a run in the last two Updates)", 0, 1),
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
        if k == "rfps" and k not in form:        # a form that predates this setting keeps the default
            out[k] = UPDATE_DEFAULTS[k]
            continue
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


# ---- scan cycles: how often the deep technology scan and the job-board search repeat -------------------------------------------------------------
def load_cycle_overrides(conn: sqlite3.Connection) -> dict[str, int]:
    r = conn.execute("SELECT value_json FROM settings WHERE key = 'cycles'").fetchone()
    try:
        saved = json.loads(r["value_json"]) if r else {}
    except ValueError:
        saved = {}
    return {k: v for k, v in saved.items() if k in CYCLE_FIELDS and isinstance(v, int) and not isinstance(v, bool) and CYCLE_FIELDS[k][1] <= v <= CYCLE_FIELDS[k][2]}


def cycle_values(cfg: Config, conn: sqlite3.Connection) -> dict[str, tuple[int, int, bool]]:
    """name -> (value in force, the config.yaml default, whether a saved value overrides it)."""
    ov = load_cycle_overrides(conn)
    return {k: (ov.get(k, getattr(getattr(cfg, sec), k)), getattr(getattr(cfg, sec), k), k in ov) for k, (_l, _lo, _hi, sec) in CYCLE_FIELDS.items()}


def save_cycles(conn: sqlite3.Connection, form: dict[str, str], actor: str = "ui") -> list[str]:
    errs, out = [], {}
    for k, (label, lo, hi, _sec) in CYCLE_FIELDS.items():
        raw = (form.get(k) or "").strip()
        if not raw.isdigit() or not lo <= int(raw) <= hi:
            errs.append(f"{label} must be a whole number from {lo} to {hi}")
        else:
            out[k] = int(raw)
    if errs:
        return errs
    old = load_cycle_overrides(conn)
    with conn:
        conn.execute("INSERT INTO settings(key, value_json, updated) VALUES ('cycles', ?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated = excluded.updated",
                     (json.dumps(out), datetime.now(timezone.utc).isoformat(timespec="seconds")))
        if old != out:
            log_audit(conn, actor, "update", None, "settings.cycles", json.dumps(old) if old else None, json.dumps(out))
    return []


def reset_cycles(conn: sqlite3.Connection, actor: str = "ui") -> None:
    with conn:
        old = load_cycle_overrides(conn)
        conn.execute("DELETE FROM settings WHERE key = 'cycles'")
        if old:
            log_audit(conn, actor, "update", None, "settings.cycles", json.dumps(old), None)


# ---- changes a release makes to saved settings, once, and says so --------------------------------------------------------------------------------
RELEASE_ACTOR = "release"
# (id, the Update number, the lowest value it should have). A saved number below it is raised once; a number you set higher, or lower again
# afterwards, is never touched. Each change is audited under actor "release" and shown on Admin Home and Settings.
RELEASE_CHANGES = [
    ("2026-10-20-research-25", "research", 25),      # companies researched per update: about 49 of 72 companies had an unconfirmed location or size
]


def apply_release_changes(conn: sqlite3.Connection) -> list[tuple[str, int, int]]:
    """Apply each release change not yet applied. Returns [(field, old, new)] for the ones that changed a saved value."""
    r = conn.execute("SELECT value_json FROM settings WHERE key = 'release_changes'").fetchone()
    try:
        done = set(json.loads(r["value_json"])) if r else set()
    except ValueError:
        done = set()
    changed: list[tuple[str, int, int]] = []
    for cid, key, floor in RELEASE_CHANGES:
        if cid in done:
            continue
        saved = conn.execute("SELECT value_json FROM settings WHERE key = 'update'").fetchone()
        if saved is not None:                                           # nothing saved = the default is in force, which is already the new value
            cur = load_update(conn)
            if cur[key] < floor:
                new = {**cur, key: floor}
                with conn:
                    conn.execute("UPDATE settings SET value_json = ?, updated = ? WHERE key = 'update'",
                                 (json.dumps(new), datetime.now(timezone.utc).isoformat(timespec="seconds")))
                    log_audit(conn, RELEASE_ACTOR, "update", None, "settings.update", json.dumps(cur), json.dumps(new))
                changed.append((key, cur[key], floor))
        done.add(cid)
        with conn:
            conn.execute("INSERT INTO settings(key, value_json, updated) VALUES ('release_changes', ?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated = excluded.updated",
                         (json.dumps(sorted(done)), datetime.now(timezone.utc).isoformat(timespec="seconds")))
    return changed


def recent_release_changes(conn: sqlite3.Connection, days: int = 30) -> list[dict]:
    """What releases changed in saved settings lately: [{when, label, old, new}], newest first, from the audit log."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    out = []
    for r in conn.execute("SELECT ts, old, new FROM audit_log WHERE actor = ? AND field = 'settings.update' AND ts >= ? ORDER BY id DESC", (RELEASE_ACTOR, since)):
        try:
            old, new = json.loads(r["old"] or "{}"), json.loads(r["new"] or "{}")
        except ValueError:
            continue
        for k, (label, _lo, _hi) in UPDATE_FIELDS.items():
            if old.get(k) != new.get(k):
                out.append({"when": r["ts"][:10], "label": label, "old": old.get(k), "new": new.get(k)})
    return out
