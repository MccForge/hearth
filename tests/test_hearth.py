import datetime as dt, os, tempfile
import pytest

os.environ["HEARTH_DB"] = os.path.join(tempfile.gettempdir(), "hearth_test.db")
os.environ["HEARTH_MEDIA"] = os.path.join(tempfile.gettempdir(), "hearth_test_media")
from hearth import db, core, seed, escalation, agent   # noqa: E402
from hearth.mcp_server import TOOLS, parse_scale, parse_bool  # noqa: E402


@pytest.fixture(autouse=True)
def fresh():
    seed.run()
    yield


def _today():
    return core.today_str(db.person(1))


def test_flags_and_negation():
    assert core.detect_flags("I fell getting to the bathroom") == ["fall"]
    assert core.detect_flags("No, I am not hurt at all") == []
    assert core.detect_flags("I did not fall") == []
    assert "emergency" in core.detect_flags("I can't get up, help me")
    assert "skipped_meds" in core.detect_flags("I forgot my pills")


def test_parsers():
    assert parse_scale("not great") == 2 and parse_scale("pretty good") == 4 and parse_scale("didn't sleep a wink") == 1
    assert parse_scale("not too bad") == 3 and parse_scale("3") == 3 and parse_scale("blue skies") is None
    assert parse_bool("yes I took them with breakfast") == 1 and parse_bool("not yet") == 0 and parse_bool("I think so") == 1
    assert parse_bool("maybe") is None


def test_fresh_checkin_after_completion_keeps_history():
    cid1 = TOOLS["start_checkin"](1)["checkin_id"]
    TOOLS["record_answer"](cid1, "sleep", "badly", "I fell in the night")
    TOOLS["complete_checkin"](cid1)
    s2 = TOOLS["start_checkin"](1)
    assert s2["resumed"] is False and s2["checkin_id"] != cid1
    assert db.checkin(s2["checkin_id"])["flags"] == []            # no carry-over
    assert db.checkin(cid1)["superseded"] == 1
    assert db.checkin_for(1, _today())["id"] == s2["checkin_id"]


def test_context_carries_messages_questions_events_away_trends():
    ctx = TOOLS["get_checkin_context"](1)
    assert ctx["family_messages"] and ctx["family_messages"][0]["from"] == "Anna"
    assert ctx["questions_from_family"][0]["text"].startswith("Did the plumber")
    assert ctx["events_today"][0]["title"].startswith("Dr. Patel") and ctx["events_today"][0]["time"] == "2 pm"
    assert ctx["events_tomorrow"][0]["title"] == "Hair appointment"
    assert ctx["away"][0]["who"] == "Anna Hale" and ctx["away"][0]["cover"] == "Tom Reilly"
    assert any("Slept badly 2 nights" in i for i in ctx["trends"])


def test_away_contact_is_covered_in_escalation():
    cs = core.contacts_for_level(1, "info")
    assert cs[0]["name"] == "Tom Reilly" and cs[0].get("covering_for") == "Anna Hale"
    everyone = core.contacts_for_level(1, "urgent")
    assert [c["name"] for c in everyone] == ["Tom Reilly", "David Hale"]


def test_full_flow_with_question_and_event_in_summary():
    ctx = TOOLS["get_checkin_context"](1)
    cid = TOOLS["start_checkin"](1)["checkin_id"]
    for f, v in [("mood", "good"), ("sleep", "fine"), ("meds_taken", "yes"), ("ate", "toast"), ("concern", "nothing really")]:
        TOOLS["record_answer"](cid, f, v, v)
    TOOLS["record_answer"](cid, f"event:{ctx['events_today'][0]['id']}", "yes", "Yes, Tom said half past one")
    TOOLS["record_answer"](cid, f"question:{ctx['questions_from_family'][0]['id']}", "yes", "He came Tuesday, it's fixed")
    done = TOOLS["complete_checkin"](cid)
    assert "plumber" in done["summary"] and "Dr. Patel" in done["summary"] and "This week:" in done["summary"]
    assert done["summary_sent_to"] == ["Tom Reilly"]
    assert db.event(ctx["events_today"][0]["id"])["status"] == "done"


def test_family_message_audio_roundtrip_and_reply():
    import base64
    mid = db.execute("INSERT INTO messages(person_id, direction, from_name, kind, transcript, mime, created_at) VALUES (?,?,?,?,?,?,?)",
                     (1, "to_person", "Anna", "voice", "Morning Mom", "audio/wav", db.now_iso()))
    db.execute("UPDATE messages SET audio_path=? WHERE id=?", (db.save_media(mid, b"RIFFfakewav", "audio/wav"), mid))
    blocks = TOOLS["get_family_message"](mid)
    assert blocks[0].type == "audio" and base64.b64decode(blocks[0].data) == b"RIFFfakewav"
    TOOLS["mark_message_played"](mid)
    assert db.message(mid)["status"] == "played"
    r = TOOLS["record_reply"](1, "Tell Anna the tap is fixed", "Anna", base64.b64encode(b"RIFFreply").decode(), "audio/wav")
    assert r["ok"] and db.message(r["message_id"])["direction"] == "to_family"


def test_add_event_and_list():
    r = TOOLS["add_event"](1, (dt.date.today() + dt.timedelta(days=2)).isoformat(), "Dentist", "10:00")
    assert r["ok"]
    titles = [e["title"] for e in TOOLS["list_events"](1, 7)["events"]]
    assert "Dentist" in titles
    assert "error" in TOOLS["add_event"](1, "not-a-date", "x")


def test_request_help_notifies_active_contacts():
    r = TOOLS["request_help"](1, "I can't get up", "urgent")
    assert [n["contact"] for n in r["notified"]] == ["Tom Reilly", "David Hale"]


def test_escalation_ladder_fires_once_per_level():
    p = db.person(1)
    _, end = core.window_bounds(p, _today())
    assert escalation.run_once(end - dt.timedelta(minutes=5)) == []
    a1 = escalation.run_once(end + dt.timedelta(minutes=1)); assert [a["level"] for a in a1] == ["missed_1"]
    assert escalation.run_once(end + dt.timedelta(minutes=2)) == []
    a2 = escalation.run_once(end + dt.timedelta(minutes=45)); assert [a["level"] for a in a2] == ["missed_2"]
    a3 = escalation.run_once(end + dt.timedelta(minutes=120)); assert [a["level"] for a in a3] == ["missed_3"]
    cid = TOOLS["start_checkin"](1)["checkin_id"]
    assert TOOLS["complete_checkin"](cid)["missed_alerts_resolved"] == 3 and db.open_alerts(1) == []


def test_snooze_pauses_ladder():
    p = db.person(1)
    _, end = core.window_bounds(p, _today())
    TOOLS["snooze_checkin"](1, 30)
    assert escalation.run_once(end + dt.timedelta(minutes=10)) == []


def test_scripted_agent_full_conversation():
    s = agent.start(1)
    assert "Margaret" in s["agent"] and "Anna" in s["agent"] and "Tom" in s["agent"]      # message + away notice
    names = [c["tool"] for c in s["tool_calls"]]
    assert "get_family_message" in names and "mark_message_played" in names
    replies = ["pretty good", "not great, I fell getting to the bathroom", "no I'm not hurt, I got right up", "yes with breakfast", "toast and an egg",
               "nothing really", "yes Tom is picking me up at half one", "he came Tuesday, all fixed", "the garden, then a nap"]
    out = None
    for r in replies:
        out = agent.turn(s["session_id"], r)
        if out["done"]: break
    assert out["done"] and "family" in out["agent"].lower() and "Hair appointment" in out["agent"]
    log = [c["tool"] for c in agent.SESSIONS[s["session_id"]]["tool_log"]]
    assert log[-1] == "complete_checkin"
    c = db.checkin_for(1, _today())
    assert c["mood"] == 4 and c["sleep"] == 2 and c["meds_taken"] == 1 and c["ate"] == 1 and "fall" in c["flags"] and "pain" not in c["flags"]
    assert db.q("SELECT status FROM questions")[0]["status"] == "answered"


def test_scripted_agent_multi_fact_answer_skips_questions():
    s = agent.start(1)
    out = agent.turn(s["session_id"], "I'm fine, slept well and took my pills with my toast")
    asked = out["agent"]
    assert "sleep" not in asked.lower() and "pills" not in asked.lower() and "Lisinopril" not in asked
    c = db.checkin_for(1, _today())
    assert c["sleep"] == 4 and c["meds_taken"] == 1 and c["ate"] == 1


def test_scripted_agent_help_later_message_and_appointment():
    s = agent.start(1)
    out = agent.turn(s["session_id"], "please call my daughter, I need help")
    assert out["done"] and any(c["tool"] == "request_help" for c in out["tool_calls"])
    s = agent.start(1)
    assert agent.turn(s["session_id"], "not now, later")["done"] and db.one("SELECT until FROM snoozes WHERE person_id=1")
    s = agent.start(1)
    out = agent.turn(s["session_id"], "tell Anna I love her and the tap is fixed")
    assert any(c["tool"] == "record_reply" for c in out["tool_calls"]) and "Anna" in out["agent"]   # named contact wins even while away
    s = agent.start(1)
    out = agent.turn(s["session_id"], "I have the dentist on Friday at 10")
    ev = [c for c in out["tool_calls"] if c["tool"] == "add_event"]
    assert ev and ev[0]["args"]["time"] == "10:00" and ev[0]["args"]["title"].lower().startswith("dentist")


def test_scripted_agent_clarifies_unclear_scale():
    s = agent.start(1)
    out = agent.turn(s["session_id"], "oh you know, the usual")
    assert "good, okay, or not so good" in out["agent"]
    out = agent.turn(s["session_id"], "okay I suppose")
    assert db.checkin_for(1, _today())["mood"] == 3
