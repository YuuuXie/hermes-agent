"""Gateway-side session binding for async delegations (#57498, #55578).

Three invariants on the messaging-gateway surface, mirroring the TUI rules:

1. Completions are pinned to the spawning session, and delivered only when that
   session still owns the routing key (or via a verified compression
   continuation). An internal completion never re-points a route.
2. A dead/ended spawning session is never resurrected: the injection is
   dropped, fail-closed (never rerouted to the peer's current session).
3. /new interrupts the old conversation's in-flight async delegations.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

import tools.async_delegation as ad


@pytest.fixture(autouse=True)
def _reset_async_delegation():
    ad._reset_for_tests()
    yield
    ad._reset_for_tests()


def _seed_record(delegation_id, session_key="", parent_session_id="", status="running"):
    fn = MagicMock()
    with ad._records_lock:
        ad._records[delegation_id] = {
            "delegation_id": delegation_id,
            "status": status,
            "session_key": session_key,
            "parent_session_id": parent_session_id,
            "interrupt_fn": fn,
        }
    return fn


class TestInterruptForSessionByParentId:
    def test_parent_session_id_selector(self):
        mine = _seed_record("d1", session_key="agent:main:telegram:dm:1", parent_session_id="sess_old")
        other = _seed_record("d2", session_key="agent:main:telegram:dm:2", parent_session_id="sess_other")
        n = ad.interrupt_for_session(parent_session_id="sess_old")
        assert n == 1
        mine.assert_called_once()
        other.assert_not_called()


class TestGatewayPinningFailsClosed:
    """The gateway must follow only verified compression continuations."""

    @staticmethod
    def _entry(session_id):
        from datetime import datetime

        from gateway.config import Platform
        from gateway.session import SessionEntry

        return SessionEntry(
            session_key="agent:main:telegram:group:-100:4",
            session_id=session_id,
            created_at=datetime.now(),
            updated_at=datetime.now(),
            platform=Platform.TELEGRAM,
            chat_type="group",
        )

    def _make_runner(
        self,
        rows,
        *,
        compression_tip=None,
        compression_error=None,
        switched_entry=None,
    ):
        from gateway.run import GatewayRunner
        from gateway.session import AsyncSessionStore

        runner = object.__new__(GatewayRunner)
        db = MagicMock()
        db.get_session = AsyncMock(side_effect=lambda session_id: rows.get(session_id))
        db.get_compression_tip = AsyncMock(
            return_value=compression_tip,
            side_effect=compression_error,
        )
        runner._session_db = db
        runner.session_store = MagicMock()
        runner.session_store.switch_session = MagicMock(return_value=switched_entry)
        runner.session_store.advance_compression_session = MagicMock(
            return_value=switched_entry
        )
        runner._async_session_store = AsyncSessionStore(runner.session_store)
        return runner

    @staticmethod
    def _assert_no_route_change(runner):
        getattr(runner.session_store, "switch_session").assert_not_called()
        getattr(
            runner.session_store, "advance_compression_session"
        ).assert_not_called()


    @pytest.mark.asyncio
    async def test_live_spawning_session_never_steals_a_different_route(self):
        """An internal completion is not a ``/resume``.

        This used to assert the opposite — that a live pin on a DIFFERENT route was
        rebound with ``switch_session``, which ends the route's current session with
        ``session_switch`` and blanks its heartbeat. A background process started
        inside a delegated child carries the human room's key with the CHILD's
        session id, so that rebind silently replaced human conversations with
        workers. Ownership of the pinned row proves nothing about who owns the
        routing key now: fail closed and leave the result in the delegation records.
        """
        current = self._entry("sess_current")
        pinned = self._entry("sess_live")
        runner = self._make_runner(
            {"sess_live": {"id": "sess_live", "ended_at": None}},
            switched_entry=pinned,
        )

        resolved = await runner._resolve_async_delegation_session(
            current, "sess_live"
        )

        assert resolved is None
        self._assert_no_route_change(runner)

    @pytest.mark.asyncio
    async def test_live_pin_equal_to_the_current_route_is_delivered(self):
        """Fail-closed must not mean undeliverable: a completion pinned to the
        session that currently owns the route is still delivered, with no route
        mutation."""
        current = self._entry("sess_current")
        runner = self._make_runner(
            {"sess_current": {"id": "sess_current", "ended_at": None}}
        )

        resolved = await runner._resolve_async_delegation_session(
            current, "sess_current"
        )

        assert resolved is current
        self._assert_no_route_change(runner)

    @pytest.mark.asyncio
    async def test_non_compression_ended_parent_drops(self):
        current = self._entry("sess_old")
        runner = self._make_runner(
            {
                "sess_old": {
                    "id": "sess_old",
                    "ended_at": "2026-07-08T00:00:00",
                    "end_reason": "session_reset",
                }
            }
        )

        resolved = await runner._resolve_async_delegation_session(
            current, "sess_old"
        )

        assert resolved is None
        self._assert_no_route_change(runner)


    @pytest.mark.asyncio
    async def test_intermediate_compression_route_advances_to_same_live_tip(self):
        current = self._entry("sess_middle")
        tip = self._entry("sess_tip")
        runner = self._make_runner(
            {
                "sess_parent": {
                    "id": "sess_parent",
                    "ended_at": "2026-07-08T00:00:00",
                    "end_reason": "compression",
                },
                "sess_middle": {
                    "id": "sess_middle",
                    "ended_at": "2026-07-08T00:01:00",
                    "end_reason": "compression",
                    "parent_session_id": "sess_parent",
                },
                "sess_tip": {
                    "id": "sess_tip",
                    "ended_at": None,
                    "parent_session_id": "sess_middle",
                },
            },
            compression_tip="sess_tip",
            switched_entry=tip,
        )

        resolved = await runner._resolve_async_delegation_session(
            current, "sess_parent"
        )

        assert resolved is tip
        getattr(
            runner.session_store, "advance_compression_session"
        ).assert_called_once_with(current.session_key, "sess_middle", "sess_tip")

    @pytest.mark.asyncio
    async def test_compression_parent_follows_real_sessiondb_lineage(self, tmp_path):
        from gateway.run import GatewayRunner
        from gateway.session import AsyncSessionStore
        from hermes_state import AsyncSessionDB, SessionDB

        session_db = SessionDB(db_path=tmp_path / "state.db")
        session_db.create_session("sess_parent", source="telegram")
        session_db.end_session("sess_parent", end_reason="compression")
        session_db.create_session(
            "sess_tip",
            source="telegram",
            parent_session_id="sess_parent",
        )

        current = self._entry("sess_parent")
        tip = self._entry("sess_tip")
        runner = object.__new__(GatewayRunner)
        runner._session_db = AsyncSessionDB(session_db)
        runner.session_store = MagicMock()
        runner.session_store.switch_session = MagicMock(return_value=tip)
        runner.session_store.advance_compression_session = MagicMock(return_value=tip)
        runner._async_session_store = AsyncSessionStore(runner.session_store)

        resolved = await runner._resolve_async_delegation_session(
            current, "sess_parent"
        )

        assert resolved is tip
        getattr(
            runner.session_store, "advance_compression_session"
        ).assert_called_once_with(current.session_key, "sess_parent", "sess_tip")




@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["none", "revoke", "replace"])
async def test_pending_pin_respects_concurrent_boundary(tmp_path, boundary):
    """Upstream #113690 scenario, asserted against the RETAINED fail-closed contract.

    Upstream f88 lets an undisturbed non-compression pin re-point the route under a CAS.
    This candidate keeps the stricter local guard (#57498): an internal completion is not a
    ``/resume``, so it NEVER takes the routing key from whoever holds it — with or without a
    concurrent boundary. What is retained from upstream is the run-generation invalidation
    check itself (a boundary landing across the await must also invalidate), which is exercised
    here via ``_is_session_run_current``. In every arm the route must stay where it was.
    Real store + real resolver; the DB lookup is event-gated.
    """
    import asyncio
    from types import SimpleNamespace

    from gateway.config import GatewayConfig, Platform
    from gateway.run import GatewayRunner
    from gateway.session import AsyncSessionStore, SessionSource, SessionStore

    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="test-chat", chat_type="dm", user_id="test-user")
    entry = store.get_or_create_session(source)
    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    generation = runner._begin_session_run_generation(entry.session_key)
    entered, release = asyncio.Event(), asyncio.Event()

    async def get_session(session_id):
        entered.set()
        await release.wait()
        return {"id": session_id, "ended_at": None}

    runner._session_db = SimpleNamespace(get_session=AsyncMock(side_effect=get_session))
    task = asyncio.create_task(runner._resolve_async_delegation_session(entry, "test-pinned"))
    await asyncio.wait_for(entered.wait(), 3)
    expected = entry.session_id
    if boundary != "none":
        runner._invalidate_session_run_generation(entry.session_key, reason="test boundary")
        assert not runner._is_session_run_current(entry.session_key, generation)
    if boundary == "replace":
        store.switch_session(entry.session_key, "test-replacement")
        expected = "test-replacement"
    release.set()
    result = await asyncio.wait_for(task, 3)

    # Retained guard: the pinned session never becomes the route owner.
    assert store.lookup_by_session_key(entry.session_key).session_id == expected
    assert store.lookup_by_session_key(entry.session_key).session_id != "test-pinned"
    assert result is None
