"""Demo data: Margaret, 79, lives alone; daughter Anna is the primary contact. Two weeks of history so the dashboard has a story,
plus the things that make a check-in interesting today: a message from Anna, a question she wants asked, an appointment, and Anna away next week."""
from __future__ import annotations
import os, datetime as dt, json, random
from . import db, core


def run() -> None:
    db.reset()
    pid = db.execute("INSERT INTO persons(name, nickname, timezone, window_start, window_end, notes, created_at) VALUES (?,?,?,?,?,?,?)",
                     ("Margaret Hale", "Margaret", "America/New_York", "08:00", "11:00",
                      "Lives alone in Columbus. Knee replacement in March. Likes to talk about her garden and the Reds.", db.now_iso()))
    ids = {}
    family_email = os.environ.get("HEARTH_FAMILY_EMAIL", "")          # a real inbox for the demo family, when configured
    for name, rel, ch, addr, pr in [("Anna Hale", "daughter", "email" if family_email else "dashboard", family_email, 1),
                                    ("Tom Reilly", "neighbor", "email" if family_email else "dashboard", family_email, 2), ("David Hale", "son", "dashboard", "", 3)]:
        ids[name] = db.execute("INSERT INTO contacts(person_id, name, relation, channel, address, priority) VALUES (?,?,?,?,?,?)", (pid, name, rel, ch, addr, pr))
    for m in ("Lisinopril 10mg", "Metformin 500mg"):
        db.execute("INSERT INTO medications(person_id, name, schedule) VALUES (?,?,?)", (pid, m, "morning"))
    p = db.person(pid)
    rng = random.Random(7)
    today = dt.date.fromisoformat(core.today_str(p))
    story = {  # a few notable days
        13: dict(mood=2, sleep=2, meds_taken=1, ate=1, concern="My knee kept me up most of the night.", flags=["pain", "no_sleep"]),
        9: dict(mood=3, sleep=3, meds_taken=0, ate=1, concern="I forgot my pills, I'll take them now.", flags=["skipped_meds"]),
        4: dict(mood=2, sleep=3, meds_taken=1, ate=0, concern="Just feeling a bit lonely today, it's Frank's birthday.", flags=["lonely", "no_food"]),
        2: dict(mood=3, sleep=2, meds_taken=1, ate=1, concern="", flags=["no_sleep"]),
        1: dict(mood=3, sleep=2, meds_taken=1, ate=1, concern="", flags=["no_sleep"]),
    }
    for back in range(14, 0, -1):
        d = today - dt.timedelta(days=back)
        base = dict(mood=rng.choice([3, 4, 4, 5]), sleep=rng.choice([3, 4, 4]), meds_taken=1, ate=1, concern="", flags=[], plans=rng.choice(
            ["Watering the tomatoes.", "Church group at two.", "Anna is calling later.", "Crossword and a nap.", "Tom is bringing groceries."]))
        rec = {**base, **story.get(back, {})}
        started = dt.datetime.combine(d, dt.time(8, rng.randint(5, 50)), tzinfo=core.tz(p)).astimezone(dt.timezone.utc)
        rec["risk"] = core.risk_score(rec)
        cid = db.execute("INSERT INTO checkins(person_id, date, started_at, completed_at, mood, sleep, meds_taken, ate, concern, plans, risk, flags, transcript) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (pid, d.isoformat(), started.isoformat(), (started + dt.timedelta(minutes=4)).isoformat(), rec["mood"], rec["sleep"], rec["meds_taken"], rec["ate"],
                          rec["concern"], rec["plans"], rec["risk"], json.dumps(rec["flags"]), "[]"))
        db.update_checkin(cid, summary=core.build_summary(p, {**rec, "flags": rec["flags"]}))
        if back == 9:
            db.execute("INSERT INTO alerts(person_id, created_at, level, reason, detail, status, acknowledged_by, acknowledged_at) VALUES (?,?,?,?,?,?,?,?)",
                       (pid, (started + dt.timedelta(minutes=5)).isoformat(), "watch", "check-in flagged concern", "Margaret said she forgot her pills; she took them during the call.",
                        "acknowledged", "Anna Hale", (started + dt.timedelta(minutes=40)).isoformat()))
    # today's extras
    db.execute("INSERT INTO messages(person_id, direction, from_name, contact_id, kind, transcript, created_at) VALUES (?,?,?,?,?,?,?)",
               (pid, "to_person", "Anna", ids["Anna Hale"], "text", "Hi Mom, it's Anna. I'm in Portugal until the twelfth. Tom's your go-to this week, and I'll call Sunday. Love you.", db.now_iso()))
    db.execute("INSERT INTO questions(person_id, from_name, text, created_at) VALUES (?,?,?,?)", (pid, "Anna", "Did the plumber come about the kitchen tap?", db.now_iso()))
    db.execute("INSERT INTO events(person_id, date, time, title, kind, notes, added_by, remind_day_before, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
               (pid, today.isoformat(), "14:00", "Dr. Patel, cardiology", "appointment", "Tom is driving you, he'll be there at 1:30.", "Anna", 1, db.now_iso()))
    db.execute("INSERT INTO events(person_id, date, time, title, kind, notes, added_by, remind_day_before, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
               (pid, (today + dt.timedelta(days=1)).isoformat(), "10:30", "Hair appointment", "appointment", "", "Margaret", 1, db.now_iso()))
    db.execute("INSERT INTO events(person_id, date, time, title, kind, notes, added_by, remind_day_before, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
               (pid, (today + dt.timedelta(days=3)).isoformat(), "", "Pharmacy refill due", "reminder", "Metformin runs out this week.", "Anna", 0, db.now_iso()))
    db.execute("INSERT INTO away(person_id, contact_id, start_date, end_date, cover_contact_id, note) VALUES (?,?,?,?,?,?)",
               (pid, ids["Anna Hale"], today.isoformat(), (today + dt.timedelta(days=7)).isoformat(), ids["Tom Reilly"], "Portugal"))
    print("seeded demo data for", p["name"])


if __name__ == "__main__":
    run()
