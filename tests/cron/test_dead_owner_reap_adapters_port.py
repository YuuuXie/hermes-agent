"""The local dead-owner reap delta must keep receiving this tick's ``adapters``/``loop``.

Upstream extracted ``tick()`` into ``cron/scheduler_tick.py`` and, in the process, called
``_maybe_reap_dead_owners()`` with NO arguments. Locally the reclaim path is the alert-carrying
``_recover_interrupted_executions_with_alerts`` → ``_alert_reclaimed_execution`` →
``_deliver_result(adapters=…, loop=…)``. Without those two kwargs the "run reclaimed" notice cannot
reach a live gateway adapter (an E2EE room has no other route) — the reclaim still happens, silently.

The existing ``tests/cron/test_dead_owner_claim_reclaim.py`` calls ``scheduler.tick(verbose=False)``
with no adapters, so reverting ``scheduler_tick.py`` to upstream's zero-arg call leaves that file
fully green. These tests close that gap: they assert the plumbing END TO END from ``tick(adapters=…,
loop=…)`` down to ``_deliver_result``.
"""

from __future__ import annotations

import subprocess
import sys
from unittest.mock import patch

import pytest

import cron.scheduler as scheduler_mod


@pytest.fixture()
def executions(monkeypatch, tmp_path):
    import cron.executions as executions_mod

    monkeypatch.setattr(executions_mod, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    return executions_mod


@pytest.fixture(autouse=True)
def _fresh_reap_window(monkeypatch):
    # Per-home reap throttle map (upstream made this a dict keyed by home); empty = reap now.
    monkeypatch.setattr(scheduler_mod, "_last_dead_owner_reap_at", {})


def _dead_pid() -> int:
    proc = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        capture_output=True, text=True, check=True,
    )
    return int(proc.stdout.strip())


def _orphan_claimed_row(executions, job_id: str) -> str:
    record = executions.create_execution(job_id, source="direct")
    with executions._transaction() as conn:
        conn.execute(
            "UPDATE executions SET process_id='dead-cli-process', pid=?, "
            "process_started_at=NULL WHERE id=?",
            (_dead_pid(), record["id"]),
        )
    return record["id"]


def _run_tick(**kwargs):
    with (
        patch.object(scheduler_mod, "get_due_jobs", return_value=[]),
        patch("tools.mcp_tool_lifecycle._kill_orphaned_mcp_children", lambda: None),
    ):
        return scheduler_mod.tick(verbose=False, **kwargs)


def test_tick_forwards_its_adapters_and_loop_into_the_dead_owner_reap(monkeypatch):
    """The narrow regression guard: upstream's zero-arg call fails this with a TypeError."""
    seen = {}

    def _spy(*args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs

    monkeypatch.setattr(scheduler_mod, "_maybe_reap_dead_owners", _spy)
    sentinel_adapters, sentinel_loop = object(), object()

    _run_tick(adapters=sentinel_adapters, loop=sentinel_loop)

    assert seen, "_maybe_reap_dead_owners was never called by the tick"
    assert seen["args"] == (), "the delta passes adapters/loop by keyword"
    assert seen["kwargs"].get("adapters") is sentinel_adapters
    assert seen["kwargs"].get("loop") is sentinel_loop


def test_reclaimed_run_notice_reaches_delivery_with_this_ticks_adapters(monkeypatch, executions):
    """End-to-end: a real dead-owner row, reclaimed by a real tick, must reach ``_deliver_result``
    carrying the tick's adapters/loop — that is the only route to a live gateway adapter."""
    _orphan_claimed_row(executions, "orphaned-job")

    delivered = {}

    def _fake_deliver_result(job, summary, *, adapters=None, loop=None, for_failure=False, **_kw):
        delivered["adapters"] = adapters
        delivered["loop"] = loop
        delivered["for_failure"] = for_failure
        return None  # no delivery error

    monkeypatch.setattr(scheduler_mod, "get_job", lambda _jid: {"id": "orphaned-job", "name": "j"},
                        raising=False)
    monkeypatch.setattr("cron.jobs.get_job", lambda _jid: {"id": "orphaned-job", "name": "j"})
    monkeypatch.setattr(scheduler_mod, "_upsert_incident_for_failure", lambda *_a, **_k: (False, "inc-1"))
    monkeypatch.setattr(scheduler_mod, "_deliver_result", _fake_deliver_result)

    sentinel_adapters, sentinel_loop = object(), object()
    _run_tick(adapters=sentinel_adapters, loop=sentinel_loop)

    assert executions.latest_execution("orphaned-job")["status"] == "unknown"
    assert delivered, "the reclaimed-run notice never reached _deliver_result"
    assert delivered["adapters"] is sentinel_adapters, (
        "reclaim notice lost the tick's adapters — it cannot reach a live gateway adapter")
    assert delivered["loop"] is sentinel_loop
    assert delivered["for_failure"] is True
