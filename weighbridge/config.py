"""Settings: config.example.toml supplies every default, config.toml overrides them."""
from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "config.example.toml"


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


class Config(dict):
    """Nested dict of sections, e.g. cfg["indicator"]["port"]."""

    @property
    def data_dir(self) -> Path:
        path = Path(self["server"]["data_dir"])
        if not path.is_absolute():
            path = ROOT / path
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def db_path(self) -> Path:
        return self.data_dir / "weighbridge.db"

    @property
    def images_dir(self) -> Path:
        path = self.data_dir / "tickets"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def backup_dir(self) -> Path:
        path = self.data_dir / "backups"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self["server"]["timezone"])


def load_config(path: str | os.PathLike | None = None, overrides: dict | None = None) -> Config:
    with open(EXAMPLE, "rb") as fh:
        base = tomllib.load(fh)
    path = Path(path or os.environ.get("WB_CONFIG", ROOT / "config.toml"))
    if path.exists():
        with open(path, "rb") as fh:
            base = _merge(base, tomllib.load(fh))
    if overrides:
        base = _merge(base, overrides)
    return Config(base)
