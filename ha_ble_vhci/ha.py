"""Home Assistant websocket client streaming Bluetooth advertisements."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

import aiohttp

log = logging.getLogger(__name__)


class HaError(Exception):
    pass


async def _stream_once(url: str, token: str, check_only: bool = False) -> AsyncIterator[dict]:
    ws_url = url.rstrip("/").replace("http", "ws", 1) + "/api/websocket"
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(ws_url, heartbeat=30) as ws:
            hello = await ws.receive_json()
            if hello.get("type") != "auth_required":
                raise HaError(f"unexpected greeting: {hello}")
            await ws.send_json({"type": "auth", "access_token": token})
            reply = await ws.receive_json()
            if reply.get("type") != "auth_ok":
                raise HaError(f"authentication failed: {reply}")
            await ws.send_json({"id": 1, "type": "bluetooth/subscribe_advertisements"})
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = msg.json()
                if data.get("id") == 1 and data.get("type") == "result":
                    if not data.get("success", True):
                        raise HaError(f"subscribe failed (admin token needed?): {data}")
                    log.info("subscribed to Home Assistant Bluetooth advertisements")
                    if check_only:
                        return
                elif data.get("id") == 1 and data.get("type") == "event":
                    for adv in data["event"].get("add", []):
                        yield adv


async def check(url: str, token: str) -> None:
    """Authenticate and subscribe once; raise HaError/ClientError on failure."""
    async for _ in _stream_once(url, token, check_only=True):
        pass


async def advertisements(url: str, token: str, sources: set[str] | None = None) -> AsyncIterator[dict]:
    """Yield advertisements forever, reconnecting with backoff."""
    delay = 1.0
    while True:
        try:
            async for adv in _stream_once(url, token):
                delay = 1.0
                if sources and adv.get("source") not in sources:
                    continue
                yield adv
            log.warning("Home Assistant closed the connection")
        except HaError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as err:
            log.warning("Home Assistant connection lost: %s", err)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 30.0)
