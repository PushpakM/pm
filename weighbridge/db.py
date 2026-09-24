"""SQLite storage. One file, WAL mode, one short-lived connection per request."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    full_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin','supervisor','operator','accounts','auditor')),
    pw_hash TEXT NOT NULL,
    must_change_pw INTEGER NOT NULL DEFAULT 1,
    totp_secret TEXT,
    totp_enabled INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    failed_logins INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    created_at TEXT NOT NULL,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    csrf TEXT NOT NULL,
    mfa_ok INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    ip TEXT
);

CREATE TABLE IF NOT EXISTS parties (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    kind TEXT NOT NULL CHECK (kind IN ('customer','contractor','supplier')),
    gstin TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    mobile TEXT NOT NULL DEFAULT '',
    whatsapp TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    rate_per_mt REAL NOT NULL DEFAULT 0,
    tally_ledger TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS materials (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    hsn TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS vehicles (
    id INTEGER PRIMARY KEY,
    number TEXT NOT NULL UNIQUE,
    transporter TEXT NOT NULL DEFAULT '',
    driver_mobile TEXT NOT NULL DEFAULT '',
    owner_whatsapp TEXT NOT NULL DEFAULT '',
    stored_tare_kg REAL,
    tare_updated_at TEXT,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS bills (
    id INTEGER PRIMARY KEY,
    bill_no TEXT NOT NULL UNIQUE,
    party_id INTEGER NOT NULL REFERENCES parties(id),
    kind TEXT NOT NULL,
    period_from TEXT NOT NULL,
    period_to TEXT NOT NULL,
    trips INTEGER NOT NULL,
    total_net_kg REAL NOT NULL,
    rate_per_mt REAL NOT NULL,
    amount REAL NOT NULL,
    gst_pct REAL NOT NULL,
    cgst REAL NOT NULL,
    sgst REAL NOT NULL,
    igst REAL NOT NULL,
    total REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','cancelled')),
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    tally_exported_at TEXT,
    cancel_reason TEXT
);

CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY,
    ticket_no TEXT NOT NULL UNIQUE,
    vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),
    vehicle_no TEXT NOT NULL,
    party_id INTEGER REFERENCES parties(id),
    material_id INTEGER REFERENCES materials(id),
    first_kg REAL NOT NULL,
    first_at TEXT NOT NULL,
    first_user INTEGER NOT NULL REFERENCES users(id),
    first_manual INTEGER NOT NULL DEFAULT 0,
    second_kg REAL,
    second_at TEXT,
    second_user INTEGER REFERENCES users(id),
    second_manual INTEGER NOT NULL DEFAULT 0,
    stored_tare INTEGER NOT NULL DEFAULT 0,
    gross_kg REAL,
    tare_kg REAL,
    net_kg REAL,
    status TEXT NOT NULL CHECK (status IN ('open','closed','cancelled')),
    flags TEXT NOT NULL DEFAULT '',
    remarks TEXT NOT NULL DEFAULT '',
    bill_id INTEGER REFERENCES bills(id),
    closed_at TEXT,
    record_hash TEXT
);
CREATE INDEX IF NOT EXISTS ix_tickets_status ON tickets(status);
CREATE INDEX IF NOT EXISTS ix_tickets_closed ON tickets(closed_at);
CREATE INDEX IF NOT EXISTS ix_tickets_vehicle ON tickets(vehicle_id);

CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    user_id INTEGER,
    username TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    entity TEXT NOT NULL DEFAULT '',
    entity_id TEXT NOT NULL DEFAULT '',
    details TEXT NOT NULL DEFAULT '{}',
    ip TEXT NOT NULL DEFAULT '',
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY,
    channel TEXT NOT NULL CHECK (channel IN ('sms','whatsapp','email')),
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    attachment TEXT NOT NULL DEFAULT '',
    params TEXT NOT NULL DEFAULT '{}',
    ticket_id INTEGER REFERENCES tickets(id),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sent','failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    next_try_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sent_at TEXT
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Audit rows and closed tickets are append-only at the database level too.
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS ticket_no_delete BEFORE DELETE ON tickets
BEGIN SELECT RAISE(ABORT, 'tickets cannot be deleted; cancel them instead'); END;
CREATE TRIGGER IF NOT EXISTS ticket_weights_frozen BEFORE UPDATE ON tickets
WHEN OLD.status = 'closed' AND (
    NEW.first_kg IS NOT OLD.first_kg OR NEW.second_kg IS NOT OLD.second_kg OR
    NEW.gross_kg IS NOT OLD.gross_kg OR NEW.tare_kg IS NOT OLD.tare_kg OR
    NEW.net_kg IS NOT OLD.net_kg OR NEW.vehicle_no IS NOT OLD.vehicle_no OR
    NEW.first_at IS NOT OLD.first_at OR NEW.second_at IS NOT OLD.second_at)
BEGIN SELECT RAISE(ABORT, 'weights of a closed ticket cannot change'); END;
"""


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=15, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


def init_db(path: Path) -> None:
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE so ticket numbers and audit hashes are assigned one writer at a time."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def next_counter(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute("SELECT value FROM counters WHERE name = ?", (name,)).fetchone()
    value = (row["value"] if row else 0) + 1
    conn.execute(
        "INSERT INTO counters(name, value) VALUES (?, ?) "
        "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
        (name, value),
    )
    return value


def financial_year(ts: datetime) -> str:
    """Indian financial year label, e.g. 2627 for Apr 2026 – Mar 2027."""
    start = ts.year if ts.month >= 4 else ts.year - 1
    return f"{start % 100:02d}{(start + 1) % 100:02d}"


def kv_get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def kv_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO kv(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
