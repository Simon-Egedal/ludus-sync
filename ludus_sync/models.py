from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class LessonKind(Enum):
    NORMAL = "normal"
    STUDY = "study"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


@dataclass
class Lesson:
    title: str
    start: datetime  # timezone-aware
    end: datetime
    room: str = ""
    teacher: str = ""
    # Topic text Ludus shows next to the time, e.g. "Aftaleret".
    note: str = ""
    homework: list[str] = field(default_factory=list)
    # Raw status/label text Ludus shows on the module, e.g. "Aflyst" or "Studietid".
    status_text: str = ""
    # Ludus' own id for the module, if the page exposes one.
    ludus_id: str = ""

    @property
    def key(self) -> str:
        """Stable identifier used to match the lesson with its Google event."""
        if self.ludus_id:
            return f"id:{self.ludus_id}"
        raw = f"{self.start.isoformat()}|{self.title.strip().lower()}"
        return "h:" + hashlib.sha1(raw.encode()).hexdigest()[:16]


def classify(
    lesson: Lesson, cancelled_keywords: list[str], study_keywords: list[str], skip_keywords: list[str] = ()
) -> LessonKind:
    haystack = f"{lesson.title} {lesson.status_text}".lower()
    if any(k.lower() in haystack for k in skip_keywords):
        return LessonKind.SKIPPED
    if any(k.lower() in haystack for k in cancelled_keywords):
        return LessonKind.CANCELLED
    if any(k.lower() in haystack for k in study_keywords):
        return LessonKind.STUDY
    return LessonKind.NORMAL
