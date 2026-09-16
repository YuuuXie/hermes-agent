"""Real completion queue transport, not just a renderer fixture (#65824)."""
from queue import Queue
from tools import async_delegation
from tools.process_registry import process_registry
from tools.process_registry_notifications import _format_async_delegation


def test_timeout_evidence_crosses_queue_fail_closed(monkeypatch):
    queue = Queue()
    monkeypatch.setattr(process_registry, "completion_queue", queue)
    monkeypatch.setattr(async_delegation, "_persist_completion", lambda *a: None)
    record = {"delegation_id": "handoff-transport", "dispatched_at": 1}
    result = {"status": "timeout", "summary": None, "partial": True,
              "partial_output_tail": [{"tool": "terminal", "preview": "verified output", "is_error": False}],
              "child_session_id": "child-handoff", "worker_state": "quarantined", "worker_still_running": True,
              "files_written": ["/work/HANDOFF.md"], "worktree": {"path": "/work", "branch": "child"}}
    async_delegation._push_completion_event(record, result, "timeout")
    event = queue.get_nowait()
    assert event["partial_output_tail"][0]["preview"] == "verified output"
    text = _format_async_delegation(event)
    assert "quarantined" in text and "/work/HANDOFF.md" in text and "child-handoff" in text
    import agent.redact
    monkeypatch.setattr(agent.redact, "redact_sensitive_text", lambda *a, **k: (_ for _ in ()).throw(ValueError("failed")))
    async_delegation._push_completion_event(record, result, "timeout")
    failed = queue.get_nowait()
    assert "partial_output_tail" not in failed
    assert failed["child_session_id"] == "child-handoff"
