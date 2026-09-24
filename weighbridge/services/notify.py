"""Outbox for SMS, WhatsApp and email. Messages queue locally and retry until sent,
so weighing never waits on the internet."""
from __future__ import annotations

import json
import logging
import mimetypes
import re
import smtplib
import ssl
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import quote

import requests

from ..db import transaction
from . import ticket_image
from .weighing import get_ticket, now_iso

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 6


def phone(raw: str) -> str:
    """Indian mobile to international digits: 9800000000 → 919800000000."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 10:
        digits = "91" + digits
    if len(digits) == 11 and digits.startswith("0"):
        digits = "91" + digits[1:]
    return digits if 11 <= len(digits) <= 15 else ""


def image_path(cfg, ticket_no: str) -> Path:
    return cfg.images_dir / (re.sub(r"[^A-Za-z0-9]+", "-", ticket_no) + ".png")


def ticket_vars(ticket: dict, cfg) -> dict:
    return {
        "ticket_no": ticket["ticket_no"], "vehicle": ticket["vehicle_no"],
        "net_t": f"{(ticket['net_kg'] or 0) / 1000:.3f}", "net_kg": f"{ticket['net_kg'] or 0:.0f}",
        "gross_kg": f"{ticket['gross_kg'] or 0:.0f}", "tare_kg": f"{ticket['tare_kg'] or 0:.0f}",
        "date": datetime.fromisoformat(ticket["closed_at"]).strftime("%d-%m-%Y %H:%M"),
        "party": ticket.get("party_name") or "", "material": ticket.get("material_name") or "",
        "company": cfg["company"]["name"],
    }


def _enqueue(conn, cfg, channel, recipient, *, subject="", body="", attachment="", params=None, ticket_id=None):
    at = now_iso(cfg)
    conn.execute(
        "INSERT INTO outbox(channel, recipient, subject, body, attachment, params, ticket_id, next_try_at, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (channel, recipient, subject, body, attachment, json.dumps(params or {}), ticket_id, at, at))


def queue_ticket(conn, cfg, ticket_id: int) -> int:
    """Render the ticket image and queue messages to the party and vehicle owner. Returns count queued."""
    t = get_ticket(conn, ticket_id)
    if not t or t["status"] != "closed":
        return 0
    img = ticket_image.render(t, cfg["company"], image_path(cfg, t["ticket_no"]))
    v = ticket_vars(t, cfg)
    party = conn.execute("SELECT * FROM parties WHERE id = ?", (t["party_id"],)).fetchone() if t["party_id"] else None
    vehicle = conn.execute("SELECT * FROM vehicles WHERE id = ?", (t["vehicle_id"],)).fetchone()
    caption = (f"{v['company']}\nTicket {v['ticket_no']}\nVehicle {v['vehicle']}\n"
               f"Gross {v['gross_kg']} kg, Tare {v['tare_kg']} kg\nNet {v['net_kg']} kg ({v['net_t']} MT)\n{v['date']}")
    count = 0
    with transaction(conn):
        if cfg["whatsapp"]["enabled"]:
            numbers = {phone(n) for n in ((party["whatsapp"] if party else ""), vehicle["owner_whatsapp"])} - {""}
            for number in sorted(numbers):
                _enqueue(conn, cfg, "whatsapp", number, body=caption, attachment=str(img), params=v, ticket_id=t["id"])
                count += 1
        if cfg["sms"]["enabled"]:
            numbers = {phone(n) for n in ((party["mobile"] if party else ""), vehicle["driver_mobile"])} - {""}
            for number in sorted(numbers):
                _enqueue(conn, cfg, "sms", number, body=cfg["sms"]["template"].format(**v), params=v, ticket_id=t["id"])
                count += 1
        if cfg["email"]["enabled"] and party and party["email"]:
            _enqueue(conn, cfg, "email", party["email"], subject=f"Weighment ticket {v['ticket_no']} – {v['vehicle']}",
                     body=caption + "\n\nThe ticket image is attached.", attachment=str(img), params=v, ticket_id=t["id"])
            count += 1
    return count


def queue_daily_summary(conn, cfg, day: str, totals: dict, rows: list[dict], chain_head: str) -> bool:
    to = cfg["notify"]["owner_email"]
    if not (cfg["email"]["enabled"] and to):
        return False
    lines = [f"Weighbridge summary for {day} – {cfg['company']['site_name']}", "",
             f"Trips: {totals['trips']}", f"Net: {totals['net_kg'] / 1000:,.3f} MT",
             f"Flagged tickets: {totals['flagged']}", f"Still open: {totals['open']}", "", "By party:"]
    lines += [f"  {r['name']}: {r['trips']} trips, {(r['net_kg'] or 0) / 1000:,.3f} MT" for r in rows] or ["  none"]
    lines += ["", f"Audit chain head: {chain_head}",
              "Keep this email. If the audit chain on the system ever stops matching this code, "
              "records were changed after this point."]
    with transaction(conn):
        _enqueue(conn, cfg, "email", to, subject=f"Weighbridge summary {day}", body="\n".join(lines))
    return True


# --- senders -------------------------------------------------------------------

def send_email(cfg, to: str, subject: str, body: str, attachment: str = "") -> None:
    e = cfg["email"]
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = e["from_addr"] or e["username"], to, subject
    msg.set_content(body)
    if attachment:
        path = Path(attachment)
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        msg.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name)
    context = ssl.create_default_context()
    if e["use_ssl"]:
        with smtplib.SMTP_SSL(e["host"], int(e["port"]), context=context, timeout=30) as s:
            s.login(e["username"], e["password"])
            s.send_message(msg)
    else:
        with smtplib.SMTP(e["host"], int(e["port"]), timeout=30) as s:
            if e["starttls"]:
                s.starttls(context=context)
            if e["username"]:
                s.login(e["username"], e["password"])
            s.send_message(msg)


def send_sms(cfg, to: str, message: str) -> None:
    s = cfg["sms"]
    values = {"to": to, "message": message, "api_key": s["api_key"], "sender": s["sender"],
              "template_id": s["template_id"]}
    if s["method"].upper() == "POST":
        url = s["url"].split("?")[0]
        resp = requests.post(url, data=values, timeout=20)
    else:
        url = s["url"].format(**{k: quote(str(v), safe="") for k, v in values.items()})
        resp = requests.get(url, timeout=20)
    if resp.status_code >= 300:
        raise RuntimeError(f"SMS gateway HTTP {resp.status_code}: {resp.text[:200]}")


def send_whatsapp(cfg, to: str, caption: str, image: str, params: dict) -> None:
    w = cfg["whatsapp"]
    base = f"https://graph.facebook.com/{w['api_version']}/{w['phone_number_id']}"
    headers = {"Authorization": f"Bearer {w['access_token']}"}
    media_id = None
    if image:
        with open(image, "rb") as fh:
            up = requests.post(f"{base}/media", headers=headers, timeout=30,
                               data={"messaging_product": "whatsapp", "type": "image/png"},
                               files={"file": (Path(image).name, fh, "image/png")})
        if up.status_code >= 300:
            raise RuntimeError(f"WhatsApp media upload HTTP {up.status_code}: {up.text[:200]}")
        media_id = up.json()["id"]
    if w["mode"] == "template":
        components = []
        if media_id:
            components.append({"type": "header", "parameters": [{"type": "image", "image": {"id": media_id}}]})
        components.append({"type": "body", "parameters": [
            {"type": "text", "text": str(params.get(k, ""))} for k in ("ticket_no", "vehicle", "net_t")]})
        payload = {"messaging_product": "whatsapp", "to": to, "type": "template",
                   "template": {"name": w["template_name"], "language": {"code": w["template_language"]},
                                "components": components}}
    elif media_id:
        payload = {"messaging_product": "whatsapp", "to": to, "type": "image",
                   "image": {"id": media_id, "caption": caption[:1000]}}
    else:
        payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": caption[:4000]}}
    resp = requests.post(f"{base}/messages", headers=headers, json=payload, timeout=30)
    if resp.status_code >= 300:
        raise RuntimeError(f"WhatsApp HTTP {resp.status_code}: {resp.text[:300]}")


def _send(cfg, row) -> None:
    params = json.loads(row["params"] or "{}")
    if row["channel"] == "email":
        send_email(cfg, row["recipient"], row["subject"], row["body"], row["attachment"])
    elif row["channel"] == "sms":
        send_sms(cfg, row["recipient"], row["body"])
    else:
        send_whatsapp(cfg, row["recipient"], row["body"], row["attachment"], params)


def process_due(conn, cfg, sender=_send) -> int:
    """Send everything that is due. Returns the number sent."""
    now = now_iso(cfg)
    due = conn.execute("SELECT * FROM outbox WHERE status='pending' AND next_try_at <= ? ORDER BY id LIMIT 20",
                       (now,)).fetchall()
    sent = 0
    for row in due:
        try:
            sender(cfg, row)
        except Exception as exc:
            attempts = row["attempts"] + 1
            status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
            retry = (datetime.fromisoformat(now) + timedelta(minutes=2 ** attempts)).isoformat(timespec="seconds")
            conn.execute("UPDATE outbox SET attempts=?, status=?, last_error=?, next_try_at=? WHERE id=?",
                         (attempts, status, str(exc)[:500], retry, row["id"]))
            log.warning("outbox %s to %s failed: %s", row["channel"], row["recipient"], exc)
        else:
            conn.execute("UPDATE outbox SET status='sent', attempts=attempts+1, sent_at=?, last_error='' WHERE id=?",
                         (now, row["id"]))
            sent += 1
    return sent


def retry(conn, cfg, outbox_id: int) -> None:
    conn.execute("UPDATE outbox SET status='pending', attempts=0, next_try_at=? WHERE id=? AND status='failed'",
                 (now_iso(cfg), outbox_id))
