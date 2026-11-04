from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

APP_NAME = "ludus-sync"
KEYRING_SERVICE = "ludus-sync"

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_DIR / "config.toml"
ENV_PATH = PROJECT_DIR / ".env"
DATA_DIR = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME


@dataclass
class Config:
    ludus_url: str
    username: str
    calendar_id: str = "primary"
    timezone: str = "Europe/Copenhagen"
    weeks_ahead: int = 5
    # Google Calendar colorId values: 3 = "Drue" (purple), 1 = "Lavendel".
    study_color_id: str = "3"
    normal_color_id: str = ""
    cancelled_keywords: list[str] = field(default_factory=lambda: ["aflyst"])
    skip_keywords: list[str] = field(default_factory=lambda: ["frivillig lektiecafe"])
    study_keywords: list[str] = field(
        default_factory=lambda: ["studietid", "studietime", "studiecafé", "studiecafe", "lektiecafé"]
    )
    network_wait_seconds: int = 300


@dataclass
class LessorConfig:
    company: str
    username: str
    url: str = "https://workforce.lessor.dk/"
    calendar_id: str = "primary"
    timezone: str = "Europe/Copenhagen"
    weeks_ahead: int = 8
    # Google Calendar colorId for shifts; empty = the calendar's default colour.
    color_id: str = ""
    network_wait_seconds: int = 300


def load_lessor_config(path: Path = CONFIG_PATH) -> LessorConfig:
    """The [lessor] table in config.toml (calendar_id/timezone default to the top-level ones)."""
    if not path.exists():
        raise SystemExit(f"Mangler {path}. Kopiér config.example.toml til config.toml og udfyld den.")
    with path.open("rb") as f:
        raw = tomllib.load(f)
    if "lessor" not in raw:
        raise SystemExit(f"Mangler en [lessor]-sektion i {path}. Se config.example.toml.")
    known = {f.name for f in fields(LessorConfig)}
    inherited = {k: raw[k] for k in ("calendar_id", "timezone") if k in raw}
    return LessorConfig(**{**inherited, **{k: v for k, v in raw["lessor"].items() if k in known}})


def load_config(path: Path = CONFIG_PATH) -> Config:
    if not path.exists():
        raise SystemExit(f"Mangler {path}. Kopiér config.example.toml til config.toml og udfyld den.")
    with path.open("rb") as f:
        raw = tomllib.load(f)
    known = {f.name for f in fields(Config)}
    return Config(**{k: v for k, v in raw.items() if k in known})


def load_dotenv(path: Path = ENV_PATH):
    """Read KEY=value lines from .env into os.environ (real env vars win)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def ensure_data_dir() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR
