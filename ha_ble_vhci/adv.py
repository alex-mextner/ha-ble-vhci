"""Convert Home Assistant advertisement dicts into raw BLE advertising data."""

from __future__ import annotations

import uuid

_BASE_UUID_SUFFIX = "-0000-1000-8000-00805f9b34fb"

AD_UUID128_COMPLETE = 0x07
AD_COMPLETE_LOCAL_NAME = 0x09
AD_SERVICE_DATA_16 = 0x16
AD_SERVICE_DATA_128 = 0x21
AD_MANUFACTURER = 0xFF

LEGACY_MAX = 31


def _tlv(ad_type: int, payload: bytes) -> bytes:
    return bytes((len(payload) + 1, ad_type)) + payload


def _uuid_to_le(value: str) -> tuple[bytes, bool]:
    """Return (little-endian uuid bytes, is_16bit)."""
    lowered = value.lower()
    if lowered.startswith("0000") and lowered.endswith(_BASE_UUID_SUFFIX):
        return bytes.fromhex(lowered[4:8])[::-1], True
    return uuid.UUID(lowered).bytes[::-1], False


def rebuild_adv_data(info: dict) -> bytes:
    """Rebuild AD structures from the parsed fields of an HA advertisement.

    Used only when HA does not provide the ``raw`` packet.
    """
    out = bytearray()
    if info.get("name"):
        out += _tlv(AD_COMPLETE_LOCAL_NAME, info["name"].encode()[:29])
    uuids128 = b""
    for value in info.get("service_uuids") or ():
        le, is16 = _uuid_to_le(value)
        if not is16:
            uuids128 += le
    if uuids128:
        out += _tlv(AD_UUID128_COMPLETE, uuids128)
    for key, hexdata in (info.get("service_data") or {}).items():
        le, is16 = _uuid_to_le(key)
        out += _tlv(
            AD_SERVICE_DATA_16 if is16 else AD_SERVICE_DATA_128,
            le + bytes.fromhex(hexdata),
        )
    for company, hexdata in (info.get("manufacturer_data") or {}).items():
        out += _tlv(AD_MANUFACTURER, int(company).to_bytes(2, "little") + bytes.fromhex(hexdata))
    return bytes(out)


def adv_payload(info: dict) -> bytes:
    raw = info.get("raw")
    if raw:
        return bytes.fromhex(raw)
    return rebuild_adv_data(info)


def split_legacy(data: bytes) -> list[bytes]:
    """Legacy advertising reports carry at most 31 bytes per report."""
    return [data[i : i + LEGACY_MAX] for i in range(0, len(data), LEGACY_MAX)] or [b""]


def is_random_address(address: str) -> bool:
    """Guess the address type: HA does not forward it.

    Static random (0b11) and resolvable private (0b01) addresses are reported
    as random; everything else as public.
    """
    top = int(address.split(":")[0], 16) & 0xC0
    return top in (0xC0, 0x40)
