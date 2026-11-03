from __future__ import annotations

import argparse
import getpass
import logging
import os
import socket
import sys
import time
from datetime import date
from logging.handlers import RotatingFileHandler
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import keyring

from .config import KEYRING_SERVICE, PROJECT_DIR, ensure_data_dir, load_config, load_dotenv
from .gcal import build_service, remove_synced_events, sync
from .ludus import LudusSession, window_bounds

log = logging.getLogger("ludus_sync")


def setup_logging(data_dir):
    handlers = [RotatingFileHandler(data_dir / "sync.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")]
    if sys.stdout is not None:  # pythonw has no console
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)


def wait_for_network(host: str, timeout: int):
    """At boot the network is often not up yet; poll DNS until it is."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            socket.getaddrinfo(host, 443)
            return
        except OSError:
            if time.monotonic() > deadline:
                raise SystemExit(f"Intet netværk efter {timeout}s – giver op")
            time.sleep(10)


def get_password(username: str) -> str:
    password = os.environ.get("LUDUS_PASSWORD") or keyring.get_password(KEYRING_SERVICE, username)
    if not password:
        raise SystemExit("Ingen Ludus-adgangskode. Skriv den i .env (LUDUS_PASSWORD=...) eller kør: python -m ludus_sync setup")
    return password


def cmd_setup(cfg, data_dir):
    password = os.environ.get("LUDUS_PASSWORD")
    if password:
        print("Bruger adgangskoden fra .env.")
    else:
        password = getpass.getpass(f"Ludus-adgangskode for {cfg.username}: ")
        keyring.set_password(KEYRING_SERVICE, cfg.username, password)
        print("Adgangskode gemt i Windows Credential Manager.")
    with LudusSession(cfg, password, data_dir / "ludus_state.json"):
        print("Login til Ludus virker.")
    build_service(PROJECT_DIR / "credentials.json", data_dir / "google_token.json", interactive=True)
    print("Google Kalender er forbundet.")


def cmd_discover(cfg, data_dir, headed: bool):
    with LudusSession(cfg, get_password(cfg.username), data_dir / "ludus_state.json", headless=not headed) as s:
        s.discover(data_dir / "discover")
    print(f"Filer gemt i {data_dir / 'discover'}")


def cmd_sync(cfg, data_dir):
    wait_for_network(urlparse(cfg.ludus_url).hostname, cfg.network_wait_seconds)
    tz = ZoneInfo(cfg.timezone)
    today = date.today()
    with LudusSession(cfg, get_password(cfg.username), data_dir / "ludus_state.json") as s:
        lessons = s.fetch_lessons(today, cfg.weeks_ahead)
    if not lessons:
        # More likely a changed Ludus layout than an empty schedule; don't wipe the calendar.
        raise SystemExit("Fandt ingen moduler i Ludus – ændrer intet i kalenderen. Kør 'discover' for at fejlsøge.")
    service = build_service(PROJECT_DIR / "credentials.json", data_dir / "google_token.json", interactive=False)

    # If calendar_id was changed, take our events out of the old calendar.
    last_calendar_file = data_dir / "last_calendar.txt"
    last_calendar = last_calendar_file.read_text().strip() if last_calendar_file.exists() else "primary"
    if last_calendar != cfg.calendar_id:
        remove_synced_events(service, last_calendar)

    start, end = window_bounds(today, cfg.weeks_ahead, tz)
    sync(service, cfg, lessons, start, end)
    last_calendar_file.write_text(cfg.calendar_id)


def main():
    parser = argparse.ArgumentParser(prog="ludus_sync", description="Synkroniser Ludus-skema til Google Kalender")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup", help="Gem Ludus-adgangskode og forbind Google Kalender")
    disc = sub.add_parser("discover", help="Gem Ludus-sider til udvikling af skema-parseren")
    disc.add_argument("--headed", action="store_true", help="Vis browservinduet")
    sub.add_parser("sync", help="Synkroniser nu")
    args = parser.parse_args()

    data_dir = ensure_data_dir()
    setup_logging(data_dir)
    load_dotenv()
    cfg = load_config()
    try:
        if args.cmd == "setup":
            cmd_setup(cfg, data_dir)
        elif args.cmd == "discover":
            cmd_discover(cfg, data_dir, args.headed)
        else:
            cmd_sync(cfg, data_dir)
    except Exception:
        log.exception("Kørsel fejlede")
        raise


if __name__ == "__main__":
    main()
