"""Handoff evidence must not prune or declare a live worker finished."""
from types import SimpleNamespace
from unittest.mock import Mock
import threading
from tools.delegate_tool_child_run import _ChildRun
from tools.process_registry_notifications import _format_async_delegation


def test_live_timeout_preserves_worktree_and_labels_single_and_batch(monkeypatch):
    from tools import subagent_worktree
    finalize = Mock(side_effect=AssertionError("must not inspect/prune live worktree"))
    monkeypatch.setattr(subagent_worktree, "finalize_subagent_worktree", finalize)
    run = _ChildRun(task_index=0, goal="work", child=SimpleNamespace(), parent_agent=None,
                    subagent_id="held", child_progress_cb=None)
    run.worktree_info = {"path": "/workspace/held", "branch": "held"}
    entry = {"status": "timeout", "summary": None, "worker_still_running": True,
             "worker_state": "quarantined", "duration_seconds": 1}
    run.attach_worktree(entry)
    assert entry["worktree"]["path"] == "/workspace/held"
    assert entry["worktree"]["pruned"] is False
    finalize.assert_not_called()
    for evt in [entry, {"is_batch": True, "results": [entry]}]:
        text = _format_async_delegation(evt)
        assert "quarantined" in text and "still running" in text
        assert "/workspace/held" in text
        assert "has finished" not in text


def test_config_file_reaches_grace_reader(tmp_path, monkeypatch):
    from tools.delegate_tool_config import _get_handoff_grace_seconds
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("delegation:\n  handoff_grace_seconds: 17\n")
    assert _get_handoff_grace_seconds() == 17
