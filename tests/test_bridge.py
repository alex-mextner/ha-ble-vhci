"""Advertisement path: HA advert dict -> HCI LE Advertising Report events a host (BlueZ) would see.

The host side is a fake transport (no /dev/vhci); Bumble's real Controller answers the HCI commands.
Run: python3 -m unittest discover -s tests
"""

import asyncio
import unittest

from ha_ble_vhci.adv import full_advert, is_random_address, pack_reports, tlvs
from ha_ble_vhci.bridge import Feeder, inject_advertisement, make_controller

FIDO = "0000fff9-0000-1000-8000-00805f9b34fb"
FIDO_PAYLOAD = "fc23f03f7228492052d7f0aa379ed4fbb40d733f"  # 20 bytes: what caBLE trial-decrypts
IPHONE = {
    "address": "7C:19:53:41:12:42",
    "rssi": -49,
    "raw": "02011a0dff4c001608004da1f9030872b2",  # the latest packet only: Apple continuity
    "manufacturer_data": {"76": "1608004da1f9030872b2"},
    "service_data": {FIDO: FIDO_PAYLOAD},  # HA aggregates what earlier packets carried
    "service_uuids": [],
}
LE_META_EVENT = 0x3E  # HCI event code "LE Meta" (advertising reports ride in it)
COMMAND_STATUS_EVENT = 0x0F
AD_LEN_OFFSET = 13  # H4 type, event code, plen, subevent, num_reports, event type, address type, address[6]


def scan_enable(filter_duplicates: bool = False) -> bytes:
    """H4 command packet: LE Set Scan Enable (opcode 0x200C), enable=1."""
    return bytes([1, 0x0C, 0x20, 2, 1, int(filter_duplicates)])


class FakeHost:
    """Plays both ends of the transport: feeds commands in, records what the controller sends back."""

    def __init__(self):
        self.sink = None
        self.packets = []

    def set_packet_sink(self, sink):  # transport source interface
        self.sink = sink

    def on_packet(self, data):  # transport sink interface
        self.packets.append(bytes(data))

    def send(self, packet):
        self.sink.on_packet(packet)

    def reports(self):
        return [p for p in self.packets if len(p) > 1 and p[1] == LE_META_EVENT]


def reported_ad_data(event: bytes) -> bytes:
    """The AD data inside one LE Advertising Report event (single report, legacy format)."""
    return event[AD_LEN_OFFSET + 1 : AD_LEN_OFFSET + 1 + event[AD_LEN_OFFSET]]


class Adv(unittest.TestCase):
    def test_structures_are_split_whole(self):
        data = bytes.fromhex("02011a0dff4c001608004da1f9030872b2")
        self.assertEqual([t.hex() for t in tlvs(data)], ["02011a", "0dff4c001608004da1f9030872b2"])
        self.assertEqual(tlvs(data[:-3]), [data[:3]])  # a truncated tail is dropped, never reported

    def test_the_fido_service_data_survives_a_continuity_only_raw_packet(self):
        merged = b"".join(full_advert(IPHONE))
        self.assertIn(bytes.fromhex("1716f9ff" + FIDO_PAYLOAD), merged)
        self.assertIn(bytes.fromhex("0dff4c001608004da1f9030872b2"), merged)

    def test_flags_open_every_chunk_and_no_chunk_exceeds_a_legacy_report(self):
        chunks = pack_reports(full_advert(IPHONE))
        self.assertGreaterEqual(len(chunks), 2)  # 15 + 24 bytes do not fit into one 31-byte report
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 31)
            self.assertTrue(chunk.startswith(bytes.fromhex("02011a")), chunk.hex())

    def test_without_raw_the_advert_is_rebuilt_from_parsed_fields(self):
        info = {k: v for k, v in IPHONE.items() if k != "raw"}
        self.assertIn(bytes.fromhex("1716f9ff" + FIDO_PAYLOAD), b"".join(full_advert(info)))

    def test_random_address_types(self):
        self.assertTrue(is_random_address("7C:19:53:41:12:42"))  # resolvable private (top bits 01)
        self.assertTrue(is_random_address("D1:22:33:44:55:66"))  # static random (top bits 11)
        self.assertFalse(is_random_address("10:22:33:44:55:66"))  # public


class Bridge(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):  # Bumble's Controller needs a running loop
        self.host = FakeHost()
        self.controller = make_controller(self.host, self.host, "F0:CA:FE:00:00:09")
        self.feeder = Feeder(self.controller)

    async def wait_for(self, predicate, timeout: float = 3.0):
        """Poll instead of sleeping a fixed time: fast when idle, tolerant on a loaded CI runner."""
        deadline = asyncio.get_running_loop().time() + timeout
        while not predicate():
            if asyncio.get_running_loop().time() > deadline:
                self.fail("timed out waiting for the controller")
            await asyncio.sleep(0.01)

    async def settle(self):
        """Let everything already scheduled on the loop run (the controller delivers with call_soon)."""
        for _ in range(5):
            await asyncio.sleep(0)
        await asyncio.sleep(0.05)

    async def start_scan(self, filter_duplicates: bool = False):
        self.host.send(scan_enable(filter_duplicates))
        await self.wait_for(lambda: self.controller.le_scan_enable)
        await self.settle()  # the replay of known devices is scheduled right after the command completes
        await asyncio.sleep(0.4)

    async def test_nothing_is_reported_while_the_host_is_not_scanning(self):
        self.assertFalse(self.feeder.feed(IPHONE))
        await self.settle()
        self.assertEqual(self.host.reports(), [])

    async def test_known_devices_are_replayed_when_scanning_starts(self):
        self.feeder.feed(IPHONE)  # seen before the host scanned
        await self.start_scan()
        data = b"".join(reported_ad_data(e) for e in self.host.reports())
        self.assertIn(bytes.fromhex("1716f9ff" + FIDO_PAYLOAD), data)

    async def test_live_adverts_reach_a_scanning_host_with_the_fido_service_data(self):
        await self.start_scan()
        self.host.packets.clear()
        self.assertTrue(inject_advertisement(self.controller, IPHONE))
        await self.wait_for(lambda: len(self.host.reports()) >= 2)
        events = self.host.reports()
        self.assertGreaterEqual(len(events), 2)
        self.assertIn(bytes.fromhex("1716f9ff" + FIDO_PAYLOAD), b"".join(reported_ad_data(e) for e in events))

    async def test_every_advert_is_forwarded_even_if_the_host_asks_to_filter_duplicates(self):
        await self.start_scan(filter_duplicates=True)
        self.host.packets.clear()
        inject_advertisement(self.controller, IPHONE)
        inject_advertisement(self.controller, IPHONE)
        expected = 2 * len(pack_reports(full_advert(IPHONE)))
        await self.wait_for(lambda: len(self.host.reports()) >= expected)
        await self.settle()
        self.assertEqual(len(self.host.reports()), expected)

    async def test_malformed_adverts_are_skipped_not_fatal(self):
        await self.start_scan()
        self.host.packets.clear()
        self.assertFalse(inject_advertisement(self.controller, {"rssi": -50}))
        self.assertFalse(inject_advertisement(self.controller, {**IPHONE, "service_data": {FIDO: "zz"}}))
        await self.settle()
        self.assertEqual(self.host.reports(), [])
        self.assertTrue(inject_advertisement(self.controller, IPHONE))  # the pipeline survived

    async def test_connecting_fails_instead_of_hanging(self):
        # LE Create Connection to some address: this controller has no radio, so BlueZ must get a failure
        command = bytes([1, 0x0D, 0x20, 25]) + bytes(25)
        self.host.send(command)
        statuses = lambda: [p for p in self.host.packets if len(p) > 3 and p[1] == COMMAND_STATUS_EVENT]
        await self.wait_for(statuses)
        self.assertNotEqual(statuses()[-1][3], 0)  # non-zero status = the connection failed


if __name__ == "__main__":
    unittest.main()
