"""Timeout artifact fallback: ``files_written`` must come from real registry state.

The timeout entry's durable artifact references (child_session_id + files_written) are the
fallback for a handoff steer that never landed, so they have to work on their own. These tests
drive the REAL ``FileStateRegistry`` (``note_write``) instead of stubbing the query, so a query
that filters everything out is caught.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from tools import file_state
from tools.delegate_tool_child_run import _ChildRun


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch):
    monkeypatch.delenv("HERMES_DISABLE_FILE_STATE_GUARD", raising=False)
    file_state.get_registry().clear()
    yield
    file_state.get_registry().clear()


def _child_run(child_task_id: str, wall_start: float) -> _ChildRun:
    run = _ChildRun(
        child=SimpleNamespace(session_id="child-session-1"),
        parent_agent=SimpleNamespace(),
        task_index=0,
        goal="goal",
        subagent_id=None,
        child_progress_cb=None,
    )
    run.child_task_id = child_task_id
    run.parent_task_id = "parent-1"
    run.wall_start = wall_start
    return run


def _write(tmp_path, name: str) -> str:
    p = tmp_path / name
    p.write_text("x\n")
    return str(p)


def test_timeout_entry_reports_files_the_child_actually_wrote(tmp_path):
    """RED without a task/time-scoped query: ``writes_since('', wall_start, [])`` has an empty
    path filter, so it returns {} and ``files_written`` is never attached."""
    wall_start = time.time()
    time.sleep(0.01)
    a = _write(tmp_path, "a.txt")
    b = _write(tmp_path, "b.txt")
    file_state.note_write("child-1", a)
    file_state.note_write("child-1", b)

    run = _child_run("child-1", wall_start)
    entry: dict = {}
    run.attach_artifact_refs(entry)

    assert entry.get("child_session_id") == "child-session-1"
    assert entry.get("files_written") == sorted([a, b])


def test_other_task_writes_are_excluded(tmp_path):
    wall_start = time.time()
    time.sleep(0.01)
    mine = _write(tmp_path, "mine.txt")
    theirs = _write(tmp_path, "theirs.txt")
    file_state.note_write("child-1", mine)
    file_state.note_write("sibling-2", theirs)

    entry: dict = {}
    _child_run("child-1", wall_start).attach_artifact_refs(entry)

    assert entry.get("files_written") == [mine]


def test_writes_before_wall_start_are_excluded(tmp_path):
    old = _write(tmp_path, "old.txt")
    file_state.note_write("child-1", old)
    time.sleep(0.02)
    wall_start = time.time()
    time.sleep(0.02)
    new = _write(tmp_path, "new.txt")
    file_state.note_write("child-1", new)

    entry: dict = {}
    _child_run("child-1", wall_start).attach_artifact_refs(entry)

    assert entry.get("files_written") == [new]


def test_files_written_is_bounded_to_40_paths(tmp_path):
    wall_start = time.time()
    time.sleep(0.01)
    for i in range(50):
        file_state.note_write("child-1", _write(tmp_path, f"f{i:02d}.txt"))

    entry: dict = {}
    _child_run("child-1", wall_start).attach_artifact_refs(entry)

    written = entry.get("files_written")
    assert written is not None and len(written) == 40
    assert written == sorted(written)


def test_no_writes_leaves_files_written_absent(tmp_path):
    entry: dict = {}
    _child_run("child-1", time.time()).attach_artifact_refs(entry)
    assert "files_written" not in entry


def test_guard_disabled_degrades_without_raising(tmp_path, monkeypatch):
    wall_start = time.time()
    time.sleep(0.01)
    p = _write(tmp_path, "a.txt")
    file_state.note_write("child-1", p)
    monkeypatch.setenv("HERMES_DISABLE_FILE_STATE_GUARD", "1")

    entry: dict = {}
    _child_run("child-1", wall_start).attach_artifact_refs(entry)
    assert "files_written" not in entry
    assert entry.get("child_session_id") == "child-session-1"


def test_emit_complete_reports_the_same_child_writes(tmp_path):
    """Success path carries the identical scoped query (same class of bug)."""
    wall_start = time.time()
    time.sleep(0.01)
    mine = _write(tmp_path, "mine.txt")
    theirs = _write(tmp_path, "theirs.txt")
    file_state.note_write("child-1", mine)
    file_state.note_write("sibling-2", theirs)

    events: list = []
    run = _child_run("child-1", wall_start)
    run.child_progress_cb = lambda event, **kw: events.append((event, kw))
    entry = {"summary": "done", "status": "completed", "api_calls": 1}
    run.emit_complete({"messages": []}, entry, 1.0)

    assert events, "expected subagent.complete"
    assert events[-1][1]["files_written"] == [mine]
