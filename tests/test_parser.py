import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

from ludus_sync.ludus import attach_homework, parse_homework_row, parse_week, week_monday
from ludus_sync.models import LessonKind, classify

TZ = ZoneInfo("Europe/Copenhagen")

# Shape of what WEEK_JS returns from the "Mit skema" view (taken from a real week).
RAW_WEEK = {
    "date": "03-11-26",
    "days": ["Mandag 02.11", "Tirsdag 03.11", "Onsdag 04.11", "Torsdag 05.11", "Fredag 06.11"],
    "blocks": [
        {"kind": "skemabrik", "left": "20%", "labels": ["2L JURA", "08:15 - 09:00", "Aftaleret", "K116", "RADP"],
         "text": "2L JURA\n08:15 - 09:00 Aftaleret\nK116 RADP"},
        {"kind": "aendret-lektion", "left": "0%", "labels": ["2G TYSK4", "12:00 - 12:45", "", "Studietime A135", "MEWI"],
         "text": "2G TYSK4\n12:00 - 12:45\nStudietime A135 MEWI"},
        {"kind": "aflyst-lektion", "left": "60%", "labels": ["2L DA", "12:00 - 12:45", "", "K116", "SUOS"],
         "text": "2L DA\n12:00 - 12:45\nK116 SUOS"},
        {"kind": "begivenhed", "left": "20%", "labels": ["13:40 - 16:00", "D101"],
         "text": "Frivillig lektiecafe Matematik\n13:40 - 16:00\nD101"},
    ],
}

KW = (["aflyst"], ["studietime"])


class ParseWeekTest(unittest.TestCase):
    def setUp(self):
        self.lessons = {l.title: l for l in parse_week(RAW_WEEK, TZ)}

    def test_lesson_fields(self):
        jura = self.lessons["2L JURA"]
        self.assertEqual(jura.start, datetime(2026, 11, 3, 8, 15, tzinfo=TZ))
        self.assertEqual(jura.end, datetime(2026, 11, 3, 9, 0, tzinfo=TZ))
        self.assertEqual((jura.room, jura.teacher, jura.note), ("K116", "RADP", "Aftaleret"))
        self.assertIs(classify(jura, *KW), LessonKind.NORMAL)

    def test_studietime(self):
        tysk = self.lessons["2G TYSK4"]
        self.assertEqual(tysk.start.date(), date(2026, 11, 2))
        self.assertEqual(tysk.room, "A135")
        self.assertIs(classify(tysk, *KW), LessonKind.STUDY)

    def test_cancelled(self):
        da = self.lessons["2L DA"]
        self.assertEqual(da.start.date(), date(2026, 11, 5))
        self.assertIs(classify(da, *KW), LessonKind.CANCELLED)

    def test_event_title_from_text(self):
        cafe = self.lessons["Frivillig lektiecafe Matematik"]
        self.assertEqual((cafe.room, cafe.start.hour, cafe.end.hour), ("D101", 13, 16))

    def test_seven_day_week_columns(self):
        raw = {**RAW_WEEK, "days": RAW_WEEK["days"] + ["Lørdag 07.11", "Søndag 08.11"],
               "blocks": [{**RAW_WEEK["blocks"][0], "left": "57%"}]}
        self.assertEqual(parse_week(raw, TZ)[0].start.date(), date(2026, 11, 6))

    def test_split_day_column_stays_on_its_day(self):
        # Week 47: Tuesday (20-40%) is split between the lektiecafé (20-30%) and VØ (30-40%).
        blocks = [
            {"kind": "begivenhed", "left": "20%", "right": "70%", "labels": ["13:40 - 16:00", "D101"],
             "text": "Frivillig lektiecafe Matematik\n13:40 - 16:00\nD101"},
            {"kind": "skemabrik", "left": "30%", "right": "60%", "labels": ["2L VØ", "13:40 - 14:25", "", "K116", "PERE"],
             "text": ""},
            {"kind": "skemabrik", "left": "40%", "right": "40%", "labels": ["2L MAT", "13:40 - 14:25", "", "K116", "MIHY"],
             "text": ""},
        ]
        raw = {"date": "17-11-26", "days": ["Mandag 16.11", "Tirsdag 17.11", "Onsdag 18.11", "Torsdag 19.11",
                                            "Fredag 20.11"], "blocks": blocks}
        days = {l.title: l.start.date() for l in parse_week(raw, TZ)}
        self.assertEqual(days["2L VØ"], date(2026, 11, 17))
        self.assertEqual(days["2L MAT"], date(2026, 11, 18))
        self.assertEqual(days["Frivillig lektiecafe Matematik"], date(2026, 11, 17))

    def test_new_year_week(self):
        raw = {"date": "30-12-26", "days": ["Mandag 28.12", "Fredag 01.01"],
               "blocks": [{**RAW_WEEK["blocks"][0], "left": "50%"}]}
        self.assertEqual(parse_week(raw, TZ)[0].start.date(), date(2027, 1, 1))

    def test_week_monday(self):
        self.assertEqual(week_monday("05-11-26"), date(2026, 11, 2))


class HomeworkTest(unittest.TestCase):
    def test_rows(self):
        self.assertEqual(
            parse_homework_row(["04.11.26", "Onsdag", "2L MAT", "Lektier:\nBestem f'(x)"]),
            (date(2026, 11, 4), "2L MAT", "Lektier:\nBestem f'(x)"),
        )
        day, subject, text = parse_homework_row(["13.11.26", "Fredag", "13.11.26: 2L MAT\nAflevering", "Opgave"])
        self.assertEqual((day, subject, text), (date(2026, 11, 13), "2L MAT", "Aflevering:\nOpgave"))
        self.assertIsNone(parse_homework_row(["Dato", "Ugedag", "Fag", ""]))

    def test_attached_to_first_module_of_the_day(self):
        lessons = parse_week(RAW_WEEK, TZ)
        second = parse_week({**RAW_WEEK, "blocks": [
            {**RAW_WEEK["blocks"][0], "labels": ["2L JURA", "09:00 - 09:45", "", "K116", "RADP"]}]}, TZ)
        lessons += second
        attach_homework(lessons, [(date(2026, 11, 3), "2L JURA", "Læs kap. 11")])
        jura = sorted((l for l in lessons if l.title == "2L JURA"), key=lambda l: l.start)
        self.assertEqual([j.homework for j in jura], [["Læs kap. 11"], []])


if __name__ == "__main__":
    unittest.main()
