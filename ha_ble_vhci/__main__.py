"""Expose Home Assistant Bluetooth proxies (ESPHome) as a local scan-only hciN."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from bumble.transport import open_transport

from .bridge import Feeder, make_controller
from .ha import HaError, HaUnreachable, ScannerTracker, advertisements, check, list_scanners, scanner_label

DEFAULT_TOKEN_FILE = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "ha-ble-vhci" / "token"


EXIT_NO_SCANNER = 4
EXIT_UNREACHABLE = 5  # Home Assistant down, restarting or not resolvable; exit 1 stays "reachable but refuses"
NO_SCANNER_MESSAGE = "no Bluetooth proxy is online in Home Assistant"


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
    if args.scanners:
        scanners = await list_scanners(args.url, token)
        for scanner in scanners:
            print(f"{scanner_label(scanner)}  source={scanner.get('source')}")
        if not scanners:
            print(NO_SCANNER_MESSAGE, file=sys.stderr)
            sys.exit(EXIT_NO_SCANNER)
        return
    transport = await open_transport("vhci:")
    feeder = Feeder(make_controller(transport.source, transport.sink, args.address))
    logging.info("virtual controller registered; see `hciconfig` / `bluetoothctl list`")
    delivered = dropped = 0
    async for adv in advertisements(args.url, token, set(args.source) or None, ScannerTracker()):
        if feeder.feed(adv):
            delivered += 1
        else:
            dropped += 1
        if (delivered + dropped) % 500 == 0:
            logging.debug("advertisements delivered=%d cached-only(not scanning)=%d", delivered, dropped)


def main() -> None:
    parser = argparse.ArgumentParser(prog="ha-ble-vhci", description=__doc__)
    parser.add_argument("--url", default=os.environ.get("HA_URL", "http://homeassistant.local:8123"))
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE)
    parser.add_argument("--source", action="append", default=[], help="only forward adverts from this HA scanner source (MAC); repeatable")
    parser.add_argument("--address", default="F0:CA:FE:00:00:01", help="public address of the virtual controller")
    parser.add_argument("--check", action="store_true", help="only verify URL, token and admin rights, then exit (no /dev/vhci needed)")
    parser.add_argument("--scanners", action="store_true", help="list Bluetooth proxies online in Home Assistant; exit 4 if none, 5 if Home Assistant cannot be reached (no /dev/vhci needed)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    try:
        asyncio.run(run(args))
    except HaUnreachable as err:
        print(f"cannot reach Home Assistant at {args.url}: {err}", file=sys.stderr)
        sys.exit(EXIT_UNREACHABLE)
    except HaError as err:
        sys.exit(str(err))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
