"""The app's local time zone (tech-design §3.1, PRD §10.3).

The server clock may run in UTC (WSL does), but the household's calendar is
India's. Any "today" that a person would recognise comes from here, never from
date.today(), which on a UTC server is still yesterday until 05:30 in India.
"""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("Asia/Kolkata")


def local_today(now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).astimezone(LOCAL_TZ).date()
