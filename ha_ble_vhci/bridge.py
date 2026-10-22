"""Virtual HCI controller (Bumble over /dev/vhci) fed by Home Assistant adverts."""

from __future__ import annotations

import asyncio
import logging

from bumble import hci
from bumble.controller import Controller

from .adv import full_advert, is_random_address, pack_reports

log = logging.getLogger(__name__)


class ScanOnlyController(Controller):
    """A controller that can only scan. Connection attempts always fail."""

    on_scan_enabled = None  # callback(): host just switched LE scanning on

    def on_hci_le_set_scan_enable_command(self, command):
        result = super().on_hci_le_set_scan_enable_command(command)
        self._scan_enabled_hook(command.le_scan_enable)
        return result

    def on_hci_le_set_extended_scan_enable_command(self, command):
        result = super().on_hci_le_set_extended_scan_enable_command(command)
        self._scan_enabled_hook(command.enable)
        return result

    def _scan_enabled_hook(self, enabled) -> None:
        if enabled and self.on_scan_enabled:
            # let the Command Complete event reach the host first
            asyncio.get_running_loop().call_later(0.2, self.on_scan_enabled)

    def on_hci_le_create_connection_command(self, command):
        # No radio behind this controller: report failure instead of hanging.
        self.send_hci_packet(
            hci.HCI_Command_Status_Event(
                status=hci.HCI_ErrorCode.CONNECTION_FAILED_TO_BE_ESTABLISHED,
                num_hci_command_packets=1,
                command_opcode=command.op_code,
            )
        )


def make_controller(host_source, host_sink, address: str) -> ScanOnlyController:
    controller = ScanOnlyController(
        "ha-remote",
        host_source=host_source,
        host_sink=host_sink,
        public_address=address,
    )
    # Keep the kernel on the legacy paths this scan-only controller emits
    # (same workaround as espressif/esp-emulator tools/vhci_bridge.py, Bumble #841).
    controller.le_features &= ~hci.LeFeatureMask.LL_PRIVACY
    controller.le_features &= ~hci.LeFeatureMask.LE_EXTENDED_ADVERTISING
    return controller


def inject_advertisement(controller: Controller, info: dict) -> bool:
    """Deliver one HA advertisement to the host if it is currently scanning."""
    if not controller.le_scan_enable:
        return False
    try:
        address = info["address"]
        chunks = pack_reports(full_advert(info))
    except (KeyError, ValueError):
        log.debug("skipping malformed advertisement: %r", info)
        return False
    addr_type = (
        hci.Address.RANDOM_DEVICE_ADDRESS
        if is_random_address(address)
        else hci.Address.PUBLIC_DEVICE_ADDRESS
    )
    report_cls = hci.HCI_LE_Advertising_Report_Event
    for chunk in chunks:
        # every chunk is its own non-connectable report: the kernel reports those immediately and
        # BlueZ merges the AD data of all reports of one device
        report = report_cls.Report(
            event_type=report_cls.EventType.ADV_NONCONN_IND,
            address_type=addr_type,
            address=hci.Address(address, addr_type),
            data=chunk,
            rssi=max(-127, min(20, int(info.get("rssi", -70)))),
        )
        controller.send_hci_packet(report_cls([report]))
    return True


class Feeder:
    """Feeds HA advertisements to the controller and replays the latest known
    advert of every device when the host starts scanning.

    Home Assistant only forwards an advertisement when it changed, so devices
    that were already seen before the host started scanning would otherwise
    never show up.
    """

    MAX_CACHE = 2000

    def __init__(self, controller: ScanOnlyController) -> None:
        self.controller = controller
        self.cache: dict[str, dict] = {}
        controller.on_scan_enabled = self.replay

    FIDO_UUID_PREFIXES = ("0000fff9-", "0000fde2-")  # FIDO hybrid (caBLE) advertisements

    def feed(self, info: dict) -> bool:
        if any(key.lower().startswith(self.FIDO_UUID_PREFIXES) for key in (info.get("service_data") or {})):
            log.debug("FIDO hybrid advert from %s rssi=%s via %s (host scanning: %s)", info.get("address"),
                     info.get("rssi"), info.get("source"), self.controller.le_scan_enable)
        address = info.get("address")
        if address:
            self.cache.pop(address, None)
            self.cache[address] = info
            if len(self.cache) > self.MAX_CACHE:
                self.cache.pop(next(iter(self.cache)))
        return inject_advertisement(self.controller, info)

    def replay(self) -> None:
        log.debug("host started scanning: replaying %d known device(s)", len(self.cache))
        for info in list(self.cache.values()):
            inject_advertisement(self.controller, info)
