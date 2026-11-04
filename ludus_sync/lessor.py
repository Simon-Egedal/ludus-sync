"""Synkroniserer dine vagter fra Lessor Workforce til Google Kalender.

Et selvstændigt program ved siden af Ludus-synkroniseringen:
    python -m ludus_sync.lessor setup|discover|sync
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import logging
import os
import re
import sys
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import keyring

from .config import PROJECT_DIR, LessorConfig, ensure_data_dir, load_dotenv, load_lessor_config
from .gcal import SyncResult, _busy_slots, _list_events, apply_desired, build_service, remove_synced_events

log = logging.getLogger("lessor_sync")

KEYRING_SERVICE = "lessor-sync"
SYNC_TAG = "lessor_sync"
KEY_PROP = "lessor_key"

# The profile page shows a personal iCal link (Profil -> iCal, with only "Vagter" ticked) like
# https://workforce.lessor.dk/sync/getICal.php?companyId=..&userId=..&calendarId=..
# It sits inside the "classic" iframe, not in the top-level page.
ICAL_URL_RE = re.compile(r"https://[^\"'<>\s]*getICal[^\"'<>\s]*")


class LoginError(RuntimeError):
    pass


@dataclass
class Shift:
    title: str
    start: datetime  # timezone-aware
    end: datetime
    location: str = ""
    description: str = ""
    uid: str = ""

    @property
    def key(self) -> str:
        # Lessor's feed makes up a new UID on every fetch, so match shifts on their times instead.
        raw = f"{self.start.isoformat()}|{self.end.isoformat()}"
        return "h:" + hashlib.sha1(raw.encode()).hexdigest()[:16]


# --- iCalendar ---------------------------------------------------------------------------

def _unescape(value: str) -> str:
    return re.sub(r"\\([\\;,nN])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), value)


def _parse_dt(value: str, params: dict[str, str], tz: ZoneInfo) -> datetime | None:
    """iCalendar DATE-TIME -> aware datetime; None for all-day DATE values."""
    if params.get("VALUE") == "DATE" or "T" not in value:
        return None
    if value.endswith("Z"):
        return datetime.strptime(value[:-1], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc).astimezone(tz)
    naive = datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
    try:
        zone = ZoneInfo(params["TZID"]) if "TZID" in params else tz
    except (KeyError, ValueError):
        zone = tz  # Windows-style TZIDs like "Romance Standard Time"
    return naive.replace(tzinfo=zone)


def parse_ics(text: str, tz: ZoneInfo) -> list[Shift]:
    """Timed VEVENTs from an iCalendar file. All-day entries (holidays, absence) are skipped."""
    if "BEGIN:VCALENDAR" not in text:
        raise ValueError("Eksporten fra Lessor er ikke en iCalendar-fil")
    lines: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if line[:1] in (" ", "\t") and lines:
            lines[-1] += line[1:]  # folded continuation line
        else:
            lines.append(line)

    shifts, ev = [], None
    for line in lines:
        if line == "BEGIN:VEVENT":
            ev = {}
        elif line == "END:VEVENT" and ev is not None:
            start = _parse_dt(*ev["DTSTART"], tz) if "DTSTART" in ev else None
            end = _parse_dt(*ev["DTEND"], tz) if "DTEND" in ev else None
            status = ev.get("STATUS", ("", {}))[0].upper()
            if start and end and status != "CANCELLED":
                shifts.append(Shift(
                    title=_unescape(ev.get("SUMMARY", ("", {}))[0]) or "Vagt",
                    start=start,
                    end=end,
                    location=_unescape(ev.get("LOCATION", ("", {}))[0]),
                    description=_unescape(ev.get("DESCRIPTION", ("", {}))[0]),
                    uid=ev.get("UID", ("", {}))[0],
                ))
            ev = None
        elif ev is not None and ":" in line:
            head, value = line.split(":", 1)
            name, *raw_params = head.split(";")
            params = dict(p.split("=", 1) for p in raw_params if "=" in p)
            ev.setdefault(name.upper(), (value.strip(), params))
    return shifts


# --- Lessor Workforce ----------------------------------------------------------------------

class LessorSession:
    """Headless browser session logged in to Lessor Workforce (company + username + password)."""

    def __init__(self, cfg: LessorConfig, password: str, state_path: Path, headless: bool = True):
        self.cfg = cfg
        self.password = password
        self.state_path = state_path
        self.headless = headless

    def __enter__(self):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        storage = str(self.state_path) if self.state_path.exists() else None
        self._context = self._browser.new_context(
            storage_state=storage, locale="da-DK", viewport={"width": 1600, "height": 1000}
        )
        self.page = self._context.new_page()
        self._login()
        return self

    def __exit__(self, *exc):
        try:
            self._context.storage_state(path=str(self.state_path))
        finally:
            self._browser.close()
            self._pw.stop()

    def _settle(self):
        try:
            self.page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass  # some pages keep polling; the content is there anyway

    def _login(self):
        # workforce.lessor.dk redirects to a Keycloak login page on auth.lessor.dk.
        page = self.page
        page.goto(self.cfg.url, wait_until="domcontentloaded", timeout=60_000)
        self._settle()
        if "auth.lessor.dk" not in page.url:
            log.info("Allerede logget ind (gemt session)")
            return
        company = page.locator("#autocompleteCompany")
        company.fill(self.cfg.company)
        company.press("Escape")  # close the autocomplete dropdown
        page.locator("#username").fill(self.cfg.username)
        page.locator("#password").fill(self.password)
        page.locator("button[type=submit]").click()
        try:
            page.wait_for_url(lambda u: "auth.lessor.dk" not in u, timeout=30_000)
        except Exception:
            shot = self.state_path.parent / "lessor_login_failed.png"
            page.screenshot(path=str(shot), full_page=True)
            raise LoginError(
                f"Login til Lessor fejlede – tjek virksomhed/brugernavn/adgangskode. Skærmbillede: {shot}"
            ) from None
        self._settle()
        log.info("Logget ind på Lessor Workforce")

    def _open_profile(self):
        # "Profil" lives in the user menu under the avatar; it's in the DOM but hidden.
        self.page.get_by_role("button", name="Profil").first.dispatch_event("click")
        self._settle()

    def _find_ical_url(self, timeout_ms: int = 30_000) -> str | None:
        for _ in range(timeout_ms // 1000):
            for frame in self.page.frames:
                try:
                    m = ICAL_URL_RE.search(frame.content())
                except Exception:
                    continue  # frame navigated away mid-read
                if m:
                    return m.group(0).replace("&amp;", "&")
            self.page.wait_for_timeout(1000)
        return None

    def discover(self, out_dir: Path):
        """Save the pages the sync reads from, for debugging when Lessor changes its layout."""
        out_dir.mkdir(parents=True, exist_ok=True)
        for name, open_page in [("start", None), ("profil", self._open_profile)]:
            if open_page:
                open_page()
                self.page.wait_for_timeout(3000)
            self.page.screenshot(path=str(out_dir / f"{name}.png"), full_page=True)
            for i, frame in enumerate(self.page.frames):
                (out_dir / f"{name}_frame{i}.html").write_text(frame.content(), encoding="utf-8")
        found = bool(self._find_ical_url(5_000))
        log.info("Gemte Lessor discovery-filer i %s (iCal-link fundet: %s)", out_dir, found)

    def ical_url(self) -> str:
        """The personal iCal feed URL from the profile page."""
        self._open_profile()
        url = self._find_ical_url()
        if not url:
            shot = self.state_path.parent / "lessor_profile_failed.png"
            self.page.screenshot(path=str(shot), full_page=True)
            raise RuntimeError(
                f"Fandt ikke iCal-linket under Profil i Lessor (er 'Vagter' sat til ved iCal?). Skærmbillede: {shot}"
            )
        return url


def fetch_feed(url: str) -> str:
    with urllib.request.urlopen(url, timeout=30) as resp:
        text = resp.read().decode("utf-8-sig")
    if "BEGIN:VCALENDAR" not in text:
        raise ValueError("ikke en kalender")
    return text


# --- Google Kalender -----------------------------------------------------------------------

def shift_body(shift: Shift, cfg: LessorConfig) -> dict:
    body = {
        "summary": shift.title,
        "location": shift.location,
        "description": shift.description,
        "start": {"dateTime": shift.start.isoformat(), "timeZone": cfg.timezone},
        "end": {"dateTime": shift.end.isoformat(), "timeZone": cfg.timezone},
        "extendedProperties": {"private": {SYNC_TAG: "1", KEY_PROP: shift.key}},
    }
    if cfg.color_id:
        body["colorId"] = cfg.color_id
    return body


def sync_shifts(service, cfg: LessorConfig, shifts: list[Shift], window_start: datetime, window_end: datetime) -> SyncResult:
    """Make the shift events in [window_start, window_end) mirror `shifts`.

    Only events tagged as Lessor shifts are touched. A shift overlapping an event already in
    the calendar (e.g. one you entered by hand) is left out, so it doesn't show up twice.
    """
    events = _list_events(service, cfg.calendar_id, window_start, window_end)
    busy = _busy_slots(events, SYNC_TAG)
    desired: dict[str, dict] = {}
    result = SyncResult()
    for s in shifts:
        if not (s.end > window_start and s.start < window_end):
            continue
        if any(s.start < b_end and s.end > b_start for b_start, b_end in busy):
            result.overlapping += 1
            continue
        desired[s.key] = shift_body(s, cfg)
    apply_desired(service, cfg.calendar_id, events, desired, result, SYNC_TAG, KEY_PROP)
    log.info("Google Kalender (vagter): %s", result)
    return result


# --- Program -------------------------------------------------------------------------------

def setup_logging(data_dir: Path):
    handlers = [RotatingFileHandler(data_dir / "lessor.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")]
    if sys.stdout is not None:  # pythonw has no console
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)


def get_password(username: str) -> str:
    password = os.environ.get("LESSOR_PASSWORD") or keyring.get_password(KEYRING_SERVICE, username)
    if not password:
        raise SystemExit(
            "Ingen Lessor-adgangskode. Skriv den i .env (LESSOR_PASSWORD=...) eller kør: python -m ludus_sync.lessor setup"
        )
    return password


def fetch_shifts_ics(cfg: LessorConfig, data_dir: Path) -> str:
    """The iCal feed, via the link saved from an earlier run; log in only to (re)find the link."""
    url_cache = data_dir / "lessor_ical_url.txt"
    if url_cache.exists():
        try:
            return fetch_feed(url_cache.read_text(encoding="utf-8").strip())
        except Exception:
            log.warning("Gemt iCal-link virker ikke længere – henter et nyt fra Lessor")
            url_cache.unlink()
    with LessorSession(cfg, get_password(cfg.username), data_dir / "lessor_state.json") as s:
        url = s.ical_url()
    text = fetch_feed(url)
    url_cache.write_text(url, encoding="utf-8")
    log.info("Gemte iCal-link fra Lessor")
    return text


def cmd_setup(cfg: LessorConfig, data_dir: Path):
    password = os.environ.get("LESSOR_PASSWORD")
    if password:
        print("Bruger adgangskoden fra .env.")
    else:
        password = getpass.getpass(f"Lessor-adgangskode for {cfg.username} ({cfg.company}): ")
        keyring.set_password(KEYRING_SERVICE, cfg.username, password)
        print("Adgangskode gemt i Windows Credential Manager.")
    with LessorSession(cfg, password, data_dir / "lessor_state.json"):
        print("Login til Lessor virker.")
    build_service(PROJECT_DIR / "credentials.json", data_dir / "google_token.json", interactive=True)
    print("Google Kalender er forbundet.")


def cmd_discover(cfg: LessorConfig, data_dir: Path, headed: bool):
    out = data_dir / "discover_lessor"
    with LessorSession(cfg, get_password(cfg.username), data_dir / "lessor_state.json", headless=not headed) as s:
        s.discover(out)
    print(f"Filer gemt i {out}")


def cmd_sync(cfg: LessorConfig, data_dir: Path):
    from .__main__ import wait_for_network

    wait_for_network(urlparse(cfg.url).hostname, cfg.network_wait_seconds)
    tz = ZoneInfo(cfg.timezone)
    shifts = parse_ics(fetch_shifts_ics(cfg, data_dir), tz)
    log.info("Hentede %d vagter fra Lessor", len(shifts))
    service = build_service(PROJECT_DIR / "credentials.json", data_dir / "google_token.json", interactive=False)

    last_calendar_file = data_dir / "lessor_last_calendar.txt"
    last_calendar = last_calendar_file.read_text().strip() if last_calendar_file.exists() else cfg.calendar_id
    if last_calendar != cfg.calendar_id:
        remove_synced_events(service, last_calendar, SYNC_TAG)

    start = datetime.combine(date.today(), datetime.min.time(), tz)
    sync_shifts(service, cfg, shifts, start, start + timedelta(weeks=cfg.weeks_ahead))
    last_calendar_file.write_text(cfg.calendar_id)


def main():
    parser = argparse.ArgumentParser(prog="ludus_sync.lessor", description="Synkroniser Lessor-vagter til Google Kalender")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup", help="Gem Lessor-adgangskode og forbind Google Kalender")
    disc = sub.add_parser("discover", help="Gem Lessor-sider til fejlsøgning")
    disc.add_argument("--headed", action="store_true", help="Vis browservinduet")
    sub.add_parser("sync", help="Synkroniser nu")
    args = parser.parse_args()

    data_dir = ensure_data_dir()
    setup_logging(data_dir)
    load_dotenv()
    cfg = load_lessor_config()
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
