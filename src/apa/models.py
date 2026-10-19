"""Data models: SQLite 8-table DDL (audit_log / spans / golden_cases / rules /
channel_profiles / incidents / probe_log / submissions).

The DDL is authoritatively defined in the product-design doc; this module
transcribes it table by table and adds a migration record, defining no schema
outside the design document.

Migration record:
  v1.0 (initial design-doc version)  7 tables: audit_log/spans/golden_cases/rules/
                                     channel_profiles/incidents/submissions
  D4 (2026-10-18)                    probe_log added (quantitative landing spot for the
                                     F10 three pollers; incidents rows are produced by
                                     the D5 state machine - D4 only writes probe_log)

This module is pure DDL + connection helpers, with no business logic; probe
writes live in apa.f10.store.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = "1.1-d4"   # synced with the design-doc migration record

# Table-by-table transcription of the design-doc schema (column order matches the doc; id autoincrement primary key)
_DDL = [
    """CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        event_type TEXT NOT NULL,
        entity_id TEXT,
        payload_json TEXT,
        prompt_version TEXT,
        model_version TEXT,
        reviewer TEXT,
        decision TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS spans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trace_id TEXT NOT NULL,
        span_name TEXT NOT NULL,
        parent_id TEXT,
        start_ts TEXT,
        end_ts TEXT,
        attributes_json TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS golden_cases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        incident_id TEXT NOT NULL,
        artifact_kind TEXT NOT NULL,
        before_ref TEXT,
        after_ref TEXT,
        expected_hits_json TEXT,
        notes TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        source_incident TEXT,
        severity TEXT,
        kind TEXT,
        impl_ref TEXT,
        version TEXT,
        enabled INTEGER DEFAULT 1
    )""",
    """CREATE TABLE IF NOT EXISTS channel_profiles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel TEXT NOT NULL,
        url_pattern TEXT,
        ats_backend TEXT,
        schema_json TEXT,
        limit_discovery TEXT,
        attachments TEXT,
        notes TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS incidents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL,
        started_at TEXT,
        resolved_at TEXT,
        affected_json TEXT,
        actions_json TEXT,
        report_ref TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS probe_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        kind TEXT NOT NULL,          -- statuspage / job_url / maintenance
        target TEXT NOT NULL,        -- endpoint or job URL
        outcome TEXT NOT NULL,       -- ok/degraded/outage/unreachable/blocked/gone/parse_error
        detail TEXT,
        latency_ms INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS submissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        jd_id TEXT NOT NULL,
        channel TEXT,
        submitted_at TEXT,
        package_ref TEXT,
        outcome TEXT,
        outcome_ts TEXT,
        notes TEXT
    )""",
]

_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_probe_log_ts ON probe_log(ts)",
    "CREATE INDEX IF NOT EXISTS idx_probe_log_kind_target ON probe_log(kind, target)",
    "CREATE INDEX IF NOT EXISTS idx_spans_trace ON spans(trace_id)",
]


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) the runtime SQLite and ensure the schema is ready."""
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Idempotent table creation (IF NOT EXISTS); reused by store and tests."""
    with conn:
        for ddl in _DDL:
            conn.execute(ddl)
        for idx in _INDEXES:
            conn.execute(idx)
