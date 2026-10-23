"""Regression coverage for timed-out delegation teardown."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any, cast

from tools import delegate_tool
from tools import delegate_tool_child_run as child_run


class _SlowUnwindingChild:
    def __init__(self) -> None:
        self.tool_progress_callback = None
        self._credential_pool = None
        self._delegate_saved_tool_names = []
        self._delegate_role = "leaf"
        self._delegate_depth = 1
        self._subagent_id = None
        self.model = "test-model"
        self.session_id = "held-child"
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_estimated_cost_usd = 0.0
        self.session_cost_status = "unknown"
        self.started = threading.Event()
        self.interrupted = threading.Event()
        self.unwinding = threading.Event()
        self.allow_finish = threading.Event()
        self.finished = threading.Event()
        self.closed = threading.Event()
        self.close_while_running = False

    def run_conversation(self, **_kwargs):
        self.started.set()
        # Generous bounds: these gate on events the test sets promptly; a tight bound only
        # turns a load-starved test thread into a spurious early exit that fires close().
        assert self.interrupted.wait(timeout=30)
        # Model the real child turn's finally path: it still performs session
        # activity/SQLite cleanup after the parent requests interruption.
        self.unwinding.set()
        assert self.allow_finish.wait(timeout=30)
        self.finished.set()
        return {
            "final_response": "",
            "completed": False,
            "interrupted": True,
            "api_calls": 1,
            "messages": [],
        }

    def hard_interrupt(self, _reason=None):
        self.interrupted.set()

    def get_activity_summary(self):
        return {"api_call_count": 1}

    def close(self):
        if not self.finished.is_set():
            self.close_while_running = True
        self.closed.set()


def test_timeout_does_not_close_child_while_worker_is_unwinding(monkeypatch):
    child = _SlowUnwindingChild()
    parent = SimpleNamespace(
        session_id="parent-timeout-test",
        _current_task_id=None,
        _active_children=[child],
        _active_children_lock=threading.Lock(),
    )
    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: 0.5)
    monkeypatch.setattr(delegate_tool, "_get_worktree_isolation", lambda: False)

    from tools.daemon_pool import DaemonThreadPoolExecutor

    submit = DaemonThreadPoolExecutor.submit

    def submit_started(executor, *args, **kwargs):
        future = submit(executor, *args, **kwargs)
        assert child.started.wait(timeout=10)
        return future

    # Timeout accounting starts only after this test's child is running.
    monkeypatch.setattr(DaemonThreadPoolExecutor, "submit", submit_started)
    result = delegate_tool._run_single_child(
        task_index=0,
        goal="exercise timeout teardown",
        child=child,
        parent_agent=parent,
    )

    assert result["status"] == "timeout"
    assert child.unwinding.wait(timeout=10)
    try:
        assert not child.closed.is_set(), (
            "timed-out child.close() ran before its conversation thread unwound"
        )
    finally:
        child.allow_finish.set()
    assert child.finished.wait(timeout=10)
    assert child.closed.wait(timeout=10)
    assert not child.close_while_running, (
        "timed-out child.close() raced its still-running conversation thread"
    )


def test_live_worker_retains_ownership_until_future_settles(monkeypatch):
    from tests.waiting import wait_until
    from tools import subagent_worktree

    child = _SlowUnwindingChild()

    parent = SimpleNamespace(session_id="owner", _current_task_id=None,
                             _active_children=[child], _active_children_lock=threading.Lock())
    class Pool:
        def __init__(self):
            self.released = []
        def release_lease(self, credential):
            self.released.append(credential)
    pool = Pool()
    finalized = []
    monkeypatch.setattr(delegate_tool, "_lease_child_credential", lambda _child: (pool, "lease"))
    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: 0.5)
    monkeypatch.setattr(child_run, "_create_isolated_worktree", lambda *_: {"path": "/tmp/preserved", "branch": "test"})
    monkeypatch.setattr(subagent_worktree, "build_worktree_context_note", lambda _: "")
    monkeypatch.setattr(subagent_worktree, "finalize_subagent_worktree", lambda info: finalized.append(info))
    monkeypatch.setattr(subagent_worktree, "unproven_worktree_payload", lambda info, reason: {**info, "reason": reason})

    result = delegate_tool._run_single_child(0, "held worker", child, parent)
    assert result["status"] == "timeout"
    assert result["worktree"]["path"] == "/tmp/preserved"
    assert "still running" in result["worktree"]["reason"]
    assert child.unwinding.wait(timeout=10)
    try:
        assert pool.released == [] and finalized == []
        assert child in parent._active_children
        assert not child.closed.is_set()
    finally:
        child.allow_finish.set()
    assert wait_until(lambda: child.closed.is_set() and pool.released == ["lease"] and child not in parent._active_children,
                      timeout=10)

    assert finalized == []


def test_parent_close_preserves_live_delegated_worker():
    from concurrent.futures import Future
    from run_agent import AIAgent

    child = _SlowUnwindingChild()
    future = Future()
    cast(Any, child)._delegate_worker_future = future
    parent = SimpleNamespace(_active_children=[child], _active_children_lock=threading.Lock())
    AIAgent._close_active_children(cast(Any, parent), soft=False)
    assert parent._active_children == [child]
    assert not child.closed.is_set()
    # done() alone does not mean cleanup callbacks have run; delegation still owns close.
    future.set_result(None)
    AIAgent._close_active_children(cast(Any, parent), soft=False)
    assert parent._active_children == [child]
    assert not child.closed.is_set()
