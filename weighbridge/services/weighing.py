"""Weighing tickets: first and second weighment, stored tare, flags and corrections."""
from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime, timedelta

from .. import audit
from ..audit import canonical
from ..db import financial_year, next_counter, transaction

PLATE_RE = re.compile(r"^[A-Z0-9]{6,11}$")

FLAG_LABELS = {
    "MANUAL": "Weight entered by hand",
    "STORED_TARE": "Stored tare used",
    "TARE_DEVIATION": "Tare differs from usual",
    "LONG_OPEN": "Long gap between weighments",
    "EDITED": "Details corrected after closing",
}


class WeighingError(Exception):
    pass


def now_iso(cfg) -> str:
    return datetime.now(cfg.tz).isoformat(timespec="seconds")


def normalize_plate(raw: str) -> str:
    plate = re.sub(r"[^A-Za-z0-9]", "", raw or "").upper()
    if not PLATE_RE.match(plate):
        raise WeighingError("Enter a valid vehicle number, e.g. MH34AB1234.")
    return plate


def _vehicle(conn: sqlite3.Connection, plate: str, cfg, user, ip: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM vehicles WHERE number = ?", (plate,)).fetchone()
    if row:
        if not row["active"]:
            raise WeighingError(f"Vehicle {plate} is blocked. Ask the supervisor.")
        return row
    conn.execute("INSERT INTO vehicles(number) VALUES (?)", (plate,))
    row = conn.execute("SELECT * FROM vehicles WHERE number = ?", (plate,)).fetchone()
    audit.record(conn, now_iso(cfg), user, "vehicle.auto_create", "vehicle", row["id"], {"number": plate}, ip)
    return row


def _check_ref(conn, table: str, ref_id) -> int | None:
    if ref_id in (None, "", 0, "0"):
        return None
    row = conn.execute(f"SELECT id, active FROM {table} WHERE id = ?", (int(ref_id),)).fetchone()
    if not row or not row["active"]:
        raise WeighingError(f"Choose a valid {table[:-1]}.")
    return row["id"]


def stored_tare_valid(vehicle: sqlite3.Row, cfg, at: datetime) -> bool:
    if vehicle["stored_tare_kg"] is None or not vehicle["tare_updated_at"]:
        return False
    age = at - datetime.fromisoformat(vehicle["tare_updated_at"])
    return age <= timedelta(days=int(cfg["weighing"]["stored_tare_valid_days"]))


def ticket_hash(t: dict) -> str:
    def num(v):
        return None if v is None else round(float(v), 3)

    fields = {k: t.get(k) for k in ("ticket_no", "vehicle_no", "first_at", "second_at")}
    fields.update({k: num(t.get(k)) for k in ("first_kg", "second_kg", "gross_kg", "tare_kg", "net_kg")})
    fields.update({k: int(t.get(k) or 0) for k in ("stored_tare", "first_manual", "second_manual")})
    return hashlib.sha256(canonical(fields).encode()).hexdigest()


def weigh(conn: sqlite3.Connection, cfg, user: dict, *, vehicle_no: str, weight_kg: float,
          party_id=None, material_id=None, use_stored_tare: bool = False, manual: bool = False,
          manual_reason: str = "", remarks: str = "", ip: str = "") -> dict:
    """Record one weighment. Opens a ticket, or closes the vehicle's open ticket.

    weight_kg must come from WeightMonitor.capture(), except for supervisor manual
    entries, which are flagged on the ticket and in the audit log.
    """
    plate = normalize_plate(vehicle_no)
    if manual:
        if user["role"] not in ("admin", "supervisor"):
            raise WeighingError("Only a supervisor can enter a weight by hand.")
        if len(manual_reason.strip()) < 5:
            raise WeighingError("Give a reason for entering the weight by hand.")
    if weight_kg <= 0:
        raise WeighingError("Weight must be above zero.")
    remarks = remarks.strip()[:500]

    with transaction(conn):
        at_iso = now_iso(cfg)
        at = datetime.fromisoformat(at_iso)
        vehicle = _vehicle(conn, plate, cfg, user, ip)
        party = _check_ref(conn, "parties", party_id)
        material = _check_ref(conn, "materials", material_id)
        open_t = conn.execute(
            "SELECT * FROM tickets WHERE vehicle_id = ? AND status = 'open'", (vehicle["id"],)
        ).fetchone()

        if open_t:
            return _close(conn, cfg, user, dict(open_t), vehicle, weight_kg, at_iso, at,
                          party, material, manual, manual_reason, remarks, ip)

        fy = financial_year(at)
        ticket_no = f"{cfg['weighing']['ticket_prefix']}/{fy}/{next_counter(conn, 'ticket-' + fy):06d}"
        flags = ["MANUAL"] if manual else []

        if use_stored_tare:
            if not cfg["weighing"]["allow_stored_tare"]:
                raise WeighingError("Stored-tare weighing is switched off.")
            if not stored_tare_valid(vehicle, cfg, at):
                raise WeighingError("This vehicle has no recent stored tare. Weigh it twice, "
                                    "or record a fresh tare first.")
            tare = float(vehicle["stored_tare_kg"])
            if weight_kg <= tare:
                raise WeighingError(f"Weight {weight_kg:.0f} kg is not above the stored tare "
                                    f"{tare:.0f} kg. Is the vehicle loaded?")
            flags.append("STORED_TARE")
            t = dict(ticket_no=ticket_no, vehicle_id=vehicle["id"], vehicle_no=plate, party_id=party,
                     material_id=material, first_kg=weight_kg, first_at=at_iso, first_user=user["id"],
                     first_manual=int(manual), second_kg=tare, second_at=vehicle["tare_updated_at"],
                     second_user=None, second_manual=0, stored_tare=1, gross_kg=weight_kg, tare_kg=tare,
                     net_kg=round(weight_kg - tare, 3), status="closed", flags=",".join(flags),
                     remarks=remarks, closed_at=at_iso)
            t["record_hash"] = ticket_hash(t)
            _insert(conn, t)
            action = "closed"
        else:
            t = dict(ticket_no=ticket_no, vehicle_id=vehicle["id"], vehicle_no=plate, party_id=party,
                     material_id=material, first_kg=weight_kg, first_at=at_iso, first_user=user["id"],
                     first_manual=int(manual), stored_tare=0, status="open", flags=",".join(flags),
                     remarks=remarks)
            _insert(conn, t)
            action = "opened"

        row = conn.execute("SELECT * FROM tickets WHERE ticket_no = ?", (ticket_no,)).fetchone()
        audit.record(conn, at_iso, user, f"ticket.{action}", "ticket", ticket_no, {
            "vehicle": plate, "weight_kg": weight_kg, "manual": manual, "reason": manual_reason,
            "stored_tare": bool(use_stored_tare), "net_kg": row["net_kg"],
        }, ip)
        return {"action": action, "ticket": dict(row)}


def _insert(conn, t: dict) -> None:
    cols = ",".join(t)
    conn.execute(f"INSERT INTO tickets({cols}) VALUES ({','.join('?' * len(t))})", tuple(t.values()))


def _close(conn, cfg, user, t: dict, vehicle, weight_kg, at_iso, at, party, material,
           manual, manual_reason, remarks, ip) -> dict:
    flags = [f for f in t["flags"].split(",") if f]
    if manual and "MANUAL" not in flags:
        flags.append("MANUAL")
    gross, tare = max(t["first_kg"], weight_kg), min(t["first_kg"], weight_kg)
    net = round(gross - tare, 3)
    if net < float(cfg["indicator"]["min_weight_kg"]) / 5:
        raise WeighingError(f"The two weights differ by only {net:.0f} kg. Check that the vehicle "
                            "was loaded or unloaded between weighments.")
    first_at = datetime.fromisoformat(t["first_at"])
    if at - first_at > timedelta(hours=float(cfg["weighing"]["open_ticket_warn_hours"])):
        flags.append("LONG_OPEN")

    usual = vehicle["stored_tare_kg"]
    tolerance = float(cfg["weighing"]["tare_tolerance_pct"])
    deviates = bool(usual) and abs(tare - usual) / usual * 100 > tolerance
    if deviates:
        flags.append("TARE_DEVIATION")
    elif not manual:
        conn.execute("UPDATE vehicles SET stored_tare_kg = ?, tare_updated_at = ? WHERE id = ?",
                     (tare, at_iso, vehicle["id"]))

    t.update(second_kg=weight_kg, second_at=at_iso, second_user=user["id"], second_manual=int(manual),
             gross_kg=gross, tare_kg=tare, net_kg=net, status="closed", flags=",".join(flags),
             closed_at=at_iso, party_id=party or t["party_id"], material_id=material or t["material_id"],
             remarks=(t["remarks"] + (" | " if t["remarks"] and remarks else "") + remarks)[:500])
    t["record_hash"] = ticket_hash(t)
    conn.execute(
        "UPDATE tickets SET second_kg=?, second_at=?, second_user=?, second_manual=?, gross_kg=?, "
        "tare_kg=?, net_kg=?, status='closed', flags=?, closed_at=?, party_id=?, material_id=?, "
        "remarks=?, record_hash=? WHERE id=? AND status='open'",
        (t["second_kg"], t["second_at"], t["second_user"], t["second_manual"], gross, tare, net,
         t["flags"], at_iso, t["party_id"], t["material_id"], t["remarks"], t["record_hash"], t["id"]),
    )
    audit.record(conn, at_iso, user, "ticket.closed", "ticket", t["ticket_no"], {
        "vehicle": t["vehicle_no"], "weight_kg": weight_kg, "gross_kg": gross, "tare_kg": tare,
        "net_kg": net, "manual": manual, "reason": manual_reason, "flags": t["flags"],
        "usual_tare_kg": usual,
    }, ip)
    row = conn.execute("SELECT * FROM tickets WHERE id = ?", (t["id"],)).fetchone()
    return {"action": "closed", "ticket": dict(row)}


def record_tare(conn, cfg, user, *, vehicle_no: str, weight_kg: float, manual: bool = False,
                manual_reason: str = "", ip: str = "") -> dict:
    """Weigh an empty vehicle to refresh its stored tare (no ticket)."""
    plate = normalize_plate(vehicle_no)
    if manual and (user["role"] not in ("admin", "supervisor") or len(manual_reason.strip()) < 5):
        raise WeighingError("Only a supervisor can enter a tare by hand, with a reason.")
    with transaction(conn):
        at_iso = now_iso(cfg)
        vehicle = _vehicle(conn, plate, cfg, user, ip)
        if conn.execute("SELECT 1 FROM tickets WHERE vehicle_id=? AND status='open'",
                        (vehicle["id"],)).fetchone():
            raise WeighingError("This vehicle has an open ticket. Complete the second weighment instead.")
        old = vehicle["stored_tare_kg"]
        conn.execute("UPDATE vehicles SET stored_tare_kg=?, tare_updated_at=? WHERE id=?",
                     (weight_kg, at_iso, vehicle["id"]))
        audit.record(conn, at_iso, user, "vehicle.tare", "vehicle", plate,
                     {"old_kg": old, "new_kg": weight_kg, "manual": manual, "reason": manual_reason}, ip)
    return {"vehicle_no": plate, "old_kg": old, "new_kg": weight_kg}


def cancel_ticket(conn, cfg, user, ticket_id: int, reason: str, ip: str = "") -> None:
    if user["role"] not in ("admin", "supervisor"):
        raise WeighingError("Only a supervisor can cancel a ticket.")
    if len(reason.strip()) < 5:
        raise WeighingError("Give a reason for cancelling.")
    with transaction(conn):
        t = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if not t or t["status"] == "cancelled":
            raise WeighingError("Ticket not found or already cancelled.")
        if t["bill_id"]:
            raise WeighingError("This ticket is on a bill. Cancel the bill first.")
        # Cancelling only changes status; weights stay as recorded.
        conn.execute("UPDATE tickets SET status='cancelled', remarks=? WHERE id=?",
                     ((t["remarks"] + " | CANCELLED: " + reason.strip())[:500], ticket_id))
        audit.record(conn, now_iso(cfg), user, "ticket.cancelled", "ticket", t["ticket_no"],
                     {"reason": reason, "was": t["status"], "net_kg": t["net_kg"]}, ip)


def correct_ticket(conn, cfg, user, ticket_id: int, *, party_id=None, material_id=None,
                   reason: str, ip: str = "") -> None:
    """Change party or material on an unbilled ticket. Weights can never be changed."""
    if user["role"] not in ("admin", "supervisor"):
        raise WeighingError("Only a supervisor can correct a ticket.")
    if len(reason.strip()) < 5:
        raise WeighingError("Give a reason for the correction.")
    with transaction(conn):
        t = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if not t or t["status"] == "cancelled":
            raise WeighingError("Ticket not found or cancelled.")
        if t["bill_id"]:
            raise WeighingError("This ticket is on a bill. Cancel the bill first.")
        party = _check_ref(conn, "parties", party_id)
        material = _check_ref(conn, "materials", material_id)
        flags = [f for f in t["flags"].split(",") if f]
        if "EDITED" not in flags:
            flags.append("EDITED")
        conn.execute("UPDATE tickets SET party_id=?, material_id=?, flags=? WHERE id=?",
                     (party, material, ",".join(flags), ticket_id))
        audit.record(conn, now_iso(cfg), user, "ticket.corrected", "ticket", t["ticket_no"], {
            "reason": reason, "party_before": t["party_id"], "party_after": party,
            "material_before": t["material_id"], "material_after": material}, ip)


TICKET_SELECT = """
SELECT t.*, p.name AS party_name, p.kind AS party_kind, m.name AS material_name,
       u1.full_name AS first_user_name, u2.full_name AS second_user_name, b.bill_no
FROM tickets t
LEFT JOIN parties p ON p.id = t.party_id
LEFT JOIN materials m ON m.id = t.material_id
LEFT JOIN users u1 ON u1.id = t.first_user
LEFT JOIN users u2 ON u2.id = t.second_user
LEFT JOIN bills b ON b.id = t.bill_id
"""


def get_ticket(conn, ticket_id: int) -> dict | None:
    row = conn.execute(TICKET_SELECT + " WHERE t.id = ?", (ticket_id,)).fetchone()
    return dict(row) if row else None


def search_tickets(conn, *, date_from: str = "", date_to: str = "", status: str = "",
                   party_id=None, material_id=None, vehicle: str = "", flagged: bool = False,
                   limit: int = 500) -> list[dict]:
    where, args = [], []
    if date_from:
        where.append("substr(coalesce(t.closed_at, t.first_at),1,10) >= ?"); args.append(date_from)
    if date_to:
        where.append("substr(coalesce(t.closed_at, t.first_at),1,10) <= ?"); args.append(date_to)
    if status:
        where.append("t.status = ?"); args.append(status)
    if party_id:
        where.append("t.party_id = ?"); args.append(int(party_id))
    if material_id:
        where.append("t.material_id = ?"); args.append(int(material_id))
    if vehicle:
        where.append("t.vehicle_no LIKE ?"); args.append("%" + re.sub(r"[^A-Za-z0-9]", "", vehicle).upper() + "%")
    if flagged:
        where.append("t.flags <> ''")
    sql = TICKET_SELECT + (" WHERE " + " AND ".join(where) if where else "")
    sql += " ORDER BY t.id DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn.execute(sql, args)]
