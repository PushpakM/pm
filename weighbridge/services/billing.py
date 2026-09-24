"""Bills per party for a date range: net tonnes × ₹/MT, plus GST.

Customer → a sales bill you raise. Contractor/supplier → a work statement of what
you owe (e.g. mining at ₹93/MT). Each ticket can be on one active bill only.
"""
from __future__ import annotations

from datetime import datetime

from .. import audit
from ..db import financial_year, next_counter, transaction
from .weighing import WeighingError, now_iso


def r2(x: float) -> float:
    return round(x + 1e-9, 2)


def gst_split(amount: float, gst_pct: float, party_gstin: str, company_state: str) -> tuple[float, float, float]:
    """(cgst, sgst, igst). Inter-state when the party's GSTIN state code differs from ours."""
    tax = r2(amount * gst_pct / 100)
    party_state = (party_gstin or "")[:2]
    if party_state.isdigit() and party_state != company_state:
        return 0.0, 0.0, tax
    half = r2(tax / 2)
    return half, r2(tax - half), 0.0


def unbilled_tickets(conn, party_id: int, date_from: str, date_to: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT t.*, m.name AS material_name FROM tickets t LEFT JOIN materials m ON m.id = t.material_id "
        "WHERE t.party_id = ? AND t.status = 'closed' AND t.bill_id IS NULL "
        "AND substr(t.closed_at,1,10) BETWEEN ? AND ? ORDER BY t.closed_at",
        (party_id, date_from, date_to))]


def preview(conn, cfg, party_id: int, date_from: str, date_to: str, rate: float | None = None) -> dict:
    party = conn.execute("SELECT * FROM parties WHERE id = ?", (party_id,)).fetchone()
    if not party:
        raise WeighingError("Choose a party.")
    if date_from > date_to:
        raise WeighingError("The start date is after the end date.")
    tickets = unbilled_tickets(conn, party_id, date_from, date_to)
    rate = float(party["rate_per_mt"] if rate is None else rate)
    net_kg = sum(t["net_kg"] for t in tickets)
    amount = r2(net_kg / 1000 * rate)
    gst_pct = float(cfg["billing"]["gst_pct"])
    cgst, sgst, igst = gst_split(amount, gst_pct, party["gstin"], cfg["company"]["state_code"])
    return {"party": dict(party), "tickets": tickets, "trips": len(tickets), "net_kg": net_kg,
            "rate": rate, "amount": amount, "gst_pct": gst_pct, "cgst": cgst, "sgst": sgst,
            "igst": igst, "total": r2(amount + cgst + sgst + igst),
            "date_from": date_from, "date_to": date_to}


def create_bill(conn, cfg, user, party_id: int, date_from: str, date_to: str,
                rate: float | None = None, ip: str = "") -> int:
    if user["role"] not in ("admin", "accounts"):
        raise WeighingError("Only accounts or the owner can create bills.")
    with transaction(conn):
        p = preview(conn, cfg, party_id, date_from, date_to, rate)
        if not p["trips"]:
            raise WeighingError("No unbilled tickets for this party in these dates.")
        if p["rate"] <= 0:
            raise WeighingError("Set a rate per MT for this party first.")
        at_iso = now_iso(cfg)
        fy = financial_year(datetime.fromisoformat(at_iso))
        bill_no = f"{cfg['billing']['bill_prefix']}/{fy}/{next_counter(conn, 'bill-' + fy):04d}"
        cur = conn.execute(
            "INSERT INTO bills(bill_no, party_id, kind, period_from, period_to, trips, total_net_kg, "
            "rate_per_mt, amount, gst_pct, cgst, sgst, igst, total, created_by, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (bill_no, party_id, p["party"]["kind"], date_from, date_to, p["trips"], p["net_kg"],
             p["rate"], p["amount"], p["gst_pct"], p["cgst"], p["sgst"], p["igst"], p["total"],
             user["id"], at_iso))
        bill_id = cur.lastrowid
        ids = [t["id"] for t in p["tickets"]]
        conn.executemany("UPDATE tickets SET bill_id = ? WHERE id = ? AND bill_id IS NULL",
                         [(bill_id, i) for i in ids])
        audit.record(conn, at_iso, user, "bill.created", "bill", bill_no, {
            "party": p["party"]["name"], "trips": p["trips"], "net_kg": p["net_kg"],
            "rate": p["rate"], "total": p["total"], "tickets": ids}, ip)
    return bill_id


def cancel_bill(conn, cfg, user, bill_id: int, reason: str, ip: str = "") -> None:
    if user["role"] != "admin":
        raise WeighingError("Only the owner can cancel a bill.")
    if len(reason.strip()) < 5:
        raise WeighingError("Give a reason for cancelling the bill.")
    with transaction(conn):
        bill = conn.execute("SELECT * FROM bills WHERE id = ?", (bill_id,)).fetchone()
        if not bill or bill["status"] != "active":
            raise WeighingError("Bill not found or already cancelled.")
        conn.execute("UPDATE bills SET status='cancelled', cancel_reason=? WHERE id=?", (reason, bill_id))
        conn.execute("UPDATE tickets SET bill_id = NULL WHERE bill_id = ?", (bill_id,))
        audit.record(conn, now_iso(cfg), user, "bill.cancelled", "bill", bill["bill_no"],
                     {"reason": reason, "tally_exported_at": bill["tally_exported_at"]}, ip)


def get_bill(conn, bill_id: int) -> dict | None:
    row = conn.execute(
        "SELECT b.*, p.name AS party_name, p.gstin AS party_gstin, p.address AS party_address, "
        "p.tally_ledger, u.full_name AS created_by_name FROM bills b "
        "JOIN parties p ON p.id = b.party_id JOIN users u ON u.id = b.created_by WHERE b.id = ?",
        (bill_id,)).fetchone()
    if not row:
        return None
    bill = dict(row)
    bill["tickets"] = [dict(r) for r in conn.execute(
        "SELECT t.*, m.name AS material_name FROM tickets t LEFT JOIN materials m ON m.id = t.material_id "
        "WHERE t.bill_id = ? ORDER BY t.closed_at", (bill_id,))]
    return bill


def list_bills(conn, limit: int = 200) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT b.*, p.name AS party_name FROM bills b JOIN parties p ON p.id = b.party_id "
        "ORDER BY b.id DESC LIMIT ?", (limit,))]
