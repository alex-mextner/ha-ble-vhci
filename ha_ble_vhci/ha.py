"""Home Assistant websocket client streaming Bluetooth advertisements and scanner health."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Iterator

import aiohttp

log = logging.getLogger(__name__)

ADV_ID = 1
SCANNER_ID = 2
NO_SCANNER_GRACE = 15.0  # seconds after (re)subscribing before warning about zero scanners
STABLE_SECONDS = 30.0  # a connection that lasted this long was healthy: the next retry starts at 1 s again
NO_SCANNER_MESSAGE = "no Bluetooth scanner is connected to Home Assistant: passkey QR cannot work until a proxy comes back"


class HaError(Exception):
    """Home Assistant answered and refused (token, permissions) or said something unexpected."""


class HaUnreachable(HaError):
    """Home Assistant could not be talked to at all (down, restarting, dropped the connection mid-handshake):
    transient, the streaming loop retries it, `--scanners` reports it with its own exit code."""


class Outage:
    """One Home Assistant outage is one WARNING when it starts and one INFO when it ends; the retries in
    between are DEBUG (a restart takes minutes and used to log a line per retry)."""

    def __init__(self) -> None:
        self.since: float | None = None

    def lost(self, reason: str) -> None:
        if self.since is None:
            self.since = time.monotonic()
            log.warning("Home Assistant unreachable: %s (retrying with backoff)", reason)
        else:
            log.debug("Home Assistant still unreachable: %s", reason)

    def recovered(self) -> None:
        if self.since is not None:
            log.info("Home Assistant is back after %.0f s", time.monotonic() - self.since)
            self.since = None


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


async def _receive_json(ws: aiohttp.ClientWebSocketResponse) -> dict:
    try:
        return await ws.receive_json()
    except (TypeError, ValueError) as err:  # closed or garbled mid-handshake: Home Assistant is going down
        raise HaUnreachable("Home Assistant dropped the connection during the handshake") from err


async def _open(session: aiohttp.ClientSession, url: str, token: str) -> aiohttp.ClientWebSocketResponse:
    ws_url = url.rstrip("/").replace("http", "ws", 1) + "/api/websocket"
    ws = await session.ws_connect(ws_url, heartbeat=30)
    hello = await _receive_json(ws)
    if hello.get("type") != "auth_required":
        raise HaError(f"unexpected greeting: {hello}")
    await ws.send_json({"type": "auth", "access_token": token})
    reply = await _receive_json(ws)
    if reply.get("type") != "auth_ok":
        raise HaError(f"authentication failed: {reply}")
    return ws


async def _stream_once(
    url: str, token: str, check_only: bool = False, tracker: ScannerTracker | None = None,
    outage: Outage | None = None,
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
                        if outage is not None:
                            outage.recovered()
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


@contextlib.contextmanager
def reachable() -> Iterator[None]:
    """One-shot calls: every way of not getting through to Home Assistant (refused, DNS, timeout, dropped
    socket) becomes HaUnreachable, so callers tell it from a Home Assistant that answered and refused."""
    try:
        yield
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as err:
        raise HaUnreachable(str(err) or type(err).__name__) from err


async def check(url: str, token: str) -> None:
    """Authenticate and subscribe once; raises HaError (refused) or HaUnreachable."""
    with reachable():
        async for _ in _stream_once(url, token, check_only=True):
            pass


async def _list_scanners(url: str, token: str, settle: float) -> list[dict]:
    """The scanners HA reports right now (one-shot; raises HaError/ClientError/OSError).

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
                    raise HaUnreachable("Home Assistant did not answer the scanner subscription") from None
                if msg.type != aiohttp.WSMsgType.TEXT:
                    if subscribed:
                        break
                    raise HaUnreachable("Home Assistant closed the connection before answering the scanner subscription")
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


async def list_scanners(url: str, token: str, settle: float = 2.0) -> list[dict]:
    with reachable():
        return await _list_scanners(url, token, settle)


async def advertisements(
    url: str, token: str, sources: set[str] | None = None, tracker: ScannerTracker | None = None
) -> AsyncIterator[dict]:
    """Yield advertisements forever, reconnecting with backoff (1 s doubling to 30 s) however long Home
    Assistant stays away; only a Home Assistant that answers and refuses (HaError) ends the loop. The backoff
    starts over only after a connection that held for STABLE_SECONDS, so a flapping one is not hammered."""
    delay = 1.0
    outage = Outage()
    while True:
        connected = time.monotonic()
        try:
            async for adv in _stream_once(url, token, tracker=tracker, outage=outage):
                if sources and adv.get("source") not in sources:
                    continue
                yield adv
            outage.lost("Home Assistant closed the connection")
        except HaUnreachable as err:
            outage.lost(str(err))
        except HaError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as err:
            outage.lost(f"connection lost: {err}")
        if time.monotonic() - connected >= STABLE_SECONDS:
            delay = 1.0  # it held for a while: this is a new outage, not the same flap
        await asyncio.sleep(delay)
        delay = min(delay * 2, 30.0)
