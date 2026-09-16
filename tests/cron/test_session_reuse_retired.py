"""Cron-owned session reuse is retired: fresh per-tick sessions, stale records fail closed.

Behavioural only — nothing here asserts on source text. Each test states the property an
operator depends on after the feature was removed.
"""
import pytest


def _job(tmp_path, **kwargs):
    import cron.jobs as jobs
    return jobs.create_job(kwargs.pop("prompt", "do the thing"), "every 1h", **kwargs)


def test_creation_never_stores_a_session_mode(tmp_path, monkeypatch):
    """New jobs go back to the pre-feature record shape: no session_mode key at all."""
    import cron.jobs as jobs
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    agent_job = _job(tmp_path)
    script_job = _job(tmp_path, prompt="", script="check.py", no_agent=True)

    for created in (agent_job, script_job):
        assert "session_mode" not in created
        assert "session_mode" not in jobs.get_job(created["id"])


def test_creation_rejects_a_retired_mode_argument(tmp_path, monkeypatch):
    """The store no longer accepts session_mode as a creation parameter at all."""
    import cron.jobs as jobs
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    with pytest.raises(TypeError):
        jobs.create_job("do the thing", "every 1h", session_mode="reuse")


def test_every_tick_gets_its_own_session_id(tmp_path, monkeypatch):
    """Exercise actual scheduler runs; replace inference, not session-id construction."""
    import sys
    from datetime import datetime
    from types import ModuleType
    import cron.scheduler as scheduler
    from hermes_cli import runtime_provider

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_CRON_TIMEOUT", "0")
    observed = []

    class FakeAgent:
        def __init__(self, **kwargs):
            observed.append(kwargs["session_id"])

        def run_conversation(self, *_args, **_kwargs):
            return {"final_response": "isolated test completed", "messages": []}

        def get_activity_summary(self):
            return {"seconds_since_activity": 0.0}

    fake = ModuleType("run_agent")
    fake.AIAgent = FakeAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake)
    monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", lambda **_kw: {
        "provider": "test", "api_key": "test-only", "base_url": "http://test.invalid",
        "api_mode": "chat_completions",
    })
    monkeypatch.setattr(scheduler, "_resolve_cron_enabled_toolsets", lambda job, cfg: None)
    job = _job(tmp_path)
    for hour in (12, 13):
        now = datetime(2026, 9, 16, hour)
        monkeypatch.setattr(scheduler, "_hermes_now", lambda now=now: now)
        success, _doc, _alert, error = scheduler.run_job(job)
        assert success, error
    assert len(observed) == 2 and observed[0] != observed[1]
    assert all(sid.startswith(f"cron_{job['id']}_") for sid in observed)


def test_stale_reuse_job_fails_closed_and_is_paused(tmp_path, monkeypatch):
    """A job persisted with session_mode=reuse does NOT silently run as fresh."""
    import cron.jobs as jobs
    import cron.scheduler as scheduler
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    created = _job(tmp_path)
    stored = jobs.get_job(created["id"])
    stored["session_mode"] = "reuse"  # a record written by the retired feature
    jobs.save_jobs([stored])

    success, doc, alert, error = scheduler.run_job(jobs.get_job(created["id"]))

    assert success is False
    assert error and "session_mode" in error
    assert "retired" in error.lower()
    assert alert  # the operator is told, not silently skipped
    assert jobs.get_job(created["id"]).get("enabled") is False or \
        jobs.get_job(created["id"]).get("paused_reason")


def test_a_fresh_valued_stale_record_still_runs(tmp_path, monkeypatch):
    """session_mode='fresh' means exactly today's behaviour, so it must not fail closed."""
    import cron.jobs as jobs
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    created = _job(tmp_path)
    stored = dict(jobs.get_job(created["id"]), session_mode="fresh")
    assert jobs.job_has_retired_session_mode(stored) is False
    assert jobs.job_has_retired_session_mode(jobs.get_job(created["id"])) is False
    assert jobs.job_has_retired_session_mode({"session_mode": "reuse"}) is True


def test_update_can_clear_a_stale_mode_but_not_request_one(tmp_path, monkeypatch):
    """The only supported migration is clearing the field; re-requesting reuse raises."""
    import cron.jobs as jobs
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    created = _job(tmp_path)
    stored = dict(jobs.get_job(created["id"]), session_mode="reuse")
    jobs.save_jobs([stored])

    with pytest.raises(ValueError):
        jobs.update_job(created["id"], {"session_mode": "reuse"})
    assert jobs.get_job(created["id"])["session_mode"] == "reuse"  # unchanged by the refusal

    migrated = jobs.update_job(created["id"], {"session_mode": "fresh"})
    assert "session_mode" not in migrated
    assert "session_mode" not in jobs.get_job(created["id"])
    assert jobs.job_has_retired_session_mode(jobs.get_job(created["id"])) is False


def test_run_listing_preserves_retired_conversation_history(tmp_path):
    """Execution is retired, but existing root history must remain discoverable."""
    from hermes_state import SessionDB

    db = SessionDB(tmp_path / "state.db")
    for sid in ("cron_job9_20260916_120000", "cron_job9_20260916_130000"):
        db.create_session(sid, source="cron")
    db.create_session("cron_job9", source="cron")  # a leftover reuse root, if one exists

    rows = db.list_cron_job_runs("job9")
    ids = {r["id"] for r in rows}
    assert ids == {"cron_job9", "cron_job9_20260916_120000", "cron_job9_20260916_130000"}
    db.close()
