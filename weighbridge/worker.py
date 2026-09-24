"""Background jobs: send the outbox, daily backup, daily owner summary."""
from __future__ import annotations

import logging
import threading
from datetime import datetime

from . import audit
from .db import connect, kv_get, kv_set
from .services import backup, notify, reports

log = logging.getLogger(__name__)


class Worker(threading.Thread):
    def __init__(self, cfg, interval: float = 20.0):
        super().__init__(daemon=True, name="worker")
        self.cfg = cfg
        self.interval = interval
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("background job failed")
            self._stop.wait(self.interval)

    def tick(self) -> None:
        cfg = self.cfg
        conn = connect(cfg.db_path)
        try:
            notify.process_due(conn, cfg)
            now = datetime.now(cfg.tz)
            today = now.date().isoformat()
            if kv_get(conn, "last_backup_day") != today:
                path = backup.backup_now(cfg)
                kv_set(conn, "last_backup_day", today)
                log.info("backup written to %s", path)
            if now.hour >= int(cfg["notify"]["daily_summary_hour"]) and kv_get(conn, "last_summary_day") != today:
                totals = reports.day_totals(conn, today)
                rows = reports.summary(conn, today, today, "party")
                notify.queue_daily_summary(conn, cfg, today, totals, rows, audit.chain_head(conn))
                kv_set(conn, "last_summary_day", today)
        finally:
            conn.close()
