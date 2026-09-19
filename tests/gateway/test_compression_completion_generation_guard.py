"""SPEC-review probe: the retained upstream run-generation guard must actually bite.

The candidate keeps upstream f88's ``_is_session_run_current`` pre-check but makes the
non-compression branch fail closed unconditionally, so that branch can no longer
distinguish "guard present" from "guard absent". The ONLY live path where the guard
changes behaviour is the VERIFIED compression continuation, which is still allowed to
advance the route. These tests pin that path:

* boundary="none"  -> the compression continuation advances the route (guard not tripped).
* boundary="revoke" -> a /stop landing across the pinned-row await invalidates the run,
  the route must NOT advance, and the injection is dropped.

Removing the ``_is_session_run_current`` block from
``_resolve_async_delegation_session`` must turn the "revoke" case RED.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


async def _run(tmp_path, boundary):
    from gateway.config import GatewayConfig, Platform
    from gateway.run import GatewayRunner
    from gateway.session import AsyncSessionStore, SessionSource, SessionStore

    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="c1", chat_type="dm", user_id="u1")
    entry = store.get_or_create_session(source)
    old_sid = entry.session_id
    tip_sid = "sess_compression_tip"

    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    generation = runner._begin_session_run_generation(entry.session_key)

    entered, release = asyncio.Event(), asyncio.Event()

    async def get_session(session_id):
        if session_id == old_sid:
            entered.set()
            await release.wait()
            # The pinned session was closed by COMPRESSION (not a user boundary).
            return {"id": old_sid, "ended_at": "2026-09-19T00:00:00Z", "end_reason": "compression"}
        if session_id == tip_sid:
            return {"id": tip_sid, "ended_at": None}
        return None

    async def get_compression_tip(session_id):
        return tip_sid if session_id == old_sid else None

    runner._session_db = SimpleNamespace(
        get_session=AsyncMock(side_effect=get_session),
        get_compression_tip=AsyncMock(side_effect=get_compression_tip),
    )

    task = asyncio.create_task(runner._resolve_async_delegation_session(entry, old_sid))
    await asyncio.wait_for(entered.wait(), 3)
    if boundary == "revoke":
        runner._invalidate_session_run_generation(entry.session_key, reason="spec probe /stop")
        assert not runner._is_session_run_current(entry.session_key, generation)
    release.set()
    result = await asyncio.wait_for(task, 3)
    return store, entry, old_sid, tip_sid, result


@pytest.mark.asyncio
async def test_compression_continuation_advances_route_without_boundary(tmp_path):
    """Control arm: retained compression continuation still works (not over-blocked)."""
    store, entry, old_sid, tip_sid, result = await _run(tmp_path, "none")
    assert result is not None, "verified compression continuation must remain deliverable"
    assert result.session_id == tip_sid
    assert store.lookup_by_session_key(entry.session_key).session_id == tip_sid


@pytest.mark.asyncio
async def test_compression_continuation_blocked_by_concurrent_run_invalidation(tmp_path):
    """Guard arm: a boundary landing across the await must drop the injection.

    This is the assertion that goes RED if the ``_is_session_run_current`` pre-check is
    deleted from ``_resolve_async_delegation_session``.
    """
    store, entry, old_sid, tip_sid, result = await _run(tmp_path, "revoke")
    assert result is None, "invalidated run must not deliver"
    assert store.lookup_by_session_key(entry.session_key).session_id == old_sid, (
        "run-generation guard must prevent the compression advance after invalidation"
    )
