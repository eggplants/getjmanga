"""David Bau's `seedrandom` and the `shuffle-seed` tile shuffle over it, as two viewers run them.

Piccoma's viewer and スターツ出版's shuffle the tiles of a page with the
`shuffle-seed` package, which draws its floats from `seedrandom` (an ARC4
stream keyed by a seed string). Putting a page back together means running
the same PRNG over the same seed and replaying the shuffle: `seedrandom()`
and `shuffle_order()` are those two libraries ported, and `descramble()` is
the tile shuffle both viewers do over them, each with a tile side of its own.
Where the seed comes from is the site's business.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from getjmanga.errors import GetjmangaError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from PIL import Image

_ARC4_WIDTH = 256
_ARC4_MASK = _ARC4_WIDTH - 1
_PRNG_CHUNKS = 6
_PRNG_STARTDENOM = _ARC4_WIDTH**_PRNG_CHUNKS
_PRNG_SIGNIFICANCE = 2**52
_PRNG_OVERFLOW = _PRNG_SIGNIFICANCE * 2


class _ARC4:
    """The ARC4 keystream seedrandom draws its bits from."""

    def __init__(self, key: list[int]) -> None:
        self._state = list(range(_ARC4_WIDTH))
        self._i = 0
        self._j = 0

        j = 0
        for i in range(_ARC4_WIDTH):
            swapped = self._state[i]
            j = _ARC4_MASK & (j + key[i % len(key)] + swapped)
            self._state[i] = self._state[j]
            self._state[j] = swapped
        # seedrandom discards a full round before handing any bits out.
        self.generate(_ARC4_WIDTH)

    def generate(self, count: int) -> int:
        """Return `count` keystream bytes packed into one big-endian integer."""
        state, i, j, result = self._state, self._i, self._j, 0
        for _ in range(count):
            i = _ARC4_MASK & (i + 1)
            swapped = state[i]
            j = _ARC4_MASK & (j + swapped)
            state[i] = state[j]
            state[j] = swapped
            result = result * _ARC4_WIDTH + state[_ARC4_MASK & (state[i] + state[j])]
        self._i, self._j = i, j
        return result


def _mixkey(seed: str) -> list[int]:
    """Fold a seed string into an ARC4 key the way seedrandom does."""
    key: list[int] = []
    for index, char in enumerate(seed):
        key.insert(_ARC4_MASK & index, ord(char))
    return key


def seedrandom(seed: str) -> Callable[[], float]:
    """Build the seeded PRNG the viewer shuffles its tiles with.

    Args:
        seed: The seed string the page was shuffled with.

    Returns:
        A callable handing out floats in `[0, 1)`.

    Raises:
        GetjmangaError: The seed is empty, which keys nothing.
    """
    if not seed:
        msg = "cannot seed the shuffle with an empty string."
        raise GetjmangaError(msg)
    arc4 = _ARC4(_mixkey(seed))

    def prng() -> float:
        numerator: float = arc4.generate(_PRNG_CHUNKS)
        denominator: float = _PRNG_STARTDENOM
        extra = 0
        # Pull more bytes until the fraction carries a full mantissa of them,
        # then halve it back under the range a double represents exactly.
        while numerator < _PRNG_SIGNIFICANCE:
            numerator = (numerator + extra) * _ARC4_WIDTH
            denominator *= _ARC4_WIDTH
            extra = arc4.generate(1)
        while numerator >= _PRNG_OVERFLOW:
            numerator /= 2
            denominator /= 2
            extra >>= 1
        return (numerator + extra) / denominator

    return prng


def shuffle_order(size: int, seed: str) -> list[int]:
    """Replay the viewer's shuffle of `size` tiles.

    Args:
        size: How many tiles are being shuffled.
        seed: The seed string the page was shuffled with.

    Returns:
        The source tile index for each destination tile.
    """
    prng = seedrandom(seed)
    remaining = list(range(size))
    return [remaining.pop(math.floor(prng() * len(remaining))) for _ in range(size)]


def _tile_groups(width: int, height: int, tile_size: int) -> Iterator[list[tuple[int, int]]]:
    """Group the tiles of an image by shape, top-left corner first.

    Tiles along the right and bottom edge are cut short, and the viewer shuffles
    each differently shaped group among itself rather than all tiles as one.
    """
    columns = math.ceil(width / tile_size)
    rows = math.ceil(height / tile_size)
    groups: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for index in range(columns * rows):
        row, column = divmod(index, columns)
        x, y = column * tile_size, row * tile_size
        shape = (min(tile_size, width - x), min(tile_size, height - y))
        groups.setdefault(shape, []).append((x, y))
    yield from groups.values()


def descramble(image: Image.Image, seed: str, tile_size: int) -> Image.Image:
    """Put a shuffled page back together.

    Args:
        image: The page exactly as the CDN serves it.
        seed: The seed string the page was shuffled with.
        tile_size: The side of a full tile.

    Returns:
        A new image with the tiles back where they belong.
    """
    out = image.copy()
    for corners in _tile_groups(*image.size, tile_size):
        tile_width = min(tile_size, image.width - corners[0][0])
        tile_height = min(tile_size, image.height - corners[0][1])
        group_x, group_y = corners[0]
        # A group's own grid is as wide as the run of tiles sharing its first row.
        group_columns = sum(1 for _, y in corners if y == group_y)

        for (dest_x, dest_y), source in zip(corners, shuffle_order(len(corners), seed), strict=True):
            row, column = divmod(source, group_columns)
            x, y = group_x + column * tile_width, group_y + row * tile_height
            out.paste(image.crop((x, y, x + tile_width, y + tile_height)), (dest_x, dest_y))
    return out
