"""Real ``scheduler.tick`` integration proof for opt-in cron session reuse.

Every test here drives the PRODUCTION dispatch path — ``cron.scheduler.tick`` →
``_process_due_job`` → ``run_job`` → ``TurnFacadeMixin.run_conversation`` →
``admit_durable_turn_lease`` — against a real temp SQLite ``state.db`` and a real
``cron/executions.db``. The constructed agent uses the REAL ``TurnFacadeMixin``; the
inner conversation loop is replaced by a recorder that appends test turns to SQLite.
Provider resolution, environment loading, and delivery are stubbed to avoid real services.
Nothing mocks ``run_job``, admission, the turn lease, or the session store. This proves
scheduler/facade plumbing, not live inference or the production transcript-flush algorithm.

Contract covered:
- two sequential real ticks land in ONE stable ``cron_<job_id>`` conversation, and tick 2's
  inner conversation loop receives tick 1's transcript, carried there by real facade
  admission (not by a test calling admission by hand);
- the per-fire executions ledger keeps DISTINCT real row ids across those ticks;
- compression rotation: a rotated conversation continues on the compression child, and
  compaction-archived rows are excluded from the model-fed reload;
- profile isolation A → B → A through the real ``_profile_cron_scope``;
- the LATEST prompt edit is used by the next tick (no captured stale prompt);
- pause and disable are honoured — a paused/disabled job does not fire and does not append
  to the reused conversation.
"""

from __future__ import annotations

import pathlib
import threading
from datetime import timedelta
from types import SimpleNamespace

import pytest

from unittest.mock import patch

_RUNTIME = {
    "api_key": "test-key",
    "base_url": "https://example.invalid/v1",
    "provider": "openrouter",
    "api_mode": "chat_completions",
}


class _BridgeAgent:
    """Agent the scheduler constructs, built on the REAL ``TurnFacadeMixin``.

    ``run_conversation`` is the production façade, so the durable turn lease, the reuse
    admission branch and the transcript reload are all exercised for real. Only the model
    call underneath (``agent.conversation_loop.run_conversation``) is replaced.
    """

    compression_enabled = True

    def __init__(self, **kwargs):
        self._session_db = kwargs.get("session_db")
        self.session_id = kwargs.get("session_id")
        self.platform = kwargs.get("platform") or "cron"
        self.model = kwargs.get("model") or "test-cron-default-model"
        self._end_session_on_close = True
        self._interrupt_requested = False
        self._interrupt_message = None
        self._active_session_turn_lease_holder = None
        self._active_session_turn_lease_ttl_seconds = None
        self._session_db_created = False
        self._execution_thread_id = None
        self._session_turn_lease_refresh_interval = 60.0
        self._parent_session_id = None
        self._activity_lock = threading.Lock()

    # -- surface admission / the façade touches ------------------------------------
    def _emit_status(self, _message):
        return None

    _emit_warning = _emit_status

    def _touch_activity(self, *_a, **_k):
        return None

    def _liveness_activity_lock(self):
        return self._activity_lock

    def _conversation_root_id(self):
        return self.session_id

    def clear_interrupt(self):
        self._interrupt_requested = False
        self._interrupt_message = None

    def close(self):
        return None


def _real_turn_facade_agent_class():
    from agent.turn_facade import TurnFacadeMixin

    return type("_CronBridgeAgent", (_BridgeAgent, TurnFacadeMixin), {})


class _Recorder:
    """Fake inference boundary: records what the inner loop was handed, persists like a turn."""

    def __init__(self):
        self.calls = []

    def __call__(self, agent, user_message, system_message, conversation_history, task_id,
                 *_args, **_kwargs):
        index = len(self.calls) + 1
        self.calls.append({
            "session_id": agent.session_id,
            "prompt": user_message if isinstance(user_message, str) else str(user_message),
            "history": [
                {"role": m.get("role"), "content": m.get("content")}
                for m in (conversation_history or [])
            ],
        })
        db = agent._session_db
        # Exactly what ordinary loop persistence does: create the row on first use (full
        # metadata lives in production; ``source='cron'`` is the part reuse admission checks),
        # then append the turn.
        if db is not None and db.get_session(agent.session_id) is None:
            db.create_session(agent.session_id, source="cron")
        if db is not None:
            db.append_message(agent.session_id, "user", str(user_message))
            db.append_message(agent.session_id, "assistant", f"answer-{index}")
        return {
            "final_response": f"answer-{index}",
            "messages": [],
            "api_calls": 1,
            "completed": True,
        }


@pytest.fixture()
def cron_tick_env(tmp_path, monkeypatch):
    """A real, isolated Hermes home: real state.db, real cron store, real executions ledger."""
    import cron.jobs as jobs
    import cron.scheduler as sched
    import hermes_state
    from hermes_state_registry import close_all

    home = tmp_path / "home"
    (home / "cron").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", home / "state.db")

    sched._parallel_pool = None
    sched._parallel_pool_max_workers = None
    sched._running_job_ids.clear()

    yield {"home": home, "jobs": jobs, "sched": sched}

    sched._shutdown_parallel_pool()
    sched._running_job_ids.clear()
    close_all()


def _make_due(jobs_mod, job_id):
    """Mark a job due the way a lapsed schedule does."""
    jobs_mod.update_job(job_id, {"next_run_at": "2020-01-01T00:00:00"})


def _run_tick(sched, recorder):
    """One REAL ``scheduler.tick`` with only the inference boundary faked."""
    import agent.conversation_loop as conversation_loop

    with patch("hermes_cli.env_loader.load_hermes_dotenv"), \
         patch("hermes_cli.env_loader.reset_secret_source_cache"), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=_RUNTIME), \
         patch("run_agent.AIAgent", _real_turn_facade_agent_class()), \
         patch.object(conversation_loop, "run_conversation", recorder), \
         patch("cron.scheduler_delivery._resolve_origin", return_value=None), \
         patch.object(sched, "_deliver_result", return_value=None):
        return sched.tick(verbose=False)


def _session_db(home):
    from hermes_state import SessionDB

    return SessionDB(pathlib.Path(home) / "state.db")


# ------------------------------------------------------------------ two real ticks

def test_two_real_ticks_share_one_session_and_keep_distinct_execution_ids(cron_tick_env):
    """The core proof: two real ticks, one conversation, history carried by real admission."""
    import cron.executions as executions

    jobs, sched, home = cron_tick_env["jobs"], cron_tick_env["sched"], cron_tick_env["home"]
    job = jobs.create_job(
        "summarize the queue", "every 5m", name="reuse-job", session_mode="reuse",
        deliver="local")
    job_id = job["id"]
    stable_id = f"cron_{job_id}"

    recorder = _Recorder()
    _make_due(jobs, job_id)
    assert _run_tick(sched, recorder) == 1
    _make_due(jobs, job_id)
    assert _run_tick(sched, recorder) == 1

    # Two real fires, both inside the ONE stable cron-owned session.
    assert len(recorder.calls) == 2
    assert [c["session_id"] for c in recorder.calls] == [stable_id, stable_id]

    # Tick 1 had nothing to continue; tick 2's inner loop was handed tick 1's transcript,
    # and it got there through real TurnFacade admission, not a hand-rolled call.
    assert recorder.calls[0]["history"] == []
    tick2_history = recorder.calls[1]["history"]
    assert [m["role"] for m in tick2_history] == ["user", "assistant"]
    assert "summarize the queue" in tick2_history[0]["content"]
    assert tick2_history[1]["content"] == "answer-1"

    # The durable conversation accumulated both ticks under one id.
    db = _session_db(home)
    try:
        assert db.get_session(stable_id) is not None
        assert db.get_session(stable_id)["source"] == "cron"
        contents = [
            m.get("content") for m in db.get_messages_as_conversation(stable_id)
        ]
        assert "answer-1" in contents and "answer-2" in contents
        # Only ONE cron conversation exists for this job: no per-tick stamped session.
        run_ids = {row["id"] for row in db.list_cron_job_runs(job_id)}
        assert run_ids == {stable_id}
    finally:
        db.close()

    # Per-fire ledger rows stay distinct real executions, despite the shared session.
    rows = executions.list_executions(job_id=job_id)
    assert len(rows) == 2
    assert len({r["id"] for r in rows}) == 2
    assert all(r["job_id"] == job_id and r["status"] == "completed" for r in rows)


def test_fresh_mode_still_gets_one_stamped_session_per_tick(cron_tick_env, monkeypatch):
    """The default is untouched: two ticks, two separate stamped sessions, no history reuse."""
    jobs, sched, home = cron_tick_env["jobs"], cron_tick_env["sched"], cron_tick_env["home"]
    job = jobs.create_job("do a thing", "every 5m", name="fresh-job", deliver="local")
    job_id = job["id"]

    recorder = _Recorder()
    now = sched._hermes_now()
    monkeypatch.setattr(sched, "_hermes_now", lambda: now)
    _make_due(jobs, job_id)
    assert _run_tick(sched, recorder) == 1
    now += timedelta(seconds=2)  # don't depend on real inference taking over one second
    _make_due(jobs, job_id)
    assert _run_tick(sched, recorder) == 1

    session_ids = [c["session_id"] for c in recorder.calls]
    assert len(set(session_ids)) == 2
    assert all(sid.startswith(f"cron_{job_id}_") for sid in session_ids)
    # No transcript is ever carried into a fresh run.
    assert all(c["history"] == [] for c in recorder.calls)

    db = _session_db(home)
    try:
        assert f"cron_{job_id}" not in {row["id"] for row in db.list_cron_job_runs(job_id)}
    finally:
        db.close()


# ------------------------------------------------------------------ compression / compaction

def test_tick_continues_on_the_compression_child_not_the_rotated_parent(cron_tick_env):
    """After a rotation the next real tick runs in the child and sees the carried summary."""
    jobs, sched, home = cron_tick_env["jobs"], cron_tick_env["sched"], cron_tick_env["home"]
    job = jobs.create_job(
        "watch the build", "every 5m", name="rotating-job", session_mode="reuse",
        deliver="local")
    job_id = job["id"]
    stable_id = f"cron_{job_id}"
    child_id = f"{stable_id}_c1"

    recorder = _Recorder()
    _make_due(jobs, job_id)
    assert _run_tick(sched, recorder) == 1
    assert recorder.calls[0]["session_id"] == stable_id

    # Rotate exactly as compression does: end the parent with ``compression`` and fork a child.
    db = _session_db(home)
    try:
        db.end_session(stable_id, "compression")
        db.create_session(child_id, source="cron", parent_session_id=stable_id)
        db.append_message(child_id, "user", "carried-forward summary")
        assert db.get_compression_tip(stable_id) == child_id
    finally:
        db.close()
    from hermes_state_registry import close_all

    close_all()

    _make_due(jobs, job_id)
    assert _run_tick(sched, recorder) == 1

    # The second tick ran in the CHILD, on the carried summary — not the rotated parent.
    assert recorder.calls[1]["session_id"] == child_id
    child_history = [m["content"] for m in recorder.calls[1]["history"]]
    assert "carried-forward summary" in child_history
    assert "answer-1" not in child_history


def test_compaction_archived_rows_are_excluded_from_the_reloaded_history(cron_tick_env):
    """In-place compaction must not regrow: the reload feeds the summary, not what it replaced."""
    jobs, sched, home = cron_tick_env["jobs"], cron_tick_env["sched"], cron_tick_env["home"]
    job = jobs.create_job(
        "long runner", "every 5m", name="compacting-job", session_mode="reuse", deliver="local")
    job_id = job["id"]
    stable_id = f"cron_{job_id}"

    recorder = _Recorder()
    _make_due(jobs, job_id)
    assert _run_tick(sched, recorder) == 1

    db = _session_db(home)
    try:
        # Compact in place: the tick-1 rows become active=0/compacted=1 behind one summary row.
        db.archive_and_compact(
            stable_id,
            [{"role": "user", "content": "compacted summary of earlier ticks"}],
        )
        live = [m.get("content") for m in db.get_messages_as_conversation(stable_id)]
        assert live == ["compacted summary of earlier ticks"]
        # Still on disk and searchable, just not model-fed.
        display = [
            m.get("content")
            for m in db.get_messages_as_conversation(stable_id, include_compacted=True)
        ]
        assert any("answer-1" == c for c in display)
    finally:
        db.close()
    from hermes_state_registry import close_all

    close_all()

    _make_due(jobs, job_id)
    assert _run_tick(sched, recorder) == 1

    reloaded = [m["content"] for m in recorder.calls[1]["history"]]
    assert reloaded == ["compacted summary of earlier ticks"]
    assert "answer-1" not in reloaded


# ------------------------------------------------------------------ freshness of the job record

def test_next_tick_uses_the_latest_prompt_edit(cron_tick_env):
    """The reused conversation must not pin a stale prompt: tick 2 reads the edited record."""
    jobs, sched = cron_tick_env["jobs"], cron_tick_env["sched"]
    job = jobs.create_job(
        "ORIGINAL PROMPT", "every 5m", name="editable-job", session_mode="reuse",
        deliver="local")
    job_id = job["id"]

    recorder = _Recorder()
    _make_due(jobs, job_id)
    assert _run_tick(sched, recorder) == 1
    assert "ORIGINAL PROMPT" in recorder.calls[0]["prompt"]

    jobs.update_job(job_id, {"prompt": "EDITED PROMPT"})
    _make_due(jobs, job_id)
    assert _run_tick(sched, recorder) == 1

    assert "EDITED PROMPT" in recorder.calls[1]["prompt"]
    assert "ORIGINAL PROMPT" not in recorder.calls[1]["prompt"]


@pytest.mark.parametrize("stop_mode", ["pause", "disable"])
def test_pause_and_disable_stop_the_reused_session_from_advancing(cron_tick_env, stop_mode):
    """A paused/disabled reuse job does not fire and appends nothing to its conversation.

    Pause goes through the real ``pause_job`` API rather than a hand-written ``paused`` key:
    the stored shape is ``enabled=False`` + ``state='paused'`` + ``paused_at``, and asserting
    on an invented field would pass while proving nothing.
    """
    jobs, sched, home = cron_tick_env["jobs"], cron_tick_env["sched"], cron_tick_env["home"]
    job = jobs.create_job(
        "keep watching", "every 5m", name="pausable-job", session_mode="reuse", deliver="local")
    job_id = job["id"]
    stable_id = f"cron_{job_id}"

    recorder = _Recorder()
    _make_due(jobs, job_id)
    assert _run_tick(sched, recorder) == 1

    db = _session_db(home)
    try:
        before = len(db.get_messages_as_conversation(stable_id))
    finally:
        db.close()
    from hermes_state_registry import close_all

    close_all()

    if stop_mode == "pause":
        paused = jobs.pause_job(job_id, reason="operator paused")
        assert paused["enabled"] is False and paused["state"] == "paused"
    else:
        jobs.update_job(job_id, {"enabled": False})
    _make_due(jobs, job_id)
    assert _run_tick(sched, recorder) == 0  # not dispatched at all

    assert len(recorder.calls) == 1  # no second turn reached the inner loop
    db = _session_db(home)
    try:
        assert len(db.get_messages_as_conversation(stable_id)) == before
    finally:
        db.close()
    close_all()


@pytest.mark.parametrize("unavailable", ["persistence", "compression"])
def test_reuse_fails_closed_without_required_resources(cron_tick_env, monkeypatch, unavailable):
    import cron.executions as executions
    jobs, sched = cron_tick_env["jobs"], cron_tick_env["sched"]
    job = jobs.create_job("check queue", "every 5m", session_mode="reuse", deliver="local")
    if unavailable == "persistence":
        monkeypatch.setattr(sched, "_open_cron_session_db", lambda _job: None)
    else:
        monkeypatch.setattr(_BridgeAgent, "compression_enabled", False)
    recorder = _Recorder()
    _make_due(jobs, job["id"])
    assert _run_tick(sched, recorder) == 1
    assert recorder.calls == []
    rows = executions.list_executions(job_id=job["id"])
    assert len(rows) == 1 and rows[0]["status"] == "failed"


# ------------------------------------------------------------------ profile isolation A → B → A

def test_profile_scope_isolation_a_then_b_then_a(tmp_path, monkeypatch):
    """Same reuse job id in two profiles: real ``_profile_cron_scope``, two unrelated stores.

    A → B → A so the return to A also proves B's scope did not leave the thread pointed at
    B's home or leak B's transcript into A's reused conversation.
    """
    import cron.jobs as jobs
    import cron.scheduler as sched
    import hermes_state
    from cron.scheduler_provider import _profile_cron_scope
    from hermes_state_registry import close_all

    home_a = tmp_path / "A"
    home_b = tmp_path / "B"
    for home in (home_a, home_b):
        (home / "cron").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "unused"))
    # The global conftest PINS ``DEFAULT_DB_PATH`` to one per-test home, and a pinned constant
    # wins over ``get_hermes_home()`` in ``_default_db_path()`` — which would send both
    # profiles' sessions to the SAME state.db and make this test assert nothing. Restore the
    # import-time sentinel so the scope's home override is honoured exactly as in production.
    monkeypatch.setattr(
        hermes_state, "DEFAULT_DB_PATH", hermes_state._IMPORT_DEFAULT_DB_PATH)

    recorder = _Recorder()
    job_ids = {}

    def _tick_in(home, label):
        sched._parallel_pool = None
        sched._parallel_pool_max_workers = None
        sched._running_job_ids.clear()
        with _profile_cron_scope(home):
            if label not in job_ids:
                # Same logical job/session id in both DBs makes a profile leak observable.
                with patch.object(jobs, "uuid", SimpleNamespace(
                    uuid4=lambda: SimpleNamespace(hex="0123456789ab"))):
                    created = jobs.create_job(
                        f"{label} work", "every 5m", name=f"{label}-job", session_mode="reuse",
                        deliver="local")
                job_ids[label] = created["id"]
            _make_due(jobs, job_ids[label])
            fired = _run_tick(sched, recorder)
        sched._shutdown_parallel_pool()
        close_all()
        return fired

    try:
        assert _tick_in(home_a, "A") == 1
        assert _tick_in(home_b, "B") == 1
        assert _tick_in(home_a, "A") == 1
    finally:
        sched._shutdown_parallel_pool()
        sched._running_job_ids.clear()
        close_all()

    assert job_ids["A"] == job_ids["B"]
    a1, b1, a2 = recorder.calls
    assert a1["session_id"] == f"cron_{job_ids['A']}"
    assert b1["session_id"] == f"cron_{job_ids['B']}"
    # Back in A: the SAME conversation as A's first tick, continuing A's transcript.
    assert a2["session_id"] == a1["session_id"]
    a2_history = [m["content"] for m in a2["history"]]
    assert any("A work" in c for c in a2_history)
    assert a2_history[-1] == "answer-1"

    # B started clean and never saw A's transcript.
    assert b1["history"] == []

    # Each profile's state.db holds only its own conversation.
    for home, label, other in ((home_a, "A", "B"), (home_b, "B", "A")):
        db = _session_db(home)
        try:
            own = db.get_messages_as_conversation(f"cron_{job_ids[label]}")
            assert own, f"{label} should own its reused conversation"
            assert not any(f"{other} work" in str(m.get("content")) for m in own)
        finally:
            db.close()
    close_all()
