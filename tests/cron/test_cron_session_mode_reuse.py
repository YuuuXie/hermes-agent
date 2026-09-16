"""Store/CLI and real SQLite admission tests; tick integration lives in its sibling."""
from __future__ import annotations

import argparse
import itertools
import os
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from hermes_state import SessionDB


def test_store_cli_roundtrip_and_creation_default(tmp_path, monkeypatch):
    import cron.jobs as jobs
    from hermes_cli.subcommands.cron import build_cron_parser
    from hermes_cli.cron import _job_api_kwargs
    from tools.cronjob_tools import CRONJOB_SCHEMA, _HANDLER_FORWARDED_ARGS

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plain = jobs.create_job("do a thing", "every 1h")
    assert plain["session_mode"] == "reuse"
    parser = argparse.ArgumentParser()
    build_cron_parser(parser.add_subparsers(dest="command"), cmd_cron=lambda args: 0)
    args = parser.parse_args(["cron", "create", "every 1h", "check queue", "--session-mode", "reuse"])
    assert _job_api_kwargs(args)["session_mode"] == "reuse"
    updated = jobs.update_job(plain["id"], {"session_mode": args.session_mode})
    assert updated["session_mode"] == jobs.get_job(plain["id"])["session_mode"] == "reuse"
    from tools.cronjob_job_args import _format_job
    assert _format_job(updated)["session_mode"] == "reuse"
    assert jobs.update_job(plain["id"], {"session_mode": "fresh"})["session_mode"] == "fresh"
    with pytest.raises(ValueError):
        jobs.create_job("x", "every 1h", session_mode="target")
    with pytest.raises(SystemExit):
        parser.parse_args(["cron", "create", "every 1h", "x", "--session-mode", "target"])
    # Narrow CLI/programmatic opt-in; no model schema growth or interactive-target surface.
    assert "session_mode" not in CRONJOB_SCHEMA["parameters"]["properties"]
    assert "session_mode" not in _HANDLER_FORWARDED_ARGS


class _Agent:
    def __init__(self, db, sid="cron_j", reuse=True):
        self._session_db = db
        self.session_id = sid
        self._load_durable_history_on_admission = reuse
        self._interrupt_requested = False
        self._session_db_created = False
        self._touch_activity = lambda *a, **k: None
        self._liveness_activity_lock = lambda: threading.Lock()
        self._execution_thread_id = None
        self.statuses = []
        self._emit_status = self.statuses.append
        self._emit_warning = self.statuses.append


_seq = itertools.count()


def _admit(agent):
    from agent.turn_facade_lease import admit_durable_turn_lease
    return admit_durable_turn_lease(
        agent, session_id=agent.session_id, relay_turn_id=f"test-{next(_seq)}",
        task_context={"session_id": agent.session_id, "platform": "cron"},
        conversation_history=None,
    )


@contextmanager
def _other_process_holder(path):
    # A real separate interpreter holds the EXISTING SQLite turn lease, including before a
    # session row exists. stdin EOF releases it; timeout/finally prevents orphan processes.
    code = '''
import os, sys
from pathlib import Path
from hermes_state import SessionDB
db = SessionDB(Path(sys.argv[1]))
holder = f"pid={os.getpid()}:turn=external"
assert db.try_acquire_session_turn_lease("cron_j", holder)
print("ready", flush=True)
sys.stdin.read()
db.release_session_turn_lease("cron_j", holder)
db.close()
'''
    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", code, str(path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=dict(os.environ),
    )
    try:
        import selectors
        with selectors.DefaultSelector() as ready:
            ready.register(proc.stdout, selectors.EVENT_READ)
            assert ready.select(timeout=30), "holder did not start"
        assert proc.stdout.readline().strip() == "ready"
        yield
    finally:
        try:
            _, err = proc.communicate(timeout=20)
            assert proc.returncode == 0, err
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise


@pytest.mark.parametrize("existing", [False, True])
def test_cross_process_refusal_precedes_any_history_read(tmp_path, monkeypatch, existing):
    path = tmp_path / "state.db"
    db = SessionDB(path)
    try:
        if existing:
            db.create_session("cron_j", source="cron")
            db.append_message("cron_j", "user", "private history")
        with _other_process_holder(path):
            def forbidden(*a, **k):
                pytest.fail("history read without exclusive admission")
            with monkeypatch.context() as m:
                m.setattr(db, "get_messages_as_conversation", forbidden)
                result = _admit(_Agent(db))
                assert result.lease is None
                assert result.early_result["failure_reason"] == "session_busy"
                assert result.early_result["failed"] is True  # not a successful/quietly skipped fire
                assert "next scheduled" in result.early_result["final_response"]
                assert "Your message" not in result.early_result["final_response"]
        # The same contender can enter after the foreign holder exits.
        admitted = _admit(_Agent(db))
        assert admitted.lease is not None
        admitted.lease.release()
    finally:
        db.close()


def test_rotation_and_compaction_reload_only_active_tip(tmp_path):
    from cron.scheduler import _cron_execution_session_id
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("cron_j", source="cron")
        db.append_message("cron_j", "user", "old parent")
        db.end_session("cron_j", "compression")
        # Continuations need not share the root's string prefix.
        db.create_session("rotated-tip", source="cron", parent_session_id="cron_j")
        db.append_message("rotated-tip", "user", "large tool transcript")
        db.archive_and_compact("rotated-tip", [{"role": "user", "content": "compact summary"}])
        assert _cron_execution_session_id({"session_mode": "reuse"}, "j") == "cron_j"
        agent = _Agent(db)
        result = _admit(agent)
        try:
            assert agent.session_id == "rotated-tip"
            assert [m["content"] for m in result.conversation_history] == ["compact summary"]
            assert db.get_session("cron_j")["end_reason"] == "compression"
            assert {r["id"] for r in db.list_cron_job_runs("j")} == {"rotated-tip"}
            assert db.list_cron_job_runs("other") == []
        finally:
            result.lease.release()
    finally:
        db.close()


def test_stop_and_fresh_path_do_not_load_history(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("cron_j", source="cron")
        def forbidden(*a, **k):
            pytest.fail("unexpected history read")
        monkeypatch.setattr(db, "get_messages_as_conversation", forbidden)
        fresh = _admit(_Agent(db, reuse=False))
        assert fresh.conversation_history is None
        fresh.lease.release()
        stopped = _Agent(db)
        stopped._interrupt_requested = True
        result = _admit(stopped)
        assert result.lease is None and result.early_result["interrupted"]
    finally:
        db.close()


def test_missing_db_and_foreign_source_fail_closed(tmp_path):
    for unavailable in (None, object()):
        with pytest.raises(RuntimeError, match="durable persistence"):
            _admit(_Agent(unavailable))
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("cron_j", source="cli")
        with pytest.raises(RuntimeError, match="not cron-owned"):
            _admit(_Agent(db))
        assert db.get_session("cron_j")["source"] == "cli"
        assert db.try_acquire_session_turn_lease("cron_j", "next-holder")
        db.release_session_turn_lease("cron_j", "next-holder")
    finally:
        db.close()


def test_reuse_finalizer_does_not_mutate_foreign_holder(tmp_path):
    from cron.scheduler import _finalize_cron_session
    path = tmp_path / "state.db"
    db = SessionDB(path)
    db.create_session("cron_j", source="cron")
    with _other_process_holder(path):
        before = db.get_session("cron_j")
        agent = _Agent(db)
        _finalize_cron_session(db, agent, "j", "name", "cron_j", reuse_session=True)
        assert agent._end_session_on_close is False
        reopened = SessionDB(path)
        try:
            assert reopened.get_session("cron_j") == before
        finally:
            reopened.close()
