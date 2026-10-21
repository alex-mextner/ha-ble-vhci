"""Expose Home Assistant Bluetooth proxies (ESPHome) as a local scan-only hciN."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from bumble.transport import open_transport

from .bridge import inject_advertisement, make_controller
from .ha import HaError, advertisements, check

DEFAULT_TOKEN_FILE = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "ha-ble-vhci" / "token"


def read_token(args: argparse.Namespace) -> str:
    token = os.environ.get("HA_TOKEN")
    if not token and args.token_file.exists():
        token = args.token_file.read_text().strip()
    if not token:
        sys.exit(f"No HA token: set HA_TOKEN or put an admin long-lived token in {args.token_file}")
    return token


async def run(args: argparse.Namespace) -> None:
    token = read_token(args)
    if args.check:
        await check(args.url, token)
        print("OK: authenticated and subscribed to bluetooth advertisements")
        return
    transport = await open_transport("vhci:")
    controller = make_controller(transport.source, transport.sink, args.address)
    logging.info("virtual controller registered; see `hciconfig` / `bluetoothctl list`")
    delivered = dropped = 0
    async for adv in advertisements(args.url, token, set(args.source) or None):
        if inject_advertisement(controller, adv):
            delivered += 1
        else:
            dropped += 1
        if (delivered + dropped) % 500 == 0:
            logging.info("advertisements delivered=%d dropped(not scanning)=%d", delivered, dropped)


def main() -> None:
    parser = argparse.ArgumentParser(prog="ha-ble-vhci", description=__doc__)
    parser.add_argument("--url", default=os.environ.get("HA_URL", "http://homeassistant.local:8123"))
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE)
    parser.add_argument("--source", action="append", default=[], help="only forward adverts from this HA scanner source (MAC); repeatable")
    parser.add_argument("--address", default="F0:CA:FE:00:00:01", help="public address of the virtual controller")
    parser.add_argument("--check", action="store_true", help="only verify URL, token and admin rights, then exit (no /dev/vhci needed)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    try:
        asyncio.run(run(args))
    except HaError as err:
        sys.exit(str(err))
    except OSError as err:
        sys.exit(f"cannot reach Home Assistant at {args.url}: {err}")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
