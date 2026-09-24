import sqlite3
from datetime import datetime, timedelta

import pytest

from weighbridge.services.weighing import (WeighingError, cancel_ticket, correct_ticket, normalize_plate,
                                           record_tare, ticket_hash, weigh)


def party(conn, name="Four Square Mining", kind="contractor", rate=93, gstin=""):
    conn.execute("INSERT INTO parties(name, kind, rate_per_mt, gstin) VALUES (?,?,?,?)", (name, kind, rate, gstin))
    return conn.execute("SELECT id FROM parties WHERE name=?", (name,)).fetchone()[0]


def material(conn, name="Boulder"):
    conn.execute("INSERT INTO materials(name) VALUES (?)", (name,))
    return conn.execute("SELECT id FROM materials WHERE name=?", (name,)).fetchone()[0]


def test_plate_normalization():
    assert normalize_plate("mh 34 ab-1234") == "MH34AB1234"
    with pytest.raises(WeighingError):
        normalize_plate("12")


def test_two_pass_ticket(conn, cfg, operator):
    pid, mid = party(conn), material(conn)
    first = weigh(conn, cfg, operator, vehicle_no="MH34AB1234", weight_kg=42560, party_id=pid, material_id=mid)
    assert first["action"] == "opened"
    t = first["ticket"]
    assert t["status"] == "open" and t["ticket_no"].startswith("WB/") and t["ticket_no"].endswith("000001")

    second = weigh(conn, cfg, operator, vehicle_no="MH 34 AB 1234", weight_kg=14320)
    t = second["ticket"]
    assert second["action"] == "closed"
    assert (t["gross_kg"], t["tare_kg"], t["net_kg"]) == (42560, 14320, 28240)
    assert t["party_id"] == pid and t["record_hash"] == ticket_hash(t)
    v = conn.execute("SELECT stored_tare_kg FROM vehicles WHERE number='MH34AB1234'").fetchone()
    assert v[0] == 14320  # tare learned for next time


def test_ticket_numbers_increment(conn, cfg, operator):
    a = weigh(conn, cfg, operator, vehicle_no="MH34AB0001", weight_kg=30000)["ticket"]["ticket_no"]
    b = weigh(conn, cfg, operator, vehicle_no="MH34AB0002", weight_kg=30000)["ticket"]["ticket_no"]
    assert int(b[-6:]) == int(a[-6:]) + 1


def test_stored_tare_single_pass(conn, cfg, operator):
    record_tare(conn, cfg, operator, vehicle_no="MH29C5555", weight_kg=13800)
    res = weigh(conn, cfg, operator, vehicle_no="MH29C5555", weight_kg=41000, use_stored_tare=True)
    t = res["ticket"]
    assert res["action"] == "closed" and t["stored_tare"] == 1
    assert t["net_kg"] == 27200 and "STORED_TARE" in t["flags"]


def test_stored_tare_refused_when_old(conn, cfg, operator):
    record_tare(conn, cfg, operator, vehicle_no="MH29C5555", weight_kg=13800)
    old = (datetime.now(cfg.tz) - timedelta(days=30)).isoformat(timespec="seconds")
    conn.execute("UPDATE vehicles SET tare_updated_at=?", (old,))
    with pytest.raises(WeighingError, match="no recent stored tare"):
        weigh(conn, cfg, operator, vehicle_no="MH29C5555", weight_kg=41000, use_stored_tare=True)


def test_tare_deviation_flagged_and_not_learned(conn, cfg, operator):
    record_tare(conn, cfg, operator, vehicle_no="MH40X1000", weight_kg=14000)
    weigh(conn, cfg, operator, vehicle_no="MH40X1000", weight_kg=45000)
    t = weigh(conn, cfg, operator, vehicle_no="MH40X1000", weight_kg=12000)["ticket"]  # 14% lighter tare
    assert "TARE_DEVIATION" in t["flags"]
    assert conn.execute("SELECT stored_tare_kg FROM vehicles WHERE number='MH40X1000'").fetchone()[0] == 14000


def test_net_too_small_rejected(conn, cfg, operator):
    weigh(conn, cfg, operator, vehicle_no="MH34AB1234", weight_kg=14000)
    with pytest.raises(WeighingError, match="differ by only"):
        weigh(conn, cfg, operator, vehicle_no="MH34AB1234", weight_kg=14050)


def test_manual_weight_rules(conn, cfg, operator, supervisor):
    with pytest.raises(WeighingError, match="supervisor"):
        weigh(conn, cfg, operator, vehicle_no="MH34AB1234", weight_kg=40000, manual=True, manual_reason="indicator dead")
    with pytest.raises(WeighingError, match="reason"):
        weigh(conn, cfg, supervisor, vehicle_no="MH34AB1234", weight_kg=40000, manual=True, manual_reason="")
    t = weigh(conn, cfg, supervisor, vehicle_no="MH34AB1234", weight_kg=40000, manual=True,
              manual_reason="indicator display failed")["ticket"]
    assert t["first_manual"] == 1 and "MANUAL" in t["flags"]


def test_closed_weights_are_frozen_in_database(conn, cfg, operator):
    weigh(conn, cfg, operator, vehicle_no="MH34AB1234", weight_kg=40000)
    t = weigh(conn, cfg, operator, vehicle_no="MH34AB1234", weight_kg=14000)["ticket"]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE tickets SET net_kg = 30000 WHERE id = ?", (t["id"],))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM tickets WHERE id = ?", (t["id"],))


def test_cancel_and_correct(conn, cfg, operator, supervisor):
    pid = party(conn)
    weigh(conn, cfg, operator, vehicle_no="MH34AB1234", weight_kg=40000)
    t = weigh(conn, cfg, operator, vehicle_no="MH34AB1234", weight_kg=14000)["ticket"]
    with pytest.raises(WeighingError):
        cancel_ticket(conn, cfg, operator, t["id"], "wrong truck")
    correct_ticket(conn, cfg, supervisor, t["id"], party_id=pid, reason="party missed at weighing")
    row = conn.execute("SELECT party_id, flags FROM tickets WHERE id=?", (t["id"],)).fetchone()
    assert row["party_id"] == pid and "EDITED" in row["flags"]
    cancel_ticket(conn, cfg, supervisor, t["id"], "test weighment, not a real trip")
    assert conn.execute("SELECT status FROM tickets WHERE id=?", (t["id"],)).fetchone()[0] == "cancelled"


def test_blocked_vehicle(conn, cfg, operator):
    conn.execute("INSERT INTO vehicles(number, active) VALUES ('MH34ZZ9999', 0)")
    with pytest.raises(WeighingError, match="blocked"):
        weigh(conn, cfg, operator, vehicle_no="MH34ZZ9999", weight_kg=40000)
