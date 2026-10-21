"""Virtual HCI controller (Bumble over /dev/vhci) fed by Home Assistant adverts."""

from __future__ import annotations

import logging

from bumble import hci
from bumble.controller import Controller

from .adv import adv_payload, is_random_address, split_legacy

log = logging.getLogger(__name__)


class ScanOnlyController(Controller):
    """A controller that can only scan. Connection attempts always fail."""

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
        data = adv_payload(info)
    except (KeyError, ValueError):
        log.debug("skipping malformed advertisement: %r", info)
        return False
    addr_type = (
        hci.Address.RANDOM_DEVICE_ADDRESS
        if is_random_address(address)
        else hci.Address.PUBLIC_DEVICE_ADDRESS
    )
    parts = split_legacy(data)
    report_cls = hci.HCI_LE_Advertising_Report_Event
    for index, part in enumerate(parts):
        report = report_cls.Report(
            event_type=(
                report_cls.EventType.ADV_NONCONN_IND
                if index == 0
                else report_cls.EventType.SCAN_RSP
            ),
            address_type=addr_type,
            address=hci.Address(address, addr_type),
            data=part,
            rssi=max(-127, min(20, int(info.get("rssi", -70)))),
        )
        controller.send_hci_packet(report_cls([report]))
    return True
