"""The two ways sites hide page files in transit, undone.

Neither belongs to one viewer: AES-CBC with a per-page key is what COMIC
FUZ, Link-U's sites, ゴラクうぇぶ!, Vコミ and 週刊コロコロコミック serve, and a
repeating XOR key is what カドコミ, ニコニコ漫画, レジンコミックス and Link-U's
older viewer do. Every extractor that meets one of them calls in here.
"""

from __future__ import annotations

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .errors import GetjmangaError

_AES_BLOCK = 16


def aes_cbc_decrypt(data: bytes, key: str, iv: str) -> bytes:
    """Undo the AES-CBC a CDN serves a page file under.

    Args:
        data: The file exactly as served.
        key: The page's key, hex.
        iv: The page's iv, hex.

    Returns:
        The image file, its PKCS#7 padding gone.

    Raises:
        GetjmangaError: The data is not a whole number of blocks, or the padding is off.
    """
    if len(data) % _AES_BLOCK or not data:
        msg = "the encrypted image is not a whole number of AES blocks."
        raise GetjmangaError(msg)
    decryptor = Cipher(algorithms.AES(bytes.fromhex(key)), modes.CBC(bytes.fromhex(iv))).decryptor()
    plain = decryptor.update(data) + decryptor.finalize()
    padding = plain[-1]
    if not 1 <= padding <= _AES_BLOCK or plain[-padding:] != bytes([padding]) * padding:
        msg = "the decrypted image carries no PKCS#7 padding; wrong key or iv?"
        raise GetjmangaError(msg)
    return plain[:-padding]


def xor_unmask(data: bytes, key: str) -> bytes:
    """Undo a viewer's XOR masking of a page file.

    The key, a hex string, is applied as a repeating byte key from the first
    byte on. An empty key or an empty file is handed back as it is.

    Args:
        data: The file exactly as the CDN serves it.
        key: The key, hex.

    Returns:
        The file.
    """
    stream_key = bytes.fromhex(key)
    if not stream_key or not data:
        return data
    stream = (stream_key * (len(data) // len(stream_key) + 1))[: len(data)]
    return (int.from_bytes(data, "big") ^ int.from_bytes(stream, "big")).to_bytes(len(data), "big")
