"""Reclaimed execution notices use the owning live delivery context."""

import asyncio
import threading
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cron import executions, scheduler
from cron.scheduler_provider import InProcessCronScheduler, _profile_cron_scope
from gateway.config import Platform


def _orphan(job_id):
    record = executions.create_execution(job_id, source="direct")
    with executions._transaction() as conn:
        conn.execute(
            "UPDATE executions SET process_id='dead-owner', pid=?, process_started_at=NULL WHERE id=?",
            (-1, record["id"]),
        )
    return record["id"]


@pytest.fixture
def relay_notice(monkeypatch):
    """The real cron delivery path, but a fake relay/router/loop, never a network client."""
    monkeypatch.setenv("GATEWAY_RELAY_PLATFORMS", "slack")
    adapter = MagicMock()
    adapter.fronts_platform = lambda platform: platform == Platform.SLACK
    adapters = {Platform.RELAY: adapter}
    loop = MagicMock()
    loop.is_running.return_value = True
    config = MagicMock()
    config.platforms = {}
    config.get_home_channel.return_value = None
    monkeypatch.setattr("gateway.config.load_gateway_config", lambda: config)
    monkeypatch.setattr(scheduler, "load_config", lambda: {"cron": {"wrap_response": False}})
    monkeypatch.setattr("tools.send_message_tool.resolve_send_target",
                        lambda platform, rest, **kw: (rest, None, None))
    monkeypatch.setattr("cron.scheduler_delivery._deliver_standalone",
                        lambda *_a, **_kw: pytest.fail("reclaim must not fall back to standalone"))
    delivered = []

    class Router:
        async def _deliver_to_platform(self, target, content, metadata, transport=None):
            delivered.append((target, content, transport))
            return {"success": True, "raw_response": None}

    monkeypatch.setattr("gateway.delivery.DeliveryRouter", lambda *a, **kw: Router())

    def run_coro(coro, given_loop):
        assert given_loop is loop
        future = Future()
        try:
            future.set_result(asyncio.run(coro))
        except BaseException as exc:
            future.set_exception(exc)
        return future

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", run_coro)
    job = {"id": "relay-reclaim", "name": "relay-reclaim", "deliver": "local",
           "failure_deliver": "slack:D0ALERTS"}
    monkeypatch.setattr("cron.jobs.get_job", lambda job_id: dict(job) if job_id == job["id"] else None)
    return SimpleNamespace(job=job, adapters=adapters, loop=loop, delivered=delivered)


def _assert_once(execution_id, relay_notice):
    from cron.incidents import list_incidents

    assert len(relay_notice.delivered) == 1
    target, content, transport = relay_notice.delivered[0]
    assert target.chat_id == "D0ALERTS"
    assert "unknown" in content.lower()
    assert transport.adapter is relay_notice.adapters[Platform.RELAY]
    assert executions.get_execution(execution_id)["delivery_outcome"] == "delivered"
    assert list_incidents()[0]["state"] == "alerted"
    assert scheduler._recover_interrupted_executions_with_alerts(
        adapters=relay_notice.adapters, loop=relay_notice.loop) == 0
    assert len(relay_notice.delivered) == 1


def test_single_profile_startup_sends_reclaim_over_live_relay(relay_notice, monkeypatch):
    execution_id = _orphan(relay_notice.job["id"])
    stop = threading.Event()
    original_tick = scheduler.tick
    monkeypatch.setattr("cron.scheduler.tick", lambda **_kw: stop.set())
    InProcessCronScheduler().start(stop, adapters=relay_notice.adapters, loop=relay_notice.loop)
    _assert_once(execution_id, relay_notice)
    # A later real tick must not alert the already-terminal record again.
    monkeypatch.setattr(scheduler, "get_due_jobs", list)
    monkeypatch.setattr("tools.mcp_tool_lifecycle._kill_orphaned_mcp_children", lambda: None)
    original_tick(verbose=False, adapters=relay_notice.adapters, loop=relay_notice.loop)
    assert len(relay_notice.delivered) == 1


def test_multiplex_startup_uses_owning_profile_relay(relay_notice, monkeypatch, tmp_path):
    from hermes_constants import get_hermes_home

    # An independent store per profile: a default-profile adapter must not cross the scope.
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", None)
    default = tmp_path / "default"
    owner = tmp_path / "owner"
    default.mkdir()
    owner.mkdir()
    with _profile_cron_scope(owner):
        execution_id = _orphan(relay_notice.job["id"])
    stop = threading.Event()
    seen = []

    def tick(**_kw):
        seen.append(get_hermes_home())
        if len(seen) == 2:
            stop.set()

    monkeypatch.setattr("cron.scheduler.tick", tick)
    InProcessCronScheduler().start(
        stop, adapters={Platform.RELAY: object()}, loop=relay_notice.loop, interval=0,
        profile_homes=[("default", default), ("owner", owner)],
        profile_adapters={"owner": relay_notice.adapters}, default_profile="default",
    )
    with _profile_cron_scope(owner):
        _assert_once(execution_id, relay_notice)
    assert len(seen) == 2


def test_manual_reclaim_reuses_owning_gateway_delivery_context(relay_notice, monkeypatch):
    from tools.cronjob_tools import _reap_stale_executions
    import gateway.run

    execution_id = _orphan(relay_notice.job["id"])
    resolved = []
    runner = SimpleNamespace(
        adapters={Platform.RELAY: object()}, _gateway_loop=relay_notice.loop,
        _adapters_for_profile=lambda name: resolved.append(name) or relay_notice.adapters,
        _is_shared_bot_satellite=lambda _name: False,
    )
    monkeypatch.setattr(gateway.run, "_gateway_runner_ref", lambda: runner)
    _reap_stale_executions(relay_notice.job["id"])
    assert len(resolved) == 1
    _assert_once(execution_id, relay_notice)


def test_warning_suppressed_reclaim_is_not_alerted(relay_notice, monkeypatch):
    from cron.incidents import list_incidents

    monkeypatch.setattr(scheduler, "load_config", lambda: {
        "cron": {"wrap_response": False},
        "display": {"suppress_warning_notifications": True},
    })
    execution_id = _orphan(relay_notice.job["id"])
    scheduler._recover_interrupted_executions_with_alerts(
        adapters=relay_notice.adapters, loop=relay_notice.loop)
    assert relay_notice.delivered == []
    assert executions.get_execution(execution_id)["delivery_outcome"] == "suppressed"
    assert list_incidents()[0]["state"] == "detected"
