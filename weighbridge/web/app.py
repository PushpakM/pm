"""FastAPI web app. Runs on the weighbridge PC; operators use it in a browser."""
from __future__ import annotations

import base64
import io
import logging
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, unquote

import qrcode
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import audit, security
from ..db import connect, init_db, transaction
from ..indicator import CaptureError, WeightMonitor, start_reader
from ..security import ROLE_LABELS, ROLES
from ..services import backup, billing, notify, reports, tally
from ..services.weighing import (FLAG_LABELS, WeighingError, cancel_ticket, correct_ticket, get_ticket,
                                 normalize_plate, now_iso, record_tare, search_tickets, stored_tare_valid, weigh)
from ..worker import Worker

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
SESSION_COOKIE = "wb_session"
LOGIN_CSRF_COOKIE = "wb_login"
FLASH_COOKIE = "wb_flash"

WEIGH_ROLES = ("admin", "supervisor", "operator")
SUPERVISE_ROLES = ("admin", "supervisor")
BILL_ROLES = ("admin", "accounts")
BILL_VIEW_ROLES = ("admin", "accounts", "auditor")


# --- errors ----------------------------------------------------------------------

class LoginRequired(Exception):
    pass


class Redirect(Exception):
    def __init__(self, url: str):
        self.url = url


class Forbidden(Exception):
    pass


# --- formatting helpers -------------------------------------------------------------

def inr(value, decimals: int = 2) -> str:
    """Indian digit grouping: 1234567.5 → 12,34,567.50"""
    if value is None:
        return "—"
    neg = value < 0
    whole, frac = f"{abs(value):.{decimals}f}".split(".") if decimals else (f"{abs(value):.0f}", "")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return ("-" if neg else "") + whole + (f".{frac}" if decimals else "")


def kg(value) -> str:
    return "—" if value is None else inr(value, 0)


def mt(value) -> str:
    return "—" if value is None else inr(value / 1000, 3)


def dt(value) -> str:
    if not value:
        return "—"
    return datetime.fromisoformat(value).strftime("%d-%m-%Y %H:%M")


def create_app(cfg, monitor: WeightMonitor | None = None, start_background: bool = True) -> FastAPI:
    init_db(cfg.db_path)
    monitor = monitor or WeightMonitor(cfg)
    app = FastAPI(title="Weighbridge", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.cfg = cfg
    app.state.monitor = monitor
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    env = templates.env
    env.filters.update(inr=inr, kg=kg, mt=mt, dt=dt)
    env.globals.update(company=cfg["company"], ROLE_LABELS=ROLE_LABELS, FLAG_LABELS=FLAG_LABELS)

    if start_background:
        @app.on_event("startup")
        def _start() -> None:
            app.state.reader = start_reader(cfg, monitor)
            app.state.worker = Worker(cfg)
            app.state.worker.start()

    # --- plumbing ------------------------------------------------------------------

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "frame-ancestors 'none'; form-action 'self'; base-uri 'none'; object-src 'none'")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if not request.url.path.startswith("/static"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "Please log in again."}, status_code=401)
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(Redirect)
    async def _redirect(request: Request, exc: Redirect):
        return RedirectResponse(exc.url, status_code=303)

    @app.exception_handler(Forbidden)
    async def _forbidden(request: Request, exc: Forbidden):
        return HTMLResponse("<h1>Not allowed</h1><p>Your role can't open this page.</p><p><a href='/'>Back</a></p>",
                            status_code=403)

    def db():
        return connect(cfg.db_path)

    def ip_of(request: Request) -> str:
        return request.client.host if request.client else ""

    def set_flash(resp: Response, text: str, kind: str = "ok") -> Response:
        resp.set_cookie(FLASH_COOKIE, quote(f"{kind}|{text}"), max_age=60, httponly=True, samesite="strict",
                        secure=bool(cfg["security"]["cookie_secure"]))
        return resp

    def go(url: str, text: str = "", kind: str = "ok") -> Response:
        resp = RedirectResponse(url, status_code=303)
        return set_flash(resp, text, kind) if text else resp

    def session_user(request: Request, conn, allow_pending: bool = False):
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            raise LoginRequired()
        th = security.token_hash(token)
        row = conn.execute(
            "SELECT s.*, u.username, u.full_name, u.role, u.active, u.must_change_pw, u.totp_enabled "
            "FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?", (th,)).fetchone()
        if not row or not row["active"]:
            raise LoginRequired()
        now = datetime.now(cfg.tz)
        sec = cfg["security"]
        too_old = now - datetime.fromisoformat(row["created_at"]) > timedelta(hours=float(sec["session_max_hours"]))
        idle = now - datetime.fromisoformat(row["last_seen"]) > timedelta(minutes=float(sec["session_idle_minutes"]))
        if too_old or (idle and row["role"] != "operator"):
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (th,))
            raise LoginRequired()
        conn.execute("UPDATE sessions SET last_seen = ? WHERE token_hash = ?", (now_iso(cfg), th))
        user = {"id": row["user_id"], "username": row["username"], "full_name": row["full_name"],
                "role": row["role"], "csrf": row["csrf"], "token_hash": th, "mfa_ok": bool(row["mfa_ok"]),
                "must_change_pw": bool(row["must_change_pw"]), "totp_enabled": bool(row["totp_enabled"])}
        if not allow_pending:
            if row["totp_enabled"] and not row["mfa_ok"]:
                raise Redirect("/mfa")
            if row["role"] in sec["mfa_required_roles"] and not row["totp_enabled"]:
                raise Redirect("/account/mfa")
            if row["must_change_pw"]:
                raise Redirect("/account/password")
        return user

    def require(request: Request, conn, roles=None, allow_pending=False):
        user = session_user(request, conn, allow_pending)
        if roles and user["role"] not in roles:
            raise Forbidden()
        return user

    async def form_checked(request: Request, user) -> dict:
        form = dict(await request.form())
        sent = form.get("csrf") or request.headers.get("X-CSRF-Token", "")
        if not secrets.compare_digest(str(sent), user["csrf"]):
            raise Forbidden()
        return form

    def render(request: Request, name: str, user=None, **ctx) -> HTMLResponse:
        flash = None
        raw = request.cookies.get(FLASH_COOKIE)
        if raw:
            kind, _, text = unquote(raw).partition("|")
            flash = {"kind": kind, "text": text}
        resp = templates.TemplateResponse(request, name, {"user": user, "flash": flash, "cfg": cfg, **ctx})
        if raw:
            resp.delete_cookie(FLASH_COOKIE)
        return resp

    def options(conn) -> dict:
        return {
            "parties": [dict(r) for r in conn.execute("SELECT * FROM parties WHERE active=1 ORDER BY name")],
            "materials": [dict(r) for r in conn.execute("SELECT * FROM materials WHERE active=1 ORDER BY name")],
        }

    # --- login ---------------------------------------------------------------------

    @app.get("/login")
    def login_page(request: Request):
        token = secrets.token_urlsafe(24)
        resp = render(request, "login.html", login_csrf=token)
        resp.set_cookie(LOGIN_CSRF_COOKIE, token, httponly=True, samesite="strict", max_age=900,
                        secure=bool(cfg["security"]["cookie_secure"]))
        return resp

    @app.post("/login")
    async def login(request: Request):
        form = await request.form()
        if not secrets.compare_digest(str(form.get("csrf", "")), request.cookies.get(LOGIN_CSRF_COOKIE, "-")):
            return go("/login", "The login page expired. Try again.", "err")
        username = str(form.get("username", "")).strip()
        password = str(form.get("password", ""))
        sec = cfg["security"]
        conn = db()
        try:
            with transaction(conn):
                at = now_iso(cfg)
                u = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
                locked = u and u["locked_until"] and datetime.fromisoformat(u["locked_until"]) > datetime.now(cfg.tz)
                ok = bool(u) and u["active"] and not locked and security.verify_password(password, u["pw_hash"])
                if not ok:
                    if u and not locked:
                        fails = u["failed_logins"] + 1
                        lock = None
                        if fails >= int(sec["max_failed_logins"]):
                            lock = (datetime.now(cfg.tz) + timedelta(minutes=float(sec["lockout_minutes"]))).isoformat(timespec="seconds")
                            fails = 0
                        conn.execute("UPDATE users SET failed_logins=?, locked_until=? WHERE id=?", (fails, lock, u["id"]))
                    audit.record(conn, at, None, "login.failed", "user", username[:50],
                                 {"locked": bool(locked)}, ip_of(request))
                    msg = ("This account is locked for a few minutes after too many wrong passwords."
                           if locked else "Wrong username or password.")
                    return go("/login", msg, "err")
                token = security.new_token()
                conn.execute("UPDATE users SET failed_logins=0, locked_until=NULL, last_login_at=? WHERE id=?", (at, u["id"]))
                conn.execute("INSERT INTO sessions(token_hash, user_id, csrf, mfa_ok, created_at, last_seen, ip) "
                             "VALUES (?,?,?,?,?,?,?)", (security.token_hash(token), u["id"], secrets.token_urlsafe(24),
                                                        0, at, at, ip_of(request)))
                audit.record(conn, at, dict(u), "login.password_ok", "user", u["username"], {}, ip_of(request))
        finally:
            conn.close()
        resp = RedirectResponse("/mfa" if u["totp_enabled"] else "/", status_code=303)
        resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="strict",
                        secure=bool(cfg["security"]["cookie_secure"]))
        resp.delete_cookie(LOGIN_CSRF_COOKIE)
        return resp

    @app.get("/mfa")
    def mfa_page(request: Request):
        conn = db()
        try:
            user = require(request, conn, allow_pending=True)
        finally:
            conn.close()
        return render(request, "mfa.html", user)

    @app.post("/mfa")
    async def mfa_check(request: Request):
        conn = db()
        try:
            user = require(request, conn, allow_pending=True)
            form = await form_checked(request, user)
            secret = conn.execute("SELECT totp_secret FROM users WHERE id=?", (user["id"],)).fetchone()[0]
            with transaction(conn):
                if secret and security.verify_totp(secret, str(form.get("code", ""))):
                    conn.execute("UPDATE sessions SET mfa_ok=1 WHERE token_hash=?", (user["token_hash"],))
                    audit.record(conn, now_iso(cfg), user, "login.mfa_ok", "user", user["username"], {}, ip_of(request))
                    return go("/")
                audit.record(conn, now_iso(cfg), user, "login.mfa_failed", "user", user["username"], {}, ip_of(request))
        finally:
            conn.close()
        return go("/mfa", "That code didn't match. Use the current 6-digit code from your authenticator app.", "err")

    @app.post("/logout")
    async def logout(request: Request):
        conn = db()
        try:
            user = require(request, conn, allow_pending=True)
            await form_checked(request, user)
            with transaction(conn):
                conn.execute("DELETE FROM sessions WHERE token_hash=?", (user["token_hash"],))
                audit.record(conn, now_iso(cfg), user, "logout", "user", user["username"], {}, ip_of(request))
        finally:
            conn.close()
        resp = go("/login", "You're logged out.")
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    # --- account ---------------------------------------------------------------------

    @app.get("/account/password")
    def password_page(request: Request):
        conn = db()
        try:
            user = require(request, conn, allow_pending=True)
        finally:
            conn.close()
        return render(request, "password.html", user, min_len=cfg["security"]["password_min_length"])

    @app.post("/account/password")
    async def password_change(request: Request):
        conn = db()
        try:
            user = require(request, conn, allow_pending=True)
            form = await form_checked(request, user)
            row = conn.execute("SELECT pw_hash FROM users WHERE id=?", (user["id"],)).fetchone()
            if not security.verify_password(str(form.get("current", "")), row["pw_hash"]):
                return go("/account/password", "Your current password is wrong.", "err")
            new = str(form.get("new", ""))
            if new != str(form.get("confirm", "")):
                return go("/account/password", "The two new passwords don't match.", "err")
            problems = security.password_problems(new, int(cfg["security"]["password_min_length"]), user["username"])
            if problems:
                return go("/account/password", " ".join(problems), "err")
            with transaction(conn):
                conn.execute("UPDATE users SET pw_hash=?, must_change_pw=0 WHERE id=?",
                             (security.hash_password(new), user["id"]))
                conn.execute("DELETE FROM sessions WHERE user_id=? AND token_hash<>?", (user["id"], user["token_hash"]))
                audit.record(conn, now_iso(cfg), user, "user.password_changed", "user", user["username"], {}, ip_of(request))
        finally:
            conn.close()
        return go("/", "Password changed.")

    @app.get("/account/mfa")
    def mfa_setup_page(request: Request):
        conn = db()
        try:
            user = require(request, conn, allow_pending=True)
            if user["totp_enabled"]:
                return render(request, "mfa_setup.html", user, enabled=True)
            secret = security.new_totp_secret()
            with transaction(conn):
                conn.execute("UPDATE users SET totp_secret=? WHERE id=? AND totp_enabled=0", (secret, user["id"]))
        finally:
            conn.close()
        uri = security.totp_uri(secret, user["username"], cfg["company"]["name"])
        buf = io.BytesIO()
        qrcode.make(uri, box_size=6, border=2).save(buf, format="PNG")
        qr = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
        return render(request, "mfa_setup.html", user, enabled=False, secret=secret, qr=qr)

    @app.post("/account/mfa")
    async def mfa_setup(request: Request):
        conn = db()
        try:
            user = require(request, conn, allow_pending=True)
            form = await form_checked(request, user)
            secret = conn.execute("SELECT totp_secret FROM users WHERE id=?", (user["id"],)).fetchone()[0]
            if not secret or not security.verify_totp(secret, str(form.get("code", ""))):
                return go("/account/mfa", "That code didn't match. Scan the new QR code and try again.", "err")
            with transaction(conn):
                conn.execute("UPDATE users SET totp_enabled=1 WHERE id=?", (user["id"],))
                conn.execute("UPDATE sessions SET mfa_ok=1 WHERE token_hash=?", (user["token_hash"],))
                audit.record(conn, now_iso(cfg), user, "user.mfa_enabled", "user", user["username"], {}, ip_of(request))
        finally:
            conn.close()
        return go("/", "Two-step login is on. You'll need your authenticator app each time you log in.")

    # --- weighing ----------------------------------------------------------------------

    @app.get("/")
    def home(request: Request):
        conn = db()
        try:
            user = require(request, conn)
        finally:
            conn.close()
        if user["role"] in WEIGH_ROLES:
            return RedirectResponse("/weigh", status_code=303)
        if user["role"] == "accounts":
            return RedirectResponse("/billing", status_code=303)
        return RedirectResponse("/reports", status_code=303)

    @app.get("/weigh")
    def weigh_page(request: Request):
        conn = db()
        try:
            user = require(request, conn, WEIGH_ROLES)
            today = datetime.now(cfg.tz).date().isoformat()
            ctx = options(conn)
            ctx.update(
                open_tickets=search_tickets(conn, status="open", limit=50),
                recent=search_tickets(conn, status="closed", limit=12),
                totals=reports.day_totals(conn, today),
                vehicles=[r[0] for r in conn.execute("SELECT number FROM vehicles WHERE active=1 ORDER BY number")],
            )
        finally:
            conn.close()
        return render(request, "weigh.html", user, **ctx)

    @app.get("/api/weight")
    def api_weight(request: Request):
        conn = db()
        try:
            require(request, conn)
        finally:
            conn.close()
        return monitor.snapshot()

    @app.get("/api/vehicle")
    def api_vehicle(request: Request, number: str = ""):
        conn = db()
        try:
            require(request, conn, WEIGH_ROLES)
            try:
                plate = normalize_plate(number)
            except WeighingError as exc:
                return {"valid": False, "message": str(exc)}
            v = conn.execute("SELECT * FROM vehicles WHERE number=?", (plate,)).fetchone()
            if not v:
                return {"valid": True, "number": plate, "known": False}
            open_t = conn.execute("SELECT * FROM tickets WHERE vehicle_id=? AND status='open'", (v["id"],)).fetchone()
            last = conn.execute("SELECT party_id, material_id FROM tickets WHERE vehicle_id=? AND status='closed' "
                                "ORDER BY id DESC LIMIT 1", (v["id"],)).fetchone()
            now = datetime.fromisoformat(now_iso(cfg))
            return {
                "valid": True, "number": plate, "known": True, "blocked": not v["active"],
                "transporter": v["transporter"],
                "open_ticket": dict(open_t) if open_t else None,
                "stored_tare_kg": v["stored_tare_kg"], "tare_updated_at": v["tare_updated_at"],
                "stored_tare_valid": bool(cfg["weighing"]["allow_stored_tare"]) and stored_tare_valid(v, cfg, now),
                "last_party_id": last["party_id"] if last else None,
                "last_material_id": last["material_id"] if last else None,
            }
        finally:
            conn.close()

    @app.post("/weigh")
    async def weigh_submit(request: Request):
        conn = db()
        try:
            user = require(request, conn, WEIGH_ROLES)
            form = await form_checked(request, user)
            manual = form.get("manual") == "1"
            try:
                if manual:
                    weight = float(str(form.get("manual_kg", "0")).replace(",", "") or 0)
                else:
                    weight = monitor.capture()
                result = weigh(conn, cfg, user, vehicle_no=str(form.get("vehicle_no", "")), weight_kg=weight,
                               party_id=form.get("party_id"), material_id=form.get("material_id"),
                               use_stored_tare=form.get("mode") == "stored", manual=manual,
                               manual_reason=str(form.get("manual_reason", "")), remarks=str(form.get("remarks", "")),
                               ip=ip_of(request))
            except (CaptureError, WeighingError, ValueError) as exc:
                return go("/weigh", str(exc), "err")
            t = result["ticket"]
            if result["action"] == "closed" and cfg["notify"]["send_on_ticket_close"]:
                try:
                    notify.queue_ticket(conn, cfg, t["id"])
                except Exception:
                    log.exception("could not queue ticket messages")
        finally:
            conn.close()
        if result["action"] == "opened":
            return go("/weigh", f"First weighment saved: {t['vehicle_no']} {kg(t['first_kg'])} kg, "
                                f"ticket {t['ticket_no']}. Weigh again after loading or unloading.")
        return go(f"/tickets/{t['id']}", f"Ticket {t['ticket_no']} complete. Net {kg(t['net_kg'])} kg.")

    @app.post("/tare")
    async def tare_submit(request: Request):
        conn = db()
        try:
            user = require(request, conn, WEIGH_ROLES)
            form = await form_checked(request, user)
            manual = form.get("manual") == "1"
            try:
                weight = float(str(form.get("manual_kg", "0")).replace(",", "") or 0) if manual else monitor.capture()
                res = record_tare(conn, cfg, user, vehicle_no=str(form.get("vehicle_no", "")), weight_kg=weight,
                                  manual=manual, manual_reason=str(form.get("manual_reason", "")), ip=ip_of(request))
            except (CaptureError, WeighingError, ValueError) as exc:
                return go("/weigh", str(exc), "err")
        finally:
            conn.close()
        return go("/weigh", f"Stored tare for {res['vehicle_no']} set to {kg(res['new_kg'])} kg.")

    # --- tickets -------------------------------------------------------------------------

    def ticket_filters(request: Request) -> dict:
        q = request.query_params
        today = datetime.now(cfg.tz).date()
        return {"date_from": q.get("from") or (today - timedelta(days=7)).isoformat(),
                "date_to": q.get("to") or today.isoformat(), "status": q.get("status", ""),
                "party_id": q.get("party_id") or None, "material_id": q.get("material_id") or None,
                "vehicle": q.get("vehicle", ""), "flagged": q.get("flagged") == "1"}

    @app.get("/tickets")
    def tickets_page(request: Request):
        conn = db()
        try:
            user = require(request, conn)
            f = ticket_filters(request)
            rows = search_tickets(conn, **f)
            ctx = options(conn)
        finally:
            conn.close()
        closed = [t for t in rows if t["status"] == "closed"]
        return render(request, "tickets.html", user, tickets=rows, f=f, query=request.url.query,
                      total_net=sum(t["net_kg"] or 0 for t in closed), closed_count=len(closed), **ctx)

    @app.get("/tickets.xlsx")
    def tickets_xlsx(request: Request):
        conn = db()
        try:
            require(request, conn)
            f = ticket_filters(request)
            rows = search_tickets(conn, **{**f, "limit": 20000})
        finally:
            conn.close()
        data = reports.tickets_xlsx(rows, f"Tickets {f['date_from']} to {f['date_to']}")
        return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="tickets-{f["date_from"]}-{f["date_to"]}.xlsx"'})

    def load_ticket(conn, ticket_id: int) -> dict:
        t = get_ticket(conn, ticket_id)
        if not t:
            raise Redirect("/tickets")
        return t

    @app.get("/tickets/{ticket_id}")
    def ticket_page(request: Request, ticket_id: int):
        conn = db()
        try:
            user = require(request, conn)
            t = load_ticket(conn, ticket_id)
            history = [dict(r) for r in conn.execute(
                "SELECT * FROM audit_log WHERE entity='ticket' AND entity_id=? ORDER BY id", (t["ticket_no"],))]
            messages = [dict(r) for r in conn.execute("SELECT * FROM outbox WHERE ticket_id=? ORDER BY id", (ticket_id,))]
            ctx = options(conn)
        finally:
            conn.close()
        return render(request, "ticket.html", user, t=t, history=history, messages=messages, **ctx)

    @app.get("/tickets/{ticket_id}/print")
    def ticket_print(request: Request, ticket_id: int):
        conn = db()
        try:
            user = require(request, conn)
            t = load_ticket(conn, ticket_id)
        finally:
            conn.close()
        return render(request, "ticket_print.html", user, t=t)

    @app.get("/tickets/{ticket_id}/image.png")
    def ticket_png(request: Request, ticket_id: int):
        from ..services import ticket_image
        conn = db()
        try:
            require(request, conn)
            t = load_ticket(conn, ticket_id)
        finally:
            conn.close()
        if t["status"] != "closed":
            raise Redirect(f"/tickets/{ticket_id}")
        path = notify.image_path(cfg, t["ticket_no"])
        ticket_image.render(t, cfg["company"], path)
        return FileResponse(path, media_type="image/png")

    @app.post("/tickets/{ticket_id}/cancel")
    async def ticket_cancel(request: Request, ticket_id: int):
        conn = db()
        try:
            user = require(request, conn, SUPERVISE_ROLES)
            form = await form_checked(request, user)
            try:
                cancel_ticket(conn, cfg, user, ticket_id, str(form.get("reason", "")), ip_of(request))
            except WeighingError as exc:
                return go(f"/tickets/{ticket_id}", str(exc), "err")
        finally:
            conn.close()
        return go(f"/tickets/{ticket_id}", "Ticket cancelled.")

    @app.post("/tickets/{ticket_id}/correct")
    async def ticket_correct(request: Request, ticket_id: int):
        conn = db()
        try:
            user = require(request, conn, SUPERVISE_ROLES)
            form = await form_checked(request, user)
            try:
                correct_ticket(conn, cfg, user, ticket_id, party_id=form.get("party_id"),
                               material_id=form.get("material_id"), reason=str(form.get("reason", "")), ip=ip_of(request))
            except WeighingError as exc:
                return go(f"/tickets/{ticket_id}", str(exc), "err")
        finally:
            conn.close()
        return go(f"/tickets/{ticket_id}", "Ticket corrected. The change is in the history below.")

    @app.post("/tickets/{ticket_id}/resend")
    async def ticket_resend(request: Request, ticket_id: int):
        conn = db()
        try:
            user = require(request, conn, WEIGH_ROLES + ("accounts",))
            await form_checked(request, user)
            count = notify.queue_ticket(conn, cfg, ticket_id)
        finally:
            conn.close()
        text = f"{count} message(s) queued." if count else "Nothing to send. Add a mobile, WhatsApp or email to the party or vehicle, and switch the channel on in config.toml."
        return go(f"/tickets/{ticket_id}", text, "ok" if count else "err")

    # --- masters -------------------------------------------------------------------------

    MASTER_ROLES = {"vehicles": SUPERVISE_ROLES, "materials": SUPERVISE_ROLES, "parties": ("admin", "accounts")}

    @app.get("/masters/{kind}")
    def masters_page(request: Request, kind: str):
        if kind not in MASTER_ROLES:
            raise Redirect("/")
        conn = db()
        try:
            user = require(request, conn)
            order = "number" if kind == "vehicles" else "name"
            rows = [dict(r) for r in conn.execute(f"SELECT * FROM {kind} ORDER BY active DESC, {order}")]
            edit_id = request.query_params.get("edit")
            editing = next((r for r in rows if str(r["id"]) == edit_id), None)
        finally:
            conn.close()
        return render(request, f"masters_{kind}.html", user, rows=rows, editing=editing,
                      can_edit=user["role"] in MASTER_ROLES[kind])

    FIELDS = {
        "vehicles": ("transporter", "driver_mobile", "owner_whatsapp"),
        "materials": ("name", "hsn"),
        "parties": ("name", "kind", "gstin", "address", "mobile", "whatsapp", "email", "rate_per_mt", "tally_ledger"),
    }

    @app.post("/masters/{kind}")
    async def masters_save(request: Request, kind: str):
        if kind not in MASTER_ROLES:
            raise Redirect("/")
        conn = db()
        try:
            user = require(request, conn, MASTER_ROLES[kind])
            form = await form_checked(request, user)
            values = {k: str(form.get(k, "")).strip()[:300] for k in FIELDS[kind]}
            values_active = 1 if form.get("active", "1") == "1" else 0
            row_id = form.get("id")
            try:
                if kind == "vehicles":
                    values["number"] = normalize_plate(str(form.get("number", "")))
                if kind == "parties":
                    values["rate_per_mt"] = float(values["rate_per_mt"] or 0)
                    values["gstin"] = values["gstin"].upper()
                    if values["kind"] not in ("customer", "contractor", "supplier"):
                        raise WeighingError("Choose a party type.")
                    if values["gstin"] and len(values["gstin"]) != 15:
                        raise WeighingError("A GSTIN has 15 characters.")
                if "name" in values and not values["name"]:
                    raise WeighingError("Enter a name.")
                with transaction(conn):
                    if row_id:
                        before = dict(conn.execute(f"SELECT * FROM {kind} WHERE id=?", (int(row_id),)).fetchone())
                        sets = ", ".join(f"{k}=?" for k in values)
                        conn.execute(f"UPDATE {kind} SET {sets}, active=? WHERE id=?",
                                     (*values.values(), values_active, int(row_id)))
                        changed = {k: [before[k], v] for k, v in values.items() if before.get(k) != v}
                        if before["active"] != values_active:
                            changed["active"] = [before["active"], values_active]
                        audit.record(conn, now_iso(cfg), user, f"{kind[:-1]}.updated", kind[:-1], row_id, changed, ip_of(request))
                    else:
                        cols = ", ".join(values)
                        cur = conn.execute(f"INSERT INTO {kind}({cols}) VALUES ({','.join('?' * len(values))})",
                                           tuple(values.values()))
                        audit.record(conn, now_iso(cfg), user, f"{kind[:-1]}.created", kind[:-1], cur.lastrowid, values, ip_of(request))
            except (WeighingError, ValueError) as exc:
                return go(f"/masters/{kind}", str(exc) if isinstance(exc, WeighingError) else "Check the numbers you entered.", "err")
            except Exception as exc:
                if "UNIQUE" in str(exc):
                    return go(f"/masters/{kind}", "That name or number already exists.", "err")
                raise
        finally:
            conn.close()
        return go(f"/masters/{kind}", "Saved.")

    # --- billing ---------------------------------------------------------------------------

    @app.get("/billing")
    def billing_page(request: Request):
        conn = db()
        try:
            user = require(request, conn, BILL_VIEW_ROLES)
            q = request.query_params
            today = datetime.now(cfg.tz).date()
            first = today.replace(day=1)
            f = {"party_id": q.get("party_id", ""), "from": q.get("from") or first.isoformat(),
                 "to": q.get("to") or today.isoformat(), "rate": q.get("rate", "")}
            preview = None
            if f["party_id"]:
                try:
                    preview = billing.preview(conn, cfg, int(f["party_id"]), f["from"], f["to"],
                                              float(f["rate"]) if f["rate"] else None)
                except (WeighingError, ValueError) as exc:
                    return go("/billing", str(exc), "err")
            bills = billing.list_bills(conn)
            parties = [dict(r) for r in conn.execute("SELECT * FROM parties WHERE active=1 ORDER BY name")]
        finally:
            conn.close()
        return render(request, "billing.html", user, f=f, preview=preview, bills=bills, parties=parties)

    @app.post("/billing")
    async def billing_create(request: Request):
        conn = db()
        try:
            user = require(request, conn, BILL_ROLES)
            form = await form_checked(request, user)
            try:
                bill_id = billing.create_bill(conn, cfg, user, int(form.get("party_id")), str(form.get("from")),
                                              str(form.get("to")), float(form["rate"]) if form.get("rate") else None,
                                              ip_of(request))
            except (WeighingError, ValueError, TypeError) as exc:
                return go("/billing", str(exc), "err")
        finally:
            conn.close()
        return go(f"/billing/{bill_id}", "Bill created.")

    def load_bill(conn, bill_id: int) -> dict:
        bill = billing.get_bill(conn, bill_id)
        if not bill:
            raise Redirect("/billing")
        return bill

    @app.get("/billing/{bill_id}")
    def bill_page(request: Request, bill_id: int):
        conn = db()
        try:
            user = require(request, conn, BILL_VIEW_ROLES)
            bill = load_bill(conn, bill_id)
        finally:
            conn.close()
        return render(request, "bill.html", user, bill=bill)

    @app.get("/billing/{bill_id}/bill.xlsx")
    def bill_xlsx(request: Request, bill_id: int):
        conn = db()
        try:
            require(request, conn, BILL_VIEW_ROLES)
            bill = load_bill(conn, bill_id)
        finally:
            conn.close()
        name = bill["bill_no"].replace("/", "-")
        return Response(reports.bill_xlsx(bill, cfg["company"]),
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="{name}.xlsx"'})

    @app.get("/billing/{bill_id}/tally.xml")
    def bill_tally_xml(request: Request, bill_id: int):
        conn = db()
        try:
            user = require(request, conn, BILL_ROLES)
            bill = load_bill(conn, bill_id)
            if bill["status"] != "active":
                return go(f"/billing/{bill_id}", "Cancelled bills can't be exported.", "err")
            tally.mark_exported(conn, cfg, user, bill, ip_of(request))
        finally:
            conn.close()
        name = bill["bill_no"].replace("/", "-")
        return Response(tally.voucher_xml(bill, cfg), media_type="application/xml",
                        headers={"Content-Disposition": f'attachment; filename="tally-{name}.xml"'})

    @app.post("/billing/{bill_id}/tally")
    async def bill_tally_push(request: Request, bill_id: int):
        conn = db()
        try:
            user = require(request, conn, BILL_ROLES)
            await form_checked(request, user)
            bill = load_bill(conn, bill_id)
            try:
                text = tally.push(bill, cfg, conn, user, ip_of(request))
            except WeighingError as exc:
                return go(f"/billing/{bill_id}", str(exc), "err")
        finally:
            conn.close()
        return go(f"/billing/{bill_id}", text)

    @app.post("/billing/{bill_id}/cancel")
    async def bill_cancel(request: Request, bill_id: int):
        conn = db()
        try:
            user = require(request, conn, ("admin",))
            form = await form_checked(request, user)
            try:
                billing.cancel_bill(conn, cfg, user, bill_id, str(form.get("reason", "")), ip_of(request))
            except WeighingError as exc:
                return go(f"/billing/{bill_id}", str(exc), "err")
        finally:
            conn.close()
        return go(f"/billing/{bill_id}", "Bill cancelled. Its tickets can be billed again. "
                                         "If it was already in Tally, cancel the voucher there too.")

    # --- reports -------------------------------------------------------------------------------

    def report_args(request: Request) -> tuple[str, str, str]:
        q = request.query_params
        today = datetime.now(cfg.tz).date()
        return (q.get("from") or today.replace(day=1).isoformat(), q.get("to") or today.isoformat(),
                q.get("group") if q.get("group") in reports.GROUPS else "party")

    @app.get("/reports")
    def reports_page(request: Request):
        conn = db()
        try:
            user = require(request, conn)
            d_from, d_to, group = report_args(request)
            rows = reports.summary(conn, d_from, d_to, group)
            daily = reports.summary(conn, d_from, d_to, "day")
            today = reports.day_totals(conn, datetime.now(cfg.tz).date().isoformat())
        finally:
            conn.close()
        peak = max([r["net_kg"] or 0 for r in daily] or [0])
        return render(request, "reports.html", user, rows=rows, daily=daily, peak=peak, today=today,
                      f={"from": d_from, "to": d_to, "group": group}, groups=reports.GROUPS, query=request.url.query)

    @app.get("/reports.xlsx")
    def reports_xlsx(request: Request):
        conn = db()
        try:
            require(request, conn)
            d_from, d_to, group = report_args(request)
            rows = reports.summary(conn, d_from, d_to, group)
        finally:
            conn.close()
        return Response(reports.summary_xlsx(rows, group, d_from, d_to),
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="summary-{group}-{d_from}-{d_to}.xlsx"'})

    # --- users ------------------------------------------------------------------------------

    @app.get("/users")
    def users_page(request: Request):
        conn = db()
        try:
            user = require(request, conn, ("admin",))
            rows = [dict(r) for r in conn.execute("SELECT * FROM users ORDER BY active DESC, username")]
        finally:
            conn.close()
        return render(request, "users.html", user, rows=rows, roles=ROLES,
                      min_len=cfg["security"]["password_min_length"])

    @app.post("/users")
    async def users_save(request: Request):
        conn = db()
        try:
            user = require(request, conn, ("admin",))
            form = await form_checked(request, user)
            action = form.get("action")
            ip = ip_of(request)
            try:
                with transaction(conn):
                    if action == "create":
                        username = str(form.get("username", "")).strip().lower()
                        if not username.isalnum() or len(username) < 3:
                            raise WeighingError("Usernames are 3+ letters or digits, no spaces.")
                        role = str(form.get("role"))
                        if role not in ROLES:
                            raise WeighingError("Choose a role.")
                        pw = str(form.get("password", ""))
                        problems = security.password_problems(pw, int(cfg["security"]["password_min_length"]), username)
                        if problems:
                            raise WeighingError(" ".join(problems))
                        conn.execute("INSERT INTO users(username, full_name, role, pw_hash, must_change_pw, created_at) "
                                     "VALUES (?,?,?,?,1,?)", (username, str(form.get("full_name", "")).strip() or username,
                                                              role, security.hash_password(pw), now_iso(cfg)))
                        audit.record(conn, now_iso(cfg), user, "user.created", "user", username, {"role": role}, ip)
                        msg = f"User {username} created. They must change the password at first login."
                    else:
                        target = conn.execute("SELECT * FROM users WHERE id=?", (int(form.get("id")),)).fetchone()
                        if not target:
                            raise WeighingError("User not found.")
                        if target["id"] == user["id"] and action in ("disable", "role"):
                            raise WeighingError("You can't disable yourself or change your own role.")
                        if action == "disable":
                            conn.execute("UPDATE users SET active=0 WHERE id=?", (target["id"],))
                            conn.execute("DELETE FROM sessions WHERE user_id=?", (target["id"],))
                        elif action == "enable":
                            conn.execute("UPDATE users SET active=1, failed_logins=0, locked_until=NULL WHERE id=?", (target["id"],))
                        elif action == "role":
                            role = str(form.get("role"))
                            if role not in ROLES:
                                raise WeighingError("Choose a role.")
                            conn.execute("UPDATE users SET role=? WHERE id=?", (role, target["id"]))
                        elif action == "reset_password":
                            pw = str(form.get("password", ""))
                            problems = security.password_problems(pw, int(cfg["security"]["password_min_length"]), target["username"])
                            if problems:
                                raise WeighingError(" ".join(problems))
                            conn.execute("UPDATE users SET pw_hash=?, must_change_pw=1, failed_logins=0, locked_until=NULL "
                                         "WHERE id=?", (security.hash_password(pw), target["id"]))
                            conn.execute("DELETE FROM sessions WHERE user_id=?", (target["id"],))
                        elif action == "reset_mfa":
                            conn.execute("UPDATE users SET totp_enabled=0, totp_secret=NULL WHERE id=?", (target["id"],))
                            conn.execute("DELETE FROM sessions WHERE user_id=?", (target["id"],))
                        else:
                            raise WeighingError("Unknown action.")
                        audit.record(conn, now_iso(cfg), user, f"user.{action}", "user", target["username"],
                                     {"role": form.get("role")} if action == "role" else {}, ip)
                        msg = "Saved."
            except WeighingError as exc:
                return go("/users", str(exc), "err")
            except Exception as exc:
                if "UNIQUE" in str(exc):
                    return go("/users", "That username is taken.", "err")
                raise
        finally:
            conn.close()
        return go("/users", msg)

    # --- audit & system -----------------------------------------------------------------------

    @app.get("/audit")
    def audit_page(request: Request):
        conn = db()
        try:
            user = require(request, conn, ("admin", "auditor"))
            q = request.query_params
            where, args = [], []
            if q.get("action"):
                where.append("action LIKE ?"); args.append(q["action"] + "%")
            if q.get("q"):
                where.append("(entity_id LIKE ? OR username LIKE ? OR details LIKE ?)")
                args += [f"%{q['q']}%"] * 3
            sql = "SELECT * FROM audit_log" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT 300"
            rows = [dict(r) for r in conn.execute(sql, args)]
            ok, count, bad = audit.verify_chain(conn)
            head = audit.chain_head(conn)
        finally:
            conn.close()
        return render(request, "audit.html", user, rows=rows, chain_ok=ok, chain_count=count, chain_bad=bad,
                      head=head, q=q)

    @app.get("/system")
    def system_page(request: Request):
        conn = db()
        try:
            user = require(request, conn, ("admin",))
            outbox = [dict(r) for r in conn.execute("SELECT * FROM outbox ORDER BY id DESC LIMIT 100")]
        finally:
            conn.close()
        return render(request, "system.html", user, outbox=outbox, backups=backup.list_backups(cfg),
                      weight=monitor.snapshot())

    @app.post("/system/backup")
    async def system_backup(request: Request):
        conn = db()
        try:
            user = require(request, conn, ("admin",))
            await form_checked(request, user)
            path = backup.backup_now(cfg)
            with transaction(conn):
                audit.record(conn, now_iso(cfg), user, "system.backup", "backup", path.name, {}, ip_of(request))
        finally:
            conn.close()
        return go("/system", f"Backup written: {path.name}")

    @app.post("/system/outbox/{outbox_id}/retry")
    async def outbox_retry(request: Request, outbox_id: int):
        conn = db()
        try:
            user = require(request, conn, ("admin",))
            await form_checked(request, user)
            notify.retry(conn, cfg, outbox_id)
        finally:
            conn.close()
        return go("/system", "Queued to send again.")

    return app
