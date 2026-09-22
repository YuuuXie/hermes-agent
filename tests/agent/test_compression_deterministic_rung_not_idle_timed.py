"""The deterministic fallback rung must not be policed by the token-idle timeout.

The stall ladder's last rung skips the summary LLM entirely and builds a static fallback summary
locally. Nothing in that path streams, so nothing ever calls ``CompressionCommitFence.touch_progress``.
Policing it with the *idle* (no-token) budget therefore measures host CPU time, not provider health:
whenever the local build takes longer than the idle window the host cancels its own commit fence and
the rung reports "produced no compression" — the very outcome it exists to prevent. The deterministic
rung is bounded by the total wall-clock ceiling instead.
"""

from __future__ import annotations

import time

from agent.context_compressor import DETERMINISTIC_SUMMARY_ROUTE
from agent.conversation_compression import CompressionCommitFence, _run_pinned_compression_retry


def _bounded_worker(build_seconds: float, messages: list):
    """A worker standing in for the deterministic rung: local build, never any stream progress."""

    def _worker(fence: CompressionCommitFence):
        deadline = time.monotonic() + build_seconds
        while time.monotonic() < deadline:
            if fence.is_cancelled:
                # The host pulled commit admission mid-build: the rung can only degrade.
                return messages, "sys"
            time.sleep(0.005)
        if not fence.begin_commit():
            return messages, "sys"
        return [{"role": "user", "content": "compressed"}], "sys"

    return _worker


def test_deterministic_rung_survives_a_local_build_longer_than_the_idle_window():
    """Build time above the idle budget but below the ceiling still commits the fallback summary."""
    messages = [{"role": "user", "content": f"m{i}"} for i in range(8)]
    idle, ceiling = 0.2, 4.0

    result = _run_pinned_compression_retry(
        dict(DETERMINISTIC_SUMMARY_ROUTE),
        worker=_bounded_worker(0.6, messages),
        messages=messages,
        system_prompt_fallback="sys",
        idle_timeout_seconds=idle,
        total_ceiling_seconds=ceiling,
        on_commit_overrun=None,
        on_timeout_cause=None,
        telemetry_agent=None,
        new_fence=lambda: CompressionCommitFence(total_ceiling_seconds=ceiling),
    )

    assert result is not None, "the deterministic rung was cancelled by an idle timer it can never satisfy"
    committed, _prompt = result
    assert committed is not messages and len(committed) < len(messages)


def test_deterministic_rung_still_stops_at_the_total_ceiling():
    """The ceiling remains enforced: a build that outruns the wall-clock budget still degrades."""
    messages = [{"role": "user", "content": f"m{i}"} for i in range(8)]

    result = _run_pinned_compression_retry(
        dict(DETERMINISTIC_SUMMARY_ROUTE),
        worker=_bounded_worker(5.0, messages),
        messages=messages,
        system_prompt_fallback="sys",
        idle_timeout_seconds=0.2,
        total_ceiling_seconds=0.6,
        on_commit_overrun=None,
        on_timeout_cause=None,
        telemetry_agent=None,
        new_fence=lambda: CompressionCommitFence(total_ceiling_seconds=0.6),
    )

    assert result is None, "the total ceiling must still bound the deterministic rung"
