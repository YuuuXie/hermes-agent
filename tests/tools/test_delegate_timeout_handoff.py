"""Timeout partial-delivery + near-deadline handoff.

Covers the ported upstream #84085 behaviour (redact-before-truncate, fail_closed scoping,
partial tail on the timeout entry, async rendering) and the local handoff-grace steer.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tools import delegate_tool
from tools import delegate_tool_results
from tools.delegate_tool_results import _extract_output_tail
from tools.process_registry_notifications import _format_async_delegation


# ── _extract_output_tail: redaction before truncation ───────────────────────

def _tool_result(text: str) -> dict:
    return {
        "messages": [
            {"role": "assistant", "tool_calls": [{"id": "c1", "function": {"name": "terminal"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": text},
        ]
    }


def test_redaction_runs_before_truncation_at_the_boundary():
    """A credential whose pattern has a minimum-length quantifier stops matching once cut.

    ``gAAAA[A-Za-z0-9_=-]{20,}`` sliced to 25 chars drops below the 20-char body minimum,
    so truncate-then-redact would leak it. Redact-then-truncate must not.
    """
    secret = "gAAAA" + "B" * 40
    tail = _extract_output_tail(_tool_result(f"credential {secret}"), max_entries=1, max_chars=25)
    assert tail, "expected one entry"
    preview = tail[0]["preview"]
    # The redactor elides the middle of a matched token (gAAAAB...BBBB) rather than deleting
    # it, so assert on the elision plus the longest surviving raw body run. Under
    # truncate-then-redact sabotage the preview is the untouched `credential gAAAABBBBBBBBB`,
    # which has no elision and a 9-char raw run -- both assertions below then fail.
    assert "..." in preview, f"credential was not redacted: {preview!r}"
    import re as _re
    longest = max((len(m) for m in _re.findall(r"B+", preview)), default=0)
    assert longest <= 6, f"raw credential body survived truncation: {preview!r}"


def test_redactor_failure_is_best_effort_by_default(monkeypatch):
    """A display caller keeps the healthy entries when one result cannot be redacted."""
    import agent.redact as redact_mod

    real = redact_mod.redact_sensitive_text

    def flaky(text, **kwargs):
        if "BOOM" in text:
            raise RuntimeError("redactor exploded")
        return real(text, **kwargs)

    monkeypatch.setattr(redact_mod, "redact_sensitive_text", flaky)
    result = {
        "messages": [
            {"role": "assistant", "tool_calls": [
                {"id": "a", "function": {"name": "t1"}},
                {"id": "b", "function": {"name": "t2"}},
                {"id": "c", "function": {"name": "t3"}},
            ]},
            {"role": "tool", "tool_call_id": "a", "content": "first ok"},
            {"role": "tool", "tool_call_id": "b", "content": "BOOM unredactable"},
            {"role": "tool", "tool_call_id": "c", "content": "third ok"},
        ]
    }
    tail = _extract_output_tail(result, max_entries=8, max_chars=600)
    previews = [e["preview"] for e in tail]
    assert previews == ["first ok", "third ok"]


def test_redactor_failure_is_fail_closed_for_the_timeout_caller(monkeypatch):
    """Evidence from a FAILED child is a security boundary: emit nothing at all."""
    import agent.redact as redact_mod

    def always_raises(text, **kwargs):
        raise RuntimeError("redactor exploded")

    monkeypatch.setattr(redact_mod, "redact_sensitive_text", always_raises)
    result = {
        "messages": [
            {"role": "assistant", "tool_calls": [
                {"id": "a", "function": {"name": "t1"}},
                {"id": "b", "function": {"name": "t2"}},
            ]},
            {"role": "tool", "tool_call_id": "a", "content": "healthy"},
            {"role": "tool", "tool_call_id": "b", "content": "also healthy"},
        ]
    }
    assert _extract_output_tail(result, max_entries=8, max_chars=600, fail_closed=True) == []


def test_tail_never_carries_assistant_reasoning():
    """Only role=="tool" messages are read, so chain-of-thought cannot leak."""
    result = {
        "messages": [
            {"role": "assistant", "content": "SECRET-REASONING private deliberation",
             "reasoning_content": "SECRET-REASONING more",
             "tool_calls": [{"id": "c1", "function": {"name": "terminal"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "public output"},
        ]
    }
    tail = _extract_output_tail(result, max_entries=8, max_chars=600)
    assert [e["preview"] for e in tail] == ["public output"]


# ── async re-injection rendering ────────────────────────────────────────────

def test_async_renderer_shows_partial_tail_and_artifact_refs_without_claiming_success():
    text = _format_async_delegation({
        "delegation_id": "d1",
        "status": "timeout",
        "summary": None,
        "error": "Subagent timed out after 60s with 3 API call(s) completed",
        "api_calls": 3,
        "duration_seconds": 60,
        "partial": True,
        "partial_output_tail": [{"tool": "terminal", "preview": "ran the suite", "is_error": False}],
        "child_session_id": "sess-abc",
        "files_written": ["/tmp/work/out.txt"],
    })
    assert "status=timeout" in text
    assert "did not complete successfully" in text
    assert "NOT a completed result" in text
    assert "[terminal] ran the suite" in text
    assert "sess-abc" in text
    assert "/tmp/work/out.txt" in text
    # Must never present a timed-out child as completed.
    assert "Completed" not in text


def test_async_renderer_unchanged_without_partial_fields():
    text = _format_async_delegation({
        "delegation_id": "d2", "status": "timeout", "summary": None,
        "error": "boom", "api_calls": 1, "duration_seconds": 5,
    })
    assert "Partial output (redacted)" not in text
    assert "Child session" not in text


# ── config knob ─────────────────────────────────────────────────────────────

def test_handoff_grace_default_and_disable(monkeypatch):
    from tools import delegate_tool_config

    monkeypatch.setattr(delegate_tool_config, "_cfg", lambda: {})
    monkeypatch.delenv("DELEGATION_HANDOFF_GRACE_SECONDS", raising=False)
    assert delegate_tool_config._get_handoff_grace_seconds() == 300.0

    monkeypatch.setattr(delegate_tool_config, "_cfg", lambda: {"handoff_grace_seconds": 0})
    assert delegate_tool_config._get_handoff_grace_seconds() is None

    monkeypatch.setattr(delegate_tool_config, "_cfg", lambda: {"handoff_grace_seconds": 45})
    assert delegate_tool_config._get_handoff_grace_seconds() == 45.0


def test_handoff_grace_key_is_a_registered_default():
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["delegation"]["handoff_grace_seconds"] == 300


# ── end-to-end bounded timeout against an event-held worker ─────────────────

class _HeldChild:
    """A child that completes one real tool result, then blocks until released."""

    def __init__(self) -> None:
        self.tool_progress_callback = None
        self._credential_pool = None
        self._delegate_saved_tool_names = []
        self._delegate_role = "leaf"
        self._delegate_depth = 1
        self._subagent_id = "handoff-child"
        self._parent_subagent_id = None
        self.model = "test-model"
        self.session_id = "child-session-xyz"
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_estimated_cost_usd = 0.0
        self.session_cost_status = "unknown"
        self._session_messages = [
            {"role": "assistant", "content": "PRIVATE-REASONING do not leak",
             "tool_calls": [{"id": "c1", "function": {"name": "terminal"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": '{"output": "suite green", "exit_code": 0}'},
        ]
        self.started = threading.Event()
        self.release = threading.Event()
        self.steers: list = []

    def run_conversation(self, **_kwargs):
        self.started.set()
        self.release.wait(timeout=10)
        return {"final_response": "", "completed": False, "api_calls": 1, "messages": []}

    def steer(self, text: str) -> bool:
        self.steers.append(text)
        return True

    def hard_interrupt(self, _reason=None):
        self.release.set()

    def get_activity_summary(self):
        return {"api_call_count": 1}

    def close(self):
        self.release.set()


def _run_child_to_timeout(monkeypatch, *, timeout: float, grace):
    child = _HeldChild()
    child._credential_pool = MagicMock()
    child._credential_pool.acquire_lease.return_value = "cred-1"
    child._credential_pool.current.return_value = None
    parent = SimpleNamespace(
        session_id="parent-handoff-test", _current_task_id=None,
        _active_children=[child], _active_children_lock=threading.Lock(),
    )
    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: timeout)
    monkeypatch.setattr(delegate_tool, "_get_handoff_grace_seconds", lambda: grace)
    monkeypatch.setattr(delegate_tool, "_get_worktree_isolation", lambda: False)
    try:
        entry = delegate_tool._run_single_child(
            task_index=0, goal="held worker", child=child, parent_agent=parent,
        )
    finally:
        child.release.set()
    return child, entry


def test_timeout_entry_carries_partial_tail_and_refs_but_not_success(monkeypatch):
    child, entry = _run_child_to_timeout(monkeypatch, timeout=1.0, grace=None)
    assert entry["status"] == "timeout"
    assert entry["summary"] is None
    assert entry["exit_reason"] == "timeout"
    assert entry.get("partial") is True
    tail = entry.get("partial_output_tail") or []
    assert len(tail) == 1
    assert "suite green" in tail[0]["preview"]
    # Chain-of-thought from the assistant turn must not appear.
    assert all("PRIVATE-REASONING" not in e["preview"] for e in tail)
    assert entry.get("child_session_id") == "child-session-xyz"


def test_hard_deadline_is_not_extended_by_the_grace(monkeypatch):
    import time as _time

    start = _time.monotonic()
    _child, entry = _run_child_to_timeout(monkeypatch, timeout=1.0, grace=300)
    elapsed = _time.monotonic() - start
    assert entry["status"] == "timeout"
    assert entry["timeout_seconds"] == 1.0
    # Grace (300s) is clamped to timeout/2 and measured backwards: the wall stays ~1s.
    assert elapsed < 5.0, f"hard deadline was extended: {elapsed:.1f}s"


def test_near_deadline_handoff_is_sent_exactly_once(monkeypatch):
    child, entry = _run_child_to_timeout(monkeypatch, timeout=2.0, grace=300)
    assert entry["status"] == "timeout"
    assert len(child.steers) == 1, f"expected exactly one handoff steer, got {len(child.steers)}"
    text = child.steers[0]
    assert "approaching your hard time limit" in text
    assert "COMPLETED" in text and "REMAINS" in text and "TEST RECEIPTS" in text


def test_no_handoff_steer_when_grace_disabled(monkeypatch):
    child, entry = _run_child_to_timeout(monkeypatch, timeout=1.0, grace=None)
    assert entry["status"] == "timeout"
    assert child.steers == []
