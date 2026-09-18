"""A child that finishes while the wait loop is mid-iteration must still deliver its result.

``await_child`` waits on ONE event (``_Heartbeat.settled``) that has TWO setters: the heartbeat's
stale verdict, and the worker Future's own done-callback. The attached (``release_stale_wait=True``)
branch treats "settled is set" as the stale verdict and raises ``FuturesTimeoutError``. If the
worker settles after the loop's ``future.done()`` check but before that branch reads the event, a
*successfully completed* child is reported as ``status="timeout"`` and its summary is thrown away —
the exact failure class this delegation stack exists to prevent.

These tests pin the completion window deterministically, without sleeps and without reading source:
the heartbeat's ``settled`` is a real ``threading.Event`` subclass whose first ``is_set()`` — the
read the attached branch performs while the Future is still pending — releases the worker and waits
for the real done-callback to land before answering truthfully. That is exactly the interleaving the
scheduler can produce on its own; the subclass only makes it reliable.

``test_a_genuine_stale_verdict_still_ends_the_attached_wait`` is the paired control: a fix that
simply stops honouring ``settled`` on the attached path would make this file green otherwise.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from tools import delegate_tool
from tools import delegate_tool_child_run


class _BlockedChild:
    """Worker blocks on ``release`` and then returns a real, non-empty result."""

    def __init__(self) -> None:
        self.tool_progress_callback = None
        self._credential_pool = None
        self._delegate_saved_tool_names = []
        self._delegate_role = "leaf"
        self._delegate_depth = 1
        self._subagent_id = None
        self.session_id = "racing-child"
        self.release = threading.Event()
        self.entered = threading.Event()
        self.interrupted = threading.Event()

    def run_conversation(self, **_kwargs):
        self.entered.set()
        assert self.release.wait(30), "worker was never released"
        return {"final_response": "REAL RESULT", "completed": True, "api_calls": 3, "messages": []}

    def get_activity_summary(self):
        return {"api_call_count": 3, "current_tool": None, "last_activity_ts": 1000.0, "max_iterations": 50}

    def hard_interrupt(self, *_args, **_kwargs):
        self.interrupted.set()

    def close(self):
        self.release.set()


class _CompletionRaceEvent(threading.Event):
    """A real Event that pins the worker's completion inside the wait loop's race window.

    The first read of ``is_set()`` happens while the Future is still pending (the loop checked
    ``future.done()`` immediately before). At that moment we let the worker finish and block until
    the Future's done-callback has actually set this event, then answer with the true state.
    """

    def __init__(self, release_worker) -> None:
        super().__init__()
        self._release_worker = release_worker
        self._armed = False
        self.window_hit = threading.Event()

    def is_set(self) -> bool:  # type: ignore[override]
        if not self._armed:
            self._armed = True
            self._release_worker()
            assert super().wait(20), "the worker's done-callback never set the settled event"
            self.window_hit.set()
        return super().is_set()


def _parent(child):
    return SimpleNamespace(
        session_id="parent", _current_task_id=None, _active_children=[child],
        _active_children_lock=threading.Lock(), _touch_activity=lambda _d: None,
        _interrupt_requested=False,
    )


def _no_stale_heartbeat(monkeypatch, *, child_timeout):
    """Real heartbeat, but the stale verdict can never fire — so ``settled`` has exactly one
    setter left: the worker's done-callback."""
    monkeypatch.setattr(delegate_tool, "_HEARTBEAT_INTERVAL", 0.01)
    monkeypatch.setattr(delegate_tool, "_HEARTBEAT_STALE_CYCLES_IDLE", 10_000_000)
    monkeypatch.setattr(delegate_tool, "_HEARTBEAT_STALE_CYCLES_IN_TOOL", 10_000_000)
    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: child_timeout)
    monkeypatch.setattr(delegate_tool, "_get_worktree_isolation", lambda: False)
    monkeypatch.setattr(delegate_tool, "_get_handoff_grace_seconds", lambda: None)


def _arm_race(monkeypatch, child):
    """Install the window-pinning ``settled`` event on every heartbeat built."""
    events = []
    real = delegate_tool_child_run._Heartbeat

    class _Raced(real):  # type: ignore[misc, valid-type]
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            ev = _CompletionRaceEvent(lambda: (child.entered.wait(20), child.release.set()))
            self.settled = ev
            events.append(ev)

    monkeypatch.setattr(delegate_tool_child_run, "_Heartbeat", _Raced)
    monkeypatch.setattr(delegate_tool, "_start_heartbeat", lambda c, p, i: _Raced(c, p, i))
    return events


@pytest.mark.parametrize("child_timeout", [60, None], ids=["configured-timeout", "no-hard-timeout"])
def test_child_that_settles_inside_the_wait_window_still_delivers_its_result(monkeypatch, child_timeout):
    """The worker completes between the loop's ``future.done()`` check and its ``settled`` read.
    The parent must get the child's real summary, not a fabricated timeout."""
    child = _BlockedChild()
    _no_stale_heartbeat(monkeypatch, child_timeout=child_timeout)
    events = _arm_race(monkeypatch, child)
    valve = threading.Timer(30.0, child.release.set)  # fail, never hang
    valve.daemon = True
    valve.start()
    try:
        entry = delegate_tool._run_single_child(
            0, "work", child=child, parent_agent=_parent(child), release_stale_wait=True,
        )
    finally:
        valve.cancel()
        child.release.set()

    assert events and events[0].window_hit.is_set(), "precondition: the race window was never entered"
    assert events[0].is_set(), "precondition: the Future's done-callback must have settled the event"
    assert entry["status"] == "completed", entry
    assert entry.get("summary") == "REAL RESULT", entry
    assert entry.get("error") in (None, ""), entry
    assert entry.get("timeout_seconds") in (None, 0), entry
    assert not child.interrupted.is_set(), "a child that finished must not be stopped as stale"


def test_a_worker_exception_in_the_wait_window_is_reported_as_the_child_error(monkeypatch):
    """Same window, but the worker raises: the entry must carry the child's own failure, not a
    timeout. (A re-check that swallowed exceptions would regress this.)"""

    class _RaisingChild(_BlockedChild):
        def run_conversation(self, **_kwargs):
            self.entered.set()
            assert self.release.wait(30)
            raise RuntimeError("child blew up")

    child = _RaisingChild()
    _no_stale_heartbeat(monkeypatch, child_timeout=60)
    events = _arm_race(monkeypatch, child)
    valve = threading.Timer(30.0, child.release.set)
    valve.daemon = True
    valve.start()
    try:
        entry = delegate_tool._run_single_child(
            0, "work", child=child, parent_agent=_parent(child), release_stale_wait=True,
        )
    finally:
        valve.cancel()
        child.release.set()

    assert events and events[0].window_hit.is_set(), "precondition: the race window was never entered"
    assert entry["status"] == "error", entry
    assert "child blew up" in (entry.get("error") or ""), entry
    assert "timed out" not in (entry.get("error") or ""), entry
    assert entry.get("timeout_seconds") in (None, 0), entry


def test_a_genuine_stale_verdict_still_ends_the_attached_wait(monkeypatch):
    """Control: with no completion at all, the stale verdict must still release the attached wait.
    Prevents "fixing" the race by ignoring ``settled`` on the default path."""

    class _NeverFinishes(_BlockedChild):
        def run_conversation(self, **_kwargs):
            self.entered.set()
            assert self.release.wait(30)
            return {"final_response": "TOO LATE", "completed": True, "api_calls": 3, "messages": []}

    child = _NeverFinishes()
    monkeypatch.setattr(delegate_tool, "_HEARTBEAT_INTERVAL", 0.01)
    monkeypatch.setattr(delegate_tool, "_HEARTBEAT_STALE_CYCLES_IDLE", 2)
    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: 3600)
    monkeypatch.setattr(delegate_tool, "_get_worktree_isolation", lambda: False)
    monkeypatch.setattr(delegate_tool, "_get_handoff_grace_seconds", lambda: None)
    valve = threading.Timer(60.0, child.release.set)
    valve.daemon = True
    valve.start()
    try:
        entry = delegate_tool._run_single_child(
            0, "work", child=child, parent_agent=_parent(child), release_stale_wait=True,
        )
    finally:
        valve.cancel()
        child.release.set()

    assert entry["status"] == "timeout", entry
    assert entry["timeout_seconds"] == 2 * 0.01, entry
    assert entry["timeout_phase"] == "stale_after_llm_calls", entry
    assert child.interrupted.is_set()
