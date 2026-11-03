import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ludus_sync.config import Config
from ludus_sync.gcal import remove_synced_events, sync
from ludus_sync.models import Lesson

TZ = ZoneInfo("Europe/Copenhagen")


class FakeEvents:
    def __init__(self, items):
        self.items = {ev["id"]: ev for ev in items}
        self.calls = []
        self._next_id = 100

    def _exec(self, value=None):
        class R:
            def execute(_self):
                return value
        return R()

    def list(self, **kw):
        return self._exec({"items": list(self.items.values())})

    def insert(self, calendarId, body):
        self._next_id += 1
        self.items[str(self._next_id)] = {**body, "id": str(self._next_id)}
        self.calls.append(("insert", body))
        return self._exec()

    def patch(self, calendarId, eventId, body):
        self.items[eventId].update(body)
        self.calls.append(("patch", eventId))
        return self._exec()

    def delete(self, calendarId, eventId):
        del self.items[eventId]
        self.calls.append(("delete", eventId))
        return self._exec()


class FakeService:
    def __init__(self, items=()):
        self._events = FakeEvents(items)

    def events(self):
        return self._events


def lesson(title, hour, **kw):
    start = datetime(2026, 11, 3, hour, tzinfo=TZ)
    return Lesson(title=title, start=start, end=start + timedelta(minutes=95), **kw)


WINDOW = (datetime(2026, 11, 2, tzinfo=TZ), datetime(2026, 11, 9, tzinfo=TZ))


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(ludus_url="https://x.ludus.dk", username="u")

    def test_creates_lessons_with_study_purple_and_homework_note(self):
        svc = FakeService()
        lessons = [
            lesson("Matematik A", 8, homework=["Læs s. 12-20"], room="B12"),
            lesson("Studietid", 10),
        ]
        res = sync(svc, self.cfg, lessons, *WINDOW)
        self.assertEqual(res.created, 2)
        math, study = [body for _, body in svc.events().calls]
        self.assertIn("Læs s. 12-20", math["description"])
        self.assertNotIn("colorId", math)
        self.assertEqual(study["colorId"], "3")
        self.assertEqual(study["summary"], "Studietid")

    def test_cancelled_lesson_is_removed(self):
        svc = FakeService()
        sync(svc, self.cfg, [lesson("Dansk", 8)], *WINDOW)
        res = sync(svc, self.cfg, [lesson("Dansk", 8, status_text="Aflyst")], *WINDOW)
        self.assertEqual(res.deleted, 1)
        self.assertEqual(svc.events().items, {})

    def test_rerun_is_idempotent_and_homework_change_patches(self):
        svc = FakeService()
        sync(svc, self.cfg, [lesson("Fysik", 12)], *WINDOW)
        again = sync(svc, self.cfg, [lesson("Fysik", 12)], *WINDOW)
        self.assertEqual((again.created, again.updated, again.unchanged), (0, 0, 1))
        changed = sync(svc, self.cfg, [lesson("Fysik", 12, homework=["Opgave 3"])], *WINDOW)
        self.assertEqual(changed.updated, 1)

    def test_skip_keywords_are_left_out(self):
        svc = FakeService()
        res = sync(svc, self.cfg, [lesson("Frivillig lektiecafe Matematik", 13)], *WINDOW)
        self.assertEqual(res.created, 0)

    def test_lesson_overlapping_existing_event_is_not_added(self):
        manual = {"id": "m1", "summary": "Matematik (manuel)",
                  "start": {"dateTime": "2026-11-03T08:30:00+01:00"},
                  "end": {"dateTime": "2026-11-03T09:30:00+01:00"}}
        all_day = {"id": "m2", "summary": "Lukkedag", "start": {"date": "2026-11-03"}, "end": {"date": "2026-11-04"}}
        svc = FakeService([manual, all_day])
        res = sync(svc, self.cfg, [lesson("2L MAT", 8), lesson("2L DA", 10)], *WINDOW)
        self.assertEqual((res.created, res.overlapping), (1, 1))
        self.assertIn("m1", svc.events().items)
        self.assertIn("m2", svc.events().items)

    def test_previously_synced_lesson_removed_when_overlap_appears(self):
        svc = FakeService()
        sync(svc, self.cfg, [lesson("2L MAT", 8)], *WINDOW)
        svc.events().items["m1"] = {"id": "m1", "summary": "Tandlæge",
                                    "start": {"dateTime": "2026-11-03T08:00:00+01:00"},
                                    "end": {"dateTime": "2026-11-03T08:30:00+01:00"}}
        res = sync(svc, self.cfg, [lesson("2L MAT", 8)], *WINDOW)
        self.assertEqual((res.deleted, list(svc.events().items)), (1, ["m1"]))

    def test_remove_synced_events_only_touches_own_events(self):
        svc = FakeService([{"id": "mine", "summary": "Fødselsdag"}])
        sync(svc, self.cfg, [lesson("2L MAT", 8)], *WINDOW)
        self.assertEqual(remove_synced_events(svc, "primary"), 1)
        self.assertEqual(list(svc.events().items), ["mine"])


if __name__ == "__main__":
    unittest.main()
