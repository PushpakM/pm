import time

import pytest

from weighbridge import security
from weighbridge.config import load_config
from weighbridge.db import connect, init_db
from weighbridge.indicator import WeightMonitor


@pytest.fixture
def cfg(tmp_path):
    return load_config(tmp_path / "none.toml", overrides={"server": {"data_dir": str(tmp_path / "data")}})


@pytest.fixture
def conn(cfg):
    init_db(cfg.db_path)
    c = connect(cfg.db_path)
    yield c
    c.close()


def make_user(conn, username="op", role="operator", password="correct horse battery", must_change=0, cfg=None):
    conn.execute("INSERT INTO users(username, full_name, role, pw_hash, must_change_pw, created_at) VALUES (?,?,?,?,?,?)",
                 (username, username.title(), role, security.hash_password(password), must_change, "2026-09-24T10:00:00+05:30"))
    row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    return dict(row)


@pytest.fixture
def operator(conn):
    return make_user(conn, "op", "operator")


@pytest.fixture
def supervisor(conn):
    return make_user(conn, "sup", "supervisor")


@pytest.fixture
def accounts(conn):
    return make_user(conn, "acc", "accounts")


@pytest.fixture
def admin(conn):
    return make_user(conn, "boss", "admin")


def hold(monitor: WeightMonitor, kg: float, seconds: float = 4.0) -> None:
    """Feed the monitor a steady reading for the last `seconds`."""
    with monitor._lock:
        monitor._readings.clear()
    now = time.monotonic()
    t = now - seconds
    while t <= now:
        monitor.add(kg, True, f"{kg}", at=t)
        t += 0.2
