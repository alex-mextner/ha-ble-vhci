"""Home Assistant outages: one log line per outage, endless quiet reconnect, and a distinct exit code.

No network except a refused connection to a closed local port. Run: python3 -m unittest discover -s tests
"""

import asyncio
import contextlib
import io
import os
import socket
import sys
import unittest
from unittest import mock

import aiohttp

from ha_ble_vhci import __main__ as cli
from ha_ble_vhci import ha
from ha_ble_vhci.ha import HaError, HaUnreachable

ADVERT = {"address": "7C:19:53:41:12:42", "rssi": -50, "source": "30:C9:22:D1:A5:72"}
SUBSCRIBED = {"id": ha.ADV_ID, "type": "result", "success": True}


class Message:
    type = aiohttp.WSMsgType.TEXT

    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


class FakeSocket:
    """A websocket that replays its messages and then closes, like Home Assistant going down."""

    def __init__(self, messages):
        self.messages = messages

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send_json(self, data):
        pass

    async def __aiter__(self):
        for message in self.messages:
            yield Message(message)


class FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def serving(*attempts):
    """Patch the websocket layer: each connection attempt either raises (the exception) or plays messages."""
    queue = list(attempts)

    async def fake_open(session, url, token):
        attempt = queue.pop(0)
        if isinstance(attempt, BaseException):
            raise attempt
        return FakeSocket(attempt)

    return mock.patch.multiple(ha, _open=fake_open, aiohttp=mock.Mock(ClientSession=FakeSession, ClientError=aiohttp.ClientError,
                                                                      WSMsgType=aiohttp.WSMsgType))


class Reconnect(unittest.IsolatedAsyncioTestCase):
    async def collect(self, count, *attempts, delays=None):
        got = []
        sleeps = delays if delays is not None else []

        async def fast_sleep(seconds):
            sleeps.append(seconds)

        with serving(*attempts), mock.patch.object(ha.asyncio, "sleep", fast_sleep):
            stream = ha.advertisements("http://ha:8123", "token")
            try:
                async for advert in stream:
                    got.append(advert)
                    if len(got) == count:
                        break
            finally:
                await stream.aclose()
        return got

    async def test_one_outage_is_one_warning_and_one_recovery_line(self):
        refused = OSError("Cannot connect to host ha:8123 [Connect call failed]")
        with self.assertLogs("ha_ble_vhci.ha", "DEBUG") as logs:
            got = await self.collect(
                2,
                [SUBSCRIBED, {"id": ha.ADV_ID, "type": "event", "event": {"add": [ADVERT]}}],
                refused, refused, refused, refused, refused, refused,
                [SUBSCRIBED, {"id": ha.ADV_ID, "type": "event", "event": {"add": [ADVERT]}}],
            )
        self.assertEqual(got, [ADVERT, ADVERT])
        by_level = {}
        for record in logs.records:
            by_level.setdefault(record.levelname, []).append(record.getMessage())
        outage_warnings = [m for m in by_level.get("WARNING", []) if "unreachable" in m]
        self.assertEqual(len(outage_warnings), 1, by_level)
        back = [m for m in by_level.get("INFO", []) if "is back after" in m]
        self.assertEqual(len(back), 1, by_level)
        self.assertEqual(len([m for m in by_level.get("DEBUG", []) if "still unreachable" in m]), 6)

    async def test_the_retry_delay_doubles_to_thirty_seconds_and_never_gives_up(self):
        delays = []
        failures = [OSError("down")] * 9
        got = await self.collect(
            1, *failures, [SUBSCRIBED, {"id": ha.ADV_ID, "type": "event", "event": {"add": [ADVERT]}}], delays=delays)
        self.assertEqual(got, [ADVERT])
        self.assertEqual(delays, [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0, 30.0])

    async def test_a_flapping_connection_keeps_backing_off_even_though_it_delivers_adverts(self):
        delays = []
        up = [SUBSCRIBED, {"id": ha.ADV_ID, "type": "event", "event": {"add": [ADVERT]}}]
        got = await self.collect(4, up, up, up, up, delays=delays)
        self.assertEqual(got, [ADVERT] * 4)
        self.assertEqual(delays, [1.0, 2.0, 4.0])

    async def test_a_connection_that_held_for_a_while_starts_the_backoff_over(self):
        delays = []
        up = [SUBSCRIBED, {"id": ha.ADV_ID, "type": "event", "event": {"add": [ADVERT]}}]
        with mock.patch.object(ha, "STABLE_SECONDS", 0.0):  # every connection counts as having held
            await self.collect(4, up, up, up, up, delays=delays)
        self.assertEqual(delays, [1.0, 1.0, 1.0])

    async def test_a_home_assistant_that_drops_the_handshake_is_an_outage_not_a_crash(self):
        got = await self.collect(
            1, HaUnreachable("Home Assistant dropped the connection during the handshake"),
            [SUBSCRIBED, {"id": ha.ADV_ID, "type": "event", "event": {"add": [ADVERT]}}])
        self.assertEqual(got, [ADVERT])

    async def test_a_rejected_token_still_ends_the_loop(self):
        with serving(HaError("authentication failed: {'type': 'auth_invalid'}")), \
                self.assertRaises(HaError) as raised:
            async for _ in ha.advertisements("http://ha:8123", "token"):
                pass
        self.assertNotIsInstance(raised.exception, HaUnreachable)

    async def test_a_socket_closed_in_the_handshake_reads_as_unreachable(self):
        class Closed:
            async def receive_json(self):
                raise TypeError("Received message 257:None is not WSMsgType.TEXT")

        with self.assertRaises(HaUnreachable):
            await ha._receive_json(Closed())

    async def test_recovery_is_only_logged_after_an_outage(self):
        outage = ha.Outage()
        with self.assertNoLogs("ha_ble_vhci.ha", "INFO"):
            outage.recovered()


def closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ExitCodes(unittest.TestCase):
    def run_main(self, *argv):
        err = io.StringIO()
        env = {"HA_TOKEN": "not-a-real-token"}
        with mock.patch.object(sys, "argv", ["ha-ble-vhci", *argv]), mock.patch.dict(os.environ, env), \
                contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as raised:
            cli.main()
        return raised.exception.code, err.getvalue()

    def test_a_refused_connection_is_exit_5_for_scanners_and_for_check(self):
        url = f"http://127.0.0.1:{closed_port()}"
        for flag in ("--scanners", "--check"):
            code, message = self.run_main(flag, "--url", url)
            self.assertEqual(code, cli.EXIT_UNREACHABLE, flag)
            self.assertIn("cannot reach Home Assistant", message)

    def test_a_home_assistant_that_answers_and_refuses_stays_exit_1(self):
        async def refuse(_):
            raise HaError("authentication failed: {'type': 'auth_invalid'}")

        with mock.patch.object(cli, "run", refuse):
            code, _ = self.run_main("--scanners", "--url", "http://ha:8123")
        self.assertEqual(code, "authentication failed: {'type': 'auth_invalid'}")  # sys.exit(str) -> status 1

    def test_a_timeout_is_unreachable_too(self):
        async def slow(_):
            raise HaUnreachable("Home Assistant did not answer the scanner subscription")

        with mock.patch.object(cli, "run", slow):
            code, message = self.run_main("--scanners", "--url", "http://ha:8123")
        self.assertEqual(code, cli.EXIT_UNREACHABLE)
        self.assertIn("did not answer", message)

    def test_no_proxy_keeps_exit_4(self):
        async def none(_):
            print(cli.NO_SCANNER_MESSAGE, file=sys.stderr)
            sys.exit(cli.EXIT_NO_SCANNER)

        with mock.patch.object(cli, "run", none):
            code, _ = self.run_main("--scanners", "--url", "http://ha:8123")
        self.assertEqual(code, cli.EXIT_NO_SCANNER)


if __name__ == "__main__":
    unittest.main()
