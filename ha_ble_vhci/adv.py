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


def tlvs(data: bytes) -> list[bytes]:
    """Split advertising data into complete AD structures (a truncated tail is dropped)."""
    out: list[bytes] = []
    i = 0
    while i < len(data) and data[i] != 0:
        end = i + 1 + data[i]
        if end > len(data):
            break
        out.append(data[i:end])
        i = end
    return out


def full_advert(info: dict) -> list[bytes]:
    """All AD structures to report for one device.

    HA's ``raw`` is only the *latest* packet, while service/manufacturer data are aggregated across
    packets. A phone alternates e.g. Apple continuity and FIDO hybrid (0xFFF9) packets, so the
    latest raw packet alone may lack the FIDO service data HA still knows. Add the structures
    that are missing from the raw packet.
    """
    raw = adv_payload(info) if info.get("raw") else b""
    structures = tlvs(raw)
    if info.get("raw"):
        structures += [t for t in tlvs(rebuild_adv_data({**info, "name": None, "service_uuids": []}))
                       if t not in structures]
    else:
        structures = tlvs(rebuild_adv_data(info))
    return structures


AD_FLAGS = 0x01


def pack_reports(structures: list[bytes], limit: int = LEGACY_MAX) -> list[bytes]:
    """Greedily pack whole AD structures into legacy-report-sized chunks.

    The Flags structure is repeated in every chunk: BlueZ treats a report without flags as coming
    from a non-discoverable device.
    """
    flags = next((t for t in structures if len(t) > 1 and t[1] == AD_FLAGS), b"")
    body = [t for t in structures if t is not flags and t != flags]
    chunks: list[bytes] = []
    current = flags
    for structure in body:
        if len(structure) + len(flags) > limit:
            continue
        if len(current) + len(structure) > limit:
            chunks.append(current)
            current = flags
        current += structure
    if len(current) > len(flags) or not chunks:
        chunks.append(current)
    return chunks


def is_random_address(address: str) -> bool:
    """Guess the address type: HA does not forward it.

    Static random (0b11) and resolvable private (0b01) addresses are reported
    as random; everything else as public.
    """
    top = int(address.split(":")[0], 16) & 0xC0
    return top in (0xC0, 0x40)
