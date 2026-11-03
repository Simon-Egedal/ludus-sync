from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import Config
from .models import Lesson, LessonKind, classify

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
SYNC_TAG = "ludus_sync"


def build_service(credentials_path: Path, token_path: Path, interactive: bool):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid:
        if not interactive:
            raise SystemExit("Google-login mangler eller er udløbet. Kør: python -m ludus_sync setup")
        if not credentials_path.exists():
            raise SystemExit(f"Mangler {credentials_path} (OAuth-klient fra Google Cloud Console). Se README.")
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
        creds = flow.run_local_server(port=0)
    token_path.write_text(creds.to_json())
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def event_body(lesson: Lesson, kind: LessonKind, cfg: Config) -> dict:
    # Ludus' homework text already carries its own "Emne:"/"Lektier:" headings.
    parts = list(lesson.homework)
    if lesson.note and not any(lesson.note in h for h in lesson.homework):
        parts.insert(0, f"Emne: {lesson.note}")
    if lesson.teacher:
        parts.append(f"Lærer: {lesson.teacher}")

    summary = lesson.title
    if kind is LessonKind.STUDY and not any(k.lower() in summary.lower() for k in cfg.study_keywords):
        summary = f"Studietime · {summary}"

    body = {
        "summary": summary,
        "location": lesson.room,
        "description": "\n\n".join(parts),
        "start": {"dateTime": lesson.start.isoformat(), "timeZone": cfg.timezone},
        "end": {"dateTime": lesson.end.isoformat(), "timeZone": cfg.timezone},
        "extendedProperties": {"private": {SYNC_TAG: "1", "ludus_key": lesson.key}},
    }
    color = cfg.study_color_id if kind is LessonKind.STUDY else cfg.normal_color_id
    if color:
        body["colorId"] = color
    return body


def _needs_update(existing: dict, desired: dict) -> bool:
    for field in ("summary", "location", "description", "colorId"):
        if (existing.get(field) or "") != (desired.get(field) or ""):
            return True
    for field in ("start", "end"):
        have = datetime.fromisoformat(existing[field]["dateTime"])
        want = datetime.fromisoformat(desired[field]["dateTime"])
        if have != want:
            return True
    return False


def _list_events(service, calendar_id: str, start: datetime | None, end: datetime | None, **filters) -> list[dict]:
    params = {"timeMin": start.isoformat()} if start else {}
    if end:
        params["timeMax"] = end.isoformat()
    events, page_token = [], None
    while True:
        resp = service.events().list(
            calendarId=calendar_id,
            singleEvents=True,
            showDeleted=False,
            maxResults=250,
            pageToken=page_token,
            **params,
            **filters,
        ).execute()
        events.extend(resp.get("items", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            return events


def _is_synced(ev: dict) -> bool:
    return ev.get("extendedProperties", {}).get("private", {}).get(SYNC_TAG) == "1"


def remove_synced_events(service, calendar_id: str) -> int:
    """Delete every event this tool ever created in `calendar_id` (used when switching calendars)."""
    events = _list_events(service, calendar_id, None, None, privateExtendedProperty=f"{SYNC_TAG}=1")
    events = [ev for ev in events if _is_synced(ev)]
    for ev in events:
        service.events().delete(calendarId=calendar_id, eventId=ev["id"]).execute()
    log.info("Fjernede %d gamle Ludus-begivenheder fra %s", len(events), calendar_id)
    return len(events)


def _busy_slots(events: list[dict]) -> list[tuple[datetime, datetime]]:
    """Timed events not created by this tool; lessons overlapping them are left out."""
    return [
        (datetime.fromisoformat(ev["start"]["dateTime"]), datetime.fromisoformat(ev["end"]["dateTime"]))
        for ev in events
        if not _is_synced(ev) and "dateTime" in ev.get("start", {}) and ev.get("status") != "cancelled"
    ]


@dataclass
class SyncResult:
    created: int = 0
    updated: int = 0
    deleted: int = 0
    unchanged: int = 0
    overlapping: int = 0


def sync(service, cfg: Config, lessons: list[Lesson], window_start: datetime, window_end: datetime) -> SyncResult:
    """Make the synced events in [window_start, window_end) mirror `lessons`.

    Only events this tool created (tagged via extendedProperties) are ever touched.
    Cancelled/skipped lessons, and lessons overlapping an event already in the calendar,
    are left out of the desired set, so any event we made for them earlier gets deleted.
    """
    events = _list_events(service, cfg.calendar_id, window_start, window_end)
    busy = _busy_slots(events)

    desired: dict[str, dict] = {}
    result = SyncResult()
    for lesson in lessons:
        kind = classify(lesson, cfg.cancelled_keywords, cfg.study_keywords, cfg.skip_keywords)
        if kind in (LessonKind.CANCELLED, LessonKind.SKIPPED):
            continue
        if any(lesson.start < b_end and lesson.end > b_start for b_start, b_end in busy):
            result.overlapping += 1
            continue
        desired[lesson.key] = event_body(lesson, kind, cfg)

    existing: dict[str, dict] = {}
    for ev in filter(_is_synced, events):
        key = ev.get("extendedProperties", {}).get("private", {}).get("ludus_key")
        if key in desired and key not in existing:
            existing[key] = ev
        else:
            # Lesson gone/cancelled in Ludus, or a duplicate left over from an earlier run.
            service.events().delete(calendarId=cfg.calendar_id, eventId=ev["id"]).execute()
            result.deleted += 1

    for key, body in desired.items():
        ev = existing.get(key)
        if ev is None:
            service.events().insert(calendarId=cfg.calendar_id, body=body).execute()
            result.created += 1
        elif _needs_update(ev, body):
            service.events().patch(calendarId=cfg.calendar_id, eventId=ev["id"], body=body).execute()
            result.updated += 1
        else:
            result.unchanged += 1

    log.info("Google Kalender: %s", result)
    return result
