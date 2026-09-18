"""An explicitly handed-off background process must still reach its new owner.

``process_manage(action='handoff')`` is the one place where a subagent's process is
authorized to change delivery owner. The route guard refuses to re-point a routing
key for an internal completion, so if the completion event still pins the CHILD the
handed-off result has nowhere to land and is dropped.

The event is built from the watcher descriptor that was armed at SPAWN time -- before
the handoff -- so the descriptor alone cannot be the authority. These tests drive the
REAL producer path: ``_apply_async_support`` + ``_register_completion_watcher`` ->
``_handoff_process`` -> ``_build_process_completion_event`` with that SAME descriptor
-> real pre-flight + real resolver.

No LLM, no network, no real child process. Temp HERMES_HOME + real SQLite.
"""
from pathlib import Path
from types import SimpleNamespace
import os
import weakref

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.session import AsyncSessionStore, SessionSource, SessionStore
from hermes_state import AsyncSessionDB

WORKER = "handoff_child_session"


def _runner(store, db, monkeypatch):
    from gateway.run import GatewayRunner
    import gateway.wake as wake

    runner = object.__new__(GatewayRunner)
    runner.config = store.config
    runner._session_db = AsyncSessionDB(db)
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    runner._session_key_for_source = store._generate_session_key
    runner._cache_session_source = lambda *args: None
    runner._is_telegram_topic_lane = lambda source: False
    runner._recover_telegram_topic_thread_id = lambda source: None
    runner._resolve_injection_adapter = lambda *args: SimpleNamespace()
    monkeypatch.setattr(wake, "adapter_supports_push", lambda adapter: True)
    return runner


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    """Human gateway session + a delegated child session, on a real temp SQLite."""
    from hermes_constants import get_hermes_home

    home = get_hermes_home().resolve()
    assert home.is_relative_to(Path(os.environ["HERMES_HOME"]).resolve()), (
        "tests must run against an explicit temporary HERMES_HOME")

    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    source = SessionSource(platform=Platform.MATRIX, chat_id="!handoff:example.invalid",
                           chat_type="dm", user_id="@human:example.invalid")
    entry = store.get_or_create_session(source)
    db = store._db_for_key(entry.session_key)
    db.create_session(WORKER, source="subagent", parent_session_id=entry.session_id,
                      model_config={"_delegate_from": entry.session_id})
    return SimpleNamespace(store=store, db=db, runner=_runner(store, db, monkeypatch),
                           human=entry.session_id, key=entry.session_key, source=source)


class _ParentAgent:
    """Stand-in for the live parent agent the registry validates the handoff against."""

    def __init__(self, session_id, gateway_session_key=None):
        self.session_id = session_id
        self._current_task_id = "parent-turn"
        if gateway_session_key is not None:
            self._gateway_session_key = gateway_session_key


def _arm_watcher_for_child(ctx):
    """Spawn (without executing) a child-owned process and arm its watcher, as the
    producer does at spawn time: the descriptor snapshots the CHILD's ids."""
    from agent.delegation_context import delegated_child_context
    from gateway.session_context import set_session_vars, clear_session_vars, get_session_env
    from tools.process_registry import process_registry
    from tools.terminal_tool_background import _apply_async_support, _register_completion_watcher

    tokens = set_session_vars(
        platform="matrix", source="matrix", chat_id=ctx.source.chat_id, chat_type="dm",
        user_id=ctx.source.user_id, session_key=ctx.key,
        session_id=ctx.store.peek_session_id(ctx.key), async_delivery=True)
    sink = SimpleNamespace(pending_watchers=[])
    try:
        with delegated_child_context(WORKER):
            proc = process_registry._new_session(
                command="NOT EXECUTED", task_id=WORKER, owner_task_id=WORKER,
                session_key=get_session_env("HERMES_SESSION_KEY"), cwd="/tmp")
            _apply_async_support(proc, {}, True, None)
            _register_completion_watcher(sink, proc, proc.session_key)
    finally:
        clear_session_vars(tokens)
    return proc, sink.pending_watchers[0]


def _hand_off(proc, parent):
    from tools.process_registry import process_registry, _handoff_process
    import tools.delegate_tool_registry as registry

    child = SimpleNamespace(_delegate_parent_ref=weakref.ref(parent))
    process_registry._running[proc.id] = proc
    with registry._active_subagents_lock:
        registry._active_subagents[WORKER] = {"agent": child}
    return _handoff_process(proc.id, {"data": "watch the build for the parent"}, WORKER)


@pytest.fixture(autouse=True)
def _clean_registry():
    yield
    from tools.process_registry import process_registry
    import tools.delegate_tool_registry as registry
    process_registry._running.clear()
    with registry._active_subagents_lock:
        registry._active_subagents.pop(WORKER, None)


@pytest.mark.asyncio
async def test_handed_off_completion_reaches_the_parent_through_the_armed_watcher(ctx, monkeypatch):
    """The descriptor armed BEFORE the handoff must not out-vote the authorized transfer."""
    import gateway.run as gateway_run
    from tools.process_registry import process_registry

    monkeypatch.setattr(gateway_run, "_gateway_runner_ref", None)
    proc, watcher = _arm_watcher_for_child(ctx)
    assert watcher["parent_session_id"] == WORKER, "descriptor snapshots the CHILD id at spawn"

    parent = _ParentAgent(ctx.human, gateway_session_key=ctx.key)
    assert _hand_off(proc, parent).get("status") == "handed_off"

    moved = process_registry.get(proc.id)
    moved.exited, moved.exit_code, moved.output_buffer = True, 0, "build ok"
    evt = ctx.runner._build_process_completion_event(watcher, moved, moved.id)

    assert evt["parent_session_id"] == ctx.human, (
        "after an authorized handoff the completion must pin the PARENT session, "
        f"not the stale descriptor's child id ({evt['parent_session_id']})")
    assert evt["session_key"] == ctx.key, "the human routing key must survive the handoff"

    claim = await ctx.runner._preflight_completion_delivery(evt)
    assert claim.proceed, "a handed-off result must stay deliverable"
    current = ctx.store.lookup_by_session_key(ctx.key)
    resolved = await ctx.runner._resolve_async_delegation_session(
        current, str(evt.get("parent_session_id") or ""))
    assert resolved is not None and resolved.session_id == ctx.human


@pytest.mark.asyncio
async def test_without_a_handoff_the_child_completion_keeps_the_child_pin(ctx, monkeypatch):
    """No handoff = no transfer of delivery ownership; the anti-takeover guard still bites."""
    import gateway.run as gateway_run
    from tools.process_registry import process_registry

    monkeypatch.setattr(gateway_run, "_gateway_runner_ref", None)
    proc, watcher = _arm_watcher_for_child(ctx)
    process_registry._running[proc.id] = proc
    proc.exited, proc.exit_code, proc.output_buffer = True, 0, "child output"

    evt = ctx.runner._build_process_completion_event(watcher, proc, proc.id)
    assert evt["parent_session_id"] == WORKER

    current = ctx.store.lookup_by_session_key(ctx.key)
    resolved = await ctx.runner._resolve_async_delegation_session(
        current, str(evt.get("parent_session_id") or ""))
    assert resolved is None, "an un-handed-off child completion must not take the human route"
    assert ctx.store.lookup_by_session_key(ctx.key).session_id == ctx.human
    assert not ctx.db.get_session(ctx.human).get("ended_at")


def test_handoff_keeps_the_parents_routing_key_not_its_raw_session_id(ctx):
    """``has_active_for_session`` is keyed by ROUTING key: writing a raw session id there
    made the handed-off process invisible to the route it is supposed to report into."""
    from tools.process_registry import process_registry

    proc, _watcher = _arm_watcher_for_child(ctx)
    parent = _ParentAgent(ctx.human, gateway_session_key=ctx.key)
    assert _hand_off(proc, parent).get("status") == "handed_off"

    moved = process_registry.get(proc.id)
    assert moved.session_key == ctx.key
    assert process_registry.has_active_for_session(ctx.key)


def test_handoff_without_a_gateway_key_preserves_the_processs_existing_key(ctx):
    """A CLI/non-gateway parent exposes no routing key: keep the process's own key
    rather than overwriting it with a raw session id."""
    from tools.process_registry import process_registry

    proc, _watcher = _arm_watcher_for_child(ctx)
    parent = _ParentAgent(ctx.human)  # no _gateway_session_key
    assert _hand_off(proc, parent).get("status") == "handed_off"

    moved = process_registry.get(proc.id)
    assert moved.session_key == ctx.key
    assert moved.parent_session_id == ctx.human


def test_handoff_ownership_is_persisted_in_the_crash_recovery_checkpoint(ctx):
    """A gateway restart rebuilds each watcher descriptor from the checkpoint entry, so the
    handoff's delivery owner and routing key must be part of that persisted state."""
    import json
    from tools.process_registry import process_registry, _checkpoint_path

    proc, _watcher = _arm_watcher_for_child(ctx)
    proc.pid = os.getpid()  # checkpoint only persists sessions with a pid
    parent = _ParentAgent(ctx.human, gateway_session_key=ctx.key)
    assert _hand_off(proc, parent).get("status") == "handed_off"
    process_registry._write_checkpoint()

    entries = json.loads(_checkpoint_path().read_text(encoding="utf-8"))
    entry = next(e for e in entries if e.get("session_id") == proc.id)
    assert entry["parent_session_id"] == ctx.human, (
        "recovery would rebuild the watcher with the pre-handoff child pin")
    assert entry["session_key"] == ctx.key
