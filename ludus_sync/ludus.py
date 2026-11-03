from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from .config import Config
from .models import Lesson

log = logging.getLogger(__name__)

# Collects every block in the "Mit skema" week view. Each block is absolutely positioned in
# its day column (left: 0%, 20%, ... of the row) and tagged with a class naming its type:
# ugeskema-skemabrik-element (lesson), -aendret-lektion- (changed), -aflyst-lektion- (cancelled)
# and -begivenhed- (event, e.g. "Frivillig lektiecafe").
WEEK_JS = r"""() => {
  const days = [...document.querySelectorAll('.v-label')].map(l => l.innerText.trim())
    .filter(t => /^(Mandag|Tirsdag|Onsdag|Torsdag|Fredag|Lørdag|Søndag) \d\d\.\d\d$/.test(t));
  const blocks = [...document.querySelectorAll('[class*="v-absolutelayout-wrapper-ugeskema-"]')].map(w => {
    const kind = [...w.classList].find(c => c.startsWith('v-absolutelayout-wrapper-ugeskema-'))
      .replace('v-absolutelayout-wrapper-ugeskema-', '').replace('-element', '');
    const el = w.firstElementChild;
    return {
      kind,
      left: w.style.left,
      right: w.style.right,
      labels: [...el.querySelectorAll('.v-label')].map(l => l.innerText.trim()),
      text: el.innerText.trim(),
    };
  });
  return {date: document.querySelector('.v-datefield-textfield').value, days, blocks};
}"""

HOMEWORK_ROWS_JS = r"""() => [...document.querySelectorAll('.v-table-body tr')]
  .map(r => [...r.querySelectorAll('td')].slice(0, 4).map(td => td.innerText.trim()))"""

TIME_RE = re.compile(r"^(\d{1,2}):(\d{2}) - (\d{1,2}):(\d{2})$")


class LoginError(RuntimeError):
    pass


class LudusSession:
    """Headless browser session logged in to Ludus Web with username/password."""

    def __init__(self, cfg: Config, password: str, state_path: Path, headless: bool = True):
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

    def _open(self, url: str):
        # Ludus Web is a Vaadin 8 app that keeps a connection open, so "networkidle" never
        # fires. Instead ask Vaadin itself whether it is done talking to the server.
        self.page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        self._wait_vaadin_idle()

    def _wait_vaadin_idle(self, timeout: int = 30_000):
        self.page.wait_for_function(
            """() => {
                const clients = window.vaadin && window.vaadin.clients;
                if (!clients || !Object.keys(clients).length) return false;
                return Object.values(clients).every(c => !c.isActive());
            }""",
            timeout=timeout,
        )

    def _click(self, locator):
        locator.click()
        self._wait_vaadin_idle()

    def _menu(self, name: str):
        self._click(self.page.locator(".v-menubar-menuitem", has_text=name).first)

    def _login(self):
        # The login fields sit in the top bar and logging in swaps the UI over AJAX
        # without a page load, so we wait on the fields, not on navigation.
        # The site root serves the old Echo UI once a session cookie exists, so go straight
        # to the Vaadin UI to always get the same page.
        page = self.page
        self._open(urljoin(self.cfg.ludus_url, "/ui/main"))
        password_field = page.locator("#kodeordField")
        if not password_field.is_visible():
            log.info("Allerede logget ind (gemt session)")
            return
        user_field = page.locator("#brugerIdField")
        user_field.click()
        user_field.press_sequentially(self.cfg.username)
        password_field.click()
        password_field.press_sequentially(self.password)
        password_field.press("Enter")
        try:
            password_field.wait_for(state="hidden", timeout=20_000)
        except Exception:
            shot = self.state_path.parent / "login_failed.png"
            page.screenshot(path=str(shot), full_page=True)
            raise LoginError(
                f"Login til Ludus fejlede – tjek brugernavn/adgangskode. Skærmbillede: {shot}"
            ) from None
        self._wait_vaadin_idle()
        log.info("Logget ind på Ludus")

    def discover(self, out_dir: Path):
        """Save the pages the sync reads from, for debugging when Ludus changes its layout."""
        out_dir.mkdir(parents=True, exist_ok=True)
        for name, menu in [("start", None), ("skema", "Skemaer"), ("lektier", "Lektier")]:
            if menu:
                self._menu(menu)
            (out_dir / f"{name}.html").write_text(self.page.content(), encoding="utf-8")
            self.page.screenshot(path=str(out_dir / f"{name}.png"), full_page=True)
        self._menu("Skemaer")
        (out_dir / "skema_raw.json").write_text(
            json.dumps(self.page.evaluate(WEEK_JS), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log.info("Gemte discovery-filer i %s", out_dir)

    def fetch_lessons(self, start: date, weeks: int) -> list[Lesson]:
        tz = ZoneInfo(self.cfg.timezone)
        monday = start - timedelta(days=start.weekday())
        lessons = self._fetch_schedule(monday, weeks, tz)
        homework = self._fetch_homework(monday, monday + timedelta(weeks=weeks, days=-1))
        attach_homework(lessons, homework)
        log.info("Hentede %d moduler og %d lektier fra Ludus", len(lessons), len(homework))
        return lessons

    def _fetch_schedule(self, monday: date, weeks: int, tz: ZoneInfo) -> list[Lesson]:
        self._menu("Skemaer")
        prev = self.page.locator('.v-button:has(img[src$="arrow-left.png"])').first
        next_ = self.page.locator('.v-button:has(img[src$="arrow-right.png"])').first

        # The view opens on the current week; step until it shows the week we want.
        for _ in range(5):
            shown = week_monday(self.page.evaluate(WEEK_JS)["date"])
            if shown == monday:
                break
            self._click(prev if shown > monday else next_)

        lessons: list[Lesson] = []
        for i in range(weeks):
            if i:
                self._click(next_)
            lessons.extend(parse_week(self.page.evaluate(WEEK_JS), tz))
        return lessons

    def _fetch_homework(self, first: date, last: date) -> list[tuple[date, str, str]]:
        self._menu("Lektier")
        for field, value in zip(self.page.locator(".v-datefield-textfield").all()[:2], (first, last)):
            field.click()
            field.fill(value.strftime("%d-%m-%Y"))
            field.press("Enter")
            self._wait_vaadin_idle()
        self._click(self.page.locator('.v-button:has(img[src$="reload.png"])').first)

        # Vaadin tables only render the rows near the viewport, so scroll through it.
        rows: dict[tuple, list[str]] = {}
        body = self.page.locator(".v-table-body").first
        for _ in range(100):
            for row in self.page.evaluate(HOMEWORK_ROWS_JS):
                rows.setdefault(tuple(row), row)
            at_end = body.evaluate(
                "b => { b.scrollTop += b.clientHeight; return b.scrollTop + b.clientHeight >= b.scrollHeight - 2; }"
            )
            self._wait_vaadin_idle()
            if at_end:
                for row in self.page.evaluate(HOMEWORK_ROWS_JS):
                    rows.setdefault(tuple(row), row)
                break
        return [item for row in rows.values() if (item := parse_homework_row(row))]


def week_monday(date_field: str) -> date:
    """'03-11-26' (the week view's date field) -> Monday of that week."""
    d = datetime.strptime(date_field, "%d-%m-%y").date()
    return d - timedelta(days=d.weekday())


def _day_date(day_label: str, ref: date) -> date:
    """'Tirsdag 03.11' -> date, taking the year from `ref` and handling the new-year week."""
    day, month = map(int, day_label.split()[1].split("."))
    year = ref.year
    if month == 1 and ref.month == 12:
        year += 1
    elif month == 12 and ref.month == 1:
        year -= 1
    return date(year, month, day)


def _pct(value: str | None) -> float | None:
    value = (value or "").strip()
    return float(value.rstrip("%")) if value.endswith("%") else None


def _day_index(block: dict, column_width: float, n_days: int) -> int:
    """Which day column a block sits in.

    When blocks share a day (e.g. an event next to a lesson) Ludus splits the column, so a
    block can start mid-column (left: 30% on Tuesday = 20-40%). Use the block's centre, which
    always lies inside its own day, rather than rounding its left edge.
    """
    left = _pct(block.get("left")) or 0.0
    right = _pct(block.get("right"))
    # Ludus rounds positions to whole percents, so nudge the left edge if right is missing.
    centre = (left + (100 - right)) / 2 if right is not None else left + 1
    return min(int(centre // column_width), n_days - 1)


def parse_week(raw: dict, tz: ZoneInfo) -> list[Lesson]:
    ref = datetime.strptime(raw["date"], "%d-%m-%y").date()
    days = [_day_date(d, ref) for d in raw["days"]]
    if not days:
        return []
    column_width = 100 / len(days)

    lessons = []
    for block in raw["blocks"]:
        labels = block["labels"]
        t = next((i for i, l in enumerate(labels) if TIME_RE.match(l)), None)
        if t is None:
            log.warning("Sprang over skemabrik uden tidspunkt: %s", block)
            continue
        h1, m1, h2, m2 = map(int, TIME_RE.match(labels[t]).groups())
        day = days[_day_index(block, column_width, len(days))]

        if block["kind"] == "begivenhed":
            # Events have the title as a link instead of a label: [time, room]
            title = block["text"].splitlines()[0].strip()
            note, room, teacher = "", (labels[t + 1:] or [""])[0], ""
        else:
            # Lessons: [title, time, note, room, teacher]
            title = labels[0]
            note, room, teacher = (labels[t + 1:t + 4] + ["", "", ""])[:3]

        status = ""
        if "aflyst" in block["kind"]:
            status = "Aflyst"
        if room.lower().startswith("studietime"):
            status = status or "Studietime"
            room = room[len("studietime"):].strip()

        lessons.append(Lesson(
            title=title,
            start=datetime.combine(day, time(h1, m1), tz),
            end=datetime.combine(day, time(h2, m2), tz),
            room=room,
            teacher=teacher,
            note=note,
            status_text=status,
        ))
    return lessons


def parse_homework_row(row: list[str]) -> tuple[date, str, str] | None:
    """[Dato, Ugedag, Fag, Lektier] -> (date, subject, text).

    The Fag cell is "2L MAT", or "13.11.26: 2L MAT\\nAflevering" for hand-ins.
    """
    if len(row) < 4 or not row[3].strip():
        return None
    try:
        day = datetime.strptime(row[0], "%d.%m.%y").date()
    except ValueError:
        return None
    fag_lines = row[2].splitlines()
    subject = re.sub(r"^\d\d\.\d\d\.\d\d:\s*", "", fag_lines[0]).strip()
    text = re.sub(r"\n{3,}", "\n\n", row[3].strip())
    if any(l.strip().lower() == "aflevering" for l in fag_lines[1:]):
        text = f"Aflevering:\n{text}"
    return day, subject, text


def attach_homework(lessons: list[Lesson], homework: list[tuple[date, str, str]]):
    """Put each homework note on the first module of that subject on that day."""
    by_slot: dict[tuple[date, str], Lesson] = {}
    for lesson in sorted(lessons, key=lambda l: l.start):
        by_slot.setdefault((lesson.start.date(), lesson.title.lower()), lesson)
    for day, subject, text in homework:
        lesson = by_slot.get((day, subject.lower()))
        if lesson:
            lesson.homework.append(text)
        else:
            log.info("Ingen modul at sætte lektie på: %s %s", day, subject)


def window_bounds(start: date, weeks: int, tz: ZoneInfo) -> tuple[datetime, datetime]:
    monday = start - timedelta(days=start.weekday())
    begin = datetime.combine(monday, datetime.min.time(), tz)
    return begin, begin + timedelta(weeks=weeks)
