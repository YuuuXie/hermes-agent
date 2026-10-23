"""Timeout evidence is useful without treating partial tool output as a final answer."""

import queue
import threading
from types import SimpleNamespace
from typing import Any, cast

from tools import async_delegation, delegate_tool, delegate_tool_child_run, delegate_tool_results
from tools import process_registry, process_registry_notifications


def test_timeout_run_returns_completed_tool_evidence_without_summary(monkeypatch):
    from tests.tools.test_delegate_timeout_cleanup import _SlowUnwindingChild

    child = _SlowUnwindingChild()
    child.session_id = "saved-child-session"
    cast(Any, child)._session_messages = [{"role": "tool", "content": "finished check"}]
    parent = SimpleNamespace(session_id="parent", _current_task_id=None,
                             _active_children=[child], _active_children_lock=threading.Lock())
    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: 0.5)
    monkeypatch.setattr(delegate_tool, "_get_worktree_isolation", lambda: False)
    try:
        result = delegate_tool._run_single_child(0, "pending", child, parent)
        assert result["status"] == "timeout" and result["summary"] is None
        assert result["partial_output_tail"][0]["preview"] == "finished check"
        assert result["child_session_id"] == "saved-child-session"
    finally:
        child.allow_finish.set()
    assert child.closed.wait(timeout=10)


def test_timeout_tail_redacts_complete_content_before_cut_and_fails_closed(monkeypatch):
    from tools import delegation_redaction
    token = "sk-" + "A" * 40
    seen = []
    original = delegate_tool_results.redact_child_text
    def checking_redactor(value, **kwargs):
        seen.append((value, kwargs))
        return original(value, **kwargs)
    monkeypatch.setattr(delegate_tool_results, "redact_child_text", checking_redactor)
    messages = [
        {"role": "assistant", "content": "private reasoning", "tool_calls": [
            {"id": "call", "function": {"name": "terminal"}}]},
        {"role": "tool", "tool_call_id": "call", "content": "x" * 580 + " " + token + " useful end"},
    ]
    tail = delegate_tool_results._extract_output_tail({"messages": messages}, max_chars=600, fail_closed=True)
    assert len(tail) == 1
    assert token not in tail[0]["preview"]
    assert "..." in tail[0]["preview"]
    assert any(token in value and kwargs["force"] for value, kwargs in seen)
    assert "private reasoning" not in str(tail)

    monkeypatch.setattr(delegate_tool_results, "redact_child_text", lambda value, **kw: delegation_redaction.REDACTION_UNAVAILABLE)
    assert delegate_tool_results._extract_output_tail({"messages": messages}, fail_closed=True) == []


def test_timeout_producer_preserves_refs_when_tail_unavailable(monkeypatch):
    from tools import file_state
    registry = file_state.FileStateRegistry()
    monkeypatch.setattr(file_state, "_registry", registry)
    registry.note_write("child-task", "/tmp/done.txt", mtime=1.0)
    registry.note_write("sibling", "/tmp/sibling.txt", mtime=1.0)
    assert registry.writes_by("child-task", float("inf")) == []
    assert registry.writes_since("", 0, []) == {}  # needs candidate paths, unlike writes_by
    child = SimpleNamespace(session_id="child-session", _session_messages=[
        {"role": "tool", "content": "completed step"}])
    run = delegate_tool_child_run._ChildRun(child, None, 0, "goal", None, None,
                                            child_task_id="child-task", wall_start=0)
    entry = {"status": "timeout", "summary": None}
    run.timeout_evidence(entry)
    assert entry["partial_output_tail"][0]["preview"] == "completed step"
    assert entry["child_session_id"] == "child-session"
    assert entry["files_written"] == ["/tmp/done.txt"]
    child._session_messages = None
    entry = {"status": "timeout", "summary": None}
    run.timeout_evidence(entry)
    assert "partial_output_tail" not in entry
    assert entry["child_session_id"] == "child-session"
    assert entry["files_written"] == ["/tmp/done.txt"]


def test_single_and_batch_timeout_event_rendering(monkeypatch):
    events = queue.Queue()
    monkeypatch.setattr(process_registry.process_registry, "completion_queue", events)
    persisted = []
    monkeypatch.setattr(async_delegation, "_persist_completion", lambda event, result: persisted.append((event, result)))
    entry = {"task_index": 0, "status": "timeout", "summary": None, "error": "inactive",
             "partial_output_tail": [{"tool": "terminal", "preview": "finished build", "is_error": False}],
             "child_session_id": "child-session", "files_written": ["/tmp/done.txt"],
             "worktree": {"path": "/tmp/tree", "branch": "feature"}}
    record = {"delegation_id": "deleg-1", "goal": "build", "goals": ["build"],
              "parent_session_id": "parent", "session_key": "parent-key"}
    async_delegation._push_completion_event(record, entry, "timeout")
    event = events.get_nowait()
    assert event["summary"] is None and event["partial_output_tail"] == entry["partial_output_tail"]
    assert persisted[0][0]["child_session_id"] == "child-session"
    text = process_registry_notifications._format_async_delegation(event)
    assert "finished build" in text and "/tmp/done.txt" in text and "/tmp/tree" in text
    assert "status=timeout" in text

    batch = {**record, "is_batch": True}
    async_delegation._push_completion_event(batch, {"results": [entry]}, "completed")
    batch_event = events.get_nowait()
    assert batch_event["results"][0]["summary"] is None
    batch_text = process_registry_notifications._format_async_delegation(batch_event)
    assert "finished build" in batch_text and "child-session" in batch_text
