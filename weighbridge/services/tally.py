"""Tally Prime voucher export (XML import format) and optional push to Tally's HTTP port.

Customer bills become Sales vouchers; contractor/supplier bills become Purchase vouchers.
Tally's port 9000 has no login, so only ever point this at Tally on the same PC or LAN.
"""
from __future__ import annotations

import re
from datetime import datetime
from xml.sax.saxutils import escape

import requests

from .. import audit
from ..db import transaction
from .weighing import WeighingError, now_iso


def _amt(x: float) -> str:
    return f"{x:.2f}"


def _entry(ledger: str, amount: float, debit: bool) -> str:
    # Tally convention: debit entries are negative with ISDEEMEDPOSITIVE=Yes.
    value = -abs(amount) if debit else abs(amount)
    return (
        "<ALLLEDGERENTRIES.LIST>"
        f"<LEDGERNAME>{escape(ledger)}</LEDGERNAME>"
        f"<ISDEEMEDPOSITIVE>{'Yes' if debit else 'No'}</ISDEEMEDPOSITIVE>"
        f"<AMOUNT>{_amt(value)}</AMOUNT>"
        "</ALLLEDGERENTRIES.LIST>"
    )


def voucher_xml(bill: dict, cfg) -> str:
    t = cfg["tally"]
    sales = bill["kind"] == "customer"
    vtype = "Sales" if sales else "Purchase"
    party_ledger = bill.get("tally_ledger") or bill["party_name"]
    date = datetime.fromisoformat(bill["created_at"]).strftime("%Y%m%d")
    narration = (f"{bill['trips']} trips, {bill['total_net_kg'] / 1000:.3f} MT @ Rs {bill['rate_per_mt']:.2f}/MT, "
                 f"{bill['period_from']} to {bill['period_to']}")
    if sales:
        entries = [_entry(party_ledger, bill["total"], debit=True),
                   _entry(t["sales_ledger"], bill["amount"], debit=False)]
        tax = [(t["cgst_output_ledger"], bill["cgst"]), (t["sgst_output_ledger"], bill["sgst"]),
               (t["igst_output_ledger"], bill["igst"])]
        entries += [_entry(name, value, debit=False) for name, value in tax if value]
    else:
        entries = [_entry(party_ledger, bill["total"], debit=False),
                   _entry(t["purchase_ledger"], bill["amount"], debit=True)]
        tax = [(t["cgst_input_ledger"], bill["cgst"]), (t["sgst_input_ledger"], bill["sgst"]),
               (t["igst_input_ledger"], bill["igst"])]
        entries += [_entry(name, value, debit=True) for name, value in tax if value]
    company = escape(t["company"] or cfg["company"]["name"])
    return (
        "<ENVELOPE><HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER><BODY><IMPORTDATA>"
        "<REQUESTDESC><REPORTNAME>Vouchers</REPORTNAME>"
        f"<STATICVARIABLES><SVCURRENTCOMPANY>{company}</SVCURRENTCOMPANY></STATICVARIABLES>"
        "</REQUESTDESC><REQUESTDATA><TALLYMESSAGE xmlns:UDF=\"TallyUDF\">"
        f"<VOUCHER VCHTYPE=\"{vtype}\" ACTION=\"Create\">"
        f"<DATE>{date}</DATE><VOUCHERTYPENAME>{vtype}</VOUCHERTYPENAME>"
        f"<VOUCHERNUMBER>{escape(bill['bill_no'])}</VOUCHERNUMBER>"
        f"<REFERENCE>{escape(bill['bill_no'])}</REFERENCE>"
        f"<PARTYLEDGERNAME>{escape(party_ledger)}</PARTYLEDGERNAME>"
        f"<NARRATION>{escape(narration)}</NARRATION>"
        + "".join(entries) +
        "</VOUCHER></TALLYMESSAGE></REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>"
    )


def push(bill: dict, cfg, conn, user, ip: str = "") -> str:
    """Send one bill to Tally. Returns Tally's summary; raises WeighingError on failure."""
    if not cfg["tally"]["enabled"]:
        raise WeighingError("Tally push is switched off in config.toml. Download the XML instead.")
    if bill["status"] != "active":
        raise WeighingError("Cancelled bills can't be sent to Tally.")
    if bill["tally_exported_at"]:
        raise WeighingError("This bill was already sent to Tally.")
    try:
        resp = requests.post(cfg["tally"]["url"], data=voucher_xml(bill, cfg).encode("utf-8"),
                             headers={"Content-Type": "text/xml"}, timeout=20)
    except requests.RequestException as exc:
        raise WeighingError(f"Could not reach Tally at {cfg['tally']['url']}. Is Tally open with "
                            f"the company loaded? ({exc.__class__.__name__})") from exc
    body = resp.text
    created = re.search(r"<CREATED>(\d+)</CREATED>", body)
    errors = re.search(r"<ERRORS>(\d+)</ERRORS>", body)
    line_error = re.search(r"<LINEERROR>(.*?)</LINEERROR>", body, re.S)
    if resp.status_code != 200 or not created or int(created.group(1)) < 1 or (errors and int(errors.group(1))):
        detail = line_error.group(1).strip() if line_error else body[:300]
        raise WeighingError(f"Tally rejected the voucher: {detail}")
    with transaction(conn):
        at = now_iso(cfg)
        conn.execute("UPDATE bills SET tally_exported_at = ? WHERE id = ?", (at, bill["id"]))
        audit.record(conn, at, user, "bill.tally_push", "bill", bill["bill_no"], {"response": body[:500]}, ip)
    return "Voucher created in Tally."


def mark_exported(conn, cfg, user, bill: dict, ip: str = "") -> None:
    with transaction(conn):
        at = now_iso(cfg)
        if not bill["tally_exported_at"]:
            conn.execute("UPDATE bills SET tally_exported_at = ? WHERE id = ?", (at, bill["id"]))
        audit.record(conn, at, user, "bill.tally_xml_download", "bill", bill["bill_no"], {}, ip)
