"""A failed pinned route's static fallback build must not be policed by the token-idle timeout.

When the pinned fallback route's summary call FAILS (rather than stalls), ``compress()`` falls through to
``_fallback_summary_for_window`` and builds a static summary locally — under the default
``abort_on_summary_failure=false`` that commit is the guaranteed handoff. That build calls no provider, so
nothing below it ever reaches ``_notify_aux_provider_response`` and the host's commit fence receives no
token progress for the rest of the attempt. Charged to the *idle* (no-token) budget, that silence measures
host CPU time rather than provider health: a build slower than one idle window makes the host cancel its
own commit fence and report "produced no compression" — destroying the very handoff the fallback exists to
guarantee, and leaving the transcript uncompressed.

``c6d22b2`` closed this hole for the deterministic rung only (``route["deterministic"] is True``). The
identical hole remained on every route that reaches the static fallback after its provider call failed.
The worker now announces the provider-free phase (``_notify_aux_local_only_phase`` ->
``CompressionCommitFence.enter_local_only_phase``) and the host stops applying the idle budget for that
leg. The total wall-clock ceiling still bounds a runaway local build.
"""

from __future__ import annotations

import concurrent.futures
import time

from agent.conversation_compression import CompressionCommitFence, _await_worker_within_budget

IDLE = 0.2
CEILING = 4.0


def _local_build_worker(build_seconds: float, *, announce: bool):
    """Stands in for the static-fallback build: local-only, never any stream progress."""

    def _worker(fence: CompressionCommitFence):
        if announce:
            # What agent/context_compressor.py::_fallback_summary_for_window now does via
            # _notify_aux_local_only_phase -> the host's enter_local_only_phase hook.
            fence.enter_local_only_phase()
        deadline = time.monotonic() + build_seconds
        while time.monotonic() < deadline:
            if fence.is_cancelled:
                return "cancelled-mid-build"
            time.sleep(0.005)
        if not fence.begin_commit():
            return "commit-refused"
        return "committed"

    return _worker


def _run(build_seconds: float, *, announce: bool):
    fence = CompressionCommitFence(total_ceiling_seconds=CEILING)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_local_build_worker(build_seconds, announce=announce), fence)
        settled, result = _await_worker_within_budget(
            future, fence, idle=IDLE, ceiling=CEILING, wait_started=time.monotonic()
        )
        if not settled:
            # Mirror the host: cancel admission, then drain the worker so the pool can shut down.
            fence.cancel_before_commit()
        outcome = future.result(timeout=CEILING + 2)
    return settled, result, outcome


def test_local_phase_hook_does_not_invent_provider_output():
    from agent.auxiliary_client import (
        _notify_aux_local_only_phase, aux_local_only_phase_hook, aux_progress_hook,
    )

    for provider_output_seen in (False, True):
        fence = CompressionCommitFence(total_ceiling_seconds=CEILING)
        if provider_output_seen:
            fence.touch_progress()
        with aux_progress_hook(fence.touch_progress), aux_local_only_phase_hook(fence.enter_local_only_phase):
            _notify_aux_local_only_phase()
        assert fence.local_only_phase is True
        assert fence.progress_observed is provider_output_seen


def test_local_only_build_longer_than_the_idle_window_still_commits():
    """The failing-pinned-route build outruns the idle budget but stays under the ceiling: it must commit."""
    settled, result, outcome = _run(IDLE * 4, announce=True)

    assert settled, "the host cancelled a provider-free local build using an idle timer it can never satisfy"
    assert result == "committed"
    assert outcome == "committed"


def test_without_the_announcement_the_same_build_is_idle_cancelled():
    """Reached negative control: disable only the phase announcement and the original failure returns.

    This is the exact defect — with no local-only signal the identical build is cancelled by the idle
    timer, which is what produced "fallback attempt produced no compression" in the integrated run.
    """
    settled, _result, outcome = _run(IDLE * 4, announce=False)

    assert not settled, "control is not reached: the build must be idle-cancelled without the announcement"
    assert outcome in {"cancelled-mid-build", "commit-refused"}


def test_the_total_ceiling_still_bounds_a_runaway_local_build():
    """Announcing the local-only phase suspends the idle budget only; the wall-clock ceiling still refuses."""
    fence = CompressionCommitFence(total_ceiling_seconds=0.6)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_local_build_worker(5.0, announce=True), fence)
        settled, _result = _await_worker_within_budget(
            future, fence, idle=IDLE, ceiling=0.6, wait_started=time.monotonic()
        )
        assert not settled, "the total ceiling must still bound a runaway local-only build"
        fence.cancel_before_commit()
        assert future.result(timeout=8) in {"cancelled-mid-build", "commit-refused"}


def test_a_stalled_provider_route_keeps_its_idle_budget():
    """Ordinary provider-backed routes are unchanged: silence with no announcement still times out fast."""
    fence = CompressionCommitFence(total_ceiling_seconds=CEILING)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        started = time.monotonic()
        future = pool.submit(_local_build_worker(CEILING, announce=False), fence)
        settled, _result = _await_worker_within_budget(
            future, fence, idle=IDLE, ceiling=CEILING, wait_started=started
        )
        elapsed = time.monotonic() - started
        fence.cancel_before_commit()
        future.result(timeout=CEILING + 2)

    assert not settled
    assert elapsed < CEILING, "a silent provider route must still fail at the idle budget, not the ceiling"


def test_failing_pinned_route_commits_through_a_slow_static_fallback_build(tmp_path, monkeypatch):
    """End-to-end at the real seam: primary stalls, the pinned route RAISES, and the static fallback
    build is slow. The handoff must still be committed.

    This is the integrated failure reduced to one deterministic reproducer. It runs the real ``AIAgent``,
    real ``compress_context`` and the real facade timeout wrap — only ``call_llm`` is stubbed and the
    local build is slowed at ``_build_static_fallback_summary``. On the unpatched product the injected
    build delay exceeds the idle window, the host cancels its own commit fence mid-build and the
    transcript comes back uncompressed; it fails on the assertions below rather than on a missing API.
    """
    import os
    from pathlib import Path
    from unittest.mock import patch

    import agent.context_compressor as context_compressor_module
    import agent.conversation_compression as cc
    from agent.auxiliary_client import AuxiliaryExplicitCancellation
    from agent.context_compressor import SUMMARY_PREFIX
    from hermes_state import SessionDB

    idle, ceiling = 0.4, 6.0
    build_delay = idle * 3  # >> idle window, << ceiling: only the idle timer can refuse this
    monkeypatch.setattr(cc, "resolve_context_compression_timeouts", lambda compression_cfg=None: (idle, ceiling))

    db = SessionDB(db_path=Path(tmp_path) / "state-localonly.db")
    session_id = "STALL_LOCAL_ONLY_BUILD"
    db.create_session(session_id, source="cli")
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="test-key", base_url="https://openrouter.ai/api/v1", model="test/model", quiet_mode=True,
            session_db=db, session_id=session_id, skip_context_files=True, skip_memory=True,
        )
    agent._compression_feasibility_checked = True
    agent.compression_in_place = True
    agent._cached_system_prompt = "sys"
    compressor = agent.context_compressor
    compressor.threshold_tokens = 1_000

    # Slow the LOCAL build only; everything else is the product's own path.
    original_build = type(compressor)._build_static_fallback_summary

    def _slow_build(self, turns_to_summarize, reason=None):
        built = original_build(self, turns_to_summarize, reason)
        time.sleep(build_delay)
        return built

    monkeypatch.setattr(context_compressor_module.ContextCompressor, "_build_static_fallback_summary", _slow_build)

    calls = []

    def _call(**kwargs):
        calls.append(kwargs.get("provider") or "primary")
        if "provider" in kwargs:
            raise RuntimeError("fallback route exploded")
        cancelled = getattr(compressor, "_compression_cancelled_check", None)
        deadline = time.monotonic() + ceiling
        while time.monotonic() < deadline and not (callable(cancelled) and cancelled()):
            time.sleep(0.001)
        raise AuxiliaryExplicitCancellation()

    chain_entry = {
        "provider": "custom", "model": "backup-summarizer", "base_url": "https://fallback.invalid/v1",
        "api_key": "sk-fallback", "timeout": idle,
    }
    live = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i} " + ("lorem ipsum " * 300)}
            for i in range(40)]

    with patch("agent.context_compressor.call_llm", side_effect=_call), \
            patch("agent.auxiliary_client._get_auxiliary_task_config", return_value={"fallback_chain": [chain_entry]}):
        out, _prompt = agent._compress_context(live, "sys", approx_tokens=50_000)

    assert calls == ["primary", "custom"], "both routes must be exercised before the static fallback"
    summary_rows = [m for m in out if isinstance(m.get("content"), str) and m["content"].startswith(SUMMARY_PREFIX)]
    assert out is not live, "the slow local-only fallback build was idle-cancelled instead of committed"
    assert len(summary_rows) == 1, "the static fallback summary must be committed as the handoff"
    assert len(out) < len(live)
    assert getattr(agent, "_last_compression_timed_out", None) is not True
