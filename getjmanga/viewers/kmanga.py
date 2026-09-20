"""The K MANGA viewer (Kodansha's, by SEGA), and the white-labels of it other sites run.

The viewer is a Nuxt app whose API signs every request with an
`x-com-sega-md-hash` header: sorted `sha256(key)_sha512(value)` pairs of the
query parameters, SHA-256'd, then SHA-512'd with a birthday cookie's
`sha256(birthday)_sha512(expires)` (both empty for an anonymous reader)
appended. `service_hash()` builds it the way the bundle does.

A scrambled page is cut into a 4 x 4 grid of tiles whose side is a multiple
of 8, the tiles are shuffled by sorting them on the values of an xorshift32
generator seeded with the episode's `scramble_seed`, and the strip left over
on the right and at the bottom stays where it is. `descramble()` undoes what
the viewer's canvas does; `tile_order()` is the permutation on its own, for
a site that tiles differently.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from PIL import Image

#: The scramble grid: `GRID x GRID` tiles whose sides are multiples of `UNIT`.
GRID = 4
UNIT = 8
#: The seeds the viewer takes; anything else leaves a page as served.
SEED_MIN = 1
SEED_MAX = 2**32 - 1
_MASK = 0xFFFFFFFF


def service_hash(params: Mapping[str, str | int], birthday: str = "", expires: str = "") -> str:
    """Sign a set of query parameters the way the viewer does.

    Args:
        params: The query parameters of the request.
        birthday: The reader's birthday cookie, empty when signed out.
        expires: When that cookie expires, empty when signed out.

    Returns:
        The value of the `x-com-sega-md-hash` header.
    """
    pairs = ",".join(f"{_sha256(key)}_{_sha512(str(params[key]))}" for key in sorted(params))
    return _sha512(_sha256(pairs) + f"{_sha256(birthday)}_{_sha512(expires)}")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _sha512(text: str) -> str:
    return hashlib.sha512(text.encode()).hexdigest()


def xorshift32(seed: int) -> Iterator[int]:
    """Yield the xorshift32 sequence the viewer shuffles tiles with.

    Args:
        seed: The `scramble_seed` of the episode.

    Yields:
        Unsigned 32-bit values, one per call.
    """
    state = seed & _MASK
    while True:
        state ^= (state << 13) & _MASK
        state ^= state >> 17
        state ^= (state << 5) & _MASK
        yield state


def tile_order(seed: int, grid: int = GRID) -> list[int]:
    """The tile permutation a seed produces.

    Args:
        seed: The `scramble_seed` of the episode.
        grid: Tiles per side.

    Returns:
        For every destination tile (row-major), the index of the source tile
        in the served image that belongs there.
    """
    values = xorshift32(seed)
    keyed = [(next(values), index) for index in range(grid * grid)]
    keyed.sort(key=lambda pair: pair[0])
    return [index for _, index in keyed]


def descramble(image: Image.Image, seed: int, grid: int = GRID) -> Image.Image:
    """Put a scrambled page back together.

    Args:
        image: The page as served.
        seed: The `scramble_seed` of the episode.
        grid: Tiles per side.

    Returns:
        A new image with the tiles in place. The image is returned as is when
        the seed is out of range or the image is too small to be tiled.
    """
    width, height = image.size
    if not (SEED_MIN <= seed <= SEED_MAX) or width < grid * UNIT or height < grid * UNIT:
        return image
    tile_width = width // UNIT // grid * UNIT
    tile_height = height // UNIT // grid * UNIT
    out = image.copy()
    for destination, source in enumerate(tile_order(seed, grid)):
        sx, sy = source % grid * tile_width, source // grid * tile_height
        dx, dy = destination % grid * tile_width, destination // grid * tile_height
        out.paste(image.crop((sx, sy, sx + tile_width, sy + tile_height)), (dx, dy))
    return out
