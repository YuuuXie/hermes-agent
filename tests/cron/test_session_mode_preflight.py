"""Creation rejects a known incompatible reuse configuration before storing a job."""
import pytest


@pytest.mark.parametrize('mode', [None, 'reuse'])
def test_disabled_compression_refuses_new_reuse_but_allows_explicit_fresh(tmp_path, monkeypatch, mode):
    import cron.jobs as jobs
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    (tmp_path/'config.yaml').write_text('compression:\n  enabled: false\n')
    before = jobs.load_jobs()
    with pytest.raises(ValueError, match='compression.enabled'):
        jobs.create_job('check queue', 'every 1h', session_mode=mode)
    assert jobs.load_jobs() == before
    fresh = jobs.create_job('independent check', 'every 1h', session_mode='fresh')
    assert fresh['session_mode'] == 'fresh'
    script = jobs.create_job('', 'every 1h', script='check.py', no_agent=True)
    assert 'session_mode' not in script
    with pytest.raises(ValueError, match='compression.enabled'):
        jobs.update_job(fresh['id'], {'session_mode': 'reuse'})
    assert jobs.get_job(fresh['id'])['session_mode'] == 'fresh'
