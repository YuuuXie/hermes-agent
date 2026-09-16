"""Creation defaults are stored per job; legacy records keep their old semantics."""
import pytest


@pytest.mark.parametrize("kwargs", [{}, {"session_mode": None}])
def test_new_agent_jobs_store_reuse_without_migrating_legacy(tmp_path, monkeypatch, kwargs):
    import cron.jobs as jobs
    from cron.scheduler import cron_session_mode

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    legacy = jobs.create_job("old task", "every 1h", session_mode="fresh")
    legacy.pop("session_mode")  # an actual pre-feature record on disk
    jobs.save_jobs([legacy])

    created = jobs.create_job("new task", "every 1h", **kwargs)
    persisted = jobs.get_job(created["id"])
    assert persisted["session_mode"] == "reuse"
    assert cron_session_mode(persisted) == "reuse"
    assert "session_mode" not in jobs.get_job(legacy["id"])
    assert cron_session_mode(jobs.get_job(legacy["id"])) == "fresh"
    updated = jobs.update_job(legacy["id"], {"prompt": "updated old task"})
    assert "session_mode" not in updated and cron_session_mode(updated) == "fresh"

    explicit = jobs.create_job("independent task", "every 1h", session_mode="fresh")
    assert jobs.get_job(explicit["id"])["session_mode"] == "fresh"
    script = jobs.create_job("", "every 1h", script="check.py", no_agent=True)
    assert "session_mode" not in script  # no conversation to reuse
