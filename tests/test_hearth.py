import datetime as dt, os, tempfile
import pytest

os.environ["HEARTH_DB"] = os.path.join(tempfile.gettempdir(), "hearth_test.db")
from hearth import db, core, seed, escalation, agent   # noqa: E402
from hearth.mcp_server import TOOLS                     # noqa: E402


@pytest.fixture(autouse=True)
def fresh():
    seed.run()
    yield


def test_flags_and_negation():
    assert core.detect_flags("I fell getting to the bathroom") == ["fall"]
    assert core.detect_flags("No, I am not hurt at all") == []
    assert "emergency" in core.detect_flags("I can't get up, help me")
    assert "skipped_meds" in core.detect_flags("I forgot my pills")


def test_full_checkin_flow_escalates_on_concern():
    ctx = TOOLS["get_checkin_context"](1)
    assert ctx["person"]["name"] == "Margaret Hale" and ctx["medications_due"]
    cid = TOOLS["start_checkin"](1)["checkin_id"]
    r = TOOLS["record_answer"](cid, "sleep", "badly", "my knee kept me up and I fell getting to the bathroom")
    assert "fall" in r["flags_added"] and r["follow_up"]
    TOOLS["record_answer"](cid, "note", "not hurt", "No, I'm not hurt, I got right up")
    for f, v in [("mood", "good"), ("meds_taken", "yes"), ("ate", "yes"), ("concern", "nothing really"), ("plans", "garden")]:
        TOOLS["record_answer"](cid, f, v, v)
    done = TOOLS["complete_checkin"](cid)
    assert done["risk_level"] == "concern" and done["escalation"]["level"] == "concern"
    assert "Anna Hale" in done["summary_sent_to"]
    assert 'Said: "nothing really"' not in done["summary"]
    st = TOOLS["get_status"](1)
    assert st["state"] == "checked_in" and st["open_alerts"] == 1


def test_request_help_notifies_everyone():
    r = TOOLS["request_help"](1, "I can't get up", "urgent")
    assert len(r["notified"]) == 3 and r["level"] == "urgent"


def test_escalation_ladder_fires_once_per_level():
    p = db.person(1)
    _, end = core.window_bounds(p, core.today_str(p))
    assert escalation.run_once(end - dt.timedelta(minutes=5)) == []                       # window still open
    a1 = escalation.run_once(end + dt.timedelta(minutes=1)); assert [a["level"] for a in a1] == ["missed_1"]
    assert escalation.run_once(end + dt.timedelta(minutes=2)) == []                        # idempotent
    a2 = escalation.run_once(end + dt.timedelta(minutes=45)); assert [a["level"] for a in a2] == ["missed_2"]
    a3 = escalation.run_once(end + dt.timedelta(minutes=120)); assert [a["level"] for a in a3] == ["missed_3"]
    assert len(a3[0]["notified"]) == 3
    cid = TOOLS["start_checkin"](1)["checkin_id"]
    done = TOOLS["complete_checkin"](cid)
    assert done["missed_alerts_resolved"] == 3 and db.open_alerts(1) == []


def test_snooze_pauses_ladder():
    p = db.person(1)
    _, end = core.window_bounds(p, core.today_str(p))
    TOOLS["snooze_checkin"](1, 30)
    assert escalation.run_once(end + dt.timedelta(minutes=10)) == []


def test_scripted_agent_conversation():
    s = agent.start(1)
    assert "Margaret" in s["agent"] and not s["done"]
    replies = ["pretty good", "not great, I fell getting to the bathroom", "no I'm not hurt", "yes with breakfast", "toast", "nothing really", "Anna is calling"]
    out = None
    for r in replies:
        out = agent.turn(s["session_id"], r)
        if out["done"]:
            break
    assert out["done"] and "family" in out["agent"].lower()
    names = [c["tool"] for c in agent.SESSIONS[s["session_id"]]["tool_log"]]
    assert names[:2] == ["get_checkin_context", "start_checkin"] and names[-1] == "complete_checkin"


def test_scripted_agent_help_path():
    s = agent.start(1)
    out = agent.turn(s["session_id"], "please call my daughter, I need help")
    assert out["done"] and any(c["tool"] == "request_help" for c in out["tool_calls"])
