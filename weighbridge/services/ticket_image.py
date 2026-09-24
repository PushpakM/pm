"""Ticket as a PNG image, for WhatsApp and email."""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .weighing import FLAG_LABELS

_FONT_DIRS = [Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts", Path("/usr/share/fonts/truetype/dejavu"),
              Path("/usr/share/fonts/TTF"), Path("/Library/Fonts"), Path("/System/Library/Fonts/Supplemental")]
_REGULAR = ["segoeui.ttf", "arial.ttf", "DejaVuSans.ttf", "Arial.ttf"]
_BOLD = ["segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf", "Arial Bold.ttf"]


def _font(names: list[str], size: int):
    for folder in _FONT_DIRS:
        for name in names:
            path = folder / name
            if path.exists():
                return ImageFont.truetype(str(path), size)
    return ImageFont.load_default(size=size)


def _fmt_time(iso: str | None) -> str:
    return datetime.fromisoformat(iso).strftime("%d-%m-%Y %H:%M") if iso else "—"


def render(ticket: dict, company: dict, out_path: Path) -> Path:
    W, pad = 900, 44
    title, big, reg, small = _font(_BOLD, 34), _font(_BOLD, 30), _font(_REGULAR, 24), _font(_REGULAR, 19)
    img = Image.new("RGB", (W, 1000), "white")
    d = ImageDraw.Draw(img)
    ink, muted, accent = (23, 32, 38), (85, 98, 107), (30, 91, 140)

    y = pad
    d.rectangle([0, 0, W, 10], fill=(217, 154, 0))
    d.text((pad, y), company["name"], font=title, fill=ink); y += 46
    d.text((pad, y), f"{company.get('site_name') or ''}  {company.get('address') or ''}".strip(), font=small, fill=muted); y += 28
    if company.get("gstin"):
        d.text((pad, y), f"GSTIN {company['gstin']}", font=small, fill=muted); y += 28
    y += 10
    d.line([pad, y, W - pad, y], fill=(200, 206, 210), width=2); y += 18
    d.text((pad, y), "WEIGHMENT TICKET", font=small, fill=accent)
    d.text((W - pad, y), ticket["ticket_no"], font=big, fill=ink, anchor="ra"); y += 50

    rows = [("Vehicle", ticket["vehicle_no"]), ("Party", ticket.get("party_name") or "—"),
            ("Material", ticket.get("material_name") or "—")]
    first_label, second_label = "First weighment", "Second weighment"
    if ticket["stored_tare"]:
        second_label = "Stored tare (recorded)"
    rows += [(first_label, f"{ticket['first_kg']:,.0f} kg   {_fmt_time(ticket['first_at'])}"),
             (second_label, f"{ticket['second_kg']:,.0f} kg   {_fmt_time(ticket['second_at'])}"
              if ticket["second_kg"] is not None else "—")]
    for label, value in rows:
        d.text((pad, y), label, font=reg, fill=muted)
        d.text((pad + 290, y), str(value), font=reg, fill=ink); y += 40

    y += 8
    d.rounded_rectangle([pad, y, W - pad, y + 150], radius=10, fill=(233, 240, 246))
    cols = [("GROSS", ticket["gross_kg"]), ("TARE", ticket["tare_kg"]), ("NET", ticket["net_kg"])]
    cw = (W - 2 * pad) / 3
    for i, (label, kg) in enumerate(cols):
        cx = pad + cw * i + cw / 2
        d.text((cx, y + 22), label, font=small, fill=muted, anchor="ma")
        d.text((cx, y + 56), f"{kg:,.0f} kg" if kg is not None else "—", font=big,
               fill=accent if label == "NET" else ink, anchor="ma")
        if label == "NET" and kg is not None:
            d.text((cx, y + 102), f"{kg / 1000:,.3f} MT", font=small, fill=muted, anchor="ma")
    y += 176

    flags = [FLAG_LABELS.get(f, f) for f in (ticket.get("flags") or "").split(",") if f]
    if flags:
        d.text((pad, y), "Note: " + "; ".join(flags), font=small, fill=(179, 38, 30)); y += 32
    if ticket.get("second_user_name") or ticket.get("first_user_name"):
        d.text((pad, y), f"Operator: {ticket.get('second_user_name') or ticket.get('first_user_name')}",
               font=small, fill=muted); y += 30
    if ticket.get("record_hash"):
        d.text((pad, y), f"Verification code: {ticket['record_hash'][:16].upper()}", font=small, fill=muted); y += 30
    y += 12
    d.rectangle([0, y, W, y + 10], fill=(217, 154, 0)); y += 10

    img = img.crop((0, 0, W, y))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG", optimize=True)
    return out_path
