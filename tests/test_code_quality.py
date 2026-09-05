"""Regression tests for code-level invariants.

These guard against whole classes of bugs that have occurred before:

* Duplicated module-level function definitions. In an earlier revision of
  ``hearth.agent`` the LLM-mode scaffolding helpers (``_checkin_state``,
  ``_system_prompt``, ``_call_chat_completions``, ``_to_converse``,
  ``_call_converse``) were each declared twice. Python silently keeps the *last*
  definition, so the first copy was 86 lines of dead code that could drift out
  of sync and mislead readers. These tests fail loudly if that ever regresses.
"""
import ast
from collections import Counter
from pathlib import Path

import hearth

SRC = Path(hearth.__file__).parent


def _top_level(module: Path):
    tree = ast.parse(module.read_text(encoding="utf-8"))
    return [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]


def test_no_duplicated_top_level_definitions():
    """Every module defines each name exactly once (dead-code regression guard)."""
    for path in sorted(SRC.glob("*.py")):
        counts = Counter(_top_level(path))
        duplicates = {name: n for name, n in counts.items() if n > 1}
        assert not duplicates, f"{path.name} defines {duplicates} more than once"


def test_public_tools_all_present_once():
    """The MCP tool registry exposes each member function without shadowing."""
    # Importing the registry exercises the module top-to-bottom.
    from hearth.mcp_server import TOOLS
    expected = {
        "get_checkin_context", "get_family_message", "mark_message_played",
        "start_checkin", "record_answer", "complete_checkin", "request_help",
        "record_reply", "add_event", "list_events", "get_status",
        "snooze_checkin", "log_medication", "list_persons",
    }
    assert expected <= set(TOOLS)


def test_version_matches_pyproject():
    """Package version stays in sync with pyproject.toml."""
    import tomllib
    meta = tomllib.loads((SRC.parent / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert meta["version"] == hearth.__version__