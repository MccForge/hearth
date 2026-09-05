"""The watchdog: if the check-in window closes with no completed check-in, climb the escalation ladder.
Level 1 (window end): nudge the person (a re-prompt on their device; here, a dashboard event) and open a 'watch' alert.
Level 2 (+30 min): notify the primary contact.  Level 3 (+90 min): notify everyone.
Each level fires at most once per person per day. A completed check-in resolves the open missed alerts (see complete_checkin)."""
from __future__ import annotations
import asyncio, datetime as dt
from . import db, core


def run_once(now: dt.datetime | None = None) -> list[dict]:
    """Evaluate every person once. `now` is injectable for tests and demos. Returns the alerts created."""
    created = []
    for p in db.persons():
        st = core.status(p, now)
        if st["state"] != "overdue" or st["snoozed_until"]:
            continue
        for minutes, level in core.LADDER:
            if st["overdue_minutes"] < minutes:
                break
            reason = f"{level}: no check-in by {p['window_end']}"
            if core.already_alerted(p["id"], reason, st["date"]):
                continue
            who = p.get("nickname") or p["name"]
            detail = {
                "missed_1": f"{who} hasn't checked in yet today (window ended {p['window_end']}). Hearth is re-prompting on the device.",
                "missed_2": f"{who} still hasn't checked in {st['overdue_minutes']} minutes after the window closed. Please try calling.",
                "missed_3": f"No check-in from {who} for {st['overdue_minutes']} minutes past the window. Notifying all contacts. "
                            f"Last known: {_last_known(p['id'], st['date'])}",
            }[level]
            created.append(core.create_alert(p["id"], level, reason, detail))
    return created


def _last_known(person_id: int, today: str) -> str:
    for c in db.recent_checkins(person_id, 3):
        if c["date"] != today and c.get("summary"):
            return f"{c['date']}: {c['summary']}"
    return "no recent check-in on record"


async def watchdog(interval_seconds: int = 60) -> None:
    while True:
        try:
            run_once()
        except Exception as ex:   # never let the watchdog die
            print("watchdog error:", ex)
        await asyncio.sleep(interval_seconds)
