"""PRD §10.1 tunables, read from the settings table (seeded by migration 0001).

A user's own row (user_id = theirs) wins over the app-level row (user_id = '').
"""

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from hisaab.models import Setting


def tunable(session: Session, key: str, user_id: str = "") -> Any:
    rows: dict[str, str] = dict(session.execute(
        select(Setting.user_id, Setting.value).where(Setting.key == key, Setting.user_id.in_(("", user_id)))
    ).tuples().all())
    raw: str | None = rows.get(user_id) or rows.get("")
    if raw is None:
        raise KeyError(f"no setting {key!r}")
    return json.loads(raw)
