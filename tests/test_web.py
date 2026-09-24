import re

import pytest
from fastapi.testclient import TestClient

from weighbridge import security
from weighbridge.indicator import WeightMonitor
from weighbridge.web.app import create_app, inr

from .conftest import hold, make_user

PW = "correct horse battery"


@pytest.fixture
def app_env(cfg, conn):
    monitor = WeightMonitor(cfg)
    app = create_app(cfg, monitor=monitor, start_background=False)
    return TestClient(app), monitor, conn


def csrf_of(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def login(client, username, password=PW):
    page = client.get("/login")
    return client.post("/login", data={"csrf": csrf_of(page.text), "username": username, "password": password},
                       follow_redirects=False)


def test_indian_number_format():
    assert inr(1234567.5) == "12,34,567.50"
    assert inr(28240, 0) == "28,240"
    assert inr(999, 0) == "999"


def test_requires_login_and_sets_security_headers(app_env):
    client, _, _ = app_env
    r = client.get("/weigh", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/api/weight").status_code == 401
    r = client.get("/login")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"


def test_login_csrf_required(app_env):
    client, _, conn = app_env
    make_user(conn, "op", "operator")
    client.get("/login")
    r = client.post("/login", data={"csrf": "forged", "username": "op", "password": PW}, follow_redirects=False)
    assert "wb_session" not in r.cookies


def test_lockout_after_wrong_passwords(app_env):
    client, _, conn = app_env
    make_user(conn, "op", "operator")
    for _ in range(5):
        login(client, "op", "wrong password here")
    r = login(client, "op")
    assert "wb_session" not in r.cookies
    assert "locked" in client.get("/login").text


def test_admin_must_enrol_mfa(app_env):
    client, _, conn = app_env
    make_user(conn, "boss", "admin")
    login(client, "boss")
    r = client.get("/weigh", follow_redirects=False)
    assert r.headers["location"] == "/account/mfa"
    page = client.get("/account/mfa").text
    secret = re.search(r'<code class="key">([A-Z2-7]+)</code>', page).group(1)
    r = client.post("/account/mfa", data={"csrf": csrf_of(page), "code": "000000"}, follow_redirects=False)
    assert client.get("/weigh", follow_redirects=False).headers["location"] == "/account/mfa"
    page = client.get("/account/mfa").text  # a fresh secret is issued on each visit
    secret = re.search(r'<code class="key">([A-Z2-7]+)</code>', page).group(1)
    client.post("/account/mfa", data={"csrf": csrf_of(page), "code": security.totp_now(secret)})
    assert client.get("/weigh", follow_redirects=False).status_code == 200

    # Next login asks for the code before anything else.
    client.cookies.clear()
    login(client, "boss")
    assert client.get("/users", follow_redirects=False).headers["location"] == "/mfa"


def test_operator_weighing_flow(app_env):
    client, monitor, conn = app_env
    make_user(conn, "op", "operator")
    login(client, "op")
    assert client.get("/users").status_code == 403

    page = client.get("/weigh").text
    token = csrf_of(page)
    # No CSRF token → refused
    assert client.post("/weigh", data={"vehicle_no": "MH34AB1234"}).status_code == 403

    # Indicator silent → friendly error, nothing saved
    client.post("/weigh", data={"csrf": token, "vehicle_no": "MH34AB1234"})
    assert conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 0

    # Operator cannot sneak in a hand-typed weight
    hold(monitor, 42560)
    client.post("/weigh", data={"csrf": token, "vehicle_no": "MH34AB1234", "manual": "1",
                                "manual_kg": "60000", "manual_reason": "trying"})
    assert conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 0

    r = client.post("/weigh", data={"csrf": token, "vehicle_no": "MH34AB1234"}, follow_redirects=False)
    assert r.headers["location"] == "/weigh"
    info = client.get("/api/vehicle", params={"number": "MH34AB1234"}).json()
    assert info["open_ticket"]["first_kg"] == 42560

    hold(monitor, 14320)
    r = client.post("/weigh", data={"csrf": token, "vehicle_no": "MH34AB1234"}, follow_redirects=False)
    assert r.headers["location"].startswith("/tickets/")
    ticket = client.get(r.headers["location"]).text
    assert "28,240" in ticket
    assert client.get(r.headers["location"] + "/print").status_code == 200
    assert client.get(r.headers["location"] + "/image.png").headers["content-type"] == "image/png"
    assert client.get("/tickets.xlsx").status_code == 200
    assert client.get("/reports").status_code == 200
