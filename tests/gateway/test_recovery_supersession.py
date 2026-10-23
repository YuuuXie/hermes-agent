"""A synthetic recovery wake belongs to one interruption, not to later human input."""
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import build_resume_recovery_note
from gateway.session import SessionEntry
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source


def _pending():
    runner, adapter = make_restart_runner()
    runner._restart_loop_guard_config = lambda: (0, 3600, 300)
    source = make_restart_source()
    key = runner._session_key_for_source(source)
    now = datetime.now()
    entry = SessionEntry(session_key=key, session_id="sid", created_at=now,
                         updated_at=now, origin=source, resume_pending=True,
                         resume_reason="restart_timeout", last_resume_marked_at=now)
    runner.session_store._entries = {key: entry}
    adapter.handle_message = AsyncMock()
    return runner, adapter, entry


@pytest.mark.asyncio
async def test_reconnect_claims_each_interruption_once_even_when_turn_failed():
    runner, adapter, entry = _pending()
    assert runner._schedule_resume_pending_sessions() == 1
    await asyncio.gather(*list(runner._background_tasks))
    assert entry.resume_pending
    assert runner._schedule_resume_pending_sessions() == 0
    assert adapter.handle_message.await_count == 1
    entry.last_resume_marked_at += timedelta(seconds=1)
    assert runner._schedule_resume_pending_sessions() == 1
    await asyncio.gather(*list(runner._background_tasks))
    assert adapter.handle_message.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["cleared", "suspended", "new_session", "human"])
async def test_scheduled_wake_rechecks_interruption_before_dispatch(change):
    runner, adapter, entry = _pending()
    assert runner._schedule_resume_pending_sessions() == 1
    if change == "cleared":
        entry.resume_pending = False
    elif change == "suspended":
        entry.suspended = True
    elif change == "new_session":
        entry.session_id = "replacement"
    else:
        human = MessageEvent(text="/stop", message_type=MessageType.COMMAND, source=entry.origin)
        runner._queue_startup_restore_event(human)
    await asyncio.gather(*list(runner._background_tasks))
    adapter.handle_message.assert_not_awaited()
    assert not runner._is_session_running(entry.session_key)
    if change == "human":
        assert await runner._drain_startup_restore_queue() == 1
        assert adapter.handle_message.await_args.args[0] is human


def test_interactive_resume_continues_but_new_instruction_wins():
    empty = build_resume_recovery_note("restart_timeout", "", interactive=True)
    assert "CONTINUE the interrupted task" in empty
    assert "already appear in the history" in empty
    assert "ask what they would like to do next" not in empty
    new = build_resume_recovery_note("restart_timeout", "Continue the unfinished task", interactive=True)
    assert "Address the user's NEW message below FIRST" in new
    assert "skip any unfinished work" not in new
    assert "Check recorded results first" in new
