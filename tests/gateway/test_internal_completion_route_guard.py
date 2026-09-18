"""Prospective guard: an internal completion must never take over a human route.

Real components: process metadata producer/stamper, completion preflight and
injection, ``_hmwa_resolve_session``, the completion resolver, SessionStore,
SQLite, goal/heartbeat, process_registry ownership transfer.

Replaced: the platform admission boundary (the stub immediately calls the real
session resolver), adapter lookup, source cache and Telegram helpers. No LLM, no
network, no real background command, no gateway service. All identifiers are
unmistakably synthetic (``example.invalid``).

The invariants here are prospective only: they say what a NEW internal
completion may do. They deliberately do not restore, migrate, or rewrite any
session that a past completion already re-pointed.
"""
from types import SimpleNamespace
from pathlib import Path
import json
import os

import pytest
from gateway.config import GatewayConfig, Platform
from gateway.session import AsyncSessionStore, SessionSource, SessionStore
from hermes_state import AsyncSessionDB
from hermes_cli.goals import GoalState, load_goal, save_goal, migrate_goal_to_session
from hermes_cli.heartbeat import (
    HeartbeatState, load_heartbeat, save_heartbeat, migrate_heartbeat_to_session,
)

WORKER = "guard_worker"


def _build_runner(store, db, monkeypatch, seen):
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
    adapter = SimpleNamespace()
    runner._resolve_injection_adapter = lambda *args: adapter
    monkeypatch.setattr(wake, "adapter_supports_push", lambda adapter: True)

    async def admit(_adapter, event):
        resolved = await runner._hmwa_resolve_session(event, event.source)
        seen.append({
            "metadata": dict(event.metadata or {}),
            "resolved": resolved[1].session_id if resolved else None,
        })
    monkeypatch.setattr(wake, "admit_internal_event", admit)
    return runner


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    """A human Matrix route with live goal + heartbeat, plus a delegated worker row."""
    from hermes_constants import get_hermes_home
    home = get_hermes_home().resolve()
    # scripts/run_tests.sh + tests/conftest.py already isolated HERMES_HOME.
    assert home != Path("/home/yuxie1/.hermes")
    assert home.is_relative_to(Path(os.environ["HERMES_HOME"]).resolve())

    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    source = SessionSource(platform=Platform.MATRIX, chat_id="!guard-room:example.invalid",
                           chat_type="dm", user_id="@guard:example.invalid")
    entry = store.get_or_create_session(source)
    human, key = entry.session_id, entry.session_key
    db = store._db_for_key(key)
    assert Path(db.db_path).resolve().is_relative_to(home)

    db.create_session(WORKER, source="subagent", parent_session_id=human,
                      model_config={"_delegate_from": human})
    save_heartbeat(human, HeartbeatState(prompt="GUARD supervise the project",
                                         interval_seconds=1800, created_at=1, fire_count=3))
    save_goal(human, GoalState(goal="GUARD whole-project acceptance", status="active",
                               last_verdict="wait", waiting_on_delegations=1, turns_used=3))
    seen = []
    runner = _build_runner(store, db, monkeypatch, seen)
    return SimpleNamespace(
        store=store, db=db, runner=runner, human=human, key=key, source=source, seen=seen,
        before_hb=db.get_meta(f"heartbeat:{human}"), before_goal=db.get_meta(f"goal:{human}"),
    )


def worker_completion_event(ctx, monkeypatch, worker_id=WORKER):
    """Real producer chain: stamp + watcher descriptor, WITHOUT running a process."""
    import gateway.run as gateway_run
    from agent.delegation_context import delegated_child_context
    from gateway.session_context import set_session_vars, clear_session_vars, get_session_env
    from tools.process_registry import ProcessRegistry
    from tools.terminal_tool_background import _apply_async_support, _register_completion_watcher

    monkeypatch.setattr(gateway_run, "_gateway_runner_ref", None)
    tokens = set_session_vars(
        platform="matrix", source="matrix", chat_id=ctx.source.chat_id, chat_type="dm",
        user_id=ctx.source.user_id, session_key=ctx.key,
        session_id=ctx.store.peek_session_id(ctx.key), async_delivery=True,
    )
    sink = SimpleNamespace(pending_watchers=[])
    try:
        with delegated_child_context(worker_id):
            proc = ProcessRegistry._new_session(
                command="NOT EXECUTED", task_id=worker_id, owner_task_id=worker_id,
                session_key=get_session_env("HERMES_SESSION_KEY"), cwd="/tmp")
            _apply_async_support(proc, {}, True, None)
            _register_completion_watcher(sink, proc, proc.session_key)
    finally:
        clear_session_vars(tokens)
    proc.exited, proc.exit_code, proc.output_buffer = True, 0, "GUARD synthetic output"
    assert proc.parent_session_id == worker_id, "producer stamps the child execution id"
    return ctx.runner._build_process_completion_event(sink.pending_watchers[0], proc, proc.id)


async def inject(ctx, event, expect_preflight=True):
    """Run the real pre-flight, then inject anyway.

    Injecting even when the pre-flight refused is deliberate defense-in-depth: it
    proves the resolver behind the pre-flight also holds the route, so neither layer
    alone is load-bearing.
    """
    claim = await ctx.runner._preflight_completion_delivery(event)
    assert claim.proceed is expect_preflight, claim
    return await ctx.runner._inject_watch_notification("GUARD synthetic result", event)


def route_state(ctx, session_id=None):
    session_id = session_id or ctx.human
    row = ctx.db.get_session(session_id)
    return {
        "route": ctx.store.peek_session_id(ctx.key),
        "ended": row["ended_at"] is not None,
        "end_reason": row["end_reason"],
        "heartbeat_raw": ctx.db.get_meta(f"heartbeat:{session_id}"),
        "goal_raw": ctx.db.get_meta(f"goal:{session_id}"),
    }


# --- Invariant 1: a worker's own completion never takes the human route --------

@pytest.mark.asyncio
@pytest.mark.parametrize("worker_source", ["subagent", "matrix"])
async def test_internal_completion_never_replaces_human_route(ctx, monkeypatch, worker_source):
    """A background process started inside a delegated child carries the human room
    key and the WORKER's session id. It must not end, replace, or strip the human
    session. ``matrix`` models a worker whose source was already written back."""
    if worker_source == "matrix":
        ctx.db.record_gateway_session_peer(WORKER, source="matrix", session_key=ctx.key,
                                           chat_id=ctx.source.chat_id, chat_type="dm",
                                           user_id=ctx.source.user_id)
    # The pre-flight already refuses: the worker's own row does not own the route, so
    # the completion is terminal for delivery instead of taking the human's session.
    await inject(ctx, worker_completion_event(ctx, monkeypatch), expect_preflight=False)
    got = route_state(ctx)
    print("GUARD_OBSERVATION", worker_source, json.dumps(got, default=str))
    assert got["route"] == ctx.human
    assert got["ended"] is False
    assert got["heartbeat_raw"] == ctx.before_hb
    assert got["goal_raw"] == ctx.before_goal
    assert load_goal(ctx.human).status == "active"
    assert load_heartbeat(ctx.human).fire_count == 3


@pytest.mark.asyncio
async def test_unrelated_live_session_cannot_take_the_route_via_internal_event(ctx):
    """Not delegation-specific: no internal completion may re-point a live route at
    another live session. The old behaviour called ``switch_session`` (a ``/resume``)
    and ended the human row."""
    other = "guard_unrelated_live"
    ctx.db.create_session(other, source="matrix")
    resolved = await ctx.runner._resolve_async_delegation_session(
        ctx.store.lookup_by_session_key(ctx.key), other)
    assert resolved is None
    got = route_state(ctx)
    assert (got["route"], got["ended"]) == (ctx.human, False)
    assert got["heartbeat_raw"] == ctx.before_hb


@pytest.mark.asyncio
async def test_preflight_does_not_falsely_ack_a_route_the_resolver_will_refuse(ctx):
    """Coherence: the pre-flight runs BEFORE adapter acceptance and gives a drop an
    honest durable disposition. If it says "deliver" for a pin the in-pipeline
    resolver then refuses, the durable row is acked for a result that never landed.
    A live pin that no longer owns its routing key must be classified terminal."""
    other = "guard_unrelated_live"
    ctx.db.create_session(other, source="matrix")
    evt = {"type": "completion", "session_key": ctx.key, "parent_session_id": other,
           "platform": "matrix", "chat_id": ctx.source.chat_id, "chat_type": "dm",
           "user_id": ctx.source.user_id}
    claim = await ctx.runner._preflight_completion_delivery(evt)
    resolved = await ctx.runner._resolve_async_delegation_session(
        ctx.store.lookup_by_session_key(ctx.key), other)
    assert resolved is None
    assert not claim.proceed, (
        "pre-flight said deliver for a pin the resolver refuses: the durable row "
        "would be acknowledged after adapter acceptance for an injection that dies "
        "inside the pipeline")


# --- Invariant 2: an already-marked (legacy) route keeps owning its room -------

@pytest.fixture
def legacy_ctx(ctx):
    """The route's CURRENT session already carries a historical ``_delegate_from``
    marker — the shape left behind by the incident. It must stay fully usable."""
    ctx.db.patch_session_model_config(ctx.human, {"_delegate_from": "guard_ancient_ancestor"})
    cfg = ctx.db.get_session(ctx.human)["model_config"]
    cfg = json.loads(cfg) if isinstance(cfg, str) else cfg
    assert cfg["_delegate_from"] == "guard_ancient_ancestor"
    return ctx


@pytest.mark.asyncio
async def test_legacy_marked_route_still_receives_its_own_completions(legacy_ctx, monkeypatch):
    """The guard must not orphan a session that a past completion already adopted:
    its own pinned completion, a human message, and its automation all survive."""
    ctx = legacy_ctx
    await inject(ctx, {"type": "completion", "session_key": ctx.key,
                       "parent_session_id": ctx.human, "platform": "matrix",
                       "chat_id": ctx.source.chat_id, "chat_type": "dm",
                       "user_id": ctx.source.user_id})
    assert ctx.seen[-1]["resolved"] == ctx.human
    got = route_state(ctx)
    assert (got["route"], got["ended"]) == (ctx.human, False)
    assert (got["heartbeat_raw"], got["goal_raw"]) == (ctx.before_hb, ctx.before_goal)

    # A normal human message carries no pin and must resolve to the same session.
    from gateway.platforms.event import MessageEvent, MessageType
    human_event = MessageEvent(text="GUARD follow-up from the human",
                               message_type=MessageType.TEXT, source=ctx.source)
    resolved = await ctx.runner._hmwa_resolve_session(human_event, ctx.source)
    assert resolved is not None and resolved[1].session_id == ctx.human


@pytest.mark.asyncio
async def test_child_of_legacy_route_cannot_switch_or_rootwalk(legacy_ctx, monkeypatch):
    """A worker spawned BY the legacy-marked route must neither take the route nor
    make the route walk back to an ancient ancestor."""
    ctx = legacy_ctx
    worker2 = "guard_worker_of_legacy"
    ctx.db.create_session(worker2, source="subagent", parent_session_id=ctx.human,
                          model_config={"_delegate_from": ctx.human})
    await inject(ctx, worker_completion_event(ctx, monkeypatch, worker_id=worker2),
                 expect_preflight=False)
    got = route_state(ctx)
    assert got["route"] == ctx.human, "no switch to the worker and no rootwalk"
    assert got["route"] != "guard_ancient_ancestor"
    assert got["ended"] is False
    assert (got["heartbeat_raw"], got["goal_raw"]) == (ctx.before_hb, ctx.before_goal)


# --- Preserved authorized behaviour -------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["completion", "async_delegation"])
async def test_own_pin_preserves_route_and_automation(ctx, event_type):
    await inject(ctx, {"type": event_type, "session_key": ctx.key,
                       "parent_session_id": ctx.human, "platform": "matrix",
                       "chat_id": ctx.source.chat_id, "chat_type": "dm",
                       "user_id": ctx.source.user_id})
    got = route_state(ctx)
    assert (got["route"], got["ended"]) == (ctx.human, False)
    assert (got["heartbeat_raw"], got["goal_raw"]) == (ctx.before_hb, ctx.before_goal)


@pytest.mark.asyncio
async def test_real_compression_continuation_still_delivers(ctx):
    tip = "guard_compression_tip"
    ctx.db.publish_compression_child(
        parent_session_id=ctx.human, child_session_id=tip, source="matrix",
        require_compression_lease=False, model="offline", model_config={},
        system_prompt="offline", messages=[{"role": "user", "content": "GUARD retained history"}])
    assert migrate_heartbeat_to_session(ctx.human, tip)
    assert migrate_goal_to_session(ctx.human, tip)
    current = ctx.store.lookup_by_session_key(ctx.key)
    resolved = await ctx.runner._resolve_async_delegation_session(current, ctx.human)
    assert resolved.session_id == tip
    assert ctx.store.peek_session_id(ctx.key) == tip
    assert ctx.db.get_session(ctx.human)["end_reason"] == "compression"
    assert load_heartbeat(tip).fire_count == 3
    assert load_goal(tip).goal == "GUARD whole-project acceptance"


@pytest.mark.asyncio
async def test_idle_ended_pin_still_reaches_the_current_route(ctx):
    """Scale-to-zero: the chat route is still valid, so the result is delivered to
    the route's current session (no route mutation)."""
    ctx.db.end_session(ctx.human, end_reason="idle_timeout")
    fresh = ctx.store.get_or_create_session(ctx.source)
    current = ctx.store.lookup_by_session_key(ctx.key)
    resolved = await ctx.runner._resolve_async_delegation_session(current, ctx.human)
    assert resolved is not None and resolved.session_id == current.session_id
    assert ctx.store.peek_session_id(ctx.key) == fresh.session_id


@pytest.mark.asyncio
async def test_user_reset_boundary_still_drops_late_results(ctx):
    """A late result must not cross a user-made boundary into the new conversation."""
    ctx.db.end_session(ctx.human, end_reason="session_reset")
    current = ctx.store.lookup_by_session_key(ctx.key)
    assert await ctx.runner._resolve_async_delegation_session(current, ctx.human) is None


# --- Producer: explicit handoff stays deliverable ------------------------------

@pytest.mark.asyncio
async def test_handoff_rebinds_the_process_to_the_parent_session(ctx):
    """``process_manage(action='handoff')`` is an explicit transfer of a child's
    process to its parent. After it, the completion must pin the PARENT's session so
    the guard can deliver it, instead of pinning the child and being dropped."""
    import weakref
    from tools.process_registry import process_registry, _handoff_process
    import tools.delegate_tool_registry as registry

    class _FakeAgent:
        pass

    parent = _FakeAgent()
    parent.session_id = ctx.human
    parent._current_task_id = "guard-parent-task"
    child = SimpleNamespace(_delegate_parent_ref=weakref.ref(parent))
    proc = process_registry._new_session(command="NOT EXECUTED", task_id=WORKER,
                                         owner_task_id=WORKER, session_key=ctx.key, cwd="/tmp")
    proc.parent_session_id = WORKER
    process_registry._running[proc.id] = proc
    with registry._active_subagents_lock:
        registry._active_subagents[WORKER] = {"agent": child}
    try:
        result = _handoff_process(proc.id, {"data": "GUARD handoff"}, WORKER)
        assert result.get("status") == "handed_off", result
        moved = process_registry.get(proc.id)
        assert moved.parent_session_id == ctx.human, "delivery owner follows the handoff"
        current = ctx.store.lookup_by_session_key(ctx.key)
        resolved = await ctx.runner._resolve_async_delegation_session(
            current, moved.parent_session_id)
        assert resolved is not None and resolved.session_id == ctx.human
    finally:
        process_registry._running.pop(proc.id, None)
        with registry._active_subagents_lock:
            registry._active_subagents.pop(WORKER, None)
