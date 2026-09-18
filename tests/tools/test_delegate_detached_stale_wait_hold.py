"""Detached units (``release_stale_wait=False``) must NOT have their wait ended by a stale verdict.

This is the local-only opt-out that upstream's ``settled``-event rewrite has no parameter for: when
``honor_parent_interrupt=False`` (a detached/background unit), the heartbeat's stale verdict is an
observation, not a reason to abandon the child. Only the child's own completion or the configured
hard cap may end the wait.

The upstream merge re-based the wait loop onto ``_Heartbeat.settled``, which the stale verdict sets.
If the ``release_stale_wait`` guard is ever dropped (e.g. a future rebase "just takes upstream"),
these tests fail: the detached child is abandoned at the stale threshold instead of being allowed to
finish. ``test_attached_unit_still_releases_on_the_stale_verdict`` is the paired control so a
sabotage that simply disables stale release everywhere cannot make this file green.

Deterministic: no wall-clock sleeps are asserted on beyond a generous cap; the child's completion is
released by an explicit event, and the stale verdict is observed via ``heartbeat.settled``.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

from tools import delegate_tool
from tools import delegate_tool_child_run


class _WedgedThenFinishes:
    """Frozen activity (so the heartbeat calls it stale almost immediately); the worker returns only
    when ``release`` is set."""

    def __init__(self) -> None:
        self.tool_progress_callback = None
        self._credential_pool = None
        self._delegate_saved_tool_names = []
        self._delegate_role = "leaf"
        self._delegate_depth = 1
        self._subagent_id = None
        self.session_id = "detached-child"
        self.release = threading.Event()
        self.interrupted = threading.Event()
        self.entered = threading.Event()

    def run_conversation(self, **_kwargs):
        self.entered.set()
        self.release.wait()
        return {"final_response": "VERDICT", "completed": True, "api_calls": 28, "messages": []}

    def get_activity_summary(self):
        # Never advances -> stale after _HEARTBEAT_STALE_CYCLES_IDLE cycles.
        return {"api_call_count": 28, "current_tool": None, "last_activity_ts": 1000.0, "max_iterations": 50}

    def hard_interrupt(self, *_args, **_kwargs):
        self.interrupted.set()

    def close(self):
        self.release.set()


def _parent(child):
    return SimpleNamespace(
        session_id="parent", _current_task_id=None, _active_children=[child],
        _active_children_lock=threading.Lock(), _touch_activity=lambda _d: None,
        _interrupt_requested=False,
    )


def _fast_heartbeat(monkeypatch, *, child_timeout):
    monkeypatch.setattr(delegate_tool, "_HEARTBEAT_INTERVAL", 0.01)
    monkeypatch.setattr(delegate_tool, "_HEARTBEAT_STALE_CYCLES_IDLE", 2)
    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: child_timeout)
    monkeypatch.setattr(delegate_tool, "_get_worktree_isolation", lambda: False)
    # No near-deadline handoff steer in these tests; it is covered by test_delegate_timeout_handoff.py.
    monkeypatch.setattr(delegate_tool, "_get_handoff_grace_seconds", lambda: None)


def _capture_heartbeats(monkeypatch):
    """Record every _Heartbeat built, so a test can prove the stale verdict actually fired."""
    made = []
    real = delegate_tool_child_run._Heartbeat

    class _Recorded(real):  # type: ignore[misc, valid-type]
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            made.append(self)

    monkeypatch.setattr(delegate_tool_child_run, "_Heartbeat", _Recorded)
    monkeypatch.setattr(delegate_tool, "_start_heartbeat", lambda c, p, i: _Recorded(c, p, i))
    return made


def test_detached_unit_keeps_waiting_through_a_stale_verdict_and_gets_the_real_result(monkeypatch):
    """release_stale_wait=False: stale fires, the child finishes later, the parent still gets its
    result. With the guard removed the entry would be a ``timeout`` at the stale threshold."""
    child = _WedgedThenFinishes()
    _fast_heartbeat(monkeypatch, child_timeout=30)
    beats = _capture_heartbeats(monkeypatch)

    # Release the child only AFTER the stale verdict has provably fired, so the test cannot pass by
    # the child simply finishing first.
    def _release_after_stale():
        assert child.entered.wait(10), "worker never started"
        hb = beats[0]
        assert hb.settled.wait(10), "heartbeat never reached the stale verdict"
        assert hb.stale_threshold_seconds is not None
        child.release.set()

    releaser = threading.Thread(target=_release_after_stale, daemon=True)
    releaser.start()
    valve = threading.Timer(30.0, child.release.set)  # safety valve: fail (timeout), never hang
    valve.daemon = True
    valve.start()
    try:
        entry = delegate_tool._run_single_child(
            0, "review", child=child, parent_agent=_parent(child), release_stale_wait=False,
        )
    finally:
        valve.cancel()
        child.release.set()
        releaser.join(timeout=5)

    assert beats and beats[0].settled.is_set(), "precondition: the stale verdict must have fired"
    assert entry["status"] == "completed", entry
    assert entry.get("summary") == "VERDICT", entry
    assert entry.get("timeout_seconds") in (None, 0), entry
    assert not child.interrupted.is_set(), "a detached child must not be stopped by a stale verdict"


def test_detached_unit_that_never_finishes_still_ends_at_the_configured_hard_cap(monkeypatch):
    """The opt-out must not remove the hard timeout: with no completion, the configured cap ends the
    wait and the entry names the CAP, not the (much smaller) stale threshold."""
    child = _WedgedThenFinishes()
    _fast_heartbeat(monkeypatch, child_timeout=1)
    beats = _capture_heartbeats(monkeypatch)
    valve = threading.Timer(30.0, child.release.set)
    valve.daemon = True
    valve.start()
    try:
        entry = delegate_tool._run_single_child(
            0, "review", child=child, parent_agent=_parent(child), release_stale_wait=False,
        )
    finally:
        valve.cancel()
        child.release.set()

    assert beats and beats[0].settled.is_set(), "precondition: the stale verdict must have fired"
    assert entry["status"] == "timeout", entry
    # The stale threshold here is 2 * 0.01 = 0.02s; reporting that would mean stale ended the wait.
    assert entry["timeout_seconds"] == 1, entry
    assert entry["timeout_phase"] == "after_llm_calls", entry
    assert "heartbeat stale threshold" not in (entry.get("error") or ""), entry["error"]
    assert (entry.get("duration_seconds") or 0) >= 0.9, entry


def test_attached_unit_still_releases_on_the_stale_verdict(monkeypatch):
    """Control: the DEFAULT (release_stale_wait=True) path keeps upstream's behaviour, so the tests
    above cannot be satisfied by disabling stale release everywhere."""
    child = _WedgedThenFinishes()
    _fast_heartbeat(monkeypatch, child_timeout=3600)
    valve = threading.Timer(30.0, child.release.set)
    valve.daemon = True
    valve.start()
    try:
        entry = delegate_tool._run_single_child(
            0, "review", child=child, parent_agent=_parent(child), release_stale_wait=True,
        )
    finally:
        valve.cancel()
        child.release.set()

    assert entry["status"] == "timeout", entry
    assert entry["timeout_seconds"] == 2 * 0.01, entry
    assert entry["timeout_phase"] == "stale_after_llm_calls", entry
    assert child.interrupted.is_set()


def test_detached_dispatch_passes_the_opt_out_down(monkeypatch):
    """Wiring check: ``honor_parent_interrupt=False`` must reach ``_run_single_child`` as
    ``release_stale_wait=False`` — the parameter is useless if the detached path never sets it."""
    from tools import delegate_tool_dispatch

    seen = {}

    def _fake_run_single_child(task_index, goal, child=None, parent_agent=None, **kwargs):
        seen["release_stale_wait"] = kwargs.get("release_stale_wait")
        return {"task_index": task_index, "status": "completed", "summary": "ok"}

    monkeypatch.setattr(delegate_tool, "_run_single_child", _fake_run_single_child)
    batch = delegate_tool_dispatch._Batch.__new__(delegate_tool_dispatch._Batch)
    batch.parent_agent = None
    batch.origin_ui_session_id = ""
    batch.origin_owner_transport = None
    batch.origin_owner_session_record = None
    batch.run_child(0, {"goal": "g"}, object(), release_stale_wait=False)
    assert seen["release_stale_wait"] is False
