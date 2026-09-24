import re

import pytest

from weighbridge.services import billing, tally
from weighbridge.services.weighing import WeighingError, weigh

from .test_weighing import party


def trips(conn, cfg, user, pid, loads):
    for i, (gross, tare) in enumerate(loads):
        plate = f"MH34AB{1000 + i}"
        weigh(conn, cfg, user, vehicle_no=plate, weight_kg=gross, party_id=pid)
        weigh(conn, cfg, user, vehicle_no=plate, weight_kg=tare)


def test_gst_split():
    assert billing.gst_split(1000, 18, "27ABCDE1234F1Z5", "27") == (90.0, 90.0, 0.0)
    assert billing.gst_split(1000, 18, "29ABCDE1234F1Z5", "27") == (0.0, 0.0, 180.0)
    assert billing.gst_split(1000, 18, "", "27") == (90.0, 90.0, 0.0)


def test_contractor_bill_at_93_per_mt(conn, cfg, operator, accounts, admin):
    pid = party(conn, rate=93)
    trips(conn, cfg, operator, pid, [(42560, 14320), (40100, 13900)])
    day = conn.execute("SELECT substr(closed_at,1,10) FROM tickets LIMIT 1").fetchone()[0]
    p = billing.preview(conn, cfg, pid, day, day)
    assert p["trips"] == 2 and p["net_kg"] == 28240 + 26200
    assert p["amount"] == round(54.44 * 93, 2)
    assert p["total"] == round(p["amount"] * 1.18, 2)

    with pytest.raises(WeighingError):
        billing.create_bill(conn, cfg, operator, pid, day, day)
    bill_id = billing.create_bill(conn, cfg, accounts, pid, day, day)
    assert billing.preview(conn, cfg, pid, day, day)["trips"] == 0  # can't bill twice
    with pytest.raises(WeighingError, match="No unbilled"):
        billing.create_bill(conn, cfg, accounts, pid, day, day)

    bill = billing.get_bill(conn, bill_id)
    assert bill["bill_no"].startswith("BILL/") and len(bill["tickets"]) == 2

    billing.cancel_bill(conn, cfg, admin, bill_id, "rate was revised")
    assert billing.preview(conn, cfg, pid, day, day)["trips"] == 2


def _amounts(xml):
    return [float(a) for a in re.findall(r"<AMOUNT>(-?[\d.]+)</AMOUNT>", xml)]


@pytest.mark.parametrize("kind, vtype, gstin", [("customer", "Sales", ""), ("contractor", "Purchase", ""),
                                                ("customer", "Sales", "29ABCDE1234F1Z5")])
def test_tally_voucher_balances(conn, cfg, operator, accounts, kind, vtype, gstin):
    pid = party(conn, name="P1", kind=kind, rate=250, gstin=gstin)
    trips(conn, cfg, operator, pid, [(42560, 14320)])
    day = conn.execute("SELECT substr(closed_at,1,10) FROM tickets LIMIT 1").fetchone()[0]
    bill = billing.get_bill(conn, billing.create_bill(conn, cfg, accounts, pid, day, day))
    xml = tally.voucher_xml(bill, cfg)
    assert f'VCHTYPE="{vtype}"' in xml and bill["bill_no"] in xml
    assert abs(sum(_amounts(xml))) < 0.01  # debits equal credits
    assert ("IGST" in xml) == bool(gstin)


def test_tally_escapes_names(conn, cfg, operator, accounts):
    pid = party(conn, name="R & S <Mines>", kind="customer", rate=100)
    trips(conn, cfg, operator, pid, [(42560, 14320)])
    day = conn.execute("SELECT substr(closed_at,1,10) FROM tickets LIMIT 1").fetchone()[0]
    xml = tally.voucher_xml(billing.get_bill(conn, billing.create_bill(conn, cfg, accounts, pid, day, day)), cfg)
    assert "R &amp; S &lt;Mines&gt;" in xml and "<Mines>" not in xml
