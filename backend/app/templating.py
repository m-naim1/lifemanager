"""Jinja2 setup and template helpers for the server-rendered UI."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import markdown as markdown_lib
from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).with_name("templates")
STATIC_DIR = Path(__file__).with_name("static")

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

_md = markdown_lib.Markdown(extensions=["extra", "sane_lists", "nl2br"])


def render_markdown(source: str | None) -> str:
    if not source:
        return ""
    _md.reset()
    return _md.convert(source)


def _to_date(value: str | date | None) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def format_date(value: str | date | None) -> str:
    d = _to_date(value)
    return f"{d.strftime('%b')} {d.day}, {d.year}" if d else ""


def format_short(value: str | date | None) -> str:
    d = _to_date(value)
    return f"{d.strftime('%b')} {d.day}" if d else ""


def format_day_long(value: str | date | None) -> str:
    d = _to_date(value)
    return f"{d.strftime('%A')}, {d.strftime('%B')} {d.day}, {d.year}" if d else ""


def month_label(year: int, month: int) -> str:
    return date(year, month, 1).strftime("%B %Y")


def format_number(value: float | int | None) -> str:
    if value is None:
        return "—"
    text = f"{float(value):,.2f}".rstrip("0").rstrip(".")
    return text or "0"


def greeting(hour: int | None = None) -> str:
    h = datetime.now().hour if hour is None else hour
    if h < 5:
        return "Up late"
    if h < 12:
        return "Good morning"
    if h < 18:
        return "Good afternoon"
    return "Good evening"


templates.env.filters["markdown"] = render_markdown
templates.env.filters["date"] = format_date
templates.env.filters["short_date"] = format_short
templates.env.filters["day_long"] = format_day_long
templates.env.filters["number"] = format_number
WEEKDAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
HEATMAP_DAYS = ("Sun", "", "Tue", "", "Thu", "", "Sat")

templates.env.globals.update(
    format_date=format_date,
    format_short=format_short,
    format_day_long=format_day_long,
    format_number=format_number,
    month_label=month_label,
    greeting=greeting,
    weekday_labels=WEEKDAY_LABELS,
    heatmap_days=HEATMAP_DAYS,
    int=int,
    len=len,
)
