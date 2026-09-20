from __future__ import annotations

import pytest
from PIL import Image

from getjmanga.errors import GetjmangaError
from getjmanga.viewers.seedrandom import descramble, seedrandom, shuffle_order

SEED = "OQI26H8XIDIYETF0TQD7BD"
TILE = 50

# `shuffle_order(12, SEED)`, as the reference implementation of the viewer's
# PRNG produces it.
ORDER_12 = [1, 10, 11, 0, 3, 6, 8, 7, 2, 9, 4, 5]


def tile_image(values, columns=4, rows=3, tile=TILE):
    """A grid of flat tiles, tile n painted with a colour derived from values[n]."""
    image = Image.new("RGB", (columns * tile, rows * tile))
    for index, value in enumerate(values):
        row, column = divmod(index, columns)
        patch = Image.new("RGB", (tile, tile), (value * 7 % 256, value * 13 % 256, value * 29 % 256))
        image.paste(patch, (column * tile, row * tile))
    return image


# --- the PRNG and the shuffle -----------------------------------------------


def test_shuffle_order_matches_the_viewer():
    assert shuffle_order(12, SEED) == ORDER_12


def test_seedrandom_rejects_an_empty_seed():
    with pytest.raises(GetjmangaError, match="empty"):
        seedrandom("")


# --- descrambling -------------------------------------------------------------


def test_descramble_puts_the_tiles_back():
    order = shuffle_order(12, SEED)
    inverse = [0] * 12
    for destination, source in enumerate(order):
        inverse[source] = destination
    original = tile_image(range(12))
    scrambled = tile_image(inverse)
    assert descramble(scrambled, SEED, TILE).tobytes() == original.tobytes()


def test_descramble_handles_edges_that_do_not_fill_a_tile():
    image = tile_image(range(12)).crop((0, 0, 137, 89))
    out = descramble(image, SEED, TILE)
    assert out.size == (137, 89)
    # Every group is permuted among itself, so no pixel value is invented.
    assert out.histogram() == image.histogram()


def test_descramble_takes_the_tile_side_of_the_site():
    original = tile_image(range(12), tile=20)
    inverse = [0] * 12
    for destination, source in enumerate(shuffle_order(12, SEED)):
        inverse[source] = destination
    assert descramble(tile_image(inverse, tile=20), SEED, 20).tobytes() == original.tobytes()
