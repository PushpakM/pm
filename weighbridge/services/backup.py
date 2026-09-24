"""Consistent SQLite backups (online backup API), kept for N days, optional second copy."""
from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


def backup_now(cfg) -> Path:
    stamp = datetime.now(cfg.tz).strftime("%Y%m%d-%H%M%S")
    dest = cfg.backup_dir / f"weighbridge-{stamp}.db"
    src = sqlite3.connect(cfg.db_path)
    try:
        out = sqlite3.connect(dest)
        with out:
            src.backup(out)
        out.close()
    finally:
        src.close()
    extra = cfg["backup"]["extra_dir"]
    if extra:
        try:
            Path(extra).mkdir(parents=True, exist_ok=True)
            shutil.copy2(dest, Path(extra) / dest.name)
        except OSError:
            pass  # USB drive unplugged etc.; the local copy still exists
    _prune(cfg)
    return dest


def _prune(cfg) -> None:
    keep = int(cfg["backup"]["keep"])
    files = sorted(cfg.backup_dir.glob("weighbridge-*.db"))
    for old in files[:-keep] if keep > 0 else []:
        old.unlink(missing_ok=True)


def list_backups(cfg) -> list[dict]:
    return [{"name": p.name, "size_mb": p.stat().st_size / 1e6}
            for p in sorted(cfg.backup_dir.glob("weighbridge-*.db"), reverse=True)]
