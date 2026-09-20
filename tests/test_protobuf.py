from __future__ import annotations

import pytest

from getjmanga.errors import GetjmangaError
from getjmanga.protobuf import decode_fields, encode_bytes_field, encode_varint_field


def test_varint_fields_round_trip():
    message = encode_varint_field(1, 0) + encode_varint_field(2, 300) + encode_varint_field(15, 2**40)
    assert list(decode_fields(message)) == [(1, 0), (2, 300), (15, 2**40)]


def test_bytes_fields_round_trip():
    message = encode_bytes_field(2, "氷舞") + encode_bytes_field(3, b"\x00\x01")
    assert list(decode_fields(message)) == [(2, "氷舞".encode()), (3, b"\x00\x01")]


def test_decode_fields_reads_fixed_width_values():
    fixed64 = bytes([(9 << 3) | 1]) + b"\x01" * 8
    fixed32 = bytes([(9 << 3) | 5]) + b"\x02" * 4
    assert list(decode_fields(fixed64 + fixed32)) == [(9, b"\x01" * 8), (9, b"\x02" * 4)]


def test_decode_fields_rejects_a_truncated_message():
    with pytest.raises(GetjmangaError, match="truncated"):
        list(decode_fields(b"\x80"))


def test_decode_fields_rejects_an_unknown_wire_type():
    with pytest.raises(GetjmangaError, match="wire type"):
        list(decode_fields(bytes([(1 << 3) | 3])))
