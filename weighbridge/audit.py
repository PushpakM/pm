"""Append-only, hash-chained audit log.

Each row stores sha256(prev_hash + canonical row). Changing or deleting any past row
breaks every hash after it, which verify_chain() reports.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

GENESIS = "0" * 64


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _row_hash(prev_hash: str, at: str, user_id, username: str, action: str,
              entity: str, entity_id: str, details: str, ip: str) -> str:
    payload = canonical([prev_hash, at, user_id, username, action, entity, entity_id, details, ip])
    return hashlib.sha256(payload.encode()).hexdigest()


def record(conn: sqlite3.Connection, at: str, user: dict | None, action: str,
           entity: str = "", entity_id: Any = "", details: dict | None = None, ip: str = "") -> str:
    """Append one audit row. Call inside the same transaction as the change it describes."""
    row = conn.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    prev = row["hash"] if row else GENESIS
    user_id = user["id"] if user else None
    username = user["username"] if user else "system"
    details_json = canonical(details or {})
    digest = _row_hash(prev, at, user_id, username, action, entity, str(entity_id), details_json, ip)
    conn.execute(
        "INSERT INTO audit_log(at, user_id, username, action, entity, entity_id, details, ip, prev_hash, hash) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (at, user_id, username, action, entity, str(entity_id), details_json, ip, prev, digest),
    )
    return digest


def verify_chain(conn: sqlite3.Connection) -> tuple[bool, int, int | None]:
    """Return (ok, rows_checked, first_bad_id)."""
    prev = GENESIS
    count = 0
    for row in conn.execute("SELECT * FROM audit_log ORDER BY id"):
        count += 1
        expected = _row_hash(prev, row["at"], row["user_id"], row["username"], row["action"],
                             row["entity"], row["entity_id"], row["details"], row["ip"])
        if row["prev_hash"] != prev or row["hash"] != expected:
            return False, count, row["id"]
        prev = row["hash"]
    return True, count, None


def chain_head(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    return row["hash"] if row else GENESIS
