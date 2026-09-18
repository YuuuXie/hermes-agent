"""Pre-flight must not acknowledge a durable row for a result it cannot deliver.

``_classify_completion_target`` is the honesty gate in front of adapter acceptance: the
in-pipeline resolver can still fail closed AFTER the adapter accepted, which would ack the
durable ledger row for a result that never landed. These tests assert the LEDGER state, not
just the verdict, so "classified deliver" cannot be mistaken for "delivered".

Real SQLite ledger under a temp HERMES_HOME; no LLM, no network, no adapters.
"""
from pathlib import Path
from types import SimpleNamespace
import os

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.session import AsyncSessionStore, SessionSource, SessionStore
from hermes_state import AsyncSessionDB

OTHER_LIVE = "unrelated_live_session"


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    from gateway.run import GatewayRunner
    import gateway.wake as wake
    from hermes_constants import get_hermes_home

    home = get_hermes_home().resolve()
    assert home.is_relative_to(Path(os.environ["HERMES_HOME"]).resolve()), (
        "tests must run against an explicit temporary HERMES_HOME")

    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    source = SessionSource(platform=Platform.MATRIX, chat_id="!ack:example.invalid",
                           chat_type="dm", user_id="@human:example.invalid")
    entry = store.get_or_create_session(source)
    db = store._db_for_key(entry.session_key)
    db.create_session(OTHER_LIVE, source="matrix")

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
    return SimpleNamespace(store=store, db=db, runner=runner, human=entry.session_id,
                           key=entry.session_key, source=source)


def _dispatch_row(delegation_id, *, session_key, parent_session_id):
    """Create a real ``pending`` durable async-delegation row."""
    import tools.async_delegation as ad
    ad._persist_dispatch({"delegation_id": delegation_id, "session_key": session_key,
                          "parent_session_id": parent_session_id, "dispatched_at": 0.0,
                          "goal": "unit"})
    ad._persist_completion({"delegation_id": delegation_id, "status": "completed"}, {"ok": True})
    return delegation_id


def _delivery_state(delegation_id):
    import tools.async_delegation as ad
    row = ad.get_durable_delegation(delegation_id)
    return (row or {}).get("delivery_state")


def _event(ctx, delegation_id, parent_session_id, *, session_key=None):
    evt = {"type": "async_delegation", "delegation_id": delegation_id,
           "platform": "matrix", "chat_id": ctx.source.chat_id, "chat_type": "dm",
           "user_id": ctx.source.user_id, "parent_session_id": parent_session_id}
    if session_key is not None:
        evt["session_key"] = session_key
    return evt


@pytest.mark.asyncio
async def test_owner_of_the_route_is_delivered_and_the_row_stays_claimable(ctx):
    """The normal case must keep working: pre-flight proceeds and the resolver agrees."""
    did = _dispatch_row("ack_owner", session_key=ctx.key, parent_session_id=ctx.human)
    evt = _event(ctx, did, ctx.human, session_key=ctx.key)

    claim = await ctx.runner._preflight_completion_delivery(evt)
    assert claim.proceed
    current = ctx.store.lookup_by_session_key(ctx.key)
    resolved = await ctx.runner._resolve_async_delegation_session(current, ctx.human)
    assert resolved is not None and resolved.session_id == ctx.human
    assert _delivery_state(did) == "pending", "still pending until delivery is acknowledged"


@pytest.mark.asyncio
async def test_live_pin_that_lost_the_route_is_dropped_not_acked(ctx):
    """Already covered by the guard; asserted here on the LEDGER, not just the verdict."""
    did = _dispatch_row("ack_lost_route", session_key=ctx.key, parent_session_id=OTHER_LIVE)
    evt = _event(ctx, did, OTHER_LIVE, session_key=ctx.key)

    claim = await ctx.runner._preflight_completion_delivery(evt)
    assert not claim.proceed
    assert _delivery_state(did) == "dropped"
    current = ctx.store.lookup_by_session_key(ctx.key)
    assert await ctx.runner._resolve_async_delegation_session(current, OTHER_LIVE) is None


@pytest.mark.asyncio
async def test_transient_route_lookup_failure_releases_the_claim_for_retry(ctx, monkeypatch):
    """A store that throws is uncertainty, not permission: never ack, never terminally drop."""
    did = _dispatch_row("ack_lookup_raises", session_key=ctx.key, parent_session_id=OTHER_LIVE)
    evt = _event(ctx, did, OTHER_LIVE, session_key=ctx.key)

    async def boom(_key):
        raise RuntimeError("simulated session-store failure")
    monkeypatch.setattr(ctx.runner._async_session_store, "lookup_by_session_key", boom)

    verdict = await ctx.runner._classify_completion_target(OTHER_LIVE, ctx.key)
    assert verdict == "route_retry", "a transient lookup failure must be retried, not ack'd"
    claim = await ctx.runner._preflight_completion_delivery(evt)
    assert not claim.proceed and claim.early_result is False, "False = retryable"
    assert _delivery_state(did) == "pending", "the claim is released for the next attempt"


@pytest.mark.asyncio
async def test_gateway_event_without_a_route_key_is_retried_not_falsely_acked(ctx, monkeypatch):
    """A gateway-routable event whose key is missing can be rebuilt; until it is, the target
    is unknown and the resolver may still refuse. Retry instead of claiming success."""
    did = _dispatch_row("ack_no_key", session_key="", parent_session_id=OTHER_LIVE)
    evt = _event(ctx, did, OTHER_LIVE)  # no session_key, but platform/chat_id present

    verdict = await ctx.runner._classify_completion_target(OTHER_LIVE, "")
    assert verdict == "route_retry"
    claim = await ctx.runner._preflight_completion_delivery(evt)
    assert not claim.proceed and claim.early_result is False
    assert _delivery_state(did) == "pending"


@pytest.mark.asyncio
async def test_raw_api_route_without_a_session_key_still_delivers(ctx):
    """Raw API/CLI sessions legitimately have no structured routing key; they must not be
    turned into a retry spin, since no key will ever appear for them."""
    did = _dispatch_row("ack_raw_api", session_key="", parent_session_id=ctx.human)
    evt = {"type": "async_delegation", "delegation_id": did, "platform": "api_server",
           "session_key": "", "parent_session_id": ctx.human}

    verdict = await ctx.runner._classify_completion_target(
        ctx.human, "", evt=evt)
    assert verdict == "deliver", "a legacy raw route has no key to lose"


@pytest.mark.asyncio
async def test_repeated_transient_failures_eventually_converge_to_dropped(ctx, monkeypatch):
    """Retry must not spin forever: the ledger's attempt budget makes it terminal."""
    import tools.async_delegation as ad

    did = _dispatch_row("ack_spin", session_key=ctx.key, parent_session_id=OTHER_LIVE)
    evt = _event(ctx, did, OTHER_LIVE, session_key=ctx.key)

    async def boom(_key):
        raise RuntimeError("simulated session-store failure")
    monkeypatch.setattr(ctx.runner._async_session_store, "lookup_by_session_key", boom)

    for _ in range(ad._MAX_DELIVERY_ATTEMPTS + 1):
        claim = await ctx.runner._preflight_completion_delivery(evt)
        assert not claim.proceed
        if _delivery_state(did) != "pending":
            break
    assert _delivery_state(did) == "dropped", "attempt budget converges the row to terminal"


@pytest.mark.asyncio
async def test_an_unavailable_session_db_does_not_spend_a_delivery_attempt(ctx):
    """A missing session DB is infrastructure downtime, not an unresolvable route: the budget
    must survive it, or a gateway restart storm would drop every pending result."""
    import tools.async_delegation as ad

    did = _dispatch_row("ack_no_db", session_key=ctx.key, parent_session_id=ctx.human)
    evt = _event(ctx, did, ctx.human, session_key=ctx.key)
    ctx.runner._session_db = None

    for _ in range(3):
        claim = await ctx.runner._preflight_completion_delivery(evt)
        assert not claim.proceed and claim.early_result is False
    row = ad.get_durable_delegation(did)
    assert (row["delivery_state"], row["delivery_attempts"]) == ("pending", 0)
