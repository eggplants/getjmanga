from __future__ import annotations

from io import BytesIO

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image, UnidentifiedImageError

from getjmanga.cipher import aes_cbc_decrypt, xor_unmask
from getjmanga.errors import GetjmangaError

KEY = "3ac550b62b4734c8411b5076b748a9c8bd6af1e87117d012789d5cc8ae1563a7"
IV = "484a87c5baeac6a0bafd335e5f5055b2"


def png_bytes(size=(8, 8), colour=(10, 20, 30)):
    raw = BytesIO()
    Image.new("RGB", size, colour).save(raw, "PNG")
    return raw.getvalue()


def encrypted_png(size=(8, 8), colour=(10, 20, 30)):
    plain = png_bytes(size, colour)
    padding = 16 - len(plain) % 16
    plain += bytes([padding]) * padding
    encryptor = Cipher(algorithms.AES(bytes.fromhex(KEY)), modes.CBC(bytes.fromhex(IV))).encryptor()
    return encryptor.update(plain) + encryptor.finalize()


# --- AES-CBC --------------------------------------------------------------------------


def test_aes_cbc_decrypt_recovers_the_image():
    data = aes_cbc_decrypt(encrypted_png(), KEY, IV)
    with Image.open(BytesIO(data)) as image:
        assert image.size == (8, 8)
        assert image.getpixel((0, 0)) == (10, 20, 30)


def test_aes_cbc_decrypt_rejects_a_partial_block():
    with pytest.raises(GetjmangaError, match="whole number"):
        aes_cbc_decrypt(b"\x00" * 17, KEY, IV)


def test_aes_cbc_decrypt_rejects_the_wrong_key():
    with pytest.raises(GetjmangaError, match="padding"):
        aes_cbc_decrypt(encrypted_png(), "00" * 32, IV)


# --- XOR ------------------------------------------------------------------------------


def test_xor_unmask_restores_a_decodable_image():
    masked = xor_unmask(png_bytes(), "62e07285b272877b")
    with pytest.raises(UnidentifiedImageError):
        Image.open(BytesIO(masked))
    assert Image.open(BytesIO(xor_unmask(masked, "62e07285b272877b"))).getpixel((0, 0)) == (10, 20, 30)


def test_xor_unmask_leaves_an_empty_key_or_file_alone():
    assert xor_unmask(b"abc", "") == b"abc"
    assert xor_unmask(b"", "ff") == b""
