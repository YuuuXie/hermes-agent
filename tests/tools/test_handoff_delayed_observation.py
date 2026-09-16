"""Observe the real timeout result after a deliberately delayed parent resumes."""
import time
from tools import delegate_tool
from tests.tools.test_delegate_timeout_handoff import _run_child_to_timeout


def test_quarantined_worker_stays_owned_until_explicit_release(monkeypatch):
    original = delegate_tool._run_single_child

    def delayed_observation(**kwargs):
        result = original(**kwargs)
        # Deliberately exceed the fixture's former 10s auto-release: this models
        # a descheduled observer, not a deadline granted to the worker.
        time.sleep(11)
        return result

    monkeypatch.setattr(delegate_tool, '_run_single_child', delayed_observation)
    _child, entry = _run_child_to_timeout(monkeypatch, timeout=1.0, grace=None)
    assert entry['status'] == 'timeout'
    assert entry['worker_state'] == 'quarantined'
