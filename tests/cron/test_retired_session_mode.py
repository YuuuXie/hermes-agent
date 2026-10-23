"""Historical reuse records must be migrated explicitly, not silently run fresh."""

import pytest

from cron import jobs, scheduler


def test_legacy_reuse_is_blocked_before_agent_admission(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    created = jobs.create_job("report", "every 1h", deliver="local")
    stored = dict(jobs.get_job(created["id"]), session_mode="reuse")
    jobs.save_jobs([stored])

    early, prompt = scheduler._prepare_job_prompt(stored, stored["id"], stored["name"], None, None)
    assert prompt is None
    assert early[0] is False
    assert "session_mode" in early[3] and "retired" in early[3]
    assert early[2]
    assert jobs.get_job(stored["id"])["state"] == "paused"


def test_legacy_field_only_clears_on_explicit_update(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    created = jobs.create_job("report", "every 1h", deliver="local")
    assert "session_mode" not in created
    stored = dict(created, session_mode="reuse")
    jobs.save_jobs([stored])

    with pytest.raises(ValueError, match="retired"):
        jobs.update_job(created["id"], {"session_mode": "reuse"})
    assert jobs.get_job(created["id"])["session_mode"] == "reuse"
    migrated = jobs.update_job(created["id"], {"session_mode": "fresh"})
    assert "session_mode" not in migrated
    assert "session_mode" not in jobs.get_job(created["id"])
