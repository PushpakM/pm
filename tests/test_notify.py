from PIL import Image

from weighbridge.config import load_config
from weighbridge.services import notify
from weighbridge.services.weighing import weigh

from .test_weighing import party


def test_phone_normalization():
    assert notify.phone("98220 12345") == "919822012345"
    assert notify.phone("+91-98220-12345") == "919822012345"
    assert notify.phone("09822012345") == "919822012345"
    assert notify.phone("123") == ""


def test_queue_and_send_with_retry(tmp_path, conn, operator):
    cfg = load_config(tmp_path / "none.toml", overrides={
        "server": {"data_dir": str(tmp_path / "data")},
        "whatsapp": {"enabled": True}, "sms": {"enabled": True}, "email": {"enabled": True}})
    # conn fixture uses its own cfg data dir; point this cfg at the same DB file
    cfg["server"]["data_dir"] = str(tmp_path / "data")
    pid = party(conn)
    conn.execute("UPDATE parties SET whatsapp='9822012345', mobile='9822012345', email='a@b.in' WHERE id=?", (pid,))
    weigh(conn, cfg, operator, vehicle_no="MH34AB1234", weight_kg=42560, party_id=pid)
    t = weigh(conn, cfg, operator, vehicle_no="MH34AB1234", weight_kg=14320)["ticket"]

    assert notify.queue_ticket(conn, cfg, t["id"]) == 3
    img = notify.image_path(cfg, t["ticket_no"])
    assert img.exists() and Image.open(img).size[0] == 900

    calls = []

    def flaky(cfg_, row):
        calls.append(row["channel"])
        if row["channel"] == "sms":
            raise RuntimeError("gateway down")

    assert notify.process_due(conn, cfg, sender=flaky) == 2
    sms = conn.execute("SELECT * FROM outbox WHERE channel='sms'").fetchone()
    assert sms["status"] == "pending" and sms["attempts"] == 1 and "gateway down" in sms["last_error"]
    assert sms["next_try_at"] > sms["created_at"]  # backed off, not retried immediately
    assert notify.process_due(conn, cfg, sender=flaky) == 0
