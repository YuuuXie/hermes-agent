"""Independent retirement probes: stale-session behavior, not legacy error text.

Only transport and sleep are mocked. Production adapter send/retry/classification
runs from frozen upstream; no source changes or local implementation are applied.
"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from gateway.config import PlatformConfig
from gateway.platforms import weixin


@pytest.mark.parametrize("has_context", [False, True])
@pytest.mark.parametrize("error_text", ["prepare failed", "unknown error"])
@pytest.mark.parametrize("code_field", ["ret", "errcode"])
def test_stale_send_exhausts_only_token_recovery_not_rate_limit(
    has_context, error_text, code_field
):
    adapter = weixin.WeixinAdapter(PlatformConfig(
        enabled=True, token="test-token", extra={"account_id": "test-account"}
    ))
    adapter._session = object()
    adapter._send_session = adapter._session
    adapter._token = "test-token"
    adapter._base_url = "https://example.invalid"
    adapter._token_store.get = lambda *_: "ctx-token" if has_context else None
    adapter._send_chunk_retries = 3
    adapter._send_chunk_retry_delay_seconds = 0
    response = {"ret": None, "errcode": None, "errmsg": error_text}
    response[code_field] = -2
    with patch("gateway.platforms.weixin._send_message", new_callable=AsyncMock) as send, \
         patch("gateway.platforms.weixin.asyncio.sleep", new_callable=AsyncMock) as sleep:
        send.return_value = response
        result = asyncio.run(adapter.send("test-peer", "message"))
    assert result.success is False
    assert send.await_count == (2 if has_context else 1)
    assert sleep.await_count == 0
    assert adapter._rate_limit_circuit_until == 0.0
    assert result.error
    assert "rate limit" not in result.error.lower()
    assert "cooldown" not in result.error.lower()
