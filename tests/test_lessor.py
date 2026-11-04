import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from ludus_sync.config import Config, LessorConfig
from ludus_sync.gcal import sync
from ludus_sync.lessor import parse_ics, sync_shifts
from tests.test_sync import WINDOW, FakeService, lesson

TZ = ZoneInfo("Europe/Copenhagen")

ICS = "\r\n".join([
    "BEGIN:VCALENDAR",
    "VERSION:2.0",
    "BEGIN:VEVENT",
    "UID:vagt-1@lessor",
    "DTSTART:20261103T070000Z",
    "DTEND:20261103T110000Z",
    "SUMMARY:Kasse",
    "LOCATION:Butik\\, Vejle",
    "DESCRIPTION:Pause 30 min\\nHusk",
    "  nøgle",
    "END:VEVENT",
    "BEGIN:VEVENT",
    "UID:vagt-2@lessor",
    "DTSTART;TZID=Europe/Copenhagen:20261105T160000",
    "DTEND;TZID=Europe/Copenhagen:20261105T200000",
    "SUMMARY:Lager",
    "END:VEVENT",
    "BEGIN:VEVENT",
    "UID:ferie@lessor",
    "DTSTART;VALUE=DATE:20261106",
    "DTEND;VALUE=DATE:20261107",
    "SUMMARY:Ferie",
    "END:VEVENT",
    "END:VCALENDAR",
])


class ParseIcsTest(unittest.TestCase):
    def test_timed_shifts_only(self):
        kasse, lager = parse_ics(ICS, TZ)
        self.assertEqual(kasse.start, datetime(2026, 11, 3, 8, tzinfo=TZ))
        self.assertEqual((kasse.title, kasse.location), ("Kasse", "Butik, Vejle"))
        self.assertEqual(kasse.description, "Pause 30 min\nHusk nøgle")
        self.assertEqual(lager.end, datetime(2026, 11, 5, 20, tzinfo=TZ))

    def test_not_a_calendar(self):
        with self.assertRaises(ValueError):
            parse_ics("<html>login</html>", TZ)


class SyncShiftsTest(unittest.TestCase):
    def setUp(self):
        self.cfg = LessorConfig(company="firma", username="u")
        self.ludus_cfg = Config(ludus_url="https://x.ludus.dk", username="u")

    def test_create_then_idempotent_then_removed(self):
        svc = FakeService()
        shifts = parse_ics(ICS, TZ)
        self.assertEqual(sync_shifts(svc, self.cfg, shifts, *WINDOW).created, 2)
        self.assertEqual(sync_shifts(svc, self.cfg, shifts, *WINDOW).unchanged, 2)
        res = sync_shifts(svc, self.cfg, shifts[1:], *WINDOW)
        self.assertEqual((res.deleted, res.unchanged), (1, 1))

    def test_new_uids_from_lessor_do_not_recreate_events(self):
        svc = FakeService()
        sync_shifts(svc, self.cfg, parse_ics(ICS, TZ), *WINDOW)
        refetched = parse_ics(ICS.replace("UID:vagt-", "UID:ny-"), TZ)
        res = sync_shifts(svc, self.cfg, refetched, *WINDOW)
        self.assertEqual((res.created, res.deleted, res.unchanged), (0, 0, 2))

    def test_shift_already_in_calendar_is_not_added_twice(self):
        manual = {"id": "m1", "summary": "Vagt (manuel)",
                  "start": {"dateTime": "2026-11-03T08:00:00+01:00"},
                  "end": {"dateTime": "2026-11-03T12:00:00+01:00"}}
        svc = FakeService([manual])
        res = sync_shifts(svc, self.cfg, parse_ics(ICS, TZ), *WINDOW)
        self.assertEqual((res.created, res.overlapping), (1, 1))
        self.assertIn("m1", svc.events().items)

    def test_programs_leave_each_others_events_alone(self):
        svc = FakeService()
        sync_shifts(svc, self.cfg, parse_ics(ICS, TZ), *WINDOW)
        # 08:00 lesson overlaps the 08-12 shift and is left out; the 13:00 one goes in.
        res = sync(svc, self.ludus_cfg, [lesson("2L MAT", 8), lesson("2L DA", 13)], *WINDOW)
        self.assertEqual((res.created, res.overlapping, res.deleted), (1, 1, 0))
        again = sync_shifts(svc, self.cfg, parse_ics(ICS, TZ), *WINDOW)
        self.assertEqual((again.unchanged, again.deleted), (2, 0))
        self.assertEqual(len(svc.events().items), 3)


if __name__ == "__main__":
    unittest.main()
