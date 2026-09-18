"""A minimal protobuf wire-format codec, for sites whose APIs speak protobuf.

Only varints and length-delimited fields are needed -- and only a handful of
fields per message -- so messages are read by field number instead of pulling
in a protobuf runtime. Field numbers come from the sites' generated JS classes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .extractors.common import GetjmangaError

if TYPE_CHECKING:
    from collections.abc import Iterator

_VARINT = 0
_LENGTH_DELIMITED = 2
_FIXED64 = 1
_FIXED32 = 5


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def encode_varint_field(number: int, value: int) -> bytes:
    """Encode one varint field (an int, a bool or an enum).

    Args:
        number: The field number.
        value: The value.

    Returns:
        The field's bytes.
    """
    return _varint((number << 3) | _VARINT) + _varint(value)


def encode_bytes_field(number: int, value: bytes | str) -> bytes:
    """Encode one length-delimited field (a string or a nested message).

    Args:
        number: The field number.
        value: The bytes, or a string to encode as UTF-8.

    Returns:
        The field's bytes.
    """
    data = value.encode() if isinstance(value, str) else value
    return _varint((number << 3) | _LENGTH_DELIMITED) + _varint(len(data)) + data


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if pos >= len(buf):
            msg = "truncated protobuf message."
            raise GetjmangaError(msg)
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def decode_fields(buf: bytes) -> Iterator[tuple[int, int | bytes]]:
    """Walk a message, yielding `(field number, value)` in wire order.

    Varints come back as ints, everything else as the raw bytes -- a string,
    a nested message or a fixed-width number, which the caller knows to tell
    apart by the field number.

    Args:
        buf: The encoded message.

    Yields:
        One pair per field occurrence; repeated fields appear once per item.

    Raises:
        GetjmangaError: The bytes are not a protobuf message.
    """
    pos = 0
    while pos < len(buf):
        key, pos = _read_varint(buf, pos)
        number, wire = key >> 3, key & 7
        if wire == _VARINT:
            value, pos = _read_varint(buf, pos)
            yield number, value
        elif wire == _LENGTH_DELIMITED:
            length, pos = _read_varint(buf, pos)
            yield number, buf[pos : pos + length]
            pos += length
        elif wire in (_FIXED64, _FIXED32):
            width = 8 if wire == _FIXED64 else 4
            yield number, buf[pos : pos + width]
            pos += width
        else:
            msg = f"unsupported protobuf wire type {wire}."
            raise GetjmangaError(msg)


def message(buf: bytes) -> dict[int, list[int | bytes]]:
    """Group a message's fields by number, keeping repeats in order.

    Args:
        buf: The encoded message.

    Returns:
        Field number to every value it took, in wire order.
    """
    fields: dict[int, list[int | bytes]] = {}
    for number, value in decode_fields(buf):
        fields.setdefault(number, []).append(value)
    return fields


def string(fields: dict[int, list[int | bytes]], number: int) -> str:
    """The last value of a string field, or "" when absent."""
    values = fields.get(number)
    return values[-1].decode() if values and isinstance(values[-1], bytes) else ""


def integer(fields: dict[int, list[int | bytes]], number: int) -> int:
    """The last value of a varint field, or 0 when absent."""
    values = fields.get(number)
    return int(values[-1]) if values and isinstance(values[-1], int) else 0


def raw(fields: dict[int, list[int | bytes]], number: int) -> bytes:
    """The last value of a bytes field (a nested message), or b"" when absent."""
    values = fields.get(number)
    return values[-1] if values and isinstance(values[-1], bytes) else b""


def messages(fields: dict[int, list[int | bytes]], number: int) -> list[bytes]:
    """Every value of a repeated message field, in order."""
    return [value for value in fields.get(number, []) if isinstance(value, bytes)]
