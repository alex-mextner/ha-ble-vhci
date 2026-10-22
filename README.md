# ha-ble-vhci

Expose the Bluetooth proxies of a Home Assistant instance (ESPHome ESP32
`bluetooth_proxy`, reached over LAN or Tailscale) as a local **scan-only**
Bluetooth adapter (`hciN`) on Linux, using `/dev/vhci` and
[Bumble](https://github.com/google/bumble).

```
phone/BLE device --BLE--> ESP32 (ESPHome) --API--> Home Assistant
   --websocket bluetooth/subscribe_advertisements--> ha-ble-vhci
   --Bumble virtual controller--> /dev/vhci --> BlueZ (hciN) --> apps
```

## What works / what does not

* Works: LE scanning (advertisement reports reach BlueZ and every app on it).
  This is enough for the **hybrid (caBLE) passkey flow**, where the desktop
  only has to *hear* the phone's BLE advertisement.
* Does not work: connecting to devices, GATT, advertising, classic Bluetooth.
  Connection attempts fail immediately.
* Address type is guessed (HA does not forward it); only matters for connecting.
* Home Assistant must expose the `raw` advertisement field (recent versions);
  older ones fall back to rebuilding AD structures from parsed fields.
* The host's `filter_duplicates` flag is ignored on purpose: every advert HA
  delivers is forwarded (caBLE adverts rotate their payload under one address).
* HA only forwards an advertisement when it changed. Devices seen before the host
  starts scanning are therefore replayed once (their latest advert) when scanning
  is switched on; afterwards only HA's updates are forwarded.
* The phone must be within BLE range of the ESP32 proxy, not of the PC.

## Requirements

* Linux with `hci_vhci` kernel module, BlueZ running (`bluetooth.service`).
* Python 3.10+.
* Home Assistant reachable over HTTP(S), plus a **long-lived access token of an
  admin user** (the websocket command is admin-only).

## Install

```sh
python3 -m venv ~/.local/share/ha-ble-vhci/venv
~/.local/share/ha-ble-vhci/venv/bin/pip install git+https://github.com/alex-mextner/ha-ble-vhci

# permissions for /dev/vhci + module autoload (root once)
sudo install -m644 packaging/99-vhci.rules /etc/udev/rules.d/
sudo install -m644 packaging/hci_vhci.conf /etc/modules-load.d/
sudo modprobe hci_vhci && sudo udevadm control --reload && sudo udevadm trigger
sudo systemctl enable --now bluetooth

# token + URL
install -d -m700 ~/.config/ha-ble-vhci
install -m600 /dev/stdin ~/.config/ha-ble-vhci/token   # paste token, Ctrl-D
echo 'HA_URL=http://HA_HOST:8123' > ~/.config/ha-ble-vhci/env

# run as user service
install -Dm644 packaging/ha-ble-vhci.service ~/.config/systemd/user/ha-ble-vhci.service
systemctl --user daemon-reload && systemctl --user enable --now ha-ble-vhci
```

Check: `bluetoothctl list` shows a new controller, `bluetoothctl scan on` lists
devices seen by the ESP32. Use `--source <scanner MAC>` to listen to one proxy only.

## Health

The bridge looks healthy even when the ESP32 proxy is powered off (Home Assistant
simply has no scanner), and Bluetooth clients such as Firefox's passkey QR dialog
then wait forever. To see whether a proxy is online:

```sh
ha-ble-vhci --scanners    # one line per proxy; exit 0, or exit 4 "no Bluetooth proxy is online in Home Assistant"
journalctl --user -u ha-ble-vhci | grep -i scanner
```

The service logs `Bluetooth scanner online: <name>` (INFO), and WARNING
`no Bluetooth scanner is connected to Home Assistant: ...` when the last proxy
disappears or when none is present 15 s after (re)connecting. `--check` is
unchanged (auth only).

## Credits

The BlueZ-side Bumble setup (disabling LL privacy / extended advertising for
current kernels) follows `tools/vhci_bridge.py` from
[espressif/esp-emulator](https://github.com/espressif/esp-emulator).
