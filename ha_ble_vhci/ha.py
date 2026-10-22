"""Home Assistant websocket client streaming Bluetooth advertisements and scanner health."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

import aiohttp

log = logging.getLogger(__name__)

ADV_ID = 1
SCANNER_ID = 2
NO_SCANNER_GRACE = 15.0  # seconds after (re)subscribing before warning about zero scanners
NO_SCANNER_MESSAGE = "no Bluetooth scanner is connected to Home Assistant: passkey QR cannot work until a proxy comes back"


class HaError(Exception):
    pass


def scanner_label(scanner: dict) -> str:
    return scanner.get("name") or scanner.get("adapter") or scanner.get("source") or "unknown"


class ScannerTracker:
    """Set of Home Assistant Bluetooth scanners currently online, keyed by source."""

    def __init__(self) -> None:
        self.online: dict[str, dict] = {}

    def apply(self, event: dict) -> None:
        """Apply one `bluetooth/subscribe_scanner_details` event ({"add": [...], "remove": [...]})."""
        for scanner in event.get("add", []) or []:
            source = scanner.get("source") if isinstance(scanner, dict) else None
            if not source:
                continue
            if source not in self.online:
                log.info("Bluetooth scanner online: %s", scanner_label(scanner))
            self.online[source] = scanner
        for scanner in event.get("remove", []) or []:
            # HA may name a removed scanner by a bare source string or by its details dict
            source = scanner.get("source") if isinstance(scanner, dict) else scanner
            if source in self.online:
                gone = self.online.pop(source)
                log.warning("Bluetooth scanner offline: %s", scanner_label(gone))
                if not self.online:
                    log.warning(NO_SCANNER_MESSAGE)

    def reset(self) -> None:
        """Forget everything (new websocket session: HA re-sends the full list)."""
        self.online.clear()

    def warn_if_empty(self) -> None:
        if not self.online:
            log.warning(NO_SCANNER_MESSAGE)


async def _open(session: aiohttp.ClientSession, url: str, token: str) -> aiohttp.ClientWebSocketResponse:
    ws_url = url.rstrip("/").replace("http", "ws", 1) + "/api/websocket"
    ws = await session.ws_connect(ws_url, heartbeat=30)
    hello = await ws.receive_json()
    if hello.get("type") != "auth_required":
        raise HaError(f"unexpected greeting: {hello}")
    await ws.send_json({"type": "auth", "access_token": token})
    reply = await ws.receive_json()
    if reply.get("type") != "auth_ok":
        raise HaError(f"authentication failed: {reply}")
    return ws


async def _stream_once(
    url: str, token: str, check_only: bool = False, tracker: ScannerTracker | None = None
) -> AsyncIterator[dict]:
    async with aiohttp.ClientSession() as session:
        async with await _open(session, url, token) as ws:
            await ws.send_json({"id": ADV_ID, "type": "bluetooth/subscribe_advertisements"})
            if tracker is not None:
                tracker.reset()
                await ws.send_json({"id": SCANNER_ID, "type": "bluetooth/subscribe_scanner_details"})
            grace: asyncio.TimerHandle | None = None
            try:
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    data = msg.json()
                    if data.get("id") == ADV_ID and data.get("type") == "result":
                        if not data.get("success", True):
                            raise HaError(f"subscribe failed (admin token needed?): {data}")
                        log.info("subscribed to Home Assistant Bluetooth advertisements")
                        if check_only:
                            return
                        if tracker is not None:
                            grace = asyncio.get_running_loop().call_later(NO_SCANNER_GRACE, tracker.warn_if_empty)
                    elif data.get("id") == ADV_ID and data.get("type") == "event":
                        for adv in data["event"].get("add", []):
                            yield adv
                    elif data.get("id") == SCANNER_ID and data.get("type") == "result":
                        if not data.get("success", True):
                            log.warning("cannot subscribe to scanner details: %s", data)
                            if grace is not None:  # without scanner info "no scanner" would be a false alarm
                                grace.cancel()
                                grace = None
                            tracker = None
                    elif data.get("id") == SCANNER_ID and data.get("type") == "event" and tracker is not None:
                        tracker.apply(data.get("event") or {})
            finally:
                if grace is not None:
                    grace.cancel()


async def check(url: str, token: str) -> None:
    """Authenticate and subscribe once; raise HaError/ClientError on failure."""
    async for _ in _stream_once(url, token, check_only=True):
        pass


async def list_scanners(url: str, token: str, settle: float = 2.0) -> list[dict]:
    """Return the scanners HA reports right now (one-shot; raises HaError/ClientError/OSError).

    HA sends the current scanners right after the subscribe result; `settle` is how long we
    wait for further events once the result arrived.
    """
    tracker = ScannerTracker()
    async with aiohttp.ClientSession() as session:
        async with await _open(session, url, token) as ws:
            await ws.send_json({"id": SCANNER_ID, "type": "bluetooth/subscribe_scanner_details"})
            subscribed = False
            while True:
                try:
                    msg = await asyncio.wait_for(ws.receive(), settle if subscribed else 10)
                except asyncio.TimeoutError:
                    if subscribed:
                        break
                    raise HaError("Home Assistant did not answer the scanner subscription") from None
                if msg.type != aiohttp.WSMsgType.TEXT:
                    if subscribed:
                        break
                    raise HaError("Home Assistant closed the connection before answering the scanner subscription")
                data = msg.json()
                if data.get("id") != SCANNER_ID:
                    continue
                if data.get("type") == "result":
                    if not data.get("success", True):
                        raise HaError(f"subscribe failed (admin token needed?): {data}")
                    subscribed = True
                elif data.get("type") == "event":
                    tracker.apply(data.get("event") or {})
    return list(tracker.online.values())


async def advertisements(
    url: str, token: str, sources: set[str] | None = None, tracker: ScannerTracker | None = None
) -> AsyncIterator[dict]:
    """Yield advertisements forever, reconnecting with backoff."""
    delay = 1.0
    while True:
        try:
            async for adv in _stream_once(url, token, tracker=tracker):
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
